#!/usr/bin/env python3
"""Trusted adapters for the read-only health and evidence stages."""
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
import config


HOOK_AUDIT = config.HOOK_AUDIT_SCRIPT
GATE_LOGS = config.gate_logs()
MACHINE_INCIDENT = "transcript-recent"
RECENT_LOG_SECONDS = 7 * 24 * 60 * 60
CLAUDE_LIMIT = 20_000
MEMORY_LIMIT = int(24.4 * 1024)


def _atomic_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".evidence-")
    with os.fdopen(fd, "w") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _atomic_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".evidence-")
    with os.fdopen(fd, "w") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _record_command(path, argv, result):
    code, stdout, stderr = result
    _atomic_json(path, {"argv": argv, "exit_code": code, "stdout": stdout, "stderr": stderr})
    return code == 0


def run_health(run_dir, runner):
    health = Path(run_dir) / "health"
    doctor = ["claude", "doctor"]
    result = runner(doctor)
    _atomic_text(health / "claude-doctor.txt", result[1] + result[2])
    if result[0] != 0:
        _atomic_json(health / "verdict.json", {"healthy": False, "reason": "claude_doctor_failed"})
        return False, "claude_doctor_failed"
    if not HOOK_AUDIT.is_file():
        _atomic_json(health / "hook-audit.json", {
            "status": "SKIPPED",
            "reason": "optional hook audit script not configured",
            "path": str(HOOK_AUDIT),
        })
        _atomic_json(health / "verdict.json", {"healthy": True, "degraded": ["hook_audit"]})
        return True, "hook_audit_optional_missing"
    audit = [sys.executable, str(HOOK_AUDIT), "--days", "7", "--json"]
    result = runner(audit)
    try:
        audit_payload = json.loads(result[1])
    except (TypeError, ValueError):
        audit_payload = {"parse_error": True, "raw": result[1]}
    _atomic_json(health / "hook-audit.json", audit_payload)
    if result[0] != 0 or audit_payload.get("parse_error"):
        _atomic_json(health / "verdict.json", {"healthy": False, "reason": "hook_audit_failed"})
        return False, "hook_audit_failed"
    _atomic_json(health / "verdict.json", {"healthy": True})
    return True, None


def _fresh_report(report_root, started_ns):
    reports = [Path(report_root) / "report.html", *Path(report_root).glob("report-*.html")]
    fresh = [path for path in reports if path.is_file() and path.stat().st_mtime_ns > started_ns]
    return max(fresh, key=lambda path: path.stat().st_mtime_ns, default=None)


class _InsightsParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.section = self.capture = self.title = None
        self.text, self.items = [], []

    def handle_starttag(self, tag, attrs):
        classes = dict(attrs).get("class", "")
        if tag in {"h2", "h3"}:
            self.capture, self.text = "heading", []
        elif self.section == "wrong" and "friction-title" in classes:
            self.capture, self.text = "title", []
        elif self.section == "wrong" and "friction-desc" in classes:
            self.capture, self.text = "wrong", []
        elif self.section == "additions" and "cmd-code" in classes:
            self.capture, self.text = "title", []
        elif self.section == "additions" and "cmd-why" in classes:
            self.capture, self.text = "additions", []

    def handle_data(self, data):
        if self.capture:
            self.text.append(data)

    def handle_endtag(self, tag):
        if not self.capture or tag not in {"h2", "h3", "div", "code"}:
            return
        value = " ".join(" ".join(self.text).split())
        if self.capture == "heading":
            self.section = {"Where Things Go Wrong": "wrong", "Suggested CLAUDE.md Additions": "additions"}.get(value)
        elif self.capture == "title":
            self.title = value
        elif self.title and value:
            self.items.append((self.title, value[:300]))
        self.capture, self.text = None, []


def _insights_observations(report):
    parser = _InsightsParser()
    parser.feed(report.read_text(encoding="utf-8", errors="replace"))
    return [{"source_type": "machine_observed", "authority": "machine", "source": "insights_prose",
             "source_ref": f"evidence/insights.html#{title}", "trust": "untrusted_evidence",
             "component": "insights", "symptom": f"{title}：{prose}", "occurrences": 1,
             "content": f"{title}：{prose}", "incident_id": MACHINE_INCIDENT,
             "supports": [MACHINE_INCIDENT]} for title, prose in parser.items]


