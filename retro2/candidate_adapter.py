#!/usr/bin/env python3
"""B5: three isolated candidate worktrees with a durable evidence ledger."""
import base64
import difflib
import hashlib
import json
import os
import stat
import sys
import tempfile
import time
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent))
from host_exits import GitWorktreeHost  # noqa: E402
from immutable_adapter import ImmutableGuard  # noqa: E402


ALIASES = ("A", "B", "C")
CANDIDATE_STRATEGIES = (
    ("minimal_text_correction", "只做最小文字修正；不得重構根因邏輯或新增自動檢查。"),
    ("root_cause_logic", "修正造成問題的根因邏輯；不要只改使用者可見文字。"),
    ("automatic_check_or_test", "加入或修正可自動檢查的保護；不得只改描述文字。"),
)
PATCH_SIMILARITY_LIMIT = 0.8
MACHINE_TOOLS = frozenset(("claude_doctor", "hook_audit", "gate_record"))


class CandidateInputError(ValueError):
    pass


class IsolationError(RuntimeError):
    pass


def _patch_similarity(left, right):
    return difflib.SequenceMatcher(None, left, right).ratio()


def _atomic_write(path, content, binary=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".candidate-")
    try:
        mode = "wb" if binary else "w"
        kwargs = {} if binary else {"encoding": "utf-8"}
        with os.fdopen(fd, mode, **kwargs) as handle:
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


