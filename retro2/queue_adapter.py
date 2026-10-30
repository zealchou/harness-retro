#!/usr/bin/env python3
"""Atomic, idempotent pressure-event queue.  It never runs the tournament."""
import fcntl
import hashlib
import json
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from host_exits import DetachedProcessHost, model_backend_environment


TRIGGER_RATIO = 0.90
LOCK_DEADLINE_SECONDS = 0.025
LOCK_RETRY_SECONDS = 0.002


class EnqueueResult(NamedTuple):
    created: bool
    key: str
    ticket: Path | None


def _atomic_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".ticket-")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _ticket_name(key):
    return hashlib.sha256(key.encode()).hexdigest() + ".ticket.json"


@contextmanager
def _short_lock(path):
    """Bound queue contention; a Hook must never wait behind a stuck writer."""
    with Path(path).open("a+") as lock_file:
        acquired = False
        deadline = time.monotonic() + LOCK_DEADLINE_SECONDS
        while not acquired:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(LOCK_RETRY_SECONDS, remaining))
        try:
            yield acquired
        finally:
            if acquired:
                fcntl.flock(lock_file, fcntl.LOCK_UN)


def launch_detached_worker(ticket, process_host=None):
    worker = Path(__file__).with_name("worker.py")
    argv = [sys.executable, str(worker), "--ticket", str(ticket)]
    return (process_host or DetachedProcessHost()).spawn_detached(argv, env=model_backend_environment())


def observe_pressure_event(
    session_id, ratio, queue_dir, launcher=None, *, transcript_path=None,
    repo_root=None, allowed_paths=(), replay_fixtures=None,
):
    """Turn low→high crossings into numbered cycles without touching gauge state."""
    queue_dir = Path(queue_dir)
    queue_dir.mkdir(parents=True, exist_ok=True)
    state_path = queue_dir / (".pressure-" + hashlib.sha256(session_id.encode()).hexdigest() + ".json")
    with _short_lock(queue_dir / ".pressure.lock") as acquired:
        if not acquired:
            return EnqueueResult(False, f"{session_id}:lock-busy", None)
        state = {"cycle": 0, "high": False}
        if state_path.exists():
            try:
                state.update(json.loads(state_path.read_text()))
            except (OSError, ValueError, TypeError):
                pass
        if ratio < TRIGGER_RATIO:
            state["high"] = False
            _atomic_json(state_path, state)
            return EnqueueResult(False, f"{session_id}:{state['cycle']}", None)
        if state["high"]:
            key = f"{session_id}:{state['cycle']}"
            return EnqueueResult(False, key, queue_dir / _ticket_name(key))
        state.update({"cycle": int(state["cycle"]) + 1, "high": True})
        _atomic_json(state_path, state)
    return enqueue_pressure_event(
        session_id, str(state["cycle"]), ratio, queue_dir, launcher,
        transcript_path=transcript_path, repo_root=repo_root,
        allowed_paths=allowed_paths, replay_fixtures=replay_fixtures,
    )


def enqueue_pressure_event(
    session_id, pressure_cycle, ratio, queue_dir, launcher=None, *,
    transcript_path=None, repo_root=None, allowed_paths=(), replay_fixtures=None,
):
    """Create one ticket per session/cycle; every failure is returned, never raised."""
    key = f"{session_id}:{pressure_cycle}"
    if ratio < TRIGGER_RATIO:
        return EnqueueResult(False, key, None)
    queue_dir = Path(queue_dir)
    ticket = queue_dir / _ticket_name(key)
    launcher = launcher or launch_detached_worker
    try:
        queue_dir.mkdir(parents=True, exist_ok=True)
        with _short_lock(queue_dir / ".enqueue.lock") as acquired:
            if not acquired:
                return EnqueueResult(False, key, None)
            if ticket.exists():
                return EnqueueResult(False, key, ticket)
            payload = {
                "schema_version": 1,
                "idempotency_key": key,
                "session_id": session_id,
                "pressure_cycle": pressure_cycle,
                "observed_ratio": ratio,
                "created_ns": time.time_ns(),
                "controller_entry": "retro2/controller/run.py",
                "mode": "competition",
                "status": "launching",
            }
            references = {
                "transcript_path": transcript_path,
                "repo_root": repo_root,
                "allowed_paths": list(allowed_paths),
                "replay_fixtures": dict(replay_fixtures or {}),
            }
            payload.update({key: value for key, value in references.items() if value})
            _atomic_json(ticket, payload)
            try:
                payload["worker_pid"] = launcher(ticket)
                payload["status"] = "queued"
            except Exception as exc:
                payload["status"] = "enqueue_error"
                payload["error"] = type(exc).__name__
            _atomic_json(ticket, payload)
            return EnqueueResult(True, key, ticket)
    except Exception:
        return EnqueueResult(False, key, ticket)

