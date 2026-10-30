#!/usr/bin/env python3
"""Thin, read-only adapter between evidence records and an x-ray agent runner."""
import json
import os
import re
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
XRAY_SKILL = ROOT / "skills/x-ray/SKILL.md"
TERMINAL_STATUSES = {"DIAGNOSED", "FALSIFIED", "UNDIAGNOSED"}
HEX64 = re.compile(r"^[0-9a-f]{64}$")
DIAGNOSTIC_LENSES = (
    ("Navigation", "找資料耗時或重複讀檔時，檢查是否缺導航指標或隱藏依賴。"),
    ("Automated checks", "可由 lint、型別、測試或 CI 擋住的錯誤，先查既有檢查是否未接上。"),
    ("Coding standards", "機械性違規先做自動檢查；只有判斷型一致性才寫規則給 reviewer。"),
    ("Global AGENTS.md／CLAUDE.md", "規則檔過肥時只保留導航指標，其餘移往被指到的文件。"),
    ("Tool economy", "工具輸出或呼叫昂貴時，找可減少 token 的批次或替代路徑。"),
    ("No-ops", "大型 steering 檔中找不改變行為的指示。"),
    ("Information access", "關鍵資訊不可得時，評估唯讀日誌或第三方存取。"),
)


class TransitionError(RuntimeError):
    pass


def _atomic_write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".diagnosis-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass  # silent-swallow-ok: preserve the original write exception
        raise


