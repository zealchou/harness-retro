#!/usr/bin/env python3
"""Project experience state and transactional cross-project promotion."""
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import config
DEFAULT_GLOBAL_PATH = config.GLOBAL_EXPERIENCE_FILE
HEADER = (
    "# Retro-2 經驗本\n\n"
    "本檔只保存由真實勝出證據支持的教訓；數字信心由程式計算，deprecated 條目保留不刪。\n"
)
ENTRY = re.compile(
    r"^## (?P<id>[^\n]+)\n.*?^```json\s*\n(?P<body>.*?)\n^```\s*$",
    re.MULTILINE | re.DOTALL,
)
def _atomic_write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".experience-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass  # silent-swallow-ok: retain the original atomic-write exception
        raise
def _restore(path, backup):
    path = Path(path)
    if backup is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".experience-restore-")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(backup)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass  # silent-swallow-ok: retain the rollback exception
        raise
def _read_entries(path):
    path = Path(path)
    if not path.is_file():
        return {}
    entries = {}
    for match in ENTRY.finditer(path.read_text(encoding="utf-8")):
        record = json.loads(match.group("body"))
        if record.get("experience_id") == match.group("id"):
            entries[record["experience_id"]] = record
    return entries
def _render(entries):
    blocks = [HEADER.rstrip()]
    for experience_id in sorted(entries):
        record = entries[experience_id]
        note = ""
        if record.get("promoted_to"):
            note = f"已升級全域：{record['promoted_to']}（{record.get('promoted_on', '')}）\n"
        body = json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2)
        blocks.append(f"## {experience_id}\n{note}```json\n{body}\n```")
    return "\n\n".join(blocks) + "\n"
def _project_path(repo):
    return Path(repo).resolve() / "docs/EXPERIENCE.md"
def _lesson_key(finding):
    # The same root cause may use different symptom strings; an optional semantic judge can merge them later.
    references = sorted(
        f"{item.get('component', '')}|{item.get('symptom', '')}"
        for item in finding.get("machine_evidence_refs", [])
    )
    angles = sorted(str(item) for item in finding.get("angles", []))
    source = f"{finding.get('target', '')}|{'|'.join(references)}|{'|'.join(angles)}"
    return hashlib.sha256(source.encode()).hexdigest()[:16]
def _incident_key(finding):
    hashes = sorted(str(item.get("content_sha256", "")) for item in finding.get("machine_evidence_refs", []))
    return hashlib.sha256("".join(hashes).encode()).hexdigest()[:16]
def experience_id(repo, finding):
    repo_key = hashlib.sha256(str(Path(repo).resolve()).encode()).hexdigest()[:8]
    return f"exp-{_lesson_key(finding)}-{repo_key}"
def has_experience(path, item_id):
    return item_id in _read_entries(path)
def _confidence(evidence):
    incidents, sessions, independent = set(), set(), 0
    for item in evidence:
        if item["incident_key"] not in incidents and item["session_id"] not in sessions:
            independent += 1
        incidents.add(item["incident_key"])
        sessions.add(item["session_id"])
    return {
        "wins": independent,
        "independent_incidents": independent,
        "basis": "distinct incident_key and session_id pairs computed from evidence",
    }
def _evidence(run_id, session_id, finding, commit_sha):
    return {
        "run_id": run_id, "incident_key": _incident_key(finding),
        "session_id": session_id, "commit_sha": commit_sha,
        "target": finding.get("target", "subject"),
    }
def _new_entry(repo, finding, evidence):
    item_id = experience_id(repo, finding)
    return {
        "experience_id": item_id,
        "lesson_key": _lesson_key(finding),
        "cue": str(finding.get("statement", ""))[:300],
        "lesson": str(finding.get("strategy_name", ""))[:300],
        "evidence": [evidence],
        "confidence": _confidence([evidence]),
        "scope": "project", "status": "provisional",
        "lineage": [{"repo": str(Path(repo).resolve()), "experience_id": item_id}],
    }
def _stub(entry, global_id):
    return {
        "experience_id": entry["experience_id"], "lesson_key": entry["lesson_key"],
        "scope": "project", "status": entry["status"],
        "confidence": entry["confidence"], "lineage": entry.get("lineage", []),
        "promoted_to": global_id, "promoted_on": date.today().isoformat(),
    }
