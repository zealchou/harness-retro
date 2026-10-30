#!/usr/bin/env python3
"""The only public runtime entry for retro-2."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import maintenance  # noqa: E402
import queue_adapter  # noqa: E402
import reporting  # noqa: E402
import config  # noqa: E402
from station_host import PythonStationRunner, maintenance_runner  # noqa: E402


ENTRY = "retro2/controller/run.py"
ALL_STATIONS = (
    "health", "evidence", "diagnosis", "candidates", "test_gate", "filters",
    "apply_or_report", "maintenance",
)
STATIONS = ALL_STATIONS[:7]
STOP_STATES = {"NO_CHANGE", "REQUIRES_HUMAN_APPROVAL", "RESTORE_VERIFIED"}


def _atomic_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".state-")
    with os.fdopen(fd, "w") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _ticket(path):
    payload = json.loads(Path(path).read_text())
    required = {"schema_version", "idempotency_key", "controller_entry"}
    missing = required - set(payload)
    if missing or payload.get("controller_entry") != ENTRY:
        raise ValueError("ticket does not target the retro-2 controller")
    if payload.get("mode", "competition") != "competition":
        raise ValueError("competition controller received another mode")
    return payload


def _subject_root(cwd):
    cwd = Path(cwd).resolve()
    result = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"], text=True, capture_output=True)
    return Path(result.stdout.strip()).resolve() if result.returncode == 0 else cwd


def enqueue_manual(session_id, queue_dir, launcher=None, *, cwd=None, transcript_path=None):
    subject = _subject_root(cwd or Path.cwd())
    transcript = transcript_path or os.environ.get("CLAUDE_TRANSCRIPT_PATH") or config.transcript_path(session_id)
    return queue_adapter.enqueue_pressure_event(
        session_id, f"manual-{time.time_ns()}", 1.0, queue_dir, launcher=launcher,
        repo_root=str(subject), transcript_path=str(Path(transcript).resolve()),
    )


def _finish(trace, run_dir, *, report=True):
    if not report:
        result = {"run_id": trace["run_id"], "status": trace["status"], "reason": trace.get("reason")}
        _atomic_json(run_dir / "result.json", result)
        return result
    paths = reporting.write_report(trace, run_dir / "report")
    result = {
        "run_id": trace["run_id"],
        "status": trace["status"],
        "report_md": str(paths["markdown"]),
        "report_html": str(paths["html"]),
    }
    _atomic_json(run_dir / "result.json", result)
    return result


def _write_skipped(run_dir, first_index, caused_by, reason):
    for index, station in enumerate(STATIONS[first_index:], first_index + 1):
        _atomic_json(run_dir / "stations" / f"{index:02d}-{station}.json", {
            "status": "SKIPPED",
            "caused_by_station": caused_by,
            "reason": reason,
        })


def execute_ticket(ticket_path, runs_root, runner):
    ticket = _ticket(ticket_path)
    run_id = "run-" + hashlib.sha256(ticket["idempotency_key"].encode()).hexdigest()[:16]
    run_dir = Path(runs_root) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    _atomic_json(run_dir / "ticket.json", ticket)
    trace = reporting.empty_trace("NO_CHANGE", run_id)
    trace["subject_repo"] = ticket.get("repo_root")
    trace["model_backend"] = getattr(runner, "model_backend", "Claude")
    previous = {"status": "QUEUED", "ticket": ticket}
    for index, station in enumerate(STATIONS, 1):
        request = {"run_id": run_id, "station": index, "input": previous}
        previous = runner(station, request)
        if not isinstance(previous, dict) or "status" not in previous:
            raise ValueError(f"station {station} returned an invalid result")
        _atomic_json(run_dir / "stations" / f"{index:02d}-{station}.json", previous)
        if station == "evidence":
            trace["diagnosis_index"] = previous.get("diagnosis_index", [])
        if station == "filters":
            trace["security_summary"] = previous.get("security_summary", "未量測")
        if station == "apply_or_report":
            trace["post_apply_mechanical_regression"] = previous.get(
                "post_apply_mechanical_regression", "未量測"
            )
        if previous["status"] in STOP_STATES:
            trace["status"] = previous["status"]
            trace["reason"] = previous.get("reason", previous["status"].lower())
            _write_skipped(
                run_dir, index, station,
                previous.get("reason", previous["status"].lower()),
            )
            return _finish(trace, run_dir, report=previous.get("reason") != "no_trusted_incident")
    trace["status"] = previous["status"]
    trace["reason"] = previous.get("reason")
    trace["commit_sha"] = previous.get("commit_sha")
    trace["revert_sha"] = previous.get("revert_sha")
    return _finish(trace, run_dir)


def apply_saved(run_id, alias, approval_ref, runs_root):
    if not re.fullmatch(r"run-[0-9a-f]{16}", run_id) or alias not in {"A", "B", "C"}:
        raise ValueError("invalid saved run or candidate")
    if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", approval_ref, re.I):
        raise ValueError("approval ref must be an explicit maintainer message UUID")
    run_dir = (Path(runs_root).resolve() / run_id).resolve()
    if run_dir.parent != Path(runs_root).resolve() or not (run_dir / "ticket.json").is_file():
        raise ValueError("saved run not found")
    ticket = _ticket(run_dir / "ticket.json")
    manifest = run_dir / "candidates" / alias / "manifest.json"
    # 主機目標候選改的是 ~/.claude，不看當初觸發的工作區（可能已被清掉，也不是套用對象）
    if manifest.is_file() and json.loads(manifest.read_text()).get("resource_policy", {}).get("target") == "host":
        ticket["repo_root"] = str(ROOT.parent)
    runner = PythonStationRunner(ticket, runs_root)
    if runner.run_id != run_id:
        raise ValueError("saved run identity mismatch")
    return runner.apply_or_report({"candidate": alias, "approval_ref": approval_ref})


def main():
    parser = argparse.ArgumentParser(
        prog="retro-2",
        description="Queue, run, inspect, and explicitly apply a safety-gated retrospective.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    enqueue = sub.add_parser("enqueue")
    enqueue.add_argument("--session-id", default="manual")
    enqueue.add_argument("--queue-dir", default=str(config.QUEUE_DIR))
    enqueue.add_argument("--transcript-path")
    execute = sub.add_parser("execute")
    execute.add_argument("ticket")
    execute.add_argument("--runs-root", default=str(config.RUNS_DIR))
    maintain = sub.add_parser("maintenance")
    maintain.add_argument("--run-dir", default=str(config.MAINTENANCE_DIR))
    apply = sub.add_parser("apply")
    apply.add_argument("--run", required=True)
    apply.add_argument("--candidate", choices=("A", "B", "C"), required=True)
    apply.add_argument("--approval-ref", required=True)
    apply.add_argument("--runs-root", default=str(config.RUNS_DIR))
    args = parser.parse_args()
    if args.command == "enqueue":
        print(json.dumps(enqueue_manual(args.session_id, args.queue_dir, transcript_path=args.transcript_path)._asdict(), ensure_ascii=False, default=str))
    elif args.command == "execute":
        runner = PythonStationRunner.from_ticket(args.ticket, args.runs_root)
        print(json.dumps(execute_ticket(args.ticket, args.runs_root, runner), ensure_ascii=False))
    elif args.command == "maintenance":
        print(json.dumps(maintenance.run_maintenance(args.run_dir, {}, maintenance_runner), ensure_ascii=False))
    else:
        print(json.dumps(apply_saved(args.run, args.candidate, args.approval_ref, args.runs_root), ensure_ascii=False))


if __name__ == "__main__":
    main()
