#!/usr/bin/env python3
"""B6: thin skill-runner adapter, artifact gate, and security transition."""
import ast
import json
import os
import re
import tempfile
import textwrap
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REVIEW_PROMPTS = {
    "gunpla-audit": "Review structure, acceptance criteria, and regressions. Report reproducible blockers only.",
    "wiring-ghost-audit": "Check that new behavior is connected to a real runtime entry point.",
    "security-review-project": "Review the change for concrete privacy, integrity, and authorization risks.",
    "security-audit-skill": "Produce an evidence-based security report with reproducible attack scenarios.",
}
SKILL_LIMIT = 8000
BLOCKER_RULE = (
    "blockers 只列能指出具體觸發情境、且會造成既有測試退步／安全隱私出境／資料遺失／正式環境壞掉／"
    "違反已拍板裁決的缺陷；『資料不足、無法驗證』只能放 findings，不得列為 blocker。"
    "test_evidence 是主控實跑的測試結果，請先讀。"
)

def _standard_schema(*required):
    return {"type": "object", "required": ["markdown", "json"], "properties": {
        "markdown": {"type": "string"}, "json": {"type": "object", "required": list(required),
        "properties": {name: {"type": "array", "items": {"type": "object"}} for name in required}},
    }}

FILTER_SCHEMAS = {
    "gunpla-audit": _standard_schema("findings", "blockers"),
    "wiring-ghost-audit": _standard_schema("findings", "blockers"),
    "security-review-project": _standard_schema("findings"),
    "security-audit-skill": {"type": "object", "required": ["architecture", "report", "details", "findings"],
        "properties": {"architecture": {"type": "string"}, "report": {"type": "string"},
        "details": {"type": "string"}, "findings": {"type": "object", "required": ["findings"],
        "properties": {"findings": {"type": "array", "items": {"type": "object"}}}}}},
}

class FilterGateError(RuntimeError):
    pass


def _atomic(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".filter-")
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
            pass
        raise


