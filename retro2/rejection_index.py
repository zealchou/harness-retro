"""Rebuild conservative rejection memory from per-run manifest/ledger evidence."""
import hashlib
import json
from pathlib import Path


INFRASTRUCTURE_PREFIXES = ("timeout", "missing", "unavailable", "_failed", "error")


def make_key(incidents, subject_repo, baseline_commit, target_preimages, patch_sha256,
             strategy_version, test_version):
    evidence = [
        {"source_ref": item.get("source_ref"), "content": item.get("content")}
        for item in incidents
    ]
    raw = json.dumps({
        "evidence": evidence, "subject_repo": str(Path(subject_repo).resolve()),
        "baseline_commit": baseline_commit, "target_preimages": dict(sorted(target_preimages.items())),
        "patch_sha256": patch_sha256,
        "strategy_version": strategy_version, "test_version": test_version,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def make_input_key(incidents, subject_repo, baseline_commit, worktree_state,
                   strategy_version, test_version):
    """Key an already-processed incident before any diagnosis model call."""
    evidence = [{"source_ref": item.get("source_ref"), "content": item.get("content")}
                for item in incidents]
    raw = json.dumps({
        "evidence": evidence, "subject_repo": str(Path(subject_repo).resolve()),
        "baseline_commit": baseline_commit, "worktree_state": worktree_state,
        "strategy_version": strategy_version, "test_version": test_version,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def stable_reason(reason):
    value = str(reason or "").casefold()
    return value == "test_failed"


def append(run_dir, key, reason, summary, subject_repo):
    row = {
        "kind": "rejection", "key": key, "reason": reason, "summary": summary,
        "subject_repo": str(Path(subject_repo).resolve()), "stable": stable_reason(reason),
    }
    path = Path(run_dir) / "evidence-ledger.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return row


def append_input(run_dir, key, reason, subject_repo):
    """Only a completed, non-infrastructure NO_CHANGE may enter this index."""
    row = {"kind": "input_rejection", "scope": "input", "key": key,
           "reason": reason, "subject_repo": str(Path(subject_repo).resolve()),
           "stable": reason == "already_processed"}
    path = Path(run_dir) / "evidence-ledger.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return row


class RejectionIndex:
    def __init__(self, rows):
        self.rows = rows

    def lookup(self, key):
        for row in reversed(self.rows):
            if row.get("kind") == "rejection" and row.get("stable") is True and row.get("key") == key:
                return row
        return None

    def lookup_input(self, key):
        for row in reversed(self.rows):
            if row.get("kind") == "input_rejection" and row.get("stable") is True and row.get("key") == key:
                return row
        return None

    def summaries(self, *, subject_repo, limit=3):
        subject = str(Path(subject_repo).resolve())
        rows = [row for row in reversed(self.rows) if row.get("stable") is True and row.get("subject_repo") == subject]
        return [{"reason": row["reason"], "summary": row.get("summary", "")} for row in rows[:limit]]


def build(runs_root, *, exclude_run_id=None):
    rows = []
    for ledger in sorted(Path(runs_root).glob("run-*/evidence-ledger.jsonl")):
        if ledger.parent.name == exclude_run_id:
            continue
        for line in ledger.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("kind") in {"rejection", "input_rejection"} and row.get("stable") is True:
                rows.append(row)
    return RejectionIndex(rows)
