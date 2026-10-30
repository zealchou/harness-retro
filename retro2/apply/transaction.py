#!/usr/bin/env python3
"""B8: apply one scored winner, canary it in a fresh session, and revert safely."""

import contextlib
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from immutable_adapter import ImmutableGuard  # noqa: E402


AUTO_SECURITY = "AUTO_APPLY_ALLOWED"
HUMAN_SECURITY = {"HUMAN_ONLY", "REQUIRES_HUMAN_SECURITY_APPROVAL"}
REQUIRED_ARTIFACTS = {
    "ponytail", "gunpla", "security-review", "security-audit", "security-verdict"
}
PROTECTED_CATEGORIES = {
    "incident_bank", "scoring_policy", "apply_controller", "revert_controller",
    "settings_security", "credentials", "security_hook_tests",
}
HEX64 = re.compile(r"^[0-9a-f]{64}$")
IDENT = re.compile(r"^[A-Za-z0-9._-]+$")
MISSING = "MISSING"

# Mutation-drill seams are constants, not runtime switches.
_REQUIRE_AUTO_SECURITY = True
_REQUIRE_FRESH_SESSION = True
_REVERT_ON_CANARY_FAILURE = True


class TransactionError(RuntimeError):
    """A precondition failed before a safe automatic transaction could finish."""


def _git(repo, *args, check=True):
    result = subprocess.run(
        ["git", "-C", str(repo), *args], text=True, capture_output=True
    )
    if check and result.returncode:
        raise TransactionError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _hash_path(path):
    if not path.exists():
        return MISSING
    if not path.is_file():
        raise TransactionError(f"hash target is not a regular file: {path}")
    return _digest(path.read_bytes())