def _write_json(path, payload):
    _atomic_write(path, json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n")


def _trusted_items(incident):
    evidence = incident.get("evidence", [])
    selected = []
    for item in evidence:
        if item.get("trust") != "trusted_decision" or not item.get("source_ref"):
            continue
        supports = item.get("supports")
        relevant = isinstance(supports, list) and incident.get("incident_id") in supports
        legacy_ref_link = (
            supports is None
            and str(incident.get("incident_id", "")).casefold()
            in item["source_ref"].casefold()
        )
        if relevant or legacy_ref_link:
            selected.append(item)
    return selected


def _trusted_refs(incident):
    return [item["source_ref"] for item in _trusted_items(incident)]


def _machine_items(incident):
    return [item for item in incident.get("evidence", []) if item.get("source_type") == "machine_observed"]


def _machine_refs(items):
    return [item["machine_evidence_ref"] for item in items if isinstance(item.get("machine_evidence_ref"), dict)]


def _eligible_input(incident, lite=False):
    return bool(
        incident.get("incident_id") and incident.get("symptom") and (_trusted_refs(incident) or _machine_items(incident))
        and (lite or incident.get("replay_fixture"))
    )


def _valid_host_replay(result, fixture_ref):
    return bool(
        isinstance(result, dict)
        and result.get("passed") is True
        and result.get("fixture_ref") == fixture_ref
        and result.get("observed")
        and HEX64.fullmatch(result.get("evidence_sha256", ""))
    )


def _valid_result(result, trusted_refs, host_replay=None, strict_links=False, lite=False):
    if not isinstance(result, dict) or result.get("status") not in {"DIAGNOSED", "FALSIFIED"}:
        return False
    replay = host_replay or result.get("replay") or {}
    if not lite and host_replay is None and (
        not replay.get("fixture_ref") or not replay.get("steps") or not replay.get("observed")
    ):
        return False
    if not result.get("hypotheses"):
        return False
    if result["status"] == "FALSIFIED":
        return True
    root_cause = result.get("root_cause") or {}
    fix = result.get("minimal_fix") or {}
    evidence = root_cause.get("evidence") or []
    root_refs = root_cause.get("trusted_decision_refs")
    fix_refs = fix.get("trusted_decision_refs")
    if strict_links and (_string_list(root_refs or []) is None or _string_list(fix_refs or []) is None
                         or set(root_refs) != set(trusted_refs) or set(fix_refs) != set(trusted_refs)):
        return False
    return bool(
        root_cause.get("statement")
        and evidence
        and all(isinstance(item, str) and ":" in item for item in evidence)
        and fix.get("subtractive_first")
        and fix.get("direction")
    )


def _finding_requirements(result, base, trusted_refs, machine_refs, lite):
    """Keep cited findings actionable even when x-ray cannot yet replay a root cause."""
    requirements, report_findings = [], []
    known_machine = {_machine_ref_key(item): item for item in machine_refs}
    findings = result.get("findings", []) if isinstance(result, dict) else []
    if not isinstance(findings, list):
        return requirements, [_report_finding(findings, "findings 必須是陣列")]
    for index, finding in enumerate(findings, 1):
        if not isinstance(finding, dict):
            report_findings.append(_report_finding(finding, "finding 必須是物件"))
            continue
        cited_trusted = _string_list(finding.get("trusted_decision_refs", []))
        raw_machine = finding.get("machine_evidence_refs", [])
        if (not isinstance(finding.get("statement"), str) or not finding["statement"]
                or _string_list(finding.get("angles")) is None or cited_trusted is None
                or not isinstance(raw_machine, list) or not all(isinstance(ref, dict) for ref in raw_machine)
                or finding.get("target") not in {"subject", "host"}
                or not isinstance(finding.get("evidence_path"), str) or not finding["evidence_path"].strip()
                or not isinstance(finding.get("change_path"), str) or not finding["change_path"].strip()):
            report_findings.append(_report_finding(finding, "缺少或含有非預期型別的 finding 欄位"))
            continue
        cited_trusted = sorted(set(cited_trusted) & set(trusted_refs))
        machine_keys = [_machine_ref_key(ref) for ref in raw_machine]
        cited_machine = [known_machine[key] for key in machine_keys if key in known_machine]
        if not cited_trusted and not cited_machine:
            report_findings.append(_report_finding(finding, "沒有本輪可驗證的 evidence_refs"))
            continue
        report_findings.append(finding)
        requirements.append({
            **base, "diagnosis_id": f"{base['diagnosis_id']}-finding-{index}", "lite": lite,
            "target": finding["target"], "finding": {"statement": finding["statement"],
                                                "angles": finding.get("angles", []),
                                                "evidence_path": finding["evidence_path"],
                                                "change_path": finding["change_path"]},
            "trusted_decision_refs": cited_trusted, "machine_evidence_refs": cited_machine,
        })
    return requirements, report_findings


def _machine_ref_key(reference):
    return json.dumps(reference, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _string_list(value):
    """Accept only model JSON arrays whose members are non-empty strings."""
    return value if isinstance(value, list) and all(isinstance(item, str) and item for item in value) else None


def _report_finding(finding, reason):
    if not isinstance(finding, dict):
        return {"statement": "診斷 finding 格式無效", "angles": [], "invalid_reason": reason}
    angles = finding.get("angles")
    return {"statement": finding.get("statement") if isinstance(finding.get("statement"), str) else "診斷 finding 格式無效",
            "angles": angles if _string_list(angles) is not None else [], "invalid_reason": reason}


def _markdown(record):
    lines = [f"# X-RAY 診斷：{record['incident_id']}", "", f"狀態：{record['status']}", "", "## 症狀", record.get("symptom", "")]
    if record.get("reason"):
        lines += ["", "## 未診斷原因", record["reason"]]
    if record.get("root_cause"):
        lines += ["", "## 可重現根因", record["root_cause"]["statement"]]
    if record.get("minimal_fix"):
        lines += ["", "## 最小修復方向", record["minimal_fix"]["subtractive_first"], record["minimal_fix"]["direction"]]
    return "\n".join(lines) + "\n"


def assert_diagnosed_transition(run_dir, incident_ids):
    diagnoses = Path(run_dir) / "diagnoses"
    records = {}
    for incident_id in incident_ids:
        path = diagnoses / f"{incident_id}.json"
        if path.is_file():
            records[incident_id] = json.loads(path.read_text(encoding="utf-8"))
    missing = [incident_id for incident_id in incident_ids if incident_id not in records]
    if missing:
        raise TransitionError("missing diagnosis: " + ", ".join(missing))
    invalid = [key for key, value in records.items() if value.get("status") not in TERMINAL_STATUSES]
    if invalid:
        raise TransitionError("invalid diagnosis status: " + ", ".join(invalid))
    return records


def diagnose_incidents(incidents, run_dir, runner, replay_runner=None, lite=False):
    run_dir = Path(run_dir)
    requirements = []
    ids = []
    for incident in incidents:
        incident_id = incident["incident_id"]
        ids.append(incident_id)
        base = {"diagnosis_id": f"diagnosis-{incident_id}", "incident_id": incident_id, "symptom": incident.get("symptom", "")}
        result = {}
        refs, machine = [], []
        if not _eligible_input(incident, lite):
            record = {**base, "status": "UNDIAGNOSED", "reason": "trusted decision and replay fixture required"}
        else:
            trusted = _trusted_items(incident)
            machine = _machine_items(incident)
            refs = [item["source_ref"] for item in trusted]
            host_replay = None
            if replay_runner is not None:
                host_replay = replay_runner({
                    "incident_id": incident_id,
                    "fixture_ref": incident["replay_fixture"],
                })
                _write_json(run_dir / "replays" / f"{incident_id}.json", host_replay)
            filtered_incident = {**incident, "evidence": trusted + machine}
            request = {
                "skill_path": str(XRAY_SKILL),
                "symptom_or_hypothesis": incident["symptom"],
                "incident": filtered_incident,
                "diagnostic_lenses": [{"angle": angle, "use_when": use_when} for angle, use_when in DIAGNOSTIC_LENSES],
                "finding_rule": "每個 finding 必須有 angle、trusted_decision_refs 或 machine_evidence_refs 至少一種、target（subject 或 host）、evidence_path（證據在哪，可是 log）及 change_path（要改的檔）。用唯讀 Read/Grep 實際找到負責該行為的程式檔；change_path 必須是 target repo 內已讀的一般檔，不能是 /tmp、log 或證據檔。缺任一只留報告，不得當候選依據。先找既有檢查是否沒接上。",
                "read_only": True,
            }
            result = runner(request) if host_replay is None or _valid_host_replay(host_replay, incident["replay_fixture"]) else {}
            strict_links = any(item.get("trust") != "trusted_decision" for item in incident.get("evidence", []))
            if lite and _valid_result(result, refs, host_replay, strict_links, lite):
                # lite：一個事故含最近 30 則訊息，只保留模型實際引用、且確實受信任的那幾則
                root_refs = _string_list((result.get("root_cause") or {}).get("trusted_decision_refs") or [])
                fix_refs = _string_list((result.get("minimal_fix") or {}).get("trusted_decision_refs") or [])
                cited = set(root_refs or []) | set(fix_refs or [])
                refs = sorted(cited & set(refs))
                if result["status"] == "DIAGNOSED" and not refs and not machine:
                    result = {}
                else:
                    result = {**result,
                              "root_cause": {**(result.get("root_cause") or {}), "trusted_decision_refs": refs},
                              "minimal_fix": {**(result.get("minimal_fix") or {}), "trusted_decision_refs": refs}}
            if _valid_result(result, refs, host_replay, strict_links, lite):
                record = {**base, **result, "trusted_decision_refs": refs}
                if host_replay is not None:
                    record["replay"] = host_replay
            else:
                record = {**base, "status": "UNDIAGNOSED", "reason": "x-ray result is not replayable",
                          "findings": result.get("findings", []) if isinstance(result, dict) else []}
        finding_requirements, report_findings = _finding_requirements(result, base, refs, _machine_refs(machine), lite)
        if result.get("findings") is not None or report_findings:
            record["findings"] = report_findings
        _write_json(run_dir / "diagnoses" / f"{incident_id}.json", record)
        _atomic_write(run_dir / "diagnoses" / f"{incident_id}.md", _markdown(record))
        if record["status"] == "DIAGNOSED":
            refs = record["trusted_decision_refs"]
            requirement = {
                "incident_id": incident_id,
                "diagnosis_id": record["diagnosis_id"],
                "lite": lite,
                "machine_observed": bool(machine),
                "trusted_decision_refs": refs,
                "machine_evidence_refs": _machine_refs(machine),
                "finding_lenses": report_findings,
                "decision_links": {
                    "root_cause": record["root_cause"].get("trusted_decision_refs", refs),
                    "minimal_fix": record["minimal_fix"].get("trusted_decision_refs", refs),
                },
                "root_cause": record["root_cause"],
                "minimal_fix": record["minimal_fix"],
            }
            if not lite:
                requirement["replay_fixture"] = record["replay"]["fixture_ref"]
                requirement["host_replay"] = record["replay"] if replay_runner is not None else None
            requirements.append(requirement)
        requirements.extend(finding_requirements)
    assert_diagnosed_transition(run_dir, ids)
    _write_json(run_dir / "candidate-requirements.json", {"schema_version": 1, "requirements": requirements})
    return {"status": "DIAGNOSED" if requirements else "UNDIAGNOSED", "eligible": len(requirements)}