def _json(path, payload):
    _atomic(path, json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n")


def _added_text(patch):
    return "\n".join(
        line[1:] for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++")
    )


def _blocking_ast_count(lines):
    source = textwrap.dedent("\n".join(lines)).strip()
    if not source:
        return 0
    wrapped = "def _candidate_hook():\n" + textwrap.indent(source, "    ")
    try:
        tree = ast.parse(wrapped)
    except SyntaxError:
        return 0
    count = sum(isinstance(node, ast.Raise) for node in ast.walk(tree))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        name = node.func.id if isinstance(node.func, ast.Name) else (
            node.func.attr if isinstance(node.func, ast.Attribute) else ""
        )
        value = node.args[0].value if isinstance(node.args[0], ast.Constant) else None
        if name == "exit" and value == 2:
            count += 1
    return count


def classify_security_risks(candidate):
    patch = candidate.get("patch", "")
    added = _added_text(patch)
    lowered_paths = [path.lower() for path in candidate.get("changed_paths", [])]
    categories = set()
    if candidate.get("host_fail_closed"):
        if any(path.startswith("hooks/") for path in lowered_paths):
            categories.add("security_hook_path")
        if any(re.fullmatch(r"settings.*\.json", Path(path).name) for path in lowered_paths):
            categories.add("settings_security")
        if any(re.search(r"credential|secret|token|key", path) for path in lowered_paths):
            categories.add("credentials")
        if candidate.get("deletes_file") or any(change.get("delete") is True for change in candidate.get("changes", [])):
            categories.add("file_deletion")
    if re.search(r"chmod\s*\([^\n]*(?:0o?777|511)|permissions?[^\n]*(?:allow|admin|write|bash\(\*\))", added, re.I):
        categories.add("permission_expansion")
    if re.search(r"\b(?:urllib\.request|requests\.|httpx\.|socket\.|urlopen\(|curl\s|wget\s|https?://)", added, re.I):
        categories.add("network_egress")
    if any(re.search(r"(?:^|/)(?:\.env|credentials?|secrets?|[^/]*\.(?:pem|key))$", path) for path in lowered_paths) or re.search(
        r"(?:api[_-]?key|access[_-]?token|secret[_-]?key|password|passwd|pwd)\s*=|BEGIN [A-Z ]*PRIVATE KEY|AKIA[0-9A-Z]{16}|ghp_",
        added,
        re.I,
    ):
        categories.add("credentials")
    hook_paths = [path for path in lowered_paths if "hook" in path]
    removed = [line[1:] for line in patch.splitlines() if line.startswith("-") and not line.startswith("---")]
    added_lines = [line[1:] for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++")]
    if hook_paths and _blocking_ast_count(removed) > _blocking_ast_count(added_lines):
        categories.add("blocking_hook_weakened")
    if any(path.endswith("settings.json") for path in lowered_paths) and re.search(r"permissions?|sandbox", patch, re.I):
        categories.add("settings_security")
    return sorted(categories)


def _needs_wiring(paths):
    return any(
        path.startswith("hooks/")
        or re.search(r"(?:^|/)(?:register|registry|cron|schedule|settings)(?:[._/-]|$)", path, re.I)
        for path in paths
    )


def _call(runner, skill, candidate):
    content = REVIEW_PROMPTS[skill]
    result = runner(
        {
            "skill_name": skill,
            "skill_path": "built-in-review-prompt",
            "skill_content": content[:SKILL_LIMIT],
            "skill_content_truncated": len(content) > SKILL_LIMIT,
            "response_schema": FILTER_SCHEMAS[skill],
            "candidate_id": candidate["candidate_id"],
            "patch": candidate.get("patch", ""),
            "changed_paths": candidate.get("changed_paths", []),
            "test_evidence": candidate.get("test_evidence"),
            "blocker_rule": BLOCKER_RULE,
            "read_only": True,
        }
    )
    if not isinstance(result, dict):
        raise FilterGateError(f"missing required artifact from {skill}")
    return result


def _write_standard(root, name, result):
    if not isinstance(result.get("markdown"), str) or not isinstance(result.get("json"), dict):
        raise FilterGateError(f"missing required artifact from {name}")
    stem = {
        "gunpla-audit": "gunpla",
        "wiring-ghost-audit": "wiring",
        "security-review-project": "security-review",
    }[name]
    _atomic(root / f"{stem}.md", result["markdown"])
    _json(root / f"{stem}.json", result["json"])
    return result["json"]

def _write_ponytail(root, candidate):
    net = candidate.get("net_lines")
    if not isinstance(net, int):
        patch = candidate.get("patch", "")
        net = sum(line.startswith("+") and not line.startswith("+++") for line in patch.splitlines()) - sum(
            line.startswith("-") and not line.startswith("---") for line in patch.splitlines())
    payload = {"net_lines": net, "source": "host_patch_count"}
    _atomic(root / "ponytail.md", f"host net lines: {net}\n")
    _json(root / "ponytail.json", payload)
    return payload


def _write_security_audit(root, result):
    mapping = {
        "architecture": "architecture.md",
        "report": "REPORT.md",
        "details": "FINDINGS-DETAIL.md",
    }
    for key, filename in mapping.items():
        if not isinstance(result.get(key), str) or not result[key].strip():
            raise FilterGateError("missing required artifact from security-audit-skill")
        _atomic(root / "security-audit" / filename, result[key])
    findings = result.get("findings")
    if not isinstance(findings, dict) or not isinstance(findings.get("findings"), list):
        raise FilterGateError("missing required artifact from security-audit-skill")
    for finding in findings["findings"]:
        if not all(finding.get(key) for key in ("location", "evidence", "attack_scenario")):
            raise FilterGateError("security finding lacks reproducible attack evidence")
    _json(root / "security-audit/findings.json", findings)
    return findings


def _validate_findings(payload, label):
    findings = payload.get("findings", [])
    if not isinstance(findings, list):
        raise FilterGateError(f"{label} findings must be a list")
    for finding in findings:
        if not isinstance(finding, dict) or not finding.get("location") or not finding.get("evidence"):
            raise FilterGateError(f"{label} finding lacks diff location and evidence")


def assert_required_reports(root, wiring_required, semantic_reviewed=True):
    if not semantic_reviewed:
        return
    root = Path(root)
    required = [
        "ponytail.md", "ponytail.json", "gunpla.md", "gunpla.json",
        "security-review.md", "security-review.json", "security-audit/architecture.md",
        "security-audit/REPORT.md", "security-audit/FINDINGS-DETAIL.md",
        "security-audit/findings.json", "security-verdict.json",
    ]
    required += ["wiring.md", "wiring.json"] if wiring_required else ["wiring/not-applicable.json"]
    missing = [name for name in required if not (root / name).is_file() or not (root / name).stat().st_size]
    if missing:
        raise FilterGateError("missing required artifact: " + ", ".join(missing))


def filter_candidate(candidate, run_dir, runner, removable_limit, apply_candidate=None):
    """Generate and validate B6 artifacts. Application is deliberately out of scope."""
    del apply_candidate
    if not candidate.get("candidate_id") or not isinstance(candidate.get("changed_paths"), list):
        raise FilterGateError("candidate manifest incomplete")
    root = Path(run_dir) / "filters" / candidate["candidate_id"]
    del removable_limit
    _write_ponytail(root, candidate)
    gunpla = _write_standard(root, "gunpla-audit", _call(runner, "gunpla-audit", candidate))
    _validate_findings(gunpla, "gunpla")
    wiring_required = _needs_wiring(candidate["changed_paths"])
    wiring = {"blockers": [], "findings": []}
    if wiring_required:
        wiring = _write_standard(root, "wiring-ghost-audit", _call(runner, "wiring-ghost-audit", candidate))
        _validate_findings(wiring, "wiring")
    else:
        _json(root / "wiring/not-applicable.json", {"reason": "non_hook_diff", "changed_paths": candidate["changed_paths"]})
    review = _write_standard(root, "security-review-project", _call(runner, "security-review-project", candidate))
    _validate_findings(review, "security-review")
    audit = _write_security_audit(root, _call(runner, "security-audit-skill", candidate))
    categories = classify_security_risks(candidate)
    security_findings = [*review.get("findings", []), *audit.get("findings", [])]
    if security_findings:
        categories = sorted(set(categories) | {"security_finding"})
    _json(root / "security-verdict.json", {
        "categories": categories,
        "finding_count": len(security_findings),
        "requires_human": bool(categories),
    })
    assert_required_reports(root, wiring_required)
    technical_blockers = [*(f"gunpla:{item}" for item in gunpla.get("blockers", [])),
                          *(f"wiring:{item}" for item in wiring.get("blockers", []))]
    if categories:
        verdict = {"status": "REQUIRES_HUMAN_SECURITY_APPROVAL", "reason": "security_risk",
                   "security_categories": categories, "technical_blockers": technical_blockers}
    elif gunpla.get("blockers"):
        verdict = {"status": "DISQUALIFIED", "reason": "gunpla_blocker", "security_categories": []}
    elif wiring.get("blockers"):
        verdict = {"status": "DISQUALIFIED", "reason": "wiring_blocker", "security_categories": []}
    else:
        verdict = {"status": "FILTERED_OK", "reason": "all_required_gates_passed", "security_categories": []}
    verdict.setdefault("technical_blockers", technical_blockers)
    _json(root / "filter-verdict.json", verdict)
    return verdict

