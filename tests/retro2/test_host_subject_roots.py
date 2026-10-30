import hashlib
import importlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "retro2"))
station_host = importlib.import_module("station_host")


def supports_bwrap_namespace():
    if not shutil.which("bwrap") or not os.access(Path.home(), os.W_OK):
        return False
    return subprocess.run(
        ["bwrap", "--ro-bind", "/", "/", "--unshare-net", "--", "true"],
        capture_output=True,
    ).returncode == 0


READ_SCOPE_CHECKER = """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path


def add_dirs():
    found, args, index = [], sys.argv[1:], 0
    while index < len(args):
        if args[index] == "--add-dir" and index + 1 < len(args):
            found.append(Path(args[index + 1]))
            index += 2
            continue
        index += 1
    return found


def covers(root, path):
    root, path = root.resolve(), path.resolve()
    return path == root or path.is_relative_to(root)


statuses = []
dirs = add_dirs()
cwd = Path.cwd()
auth_root = os.environ.get("CLAUDE_CONFIG_DIR")
auth = Path(auth_root) / ".credentials.json" if auth_root else None
marker = b""
if auth is None or not auth.is_file():
    statuses.append("AUTH_UNREADABLE")
else:
    marker = auth.read_bytes()
    statuses.append("AUTH_READABLE" if marker else "AUTH_EMPTY")
    try:
        auth.write_bytes(marker)
        statuses.append("AUTH_WRITABLE")
    except OSError:
        statuses.append("AUTH_READONLY")
    if any(covers(root, auth) for root in (cwd, *dirs)):
        statuses.append("SCOPE_ALLOWS_AUTH")
    else:
        statuses.append("SCOPE_DENIES_AUTH")
harness = next((path for path in dirs if (path / "CLAUDE.md").is_file()), None)
if harness is None:
    statuses.append("HARNESS_NOT_A_READ_ROOT")
else:
    statuses.append("HARNESS_IS_READ_ROOT")
    statuses.append("RULES_READABLE" if (harness / "CLAUDE.md").is_file() else "RULES_MISSING")
    statuses.append("SETTINGS_READABLE" if (harness / "settings.json").is_file() else "SETTINGS_MISSING")
    statuses.append("HOOKS_DIR" if (harness / "hooks").is_dir() else "HOOKS_MISSING")
    statuses.append("LOGS_DIR" if (harness / "scripts" / "logs").is_dir() else "LOGS_MISSING")
    statuses.append("HARNESS_CRED_PRESENT" if (harness / ".credentials.json").exists() else "HARNESS_CRED_ABSENT")
    statuses.append("HIDDEN_PRESENT" if (harness / "projects" / "hidden.txt").exists() else "HIDDEN_ABSENT")
leaked = False
if marker:
    for root in (cwd, *dirs):
        if not root.exists():
            continue
        files = [root] if root.is_file() else [path for path in root.rglob("*") if path.is_file()]
        for path in files:
            if auth is not None and path.resolve() == auth.resolve():
                continue
            try:
                data = path.read_bytes()
            except OSError:
                continue
            if marker in data:
                leaked = True
statuses.append("MARKER_LEAKED" if leaked else "MARKER_CONTAINED")
print(json.dumps({"structured_output": {"statuses": statuses}, "usage": {}}))
"""


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, text=True, capture_output=True).stdout.strip()