def _write_json(path, payload):
    _atomic_write(path, json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n")


def _append_ledger(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(descriptor, line.encode())
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _assert_read_only_tree(root):
    root = Path(root).resolve()
    if not root.is_dir():
        raise IsolationError("evaluator directory missing")
    paths = [root, *root.rglob("*")]
    if any(path.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH) for path in paths):
        raise IsolationError("evaluator must be read-only")
    return root


def _machine_ref_key(reference):
    return json.dumps(reference, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _valid_machine_ref(reference):
    return bool(
        isinstance(reference, dict)
        and reference.get("source_tool") in MACHINE_TOOLS
        and isinstance(reference.get("content_sha256"), str) and len(reference["content_sha256"]) == 64
        and all(character in "0123456789abcdef" for character in reference["content_sha256"])
        and isinstance(reference.get("component"), str) and reference["component"]
        and isinstance(reference.get("symptom"), str) and reference["symptom"]
        and isinstance(reference.get("occurrences"), int) and reference["occurrences"] > 0
    )


def _string_set(value):
    return set(value) if isinstance(value, list) and all(isinstance(item, str) and item for item in value) else None


def _observed_machine_refs(run_dir):
    path = Path(run_dir) / "evidence" / "incidents.json"
    if not path.is_file():
        return set()
    try:
        items = json.loads(path.read_text(encoding="utf-8")).get("items", [])
    except ValueError:
        return set()
    return {_machine_ref_key(item["machine_evidence_ref"]) for item in items
            if item.get("source_type") == "machine_observed" and _valid_machine_ref(item.get("machine_evidence_ref"))}


def _validated_requirements(requirements, run_dir=None):
    if not requirements:
        raise CandidateInputError("at least one diagnosed requirement is required")
    for item in requirements:
        decisions = _string_set(item.get("trusted_decision_refs", []))
        machine = item.get("machine_evidence_refs", [])
        finding = item.get("finding")
        actionable_finding = isinstance(finding, dict) and bool(finding.get("statement"))
        if item.get("target", "subject") not in {"subject", "host"}:
            raise CandidateInputError("candidate target is invalid")
        if not (
            item.get("diagnosis_id") and decisions is not None and (decisions or machine)
            and (actionable_finding or item.get("root_cause", {}).get("evidence"))
            and (actionable_finding or item.get("lite") is True or item.get("replay_fixture"))
        ):
            raise CandidateInputError("candidate input needs cited evidence and a replayable diagnosis or finding")
        if not isinstance(machine, list) or any(not _valid_machine_ref(ref) for ref in machine):
            raise CandidateInputError("machine evidence reference is invalid")
        if machine and (run_dir is None or any(_machine_ref_key(ref) not in _observed_machine_refs(run_dir) for ref in machine)):
            raise CandidateInputError("machine evidence reference is absent or hash-mismatched")
        links = item.get("decision_links")
        replay = item.get("host_replay")
        external_root_cause = any(
            str(value).startswith(("http://", "https://"))
            for value in item.get("root_cause", {}).get("evidence", ())
        )
        current_contract = "minimal_fix" in item and item.get("lite") is not True and not actionable_finding
        if (external_root_cause or current_contract) and (
            links is None or replay is None
        ):
            raise CandidateInputError(
                "candidate requirement requires decision links and host replay"
            )
        if links is not None and (not isinstance(links, dict) or _string_set(links.get("root_cause", [])) != decisions
                                  or _string_set(links.get("minimal_fix", [])) != decisions):
            raise CandidateInputError("every root cause and proposed change needs the same trusted decision links")
        if replay is not None and not (
            replay.get("passed") is True
            and replay.get("fixture_ref") == item["replay_fixture"]
            and len(replay.get("evidence_sha256", "")) == 64
        ):
            raise CandidateInputError("host replay evidence is invalid")
    return {
        "diagnoses": {item["diagnosis_id"] for item in requirements},
        "decisions": {
            ref for item in requirements for ref in item.get("trusted_decision_refs", ())
        },
        "machine": {_machine_ref_key(ref) for item in requirements for ref in item.get("machine_evidence_refs", ())},
    }


def generate_candidates(
    repo, run_dir, requirements, allowed_paths, evaluator_dir, runner,
    resource_policy, *, worktree_host=None, immutable_manifest=None,
    rejection_summaries=(), transplant_hints=(),
):
    repo = Path(repo).resolve()
    run_dir = Path(run_dir).resolve()
    evaluator = _assert_read_only_tree(evaluator_dir)
    provenance = _validated_requirements(requirements, run_dir)
    observed_machine = _observed_machine_refs(run_dir)
    allowed = frozenset(allowed_paths)
    if not allowed or not {"catalog_version", "seed", "timeout_seconds", "max_changed_files", "max_changed_lines"}.issubset(resource_policy):
        raise CandidateInputError("complete allowlist and resource policy required")
    manifest = Path(immutable_manifest) if immutable_manifest else repo / "retro2" / "immutable" / "immutable-manifest.json"
    guard = ImmutableGuard(manifest) if manifest.is_file() else None
    if guard:
        forbidden = allowed - frozenset(guard.candidate_view(allowed))
        if forbidden:
            raise CandidateInputError("allowlist includes immutable paths: " + ", ".join(sorted(forbidden)))
    worktree_host = worktree_host or GitWorktreeHost()
    baseline = worktree_host.baseline(repo)
    ledger = run_dir / "evidence-ledger.jsonl"
    policy_hash = hashlib.sha256(json.dumps(resource_policy, sort_keys=True).encode()).hexdigest()
    _append_ledger(ledger, {"kind": "baseline", "commit": baseline, "resource_policy_sha256": policy_hash})
    manifests = {}
    rejection_reasons = []
    for number, alias in enumerate(ALIASES, 1):
        worktree = run_dir / "worktrees" / f"candidate-{number}"
        worktree.parent.mkdir(parents=True, exist_ok=True)
        if evaluator == worktree or evaluator.is_relative_to(worktree):
            raise IsolationError("evaluator cannot live in a candidate worktree")
        prepared = worktree_host.prepare(repo, worktree, baseline)
        if prepared.get("status") != "READY":
            raise IsolationError("host failed to prepare candidate worktree")
        started = time.monotonic_ns()
        result = {}
        try:
            request = {
                "anonymous_id": alias,
                "strategy": {"name": CANDIDATE_STRATEGIES[number - 1][0],
                             "instruction": CANDIDATE_STRATEGIES[number - 1][1]},
                "worktree": str(worktree),
                "requirements": requirements,
                "allowed_paths": sorted(allowed),
                "resource_policy": dict(resource_policy),
                "rejection_summaries": list(rejection_summaries)[:3],
                "transplant_hints": list(transplant_hints),
            }
            result = runner(request) or {}
            elapsed = time.monotonic_ns() - started
            inspection = worktree_host.inspect(worktree)
            if inspection.get("status") != "INSPECTED":
                raise IsolationError("host failed to inspect candidate worktree")
            changed = inspection["changed_paths"]
            patch = base64.b64decode(inspection["patch_base64"], validate=True)
            lines = inspection["changed_lines"]
            candidate_dir = run_dir / "candidates" / alias
            _atomic_write(candidate_dir / "candidate.patch", patch, binary=True)
            cited_diagnoses = _string_set(result.get("diagnosis_ids", []))
            cited_decisions = _string_set(result.get("trusted_decision_refs", []))
            raw_machine = result.get("machine_evidence_refs", [])
            cited_machine = {_machine_ref_key(ref) for ref in raw_machine if _valid_machine_ref(ref)} if isinstance(raw_machine, list) else set()
            reasons = []
            if cited_diagnoses is None or cited_decisions is None:
                reasons.append("model_output_schema")
                cited_diagnoses, cited_decisions = cited_diagnoses or set(), cited_decisions or set()
            reasons.extend(result.get("disqualify_reasons", []))
            if not changed or not set(changed).issubset(allowed):
                reasons.append("path_allowlist")
            if guard and set(changed) - set(guard.candidate_view(changed)):
                reasons.append("immutable_manifest")
            if (not provenance["diagnoses"].issubset(cited_diagnoses)
                    or not provenance["decisions"].issubset(cited_decisions)
                    or not provenance["machine"].issubset(cited_machine)):
                reasons.append("provenance")
            if (not isinstance(raw_machine, list) or len(cited_machine) != len(raw_machine)
                    or not cited_machine.issubset(observed_machine)):
                reasons.append("machine_evidence")
            if len(changed) > resource_policy["max_changed_files"] or lines > resource_policy["max_changed_lines"]:
                reasons.append("size_limit")
            patch_hash = hashlib.sha256(patch).hexdigest()
            status = "DISQUALIFIED" if reasons else "ELIGIBLE"
            manifest = {
                "schema_version": 1,
                "anonymous_id": alias,
                "strategy_name": request["strategy"]["name"],
                "target": resource_policy.get("target", "subject"),
                "status": status,
                "reasons": reasons,
                "changed_paths": changed,
                "changed_lines": lines,
                "diagnosis_ids": sorted(cited_diagnoses),
                "trusted_decision_refs": sorted(cited_decisions),
                "machine_evidence_refs": [json.loads(ref) for ref in sorted(cited_machine)],
                "evidence_basis": "機器發現" if cited_machine and not cited_decisions else "真人裁決＋機器發現" if cited_machine else "真人裁決",
                "patch_sha256": patch_hash,
                "resource_policy": resource_policy,
                "stdout": result.get("stdout", ""),
                "stderr": result.get("stderr", ""),
                "token_usage": result.get("token_usage", {}),
                "changes": result.get("changes", []),
                "elapsed_ns": elapsed,
            }
            _write_json(candidate_dir / "manifest.json", manifest)
            manifests[alias] = manifest
            _append_ledger(ledger, {"kind": "candidate", "anonymous_id": alias, "status": status, "patch_sha256": patch_hash, "elapsed_ns": elapsed})
            if status == "DISQUALIFIED":
                rejection_reasons.extend(reasons)
        finally:
            worktree_host.remove(repo, worktree)
    retained = []
    for alias in ALIASES:
        manifest = manifests[alias]
        if manifest["status"] != "ELIGIBLE":
            continue
        duplicate = next((other for other in retained if _patch_similarity(
            (run_dir / "candidates" / other / "candidate.patch").read_text(encoding="utf-8"),
            (run_dir / "candidates" / alias / "candidate.patch").read_text(encoding="utf-8"),
        ) > PATCH_SIMILARITY_LIMIT), None)
        if duplicate:
            manifest["status"] = "DISQUALIFIED"
            manifest["reasons"].append("near_duplicate_patch")
            manifest["duplicate_of"] = duplicate
            _write_json(run_dir / "candidates" / alias / "manifest.json", manifest)
            rejection_reasons.append("near_duplicate_patch")
        else:
            retained.append(alias)
    substantive = len(retained)
    lite_ok = resource_policy.get("lite") is True and substantive >= 1
    status = "CANDIDATES_DONE" if (substantive == 3) or lite_ok else "NO_CHANGE"
    return {"status": status, "candidate_count": 3, "substantive_candidates": substantive,
            "rejection_reasons": sorted(set(rejection_reasons))}

