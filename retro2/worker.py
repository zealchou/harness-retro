#!/usr/bin/env python3
"""Detached worker: prove ownership, then dispatch the single controller."""
import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from host_exits import DetachedProcessHost, model_backend_environment


def atomic_json(path, payload):
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".heartbeat-")
    with os.fdopen(fd, "w") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def dispatch_controller(ticket, spawn=None, process_host=None):
    controller = Path(__file__).parent / "controller" / "run.py"
    runs_root = ticket.parent.parent / "runs"
    argv = [sys.executable, str(controller), "execute", str(ticket), "--runs-root", str(runs_root)]
    host = process_host or DetachedProcessHost(spawn=spawn)
    return host.spawn_detached(argv, env=model_backend_environment())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticket", required=True)
    args = parser.parse_args()
    ticket = Path(args.ticket)
    payload = json.loads(ticket.read_text())
    heartbeat = Path(str(ticket) + ".heartbeat.json")
    state = {
            "schema_version": 1,
            "idempotency_key": payload["idempotency_key"],
            "worker_pid": os.getpid(),
            "heartbeat_ns": time.time_ns(),
            "status": "skeleton_ready",
    }
    atomic_json(heartbeat, state)
    state.update({"status": "controller_dispatched", "controller_pid": dispatch_controller(ticket)})
    atomic_json(heartbeat, state)


if __name__ == "__main__":
    main()