def _transaction(changes):
    backups = {Path(path): Path(path).read_bytes() if Path(path).is_file() else None for path, _ in changes}
    attempted = []
    try:
        for path, content in changes:
            attempted.append(Path(path))
            _atomic_write(path, content)
    except Exception:
        for path in reversed(attempted):
            _restore(path, backups[path])
        raise
def record_win(repo, *, run_id, session_id, finding, commit_sha, global_path, project_repos):
    repo = Path(repo).resolve()
    project_path = _project_path(repo)
    entries = _read_entries(project_path)
    item_id = experience_id(repo, finding)
    event = _evidence(run_id, session_id, finding, commit_sha)
    entry = entries.get(item_id)
    if entry and entry.get("promoted_to"):
        return {"action": "promoted", "experience_id": item_id, "global_id": entry["promoted_to"]}
    if entry is None:
        entry = _new_entry(repo, finding, event)
        action = "provisional"
    else:
        known_incidents = {item["incident_key"] for item in entry["evidence"]}
        known_sessions = {item["session_id"] for item in entry["evidence"]}
        independent = event["incident_key"] not in known_incidents and event["session_id"] not in known_sessions
        if event not in entry["evidence"]:
            entry["evidence"].append(event)
        if independent:
            entry["confidence"] = _confidence(entry["evidence"])
            if entry["confidence"]["independent_incidents"] >= 2:
                entry["status"] = "confirmed"
            action = "confirmed" if entry["status"] == "confirmed" else "provisional"
        else:
            action = "duplicate_incident"
    entries[item_id] = entry

    source = None
    for other in sorted({Path(path).resolve() for path in project_repos if Path(path).exists()}):
        if other == repo:
            continue
        other_entries = _read_entries(_project_path(other))
        source_entry = next((item for item in other_entries.values()
                             if item.get("lesson_key") == entry["lesson_key"] and item.get("status") == "confirmed"
                             and not item.get("promoted_to")), None)
        if source_entry:
            source = other, other_entries, source_entry
            break
    if source is None:
        _atomic_write(project_path, _render(entries))
        return {"action": action, "experience_id": item_id}

    other_repo, other_entries, source_entry = source
    global_path = Path(global_path)
    global_entries = _read_entries(global_path)
    global_id = f"global-{entry['lesson_key']}"
    merged = []
    for evidence in source_entry["evidence"] + entry["evidence"]:
        if evidence not in merged:
            merged.append(evidence)
    global_entries[global_id] = {
        "experience_id": global_id, "lesson_key": entry["lesson_key"],
        "cue": entry["cue"], "lesson": entry["lesson"], "evidence": merged,
        "confidence": _confidence(merged), "scope": "global", "status": "confirmed",
        "lineage": [
            {"repo": str(other_repo), "experience_id": source_entry["experience_id"]},
            {"repo": str(repo), "experience_id": entry["experience_id"]},
        ],
    }
    entries[item_id] = _stub(entry, global_id)
    other_entries[source_entry["experience_id"]] = _stub(source_entry, global_id)
    _transaction([
        (project_path, _render(entries)),
        (_project_path(other_repo), _render(other_entries)),
        (global_path, _render(global_entries)),
    ])
    return {"action": "promoted", "experience_id": item_id, "global_id": global_id}
def deprecate(path, experience_id, reason, counter_evidence):
    entries = _read_entries(path)
    if experience_id not in entries:
        raise KeyError(experience_id)
    entry = entries[experience_id]
    entry["status"] = "deprecated"
    entry["deprecated_reason"] = reason
    entry["counter_evidence"] = counter_evidence
    _atomic_write(path, _render(entries))
    return entry
def transplant_hints(repo, project_repos):
    repo = Path(repo).resolve()
    local_keys = {item.get("lesson_key") for item in _read_entries(_project_path(repo)).values()}
    hints = []
    for other in sorted({Path(path).resolve() for path in project_repos if Path(path).exists()}):
        if other == repo:
            continue
        for entry in _read_entries(_project_path(other)).values():
            if entry.get("status") == "confirmed" and not entry.get("promoted_to") and entry.get("lesson_key") not in local_keys:
                hints.append({
                    "lesson_key": entry["lesson_key"], "cue": entry["cue"], "lesson": entry["lesson"],
                    "source_repo": str(other), "experience_id": entry["experience_id"],
                })
    return hints