def _safe_relative(value):
    if (not isinstance(value, str) or not value or "\\" in value
            or any(ord(character) < 32 for character in value)):
        raise TransactionError("winner path must be a non-empty POSIX relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise TransactionError(f"unsafe winner path: {value}")
    return path


def _repo_path(repo, value):
    root = Path(repo).resolve()
    relative = _safe_relative(value)
    path = root / Path(*relative.parts)
    parent = path.parent.resolve()
    if parent != root and root not in parent.parents:
        raise TransactionError(f"winner path escapes repo through a symlink: {value}")
    if path.is_symlink():
        raise TransactionError(f"winner path may not be a symlink: {value}")
    return path


def _atomic_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
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
            pass
        raise


def _append_ledger(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


@contextlib.contextmanager
def _locks(lock_root, repo):
    root = Path(lock_root)
    root.mkdir(parents=True, exist_ok=True)
    common = _git(repo, "rev-parse", "--git-common-dir").stdout.strip()
    common_path = Path(common)
    if not common_path.is_absolute():
        common_path = (Path(repo) / common_path).resolve()
    repo_key = _digest(str(common_path).encode())[:24]
    handles = []
    try:
        for path in (root / "retro2-global.lock", root / f"repo-{repo_key}.lock"):
            handle = path.open("a+")
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            handles.append(handle)
        yield
    finally:
        for handle in reversed(handles):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


def _validate_artifacts(package, run_dir):
    artifacts = package["winner"].get("filter_artifacts")
    if not isinstance(artifacts, dict) or not REQUIRED_ARTIFACTS.issubset(artifacts):
        raise TransactionError("winner is missing required filter artifacts")
    root = Path(run_dir).resolve()
    for name in REQUIRED_ARTIFACTS:
        item = artifacts[name]
        relative = _safe_relative(item.get("path"))
        path = (root / Path(*relative.parts)).resolve()
        if root not in path.parents or not path.is_file():
            raise TransactionError(f"filter artifact missing or outside run directory: {name}")
        if not HEX64.fullmatch(item.get("sha256", "")) or _hash_path(path) != item["sha256"]:
            raise TransactionError(f"filter artifact hash mismatch: {name}")


def _manifest_guard(repo, manifest_path=None):
    path = Path(manifest_path) if manifest_path else Path(repo) / "retro2" / "immutable" / "immutable-manifest.json"
    return ImmutableGuard(path) if path.is_file() else None


def _validate_package(package, repo, run_dir, manifest_path=None, host_root=None):
    if package.get("schema_version") != 1 or package.get("state") != "SCORED":
        raise TransactionError("winner package must be schema v1 in SCORED state")
    for key in ("run_id", "source_session_id", "repo_baseline_commit",
                "incident_bank_version", "scoring_version"):
        if not isinstance(package.get(key), str) or not package[key]:
            raise TransactionError(f"winner package requires {key}")
        if "\n" in package[key] or "\r" in package[key]:
            raise TransactionError(f"winner package {key} may not contain newlines")
    winner = package.get("winner")
    if not isinstance(winner, dict) or not HEX64.fullmatch(winner.get("candidate_sha256", "")):
        raise TransactionError("winner identity is incomplete")
    if not IDENT.fullmatch(winner.get("candidate_id", "")) or not IDENT.fullmatch(package["run_id"]):
        raise TransactionError("run and candidate identifiers must be safe tokens")
    if _REQUIRE_AUTO_SECURITY and winner.get("security_status") in HUMAN_SECURITY:
        return "REQUIRES_HUMAN_SECURITY_APPROVAL"
    if _REQUIRE_AUTO_SECURITY and winner.get("security_status") != AUTO_SECURITY:
        raise TransactionError("winner security status does not permit automatic apply")
    _validate_artifacts(package, run_dir)
    changes = winner.get("changes")
    if not isinstance(changes, list) or not 1 <= len(changes) <= 3:
        raise TransactionError("winner must change one to three exact paths")
    paths = []
    for change in changes:
        relative = _safe_relative(change.get("path"))
        path = relative.as_posix()
        if path in paths:
            raise TransactionError(f"duplicate winner path: {path}")
        if not HEX64.fullmatch(change.get("preimage_sha256", "")) and change.get("preimage_sha256") != MISSING:
            raise TransactionError(f"invalid preimage hash: {path}")
        if ("content_utf8" in change) == (change.get("delete") is True):
            raise TransactionError(f"change must contain exactly one operation: {path}")
        if "content_utf8" in change and not isinstance(change["content_utf8"], str):
            raise TransactionError(f"content_utf8 must be text: {path}")
        paths.append(path)
    guard = _manifest_guard(repo, manifest_path)
    if guard:
        protected_root = Path(host_root).resolve() if host_root else repo
        guard.assert_protected_paths_exist(protected_root)
        forbidden = [item["path"] for item in changes if guard.classify(item)["status"] == "DISQUALIFIED"]
        if forbidden:
            raise TransactionError(f"winner changes immutable manifest paths: {forbidden}")
        failed = guard.verify_snapshot(guard.snapshot(protected_root))
        if failed:
            raise TransactionError(f"immutable manifest hash mismatch: {failed}")
    if _git(repo, "rev-parse", "HEAD").stdout.strip() != package["repo_baseline_commit"]:
        raise TransactionError("HEAD changed since scoring")
    if _git(repo, "diff", "--cached", "--quiet", check=False).returncode:
        raise TransactionError("index contains pre-existing staged changes")
    for change in changes:
        if _hash_path(_repo_path(repo, change["path"])) != change["preimage_sha256"]:
            raise TransactionError(f"target preimage changed: {change['path']}")
    _verify_protected(package, protected_root if guard else repo)
    return "READY"


def _verify_protected(package, repo):
    protected = package.get("protected_assets")
    if not isinstance(protected, dict) or set(protected) != PROTECTED_CATEGORIES:
        raise TransactionError("all seven protected asset categories are required")
    failed = []
    seen = set()
    for category, assets in protected.items():
        if not isinstance(assets, dict) or not assets:
            raise TransactionError(f"protected category is empty: {category}")
        for value, expected in assets.items():
            if value in seen:
                raise TransactionError(f"protected path appears in multiple categories: {value}")
            seen.add(value)
            if not HEX64.fullmatch(expected) or _hash_path(_repo_path(repo, value)) != expected:
                failed.append(value)
    if failed:
        raise TransactionError(f"immutable hash mismatch: {failed}")


def _paths_match(repo, expected_hashes):
    try:
        return all(_hash_path(_repo_path(repo, path)) == expected
                   for path, expected in expected_hashes.items())
    except TransactionError:
        return False


def _apply_changes(repo, changes):
    originals = {}
    try:
        for change in changes:
            path = _repo_path(repo, change["path"])
            originals[change["path"]] = None if not path.exists() else (path.read_bytes(), path.stat().st_mode)
            if change.get("delete") is True:
                path.unlink()
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(change["content_utf8"].encode())
                    handle.flush()
                    os.fsync(handle.fileno())
                if originals[change["path"]] is not None:
                    os.chmod(temporary, originals[change["path"]][1])
                os.replace(temporary, path)
            except Exception:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
                raise
    except Exception:
        _restore_uncommitted(repo, originals)
        raise
    return originals


def _restore_uncommitted(repo, originals):
    for relative, original in originals.items():
        path = Path(repo) / relative
        if original is None:
            if path.exists():
                path.unlink()
            continue
        data, mode = original
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        os.chmod(path, mode)


def _canary_passed(result, package, winner_commit):
    required = (
        result.get("passed") is True,
        result.get("observed_commit") == winner_commit,
        result.get("settings_version_ok") is True,
        result.get("hook_fixture_passed") is True,
        result.get("regression_passed") is True,
        result.get("duplicate_run_enqueued") is False,
        result.get("child_processes_remaining") == 0,
    )
    session_id = result.get("session_id")
    fresh = isinstance(session_id, str) and bool(session_id) and session_id != package["source_session_id"]
    return all(required) and (fresh or not _REQUIRE_FRESH_SESSION)


def run_transaction(
    repo, package, run_dir, lock_root, qualification_runner, session_runner,
    immutable_manifest=None, host_root=None,
):
    """Execute the B8 transaction. Runners are trusted host adapters, never package commands."""
    repo = Path(repo).resolve()
    run_dir = Path(run_dir).resolve()
    state_path = run_dir / "transaction-state.json"
    ledger_path = run_dir / "evidence-ledger.jsonl"
    with _locks(lock_root, repo):
        protected_root = Path(host_root).resolve() if host_root else repo
        status = _validate_package(package, repo, run_dir, immutable_manifest, protected_root)
        if status == "REQUIRES_HUMAN_SECURITY_APPROVAL":
            result = {"run_id": package["run_id"], "state": status}
            _atomic_json(state_path, result)
            return result

        changes = package["winner"]["changes"]
        preimages = {item["path"]: item["preimage_sha256"] for item in changes}
        postimages = {item["path"]: (MISSING if item.get("delete") is True
                      else _digest(item["content_utf8"].encode())) for item in changes}
        originals = _apply_changes(repo, changes)
        try:
            qualification = qualification_runner(repo, package)
            if qualification.get("passed") is not True:
                raise TransactionError("post-apply qualification failed before commit")
            if not _paths_match(repo, postimages):
                raise TransactionError("qualification changed a winner target")
            _verify_protected(package, protected_root)
            paths = [change["path"] for change in changes]
            _git(repo, "add", "--", *paths)
            staged = [item for item in _git(repo, "diff", "--cached", "--name-only", "-z").stdout.split("\0") if item]
            if set(staged) != set(paths) or len(staged) != len(paths):
                raise TransactionError(f"staged paths differ from winner allowlist: {staged}")
            message = (
                f"[retro-2] apply {package['run_id']} {package['winner']['candidate_id']}\n\n"
                f"Incident-Bank-Version: {package['incident_bank_version']}\n"
                f"Scoring-Version: {package['scoring_version']}\n"
                f"Candidate-SHA256: {package['winner']['candidate_sha256']}"
            )
            _git(repo, "commit", "-m", message)
        except Exception:
            _git(repo, "reset", "--quiet", "--", *[item["path"] for item in changes], check=False)
            _restore_uncommitted(repo, originals)
            raise

        winner_commit = _git(repo, "rev-parse", "HEAD").stdout.strip()
        _append_ledger(ledger_path, {"event": "APPLIED", "commit": winner_commit,
                                    "paths": paths, "preimage_sha256": preimages,
                                    "postimage_sha256": postimages, "run_id": package["run_id"]})
        applied = {"run_id": package["run_id"], "state": "APPLIED", "winner_commit": winner_commit}
        _atomic_json(state_path, applied)
        try:
            canary = session_runner({"phase": "canary", "run_id": package["run_id"],
                                      "source_session_id": package["source_session_id"],
                                      "expected_commit": winner_commit})
        except Exception as error:
            canary = {"passed": False, "error": str(error)}
        post_apply_intact = (
            _git(repo, "rev-parse", "HEAD").stdout.strip() == winner_commit
            and _paths_match(repo, postimages)
        )
        try:
            _verify_protected(package, protected_root)
            protected_after_canary = True
        except TransactionError:
            protected_after_canary = False
        if _canary_passed(canary, package, winner_commit) and post_apply_intact and protected_after_canary:
            result = {**applied, "state": "CANARY_OK", "canary": canary}
            _atomic_json(state_path, result)
            return result
        if not _REVERT_ON_CANARY_FAILURE:
            result = {**applied, "state": "CANARY_FAILED", "canary": canary}
            _atomic_json(state_path, result)
            return result

        unsafe = (
            _git(repo, "rev-parse", "HEAD").stdout.strip() != winner_commit
            or _git(repo, "diff", "--cached", "--quiet", check=False).returncode != 0
            or not _paths_match(repo, postimages)
        )
        if unsafe:
            result = {**applied, "state": "MANUAL_RECOVERY_REQUIRED", "canary": canary}
            _atomic_json(state_path, result)
            return result
        _atomic_json(state_path, {**applied, "state": "REVERTING", "canary": canary})
        revert = _git(repo, "revert", "--no-edit", winner_commit, check=False)
        if revert.returncode:
            result = {**applied, "state": "MANUAL_RECOVERY_REQUIRED", "canary": canary,
                      "revert_error": revert.stderr.strip()}
            _atomic_json(state_path, result)
            return result
        revert_commit = _git(repo, "rev-parse", "HEAD").stdout.strip()
        restored_files = _paths_match(repo, preimages)
        _append_ledger(ledger_path, {"event": "REVERTED", "commit": revert_commit,
                                    "reverts": winner_commit, "restored_sha256": preimages,
                                    "run_id": package["run_id"]})
        try:
            _verify_protected(package, protected_root)
            protected_ok = True
        except TransactionError:
            protected_ok = False
        try:
            restore = session_runner({"phase": "restore", "run_id": package["run_id"],
                                      "expected_commit": revert_commit,
                                      "baseline_commit": package["repo_baseline_commit"]})
        except Exception as error:
            restore = {"passed": False, "error": str(error)}
        restore_ok = (
            restore.get("passed") is True
            and restore.get("baseline_smoke_passed") is True
            and restore.get("observed_commit") == revert_commit
            and restored_files and protected_ok
        )
        result = {**applied, "state": "RESTORE_VERIFIED" if restore_ok else "MANUAL_RECOVERY_REQUIRED",
                  "canary": canary, "revert_commit": revert_commit, "restore": restore}
        _atomic_json(state_path, result)
        return result
