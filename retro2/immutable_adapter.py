#!/usr/bin/env python3
"""Immutable incident catalog, human curation gate, and thin Jev adapter."""
import fnmatch
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
import config


ROOT = Path(__file__).resolve().parents[1]
JEV_SCRIPT = config.JEV_SCRIPT
REQUIRED = {
    "incident_id", "title", "source", "risk_class", "root_cause_family",
    "fixture", "expected_observable", "mechanical_checks", "jev_question",
    "positive_control", "negative_control", "severity", "admission",
}
HEX64 = re.compile(r"^[0-9a-f]{64}$")
SECRET = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,}")
LEGACY_CONTROLLER_ALIASES = {
    "retro2/controller/run.py": "apply_controller",
    "retro2/apply/transaction.py": "revert_controller",
}


def _atomic_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".curator-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass  # silent-swallow-ok: preserve the original write exception
        raise


def validate_incident(item):
    if not isinstance(item, dict) or not REQUIRED.issubset(item):
        return False
    source = item.get("source", {})
    admission = item.get("admission", {})
    questions = item.get("jev_question")
    controls = item.get("positive_control")
    return bool(
        item.get("incident_id")
        and source.get("kind") in {"memory", "deferred_defect"}
        and source.get("path") and source.get("lines")
        and HEX64.fullmatch(source.get("evidence_sha256", ""))
        and isinstance(item.get("fixture"), dict) and item["fixture"]
        and item.get("expected_observable")
        and isinstance(item.get("mechanical_checks"), list) and item["mechanical_checks"]
        and isinstance(questions, list) and len(questions) == 1 and questions[0].get("text")
        and isinstance(controls, list) and len(controls) == 1 and controls[0].get("text")
        and item.get("severity") in {"blocker", "major"}
        and admission.get("curator") and admission.get("maintainer_confirmation_ref")
    )


def load_catalog(immutable_root):
    items = [json.loads(path.read_text(encoding="utf-8")) for path in sorted((Path(immutable_root) / "incidents").glob("*.json"))]
    if not 10 <= len(items) <= 20 or len({item.get("incident_id") for item in items}) != len(items):
        raise ValueError("catalog must contain 10-20 unique incidents")
    if not all(validate_incident(item) for item in items):
        raise ValueError("invalid incident")
    return items


def verify_source_evidence(item):
    source = item["source"]
    path = Path(source["path"])
    if not path.is_absolute():
        path = config.INCIDENT_EVIDENCE_ROOT / path
    start, end = (int(value) for value in source["lines"].split("-", 1))
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    excerpt = "".join(lines[start - 1:end]).encode()
    return hashlib.sha256(excerpt).hexdigest() == source["evidence_sha256"]


class ImmutableGuard:
    def __init__(self, manifest_path):
        self.path = Path(manifest_path)
        self.manifest = json.loads(self.path.read_text(encoding="utf-8"))

    def _path_category(self, path):
        for rule in self.manifest["protected"]:
            if any(fnmatch.fnmatch(path, pattern) for pattern in rule["paths"]):
                return rule["category"]
        return LEGACY_CONTROLLER_ALIASES.get(path)

    def assert_protected_paths_exist(self, root):
        root = Path(root)
        missing = [path for path in self.manifest.get("required_paths", []) if not (root / path).is_file()]
        if missing:
            raise ValueError("protected paths do not exist: " + ", ".join(missing))
        return True

    def classify(self, change):
        path = change["path"]
        category = self._path_category(path)
        if category == "settings_security":
            try:
                before = json.loads(change.get("before") or "{}")
                after = json.loads(change.get("after") or "{}")
            except (TypeError, ValueError):
                return {"status": "DISQUALIFIED", "category": category}
            fields = self.manifest["settings_fields"]
            if all(before.get(field) == after.get(field) for field in fields):
                return {"status": "ALLOWED", "category": None}
        if category:
            return {"status": "DISQUALIFIED", "category": category}
        return {"status": "ALLOWED", "category": None}

    def candidate_view(self, paths):
        # Ordinary settings may be proposed, but permission and sandbox changes remain protected.
        return [path for path in paths if self._path_category(path) in (None, "settings_security")]

    def snapshot(self, root):
        root = Path(root)
        self.assert_protected_paths_exist(root)
        return {path: (root / path).read_bytes() for path in self.manifest["hashes"]}

    def verify_snapshot(self, snapshot):
        failed = []
        for path, expected in self.manifest["hashes"].items():
            content = snapshot.get(path)
            if content is None or hashlib.sha256(content).hexdigest() != expected:
                failed.append(path)
        return failed


def curate_incident(candidate, quarantine_dir, incidents_dir, confirmation=None, human_started=False):
    quarantine_dir = Path(quarantine_dir)
    incidents_dir = Path(incidents_dir)
    incident_id = candidate.get("incident_id", "invalid")
    _atomic_json(quarantine_dir / f"{incident_id}.json", candidate)
    confirmed = bool(
        human_started and confirmation and confirmation.get("authority") == "maintainer"
        and confirmation.get("confirmed") is True
        and confirmation.get("source_ref") == candidate.get("admission", {}).get("maintainer_confirmation_ref")
    )
    if not confirmed:
        return {"status": "QUARANTINED", "incident_id": incident_id}
    if not validate_incident(candidate) or SECRET.search(json.dumps(candidate, ensure_ascii=False)):
        return {"status": "REJECTED", "incident_id": incident_id}
    destination = incidents_dir / f"{incident_id}.json"
    if destination.exists():
        return {"status": "DUPLICATE", "incident_id": incident_id}
    _atomic_json(destination, candidate)
    return {"status": "ADMITTED", "incident_id": incident_id}


def evaluate_with_jev(incident, state_file, runner):
    if not validate_incident(incident):
        raise ValueError("invalid incident")
    state_file = Path(state_file)
    if not state_file.is_file() or not state_file.read_text(encoding="utf-8").strip():
        raise ValueError("state evidence required")
    policy = json.loads((Path(__file__).with_name("immutable") / "scoring-policy.json").read_text(encoding="utf-8"))
    if not JEV_SCRIPT.is_file():
        return {
            "status": "ERROR",
            "probabilities": {},
            "evidence_sha256": hashlib.sha256(state_file.read_bytes()).hexdigest(),
            "stderr": f"optional semantic judge not found: {JEV_SCRIPT}; set RETRO2_JEV_SCRIPT",
        }
    argv = [
        sys.executable, str(JEV_SCRIPT), "--state-file", str(state_file),
        "-q", "target=" + incident["jev_question"][0]["text"],
        "-q", "positive_control=" + incident["positive_control"][0]["text"],
    ]
    code, stdout, stderr = runner(argv)
    probabilities = {}
    for line in stdout.splitlines():
        match = re.fullmatch(r"\s*(0(?:\.\d+)?|1(?:\.0+)?)\s+(\w+)\s*", line)
        if match:
            probabilities[match.group(2)] = float(match.group(1))
    evidence_hash = hashlib.sha256(state_file.read_bytes()).hexdigest()
    base = {"probabilities": probabilities, "evidence_sha256": evidence_hash, "stderr": stderr}
    if code != 0 or {"target", "positive_control"} - probabilities.keys():
        return {**base, "status": "ERROR"}
    if probabilities["positive_control"] < policy["positive_control_min"]:
        return {**base, "status": "INVALID_CONTROL"}
    return {**base, "status": "PASS" if probabilities["target"] >= policy["target_pass_min"] else "FAIL"}