def _trust(item, trusted_decision_refs):
    if (
        item.get("source_type") == "trusted_decision"
        and item.get("authority") == "maintainer"
        and item.get("source_ref") in trusted_decision_refs
    ):
        return "trusted_decision"
    return "untrusted_evidence"


def _machine_item(component, symptom, occurrences, source_ref, source_tool, source_content, incident_id=MACHINE_INCIDENT):
    """Tool output remains data: this record can never carry decision trust."""
    occurrences = max(1, int(occurrences))
    machine_evidence_ref = {
        "source_tool": source_tool,
        "content_sha256": hashlib.sha256(source_content).hexdigest(),
        "component": str(component)[:240],
        "symptom": str(symptom)[:1000],
        "occurrences": occurrences,
    }
    return {
        "source_type": "machine_observed", "authority": "machine",
        "source_ref": source_ref, "trust": "untrusted_evidence",
        **machine_evidence_ref,
        "machine_evidence_ref": machine_evidence_ref,
        # The model receives only these three normalized symptoms, never raw tool prose.
        "content": f"component={machine_evidence_ref['component']}; symptom={machine_evidence_ref['symptom']}; occurrences={occurrences}",
        "incident_id": incident_id, "supports": [incident_id],
    }


def _doctor_findings(text):
    raw = str(text).encode()
    count = sum(bool(line.strip() and re.search(
        r"\b(?:error|failed|warning|timeout|missing)\b|錯誤|失敗|逾時|找不到", line, re.I
    )) for line in str(text).splitlines())
    return [] if not count else [_machine_item(
        "claude doctor", "診斷輸出含異常行", count, "health/claude-doctor.txt", "claude_doctor", raw
    )]


def _audit_findings(payload):
    findings = []
    source_content = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    for finding in payload.get("findings", []) if isinstance(payload, dict) else []:
        if not isinstance(finding, dict):
            continue
        # Only the explicit structured triple is evidence; raw/message prose is ignored.
        component, symptom = finding.get("component"), finding.get("symptom")
        if not isinstance(component, str) or not isinstance(symptom, str):
            continue
        findings.append(_machine_item(component, symptom, finding.get("count", finding.get("occurrences", 1)),
                                      "health/hook-audit.json#findings", "hook_audit", source_content))
    fails = payload.get("fails", {}) if isinstance(payload, dict) else {}
    for component, codes in fails.items():
        if not isinstance(codes, dict):
            continue
        for code, count in codes.items():
            label = {124: "逾時", 126: "腳本不可執行", 127: "腳本或命令不存在"}.get(
                int(code) if str(code).isdigit() else -1, "hook 執行錯誤"
            )
            findings.append(_machine_item(component, f"{label}（exit {code}）", count,
                                          f"health/hook-audit.json#fails/{component}/{code}", "hook_audit", source_content))
    return findings


def _gate_log_findings(subject_repo, now_ns=None, log_paths=GATE_LOGS):
    subject = str(Path(subject_repo).resolve())
    now_ns = time.time_ns() if now_ns is None else now_ns
    findings = []
    for root in log_paths:
        root = Path(root)
        paths = [root] if root.is_file() else list(root.rglob("*.log")) if root.is_dir() else []
        for path in paths[:100]:
            try:
                if now_ns - path.stat().st_mtime_ns > RECENT_LOG_SECONDS * 1_000_000_000:
                    continue
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            matches = [line.strip() for line in lines if subject in line and re.search(
                r"error|failed|timeout|blocked|deny|stop|exit|失敗|錯誤|逾時|擋", line, re.I)]
            if matches:
                findings.append(_machine_item(path.name, "受管專案閘門記錄異常", len(matches), str(path),
                                              "gate_record", "\n".join(lines).encode()))
    return findings


def _byte_size(value):
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8"))


def _tool_blocks(row):
    content = row.get("message", {}).get("content", [])
    return content if isinstance(content, list) else []