class HostSubjectRootTests(unittest.TestCase):
    def _subject(self, root):
        repo = Path(root) / "subject"
        repo.mkdir()
        git(repo, "init", "-q")
        git(repo, "config", "user.email", "test@example.invalid")
        git(repo, "config", "user.name", "test")
        (repo / "docs").mkdir()
        (repo / "docs/guide.md").write_text("before\n", encoding="utf-8")
        (repo / "tests").mkdir()
        (repo / "tests/__init__.py").write_text("", encoding="utf-8")
        (repo / "tests/test_guide.py").write_text("import unittest\nclass T(unittest.TestCase):\n def test_ok(self): self.assertTrue(True)\n", encoding="utf-8")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "baseline")
        return repo

    def test_host_immutable_is_used_when_subject_has_no_retro2_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            subject = self._subject(directory)
            transcript = Path(directory) / "session.jsonl"
            transcript.write_text(json.dumps({"type": "user", "uuid": "maintainer-1", "origin": {"kind": "human"}, "message": {"content": "修正"}}) + "\n")
            ticket = {"schema_version": 1, "idempotency_key": "host-subject", "controller_entry": "retro2/controller/run.py", "repo_root": str(subject), "transcript_path": str(transcript)}
            runner = station_host.PythonStationRunner(ticket, Path(directory) / "runs", command=lambda *_a, **_k: (0, "", ""), claude="claude")
            runner.allowed_paths = ["docs/guide.md"]
            requirement = {"requirements": [{"incident_id": "i", "diagnosis_id": "d", "lite": True, "trusted_decision_refs": ["maintainer-1"], "root_cause": {"evidence": ["docs/guide.md:1"]}, "minimal_fix": {}}]}
            runner.run_dir.mkdir(parents=True)
            (runner.run_dir / "candidate-requirements.json").write_text(json.dumps(requirement), encoding="utf-8")

            def candidate(request):
                worktree = Path(request["worktree"])
                (worktree / "docs/guide.md").write_text("after\n", encoding="utf-8")
                return {"diagnosis_ids": ["d"], "trusted_decision_refs": ["maintainer-1"], "changes": []}

            with mock.patch.object(runner, "_candidate", side_effect=candidate):
                result = runner.candidates({})
            self.assertNotEqual("candidates_failed", result.get("reason"))
            self.assertTrue((runner.run_dir / "evaluator-snapshot/immutable-manifest.json").is_file())
            self.assertFalse((subject / "retro-2").exists())

    def test_missing_host_control_resources_stop_before_any_model_call(self):
        with tempfile.TemporaryDirectory() as directory:
            subject = self._subject(directory)
            transcript = Path(directory) / "session.jsonl"
            transcript.write_text("", encoding="utf-8")
            ticket = {"schema_version": 1, "idempotency_key": "missing-host", "controller_entry": "retro2/controller/run.py", "repo_root": str(subject), "transcript_path": str(transcript)}
            runner = station_host.PythonStationRunner(ticket, Path(directory) / "runs", command=lambda *_a, **_k: (0, "", ""), claude="claude")
            runner.host_root = Path(directory) / "missing-host"
            runner.allowed_paths = ["docs/guide.md"]
            with mock.patch.object(runner, "_candidate") as model:
                result = runner.health({})
            self.assertEqual(("NO_CHANGE", "host_control_resources_missing"), (result["status"], result["reason"]))
            model.assert_not_called()

    def test_diagnosis_mounts_only_the_host_harness_allowlist(self):
        with tempfile.TemporaryDirectory() as directory:
            subject = self._subject(directory)
            ticket = {"schema_version": 1, "idempotency_key": "diagnosis-roots", "controller_entry": "retro2/controller/run.py", "repo_root": str(subject)}
            runner = station_host.PythonStationRunner(ticket, Path(directory) / "runs", claude="claude")
            completed = subprocess.CompletedProcess(["bwrap"], 0, json.dumps({"structured_output": {"status": "UNDIAGNOSED"}, "usage": {}}), "")
            with mock.patch.object(station_host, "run_restricted", return_value=completed) as restricted:
                runner._structured("diagnosis", {}, read_only_repo=True)
            roots = restricted.call_args.kwargs["readonly_roots"]
            self.assertIn(station_host.HOST_HARNESS / "hooks", roots)
            self.assertIn(station_host.HOST_HARNESS / "scripts/logs", roots)
            self.assertIn(Path("/tmp/retro2-diagnostics"), roots)
            self.assertFalse(any(".credentials" in str(path) or "/projects" in str(path) for path in roots))
            creds = restricted.call_args.kwargs["credential_paths"]
            self.assertEqual(1, len(creds)); self.assertTrue(str(creds[0]).endswith(".credentials.json"))
            argv = restricted.call_args.args[0]
            added = [argv[index + 1] for index, item in enumerate(argv) if item == "--add-dir"]
            self.assertIn(str(station_host.HOST_HARNESS), added)
            self.assertFalse(any(path.endswith(".credentials.json") for path in added))

    @unittest.skipUnless(supports_bwrap_namespace(), "requires a usable bwrap network namespace")
    def test_diagnosis_read_scope_cannot_reach_claude_credential(self):
        """The model Read roots include the harness files and exclude the credential.

        --add-dir accepts directories only, so CLAUDE.md and settings.json stay
        readable by adding the harness root. The credential file is mounted for
        the CLI outside that root. This uses a synthetic marker, never a real login.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subject = self._subject(directory)
            harness = root / "home" / ".claude"
            (harness / "hooks").mkdir(parents=True)
            (harness / "scripts" / "logs").mkdir(parents=True)
            (harness / "projects").mkdir()
            (harness / "CLAUDE.md").write_text("rules\n", encoding="utf-8")
            (harness / "settings.json").write_text("{}\n", encoding="utf-8")
            (harness / "hooks" / "sample.sh").write_text("echo ok\n", encoding="utf-8")
            (harness / "scripts" / "logs" / "note.log").write_text("log\n", encoding="utf-8")
            (harness / "projects" / "hidden.txt").write_text("not-allowlisted\n", encoding="utf-8")
            credential = harness / ".credentials.json"
            credential.write_text("SYNTHETIC-MARKER-" + secrets.token_hex(16) + "\n", encoding="utf-8")
            diagnostics = root / "diagnostics"
            diagnostics.mkdir()
            checker = subject / "check_read_scope.py"
            checker.write_text(READ_SCOPE_CHECKER, encoding="utf-8")
            checker.chmod(0o755)
            ticket = {"schema_version": 1, "idempotency_key": "read-scope", "controller_entry": "retro2/controller/run.py", "repo_root": str(subject)}
            runner = station_host.PythonStationRunner(ticket, root / "runs", claude=str(checker))
            runner.run_dir.mkdir(parents=True)
            reads = (harness / "CLAUDE.md", harness / "settings.json", harness / "hooks", harness / "scripts" / "logs", diagnostics)
            with mock.patch.object(station_host, "HOST_HARNESS", harness), \
                    mock.patch.object(station_host, "HOST_HARNESS_READS", reads), \
                    mock.patch.object(station_host.config, "RETRO2_HOME", harness), \
                    mock.patch.object(station_host.config, "CLAUDE_CREDENTIAL_FILE", credential), \
                    mock.patch.object(station_host.config, "DIAGNOSTICS_DIR", diagnostics):
                result = runner._structured("diagnosis", {}, read_only_repo=True)
            self.assertEqual([
                "AUTH_READABLE", "AUTH_READONLY", "SCOPE_DENIES_AUTH", "HARNESS_IS_READ_ROOT",
                "RULES_READABLE", "SETTINGS_READABLE", "HOOKS_DIR", "LOGS_DIR",
                "HARNESS_CRED_ABSENT", "HIDDEN_ABSENT", "MARKER_CONTAINED",
            ], result["statuses"])


if __name__ == "__main__":
    unittest.main()