def _row_timestamp_ns(row):
    if isinstance(row.get("timestamp_ns"), int):
        return row["timestamp_ns"]
    if isinstance(row.get("timestamp"), str):
        try:
            return int(datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")).timestamp() * 1_000_000_000)
        except ValueError:
            return None
    return None


def collect_session_observations(run_dir, transcript_path, subject_repo, *, global_rules_path=None, since_ns=None):
    """Count transcript tool metadata; never retain transcript prose as evidence."""
    calls, outputs, failures, reads, searches, agents = {}, [], set(), {}, {}, 0
    known = {}
    path = Path(transcript_path)
    if path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            timestamp_ns = _row_timestamp_ns(row)
            if since_ns is not None and (timestamp_ns is None or timestamp_ns < since_ns):
                continue
            for block in _tool_blocks(row):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    name = str(block.get("name", "unknown"))
                    known[block.get("id")] = name
                    calls[name] = calls.get(name, 0) + 1
                    data = block.get("input", {}) if isinstance(block.get("input"), dict) else {}
                    if name == "Read" and isinstance(data.get("file_path"), str):
                        reads[data["file_path"]] = reads.get(data["file_path"], 0) + 1
                    if name in {"Grep", "Glob"} and isinstance(data.get("pattern"), str):
                        key = f"{name}:{data['pattern']}"
                        searches[key] = searches.get(key, 0) + 1
                    agents += int(name.casefold() in {"agent", "task", "spawn_agent"})
                elif block.get("type") == "tool_result":
                    name = known.get(block.get("tool_use_id"), "unknown")
                    outputs.append({"tool": name, "bytes": _byte_size(block.get("content", ""))})
                    if (block.get("is_error") is True or block.get("isError") is True
                            or block.get("exit_code") not in (None, 0)
                            or str(block.get("status", "")).casefold() in {"error", "rejected", "denied"}):
                        failures.add(name)
    repeated_reads = sorted(name for name, count in reads.items() if count >= 3)
    repeated_searches = sorted(key.split(":", 1)[1] for key, count in searches.items() if count >= 2)
    largest = sorted(outputs, key=lambda item: (-item["bytes"], item["tool"]))[:5]
    summary = {"tool_calls_total": sum(calls.values()), "tools": dict(sorted(calls.items())),
               "largest_outputs": largest, "repeated_reads": repeated_reads,
               "repeated_searches": repeated_searches, "failed_or_refused_count": len(failures),
               "failed_or_refused_tools": sorted(failures), "subagents": agents,
               "scope": {"basis": "since_last_retro_or_recent_window" if since_ns else "whole_transcript", "since_ns": since_ns}}
    source = json.dumps(summary, ensure_ascii=False, sort_keys=True).encode()
    observations = []
    if summary["tool_calls_total"]:
        observations.append(_machine_item("本場工作統計", f"工具呼叫 {summary['tool_calls_total']} 次", 1,
                                          "health/session-statistics.json", "transcript_statistics", source))
    if repeated_reads:
        observations.append(_machine_item("重複讀檔", "、".join(repeated_reads), len(repeated_reads),
                                          "health/session-statistics.json", "transcript_statistics", source))
    if repeated_searches:
        observations.append(_machine_item("重複搜尋", "、".join(repeated_searches), len(repeated_searches),
                                          "health/session-statistics.json", "transcript_statistics", source))
    if failures:
        observations.append(_machine_item("失敗或被拒工具", "、".join(sorted(failures)), len(failures),
                                          "health/session-statistics.json", "transcript_statistics", source))
    _atomic_json(Path(run_dir) / "health/session-statistics.json", summary)
    return observations, summary


def collect_rule_file_volumes(run_dir, subject_repo, *, global_rules_path=None):
    global_rules_path = Path(global_rules_path or config.GLOBAL_RULES_FILE)
    entries = [("global", global_rules_path), *[("subject", Path(subject_repo) / name) for name in ("CLAUDE.md", "AGENTS.md", "MEMORY.md")]]
    volumes = []
    for scope, path in entries:
        text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
        limit = MEMORY_LIMIT if path.name == "MEMORY.md" else CLAUDE_LIMIT if path.name == "CLAUDE.md" else None
        byte_count = len(text.encode("utf-8"))
        over = (byte_count > limit if path.name == "MEMORY.md" else len(text) > limit) if limit is not None else False
        volumes.append({"scope": scope, "filename": path.name, "characters": len(text), "bytes": byte_count,
                        "lines": text.count("\n") + bool(text), "limit": limit, "over_limit": over})
    over = [item for item in volumes if item["over_limit"]]
    source = json.dumps(volumes, ensure_ascii=False, sort_keys=True).encode()
    observations = [] if not over else [_machine_item("規則檔體積", "、".join(item["filename"] for item in over), len(over),
                                                       "health/rule-file-volumes.json", "rule_file_volume", source)]
    _atomic_json(Path(run_dir) / "health/rule-file-volumes.json", volumes)
    return observations, volumes


def collect_machine_observations(run_dir, subject_repo, *, incident_id=MACHINE_INCIDENT, now_ns=None, log_paths=GATE_LOGS,
                                 insights=(), insights_acquired=False, session_statistics=None, rule_file_volumes=(), extra_observations=()):
    """Normalize read-only harness symptoms; never treat them as instructions or decisions."""
    health = Path(run_dir) / "health"
    doctor_path = health / "claude-doctor.txt"
    audit_path = health / "hook-audit.json"
    doctor = doctor_path.read_text(encoding="utf-8", errors="replace") if doctor_path.is_file() else ""
    try:
        audit = json.loads(audit_path.read_text(encoding="utf-8")) if audit_path.is_file() else {}
    except ValueError:
        audit = {"findings": [{"component": "hook_audit", "symptom": "health artifact is invalid JSON", "count": 1}]}
    observations = _doctor_findings(doctor) + _audit_findings(audit)
    observations += _gate_log_findings(subject_repo, now_ns=now_ns, log_paths=log_paths)
    observations += list(insights)
    observations += list(extra_observations)
    for item in observations:
        item["incident_id"] = incident_id
        item["supports"] = [incident_id]
    checks = [
        {"component": "claude doctor", "source": "health/claude-doctor.txt", "checked": doctor_path.is_file()},
        {"component": "hook-doctor", "source": "health/hook-audit.json", "checked": audit_path.is_file()},
        {"component": "受管專案閘門紀錄", "source": ",".join(map(str, GATE_LOGS)), "checked": True},
        {"component": "/insights", "source": "evidence/insights.html", "checked": insights_acquired,
         "reason": "未取得" if not insights_acquired else "自由文字僅供診斷與報告，不是改法依據"},
    ]
    _atomic_json(health / "harness-check.json", {"schema_version": 1, "checks": checks, "findings": observations,
                 "session_statistics": session_statistics or {}, "rule_file_volumes": list(rule_file_volumes)})
    return observations


def collect_evidence(
    run_dir, report_root, started_ns, incidents, runner, trusted_decision_refs=(),
    health_result=None,
):
    """Run evidence stages; unavailable insights is recorded but never stops this stage."""
    run_dir = Path(run_dir)
    healthy, reason = health_result if health_result is not None else run_health(run_dir, runner)
    if not healthy:
        return {"status": "NO_CHANGE", "reason": reason}
    command = ["claude", "-p", "/insights"]
    result = runner(command, timeout=300)
    _record_command(run_dir / "evidence/insights-command.json", command, result)
    evidence = run_dir / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    report = _fresh_report(report_root, started_ns) if result[0] == 0 else None
    observations = _insights_observations(report) if report else []
    if report:
        shutil.copyfile(report, evidence / "insights.html")
    normalized = []
    for index, item in enumerate(incidents):
        normalized.append(
            {
                "evidence_id": f"evidence-{index + 1}",
                "source_type": item.get("source_type", "unknown"),
                "authority": item.get("authority"),
                "source_ref": item.get("source_ref", ""),
                "trust": _trust(item, frozenset(trusted_decision_refs)),
                "content": item.get("content", ""),
                "incident_id": item.get("incident_id"),
                "supports": item.get("supports", []),
            }
        )
    _atomic_json(evidence / "incidents.json", {"items": normalized})
    _atomic_json(evidence / "provenance.json", {"schema_version": 1, "items": normalized})
    return {"status": "EVIDENCE_OK", "report": str(evidence / "insights.html") if report else None,
            "insights": "已取得" if report else "未取得", "insights_observations": observations}

