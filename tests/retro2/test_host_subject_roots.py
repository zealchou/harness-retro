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
host_exits = importlib.import_module("host_exits")


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
expect_path = cwd / "scope-expect.json"
expect = json.loads('''__EXPECT__''')
if expect_path.is_file():
    expect.update(json.loads(expect_path.read_text(encoding="utf-8")))
hidden = [Path(item) for item in expect.get("hidden", [])]
statuses.append("HIDDEN_PRESENT" if any(path.exists() for path in hidden) else "HIDDEN_ABSENT")
run_file = Path(expect["run_file"]) if expect.get("run_file") else None
statuses.append("RUN_PRESENT" if run_file is not None and run_file.exists() else "RUN_ABSENT")
diagnostics = Path(expect["diagnostics"]) if expect.get("diagnostics") else None
statuses.append(
    "DIAGNOSTICS_VISIBLE" if diagnostics is not None and (diagnostics / "visible.txt").is_file() else "DIAGNOSTICS_MISSING"
)
statuses.append("REPO_READABLE" if (cwd / "repo-visible.txt").is_file() else "REPO_MISSING")
canary = Path(expect["canary"]) if expect.get("canary") else None
statuses.append("CANARY_ABSENT" if canary is None or not canary.exists() else "CANARY_PRESENT")
claude_cred = Path(expect["claude_cred"]) if expect.get("claude_cred") else None
statuses.append("CLAUDE_CRED_ABSENT" if claude_cred is None or not claude_cred.exists() else "CLAUDE_CRED_PRESENT")
codex_auth = Path(expect["codex_auth"]) if expect.get("codex_auth") else None
statuses.append("CODEX_AUTH_ABSENT" if codex_auth is None or not codex_auth.exists() else "CODEX_AUTH_PRESENT")


def _denied(path):
    try:
        Path(path).write_text("nope", encoding="utf-8")
    except OSError:
        return True
    return False


write_target = expect.get("write_target")
statuses.append("WRITE_DENY" if write_target and _denied(write_target) else "WRITE_ALLOWED")
scratch = os.environ.get("RETRO2_DIAGNOSIS_SCRATCH")
if scratch:
    try:
        Path(scratch, "probe").write_text("ok", encoding="utf-8")
        statuses.append("WRITE_SCRATCH_OK")
    except OSError:
        statuses.append("WRITE_SCRATCH_FAIL")
else:
    statuses.append("WRITE_SCRATCH_FAIL")
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
            with mock.patch.object(station_host, "run_allowlisted", return_value=completed) as restricted:
                runner._structured("diagnosis", {}, read_only_repo=True)
            roots = restricted.call_args.kwargs["harness_reads"]
            self.assertIn(station_host.HOST_HARNESS / "hooks", roots)
            self.assertIn(station_host.HOST_HARNESS / "scripts/logs", roots)
            self.assertEqual(Path("/tmp/retro2-diagnostics"), Path(restricted.call_args.kwargs["diagnostics"]))
            self.assertFalse(any(".credentials" in str(path) or "/projects" in str(path) for path in roots))
            creds = restricted.call_args.kwargs["credential_paths"]
            self.assertEqual(1, len(creds)); self.assertTrue(str(creds[0]).endswith(".credentials.json"))
            argv = restricted.call_args.args[0]
            added = [argv[index + 1] for index, item in enumerate(argv) if item == "--add-dir"]
            self.assertIn(str(station_host.HOST_HARNESS), added)
            self.assertFalse(any(path.endswith(".credentials.json") for path in added))

    def _outside_tmp(self, prefix):
        parent = Path("/var/tmp")
        if not parent.is_dir() or not os.access(parent, os.W_OK):
            parent = Path.home()
        return tempfile.TemporaryDirectory(prefix=prefix, dir=parent)

    def _fill_private_tree(self, harness, marker):
        for relative in (
            "projects/hidden.txt",
            "history/note.txt",
            "todos/note.txt",
            "memory/note.txt",
            "retro-2/queue/note.txt",
        ):
            path = harness / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(marker, encoding="utf-8")
        (harness / "hooks").mkdir(parents=True, exist_ok=True)
        (harness / "scripts" / "logs").mkdir(parents=True, exist_ok=True)
        (harness / "CLAUDE.md").write_text("rules\n", encoding="utf-8")
        (harness / "settings.json").write_text("{}\n", encoding="utf-8")
        (harness / "hooks" / "sample.sh").write_text("echo ok\n", encoding="utf-8")
        (harness / "scripts" / "logs" / "note.log").write_text("log\n", encoding="utf-8")
        credential = harness / ".credentials.json"
        credential.write_text(marker, encoding="utf-8")
        return credential

    def _git_repo(self, repo):
        repo.mkdir(parents=True, exist_ok=True)
        (repo / "docs").mkdir(exist_ok=True)
        (repo / "docs/guide.md").write_text("before\n", encoding="utf-8")
        (repo / "repo-visible.txt").write_text("project\n", encoding="utf-8")
        if not (repo / ".git").is_dir():
            git(repo, "init", "-q")
            git(repo, "config", "user.email", "test@example.invalid")
            git(repo, "config", "user.name", "test")
        git(repo, "add", "docs", "repo-visible.txt")
        git(repo, "commit", "-qm", "baseline")
        return repo

    def _assert_read_scope(self, *, home, harness, credential, marker, repo, expect_repo=True):
        """Run diagnosis with the harness outside /tmp and the repo at `repo`."""
        self._git_repo(repo)
        diagnostics = repo.parent / ("diagnostics-" + repo.name)
        if diagnostics == harness or diagnostics.is_relative_to(harness):
            diagnostics = repo.parent.parent / ("diagnostics-" + repo.name)
        diagnostics.mkdir()
        (diagnostics / "visible.txt").write_text("diag\n", encoding="utf-8")
        runs = harness / "retro-2" / "runs"
        canary = home / ("canary-" + secrets.token_hex(8) + ".txt")
        canary.write_text(marker, encoding="utf-8")
        codex_dir = home / ".codex"
        codex_dir.mkdir(parents=True, exist_ok=True)
        codex_auth = codex_dir / "auth.json"
        codex_auth.write_text(marker, encoding="utf-8")
        checker = repo / "check_read_scope.py"
        hidden = [
            harness / "projects" / "hidden.txt",
            harness / "history" / "note.txt",
            harness / "todos" / "note.txt",
            harness / "memory" / "note.txt",
            harness / "retro-2" / "queue" / "note.txt",
            home / ".claude" / ".credentials.json",
        ]
        ticket = {
            "schema_version": 1, "idempotency_key": "read-scope-" + harness.name,
            "controller_entry": "retro2/controller/run.py", "repo_root": str(repo),
        }
        runner = station_host.PythonStationRunner(ticket, runs, claude=str(checker))
        runner.run_dir.mkdir(parents=True)
        (runner.run_dir / "leak.txt").write_text(marker, encoding="utf-8")
        hidden.append(runner.run_dir / "leak.txt")
        expect = {
            "hidden": [str(path) for path in hidden],
            "run_file": str(runner.run_dir / "leak.txt"),
            "diagnostics": str(diagnostics),
            "canary": str(canary),
            "claude_cred": str(home / ".claude" / ".credentials.json"),
            "codex_auth": str(codex_auth),
            "write_target": str(harness / "CLAUDE.md"),
        }
        checker.write_text(READ_SCOPE_CHECKER.replace("__EXPECT__", json.dumps(expect)), encoding="utf-8")
        checker.chmod(0o755)
        (repo / "scope-expect.json").write_text(json.dumps(expect), encoding="utf-8")
        reads = (
            harness / "CLAUDE.md", harness / "settings.json", harness / "hooks",
            harness / "scripts" / "logs", diagnostics,
        )
        with mock.patch.dict(os.environ, {"HOME": str(home)}), \
                mock.patch.object(station_host, "HOST_HARNESS", harness), \
                mock.patch.object(station_host, "HOST_HARNESS_READS", reads), \
                mock.patch.object(station_host.config, "RETRO2_HOME", harness), \
                mock.patch.object(station_host.config, "CLAUDE_CREDENTIAL_FILE", credential), \
                mock.patch.object(station_host.config, "DIAGNOSTICS_DIR", diagnostics):
            result = runner._structured("diagnosis", {}, read_only_repo=True)
        self.assertEqual([
            "AUTH_READABLE", "AUTH_READONLY", "SCOPE_DENIES_AUTH", "HARNESS_IS_READ_ROOT",
            "RULES_READABLE", "SETTINGS_READABLE", "HOOKS_DIR", "LOGS_DIR",
            "HARNESS_CRED_ABSENT", "HIDDEN_ABSENT", "RUN_ABSENT", "DIAGNOSTICS_VISIBLE",
            "REPO_READABLE" if expect_repo else "REPO_MISSING",
            "CANARY_ABSENT", "CLAUDE_CRED_ABSENT", "CODEX_AUTH_ABSENT",
            "WRITE_DENY", "WRITE_SCRATCH_OK", "MARKER_CONTAINED",
        ], result["statuses"])
        self.assertFalse(str(runner.run_dir).startswith("/tmp/"))
        self.assertFalse(str(harness).startswith("/tmp/"))

    @unittest.skipUnless(supports_bwrap_namespace(), "requires a usable bwrap network namespace")
    def test_diagnosis_read_scope_masks_home_claude_outside_tmp(self):
        with self._outside_tmp("retro2-home-claude-") as directory:
            root = Path(directory)
            home = root / "home"
            harness = home / ".claude"
            marker = "SYNTHETIC-MARKER-" + secrets.token_hex(16)
            credential = self._fill_private_tree(harness, marker)
            # Repo nested inside $HOME, beside ~/.claude. The default runs directory
            # under the harness is in the hidden set.
            self._assert_read_scope(
                home=home, harness=harness, credential=credential, marker=marker,
                repo=home / "project",
            )

    @unittest.skipUnless(supports_bwrap_namespace(), "requires a usable bwrap network namespace")
    def test_diagnosis_read_scope_when_repo_is_home(self):
        with self._outside_tmp("retro2-repo-is-home-") as directory:
            root = Path(directory)
            home = root / "home"
            harness = home / ".claude"
            marker = "SYNTHETIC-MARKER-" + secrets.token_hex(16)
            credential = self._fill_private_tree(harness, marker)
            self._assert_read_scope(
                home=home, harness=harness, credential=credential, marker=marker,
                repo=home, expect_repo=False,
            )

    @unittest.skipUnless(supports_bwrap_namespace(), "requires a usable bwrap network namespace")
    def test_diagnosis_read_scope_when_repo_contains_home_claude(self):
        with self._outside_tmp("retro2-repo-contains-claude-") as directory:
            root = Path(directory)
            home = root / "nest" / "home"
            harness = home / ".claude"
            marker = "SYNTHETIC-MARKER-" + secrets.token_hex(16)
            credential = self._fill_private_tree(harness, marker)
            repo = root / "nest"
            self.assertNotEqual(repo.resolve(), home.resolve())
            self.assertTrue(harness.resolve().is_relative_to(repo.resolve()))
            self.assertFalse(repo.resolve().is_relative_to(home.resolve()))
            self._assert_read_scope(
                home=home, harness=harness, credential=credential, marker=marker, repo=repo,
            )

    @unittest.skipUnless(supports_bwrap_namespace(), "requires a usable bwrap network namespace")
    def test_diagnosis_read_scope_when_repo_is_outside_home(self):
        with self._outside_tmp("retro2-repo-outside-home-") as directory:
            root = Path(directory)
            home = root / "home"
            harness = home / ".claude"
            repo = root / "elsewhere"
            marker = "SYNTHETIC-MARKER-" + secrets.token_hex(16)
            credential = self._fill_private_tree(harness, marker)
            self.assertFalse(repo.resolve().is_relative_to(home.resolve()))
            self.assertFalse(home.resolve().is_relative_to(repo.resolve()))
            self._assert_read_scope(
                home=home, harness=harness, credential=credential, marker=marker, repo=repo,
            )

    @unittest.skipUnless(supports_bwrap_namespace(), "requires a usable bwrap network namespace")
    def test_diagnosis_read_scope_masks_relocated_retro2_home(self):
        with self._outside_tmp("retro2-moved-home-") as directory:
            root = Path(directory)
            home = root / "home"
            original = home / ".claude"
            original.mkdir(parents=True)
            marker = "SYNTHETIC-MARKER-" + secrets.token_hex(16)
            (original / ".credentials.json").write_text(marker, encoding="utf-8")
            repo = root / "project"
            harness = repo / "harness"
            credential = self._fill_private_tree(harness, marker)
            self._assert_read_scope(home=home, harness=harness, credential=credential, marker=marker, repo=repo)

    def test_masks_follow_a_parent_repo_bind(self):
        with self._outside_tmp("retro2-mask-order-") as directory:
            root = Path(directory)
            home = root / "home"
            harness = home / ".claude"
            harness.mkdir(parents=True)
            credential = harness / ".credentials.json"
            credential.write_text("SYNTHETIC-MARKER\n", encoding="utf-8")
            writable = root / "work"
            writable.mkdir()
            completed = subprocess.CompletedProcess(["bwrap"], 0, "R2_CHILD_STARTED\nok", "")
            with mock.patch.object(host_exits.config, "RETRO2_HOME", harness), \
                    mock.patch.object(host_exits.shutil, "which", return_value="/usr/bin/bwrap"), \
                    mock.patch.object(host_exits.subprocess, "run", return_value=completed) as run:
                host_exits.run_allowlisted(
                    ["true"], writable, cwd=writable, profile="diagnosis",
                    project_root=home, harness_root=harness,
                    harness_reads=(harness / "CLAUDE.md",),
                    credential_paths=(credential,),
                    env={"HOME": str(home), "PATH": "/bin", "LANG": "C"},
                )
            argv = run.call_args.args[0]
            self.assertIn("--tmpfs", argv)
            self.assertNotIn(["--ro-bind", "/", "/"], [argv[index:index + 3] for index in range(len(argv) - 2)])
            self.assertNotIn(["--ro-bind", str(home), str(home)], [argv[index:index + 3] for index in range(len(argv) - 2)])
            self.assertFalse(any(
                argv[index] == "--ro-bind" and argv[index + 2] == str(credential)
                for index in range(len(argv) - 2)
            ))

    def test_codex_auth_is_not_read_or_mounted(self):
        with self._outside_tmp("retro2-codex-auth-") as directory:
            root = Path(directory)
            home = root / "home"
            codex_dir = home / ".codex"
            codex_dir.mkdir(parents=True)
            auth = codex_dir / "auth.json"
            auth.write_text("SYNTHETIC-CODEX-AUTH\n", encoding="utf-8")
            auth.chmod(0)
            project = root / "project"
            project.mkdir()
            writable = root / "work"
            writable.mkdir()
            with mock.patch.object(host_exits.subprocess, "run") as spawned:
                with self.assertRaisesRegex(host_exits.HostExitError, "Codex backend is disabled"):
                    host_exits.run_allowlisted(
                        ["true"], writable, cwd=project, profile="candidate",
                        project_root=project, harness_root=home / ".claude",
                        credential_paths=(auth,),
                        env={"HOME": str(home), "PATH": "/bin", "LANG": "C"},
                    )
            spawned.assert_not_called()
            self.assertEqual([], list(Path("/var/tmp").glob("retro2-codex-min-*")))

    @unittest.skipUnless(supports_bwrap_namespace(), "requires a usable bwrap network namespace")
    def test_allowlisted_symlink_to_home_is_refused(self):
        with self._outside_tmp("retro2-symlink-escape-") as directory:
            root = Path(directory)
            home = root / "home"
            harness = home / ".claude"
            (harness / "scripts" / "logs").mkdir(parents=True)
            (harness / "CLAUDE.md").write_text("rules\n", encoding="utf-8")
            (harness / "settings.json").write_text("{}\n", encoding="utf-8")
            (harness / "hooks").symlink_to(home, target_is_directory=True)
            (home / ".ssh").mkdir()
            (home / ".ssh" / "id").write_text("SYNTHETIC-SSH\n", encoding="utf-8")
            (home / "canary.txt").write_text("CANARY\n", encoding="utf-8")
            credential = harness / ".credentials.json"
            credential.write_text("SYNTHETIC-MARKER\n", encoding="utf-8")
            writable = root / "work"
            writable.mkdir()
            with mock.patch.object(host_exits.config, "RETRO2_HOME", harness):
                with self.assertRaisesRegex(host_exits.HostExitError, "symlink"):
                    host_exits.run_allowlisted(
                        ["true"], writable, cwd=writable, project_root=writable, harness_root=harness,
                        harness_reads=(
                            harness / "CLAUDE.md", harness / "settings.json",
                            harness / "hooks", harness / "scripts" / "logs",
                        ),
                        credential_paths=(credential,),
                        env={"HOME": str(home), "PATH": "/bin", "LANG": "C"},
                    )

    @unittest.skipUnless(supports_bwrap_namespace(), "requires a usable bwrap network namespace")
    def test_allowlisted_diagnostics_symlink_to_home_is_refused(self):
        with self._outside_tmp("retro2-diag-symlink-") as directory:
            root = Path(directory)
            home = root / "home"
            harness = home / ".claude"
            (harness / "scripts" / "logs").mkdir(parents=True)
            (harness / "CLAUDE.md").write_text("rules\n", encoding="utf-8")
            outside = root / "outside"
            outside.mkdir()
            diagnostics = outside / "diagnostics"
            diagnostics.symlink_to(home, target_is_directory=True)
            planted = home / "diag"
            planted.symlink_to(home, target_is_directory=True)
            writable = root / "work"
            writable.mkdir()
            with mock.patch.object(host_exits.config, "RETRO2_HOME", harness):
                for entry in (diagnostics, planted):
                    with self.assertRaisesRegex(host_exits.HostExitError, "symlink"):
                        host_exits.run_allowlisted(
                            ["true"], writable, cwd=writable, project_root=writable, harness_root=harness,
                            harness_reads=(harness / "CLAUDE.md",),
                            diagnostics=entry,
                            env={"HOME": str(home), "PATH": "/bin", "LANG": "C"},
                        )

    @unittest.skipUnless(supports_bwrap_namespace(), "requires a usable bwrap network namespace")
    def test_candidate_allowlist_hides_home_canaries_and_repo_symlink(self):
        with self._outside_tmp("retro2-candidate-hide-") as directory:
            root = Path(directory)
            home = root / "home"
            home.mkdir()
            (home / "canary.txt").write_text("CANARY-HOME\n", encoding="utf-8")
            (home / ".bash_history").write_text("history\n", encoding="utf-8")
            (home / ".ssh").mkdir()
            (home / ".ssh" / "id_ed25519").write_text("SYNTHETIC-SSH\n", encoding="utf-8")
            (home / ".codex").mkdir()
            (home / ".codex" / "auth.json").write_text("SYNTHETIC-CODEX-AUTH\n", encoding="utf-8")
            host_tmp = Path("/tmp") / ("retro2-fake-auth-" + secrets.token_hex(6))
            host_tmp.write_text("SYNTHETIC-TMP-AUTH\n", encoding="utf-8")
            self.addCleanup(host_tmp.unlink, missing_ok=True)
            harness = home / ".claude"
            harness.mkdir()
            credential = harness / ".credentials.json"
            credential.write_text("SYNTHETIC-CLAUDE-CRED\n", encoding="utf-8")
            work = root / "worktree"
            work.mkdir()
            (work / "target.py").write_text("x = 1\n", encoding="utf-8")
            (work / "escape").symlink_to(home / "canary.txt")
            probe = work / "probe.py"
            probe.write_text(
                "import json, os\nfrom pathlib import Path\n"
                "home = Path(os.environ['HOME'])\n"
                "link = Path('escape')\n"
                "try:\n"
                "    leaked = link.read_text(encoding='utf-8')\n"
                "except OSError:\n"
                "    leaked = ''\n"
                "rows = {\n"
                " 'home_canary': (home / 'canary.txt').is_file(),\n"
                " 'bash_history': (home / '.bash_history').is_file(),\n"
                " 'ssh': (home / '.ssh' / 'id_ed25519').is_file(),\n"
                " 'codex_auth': (home / '.codex' / 'auth.json').is_file(),\n"
                " 'tmp_auth': Path(%r).is_file(),\n"
                " 'repo_symlink': leaked.startswith('CANARY'),\n"
                " 'codex_home': 'CODEX_HOME' in os.environ,\n"
                "}\n"
                "print('PROBE ' + json.dumps(rows))\n" % str(host_tmp),
                encoding="utf-8",
            )
            with mock.patch.object(host_exits.config, "RETRO2_HOME", harness):
                result = host_exits.run_allowlisted(
                    [sys.executable, str(probe)], work, cwd=work, allow_network=True,
                    profile="candidate", project_root=work, harness_root=harness,
                    credential_paths=(credential,),
                    env={"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin"), "LANG": "C"},
                )
            self.assertTrue(result.child_started, result.stderr)
            self.assertEqual(0, result.returncode, result.stderr)
            rows = json.loads(result.stdout.removeprefix("PROBE ").strip())
            self.assertEqual({
                "home_canary": False, "bash_history": False, "ssh": False,
                "codex_auth": False, "tmp_auth": False, "repo_symlink": False,
                "codex_home": False,
            }, rows)

    def test_credential_namespace_must_be_16_lowercase_hex(self):
        with self.assertRaisesRegex(host_exits.HostExitError, "16 lowercase hex"):
            host_exits._private_auth_dir("retro2-claude-auth-", [], namespace="../tmp")
        with self.assertRaisesRegex(host_exits.HostExitError, "16 lowercase hex"):
            host_exits._private_auth_dir("retro2-claude-auth-", [], namespace="0123456789ABCDEF")
        with self.assertRaisesRegex(host_exits.HostExitError, "16 lowercase hex"):
            host_exits._private_auth_dir("retro2-claude-auth-", [], namespace="abc")
        dest = host_exits._private_auth_dir("retro2-claude-auth-", [], namespace="0123456789abcdef")
        self.assertEqual(dest, Path("/tmp/retro2-claude-auth-0123456789abcdef"))
        with self._outside_tmp("retro2-namespace-") as directory:
            root = Path(directory)
            home = root / "home"
            home.mkdir()
            harness = home / ".claude"
            harness.mkdir()
            credential = harness / ".credentials.json"
            credential.write_text("SYNTHETIC-MARKER\n", encoding="utf-8")
            writable = root / "work"
            writable.mkdir()
            with self.assertRaisesRegex(host_exits.HostExitError, "16 lowercase hex"):
                host_exits.build_allowlisted_argv(
                    ["true"], writable, cwd=writable, profile="diagnosis",
                    project_root=writable, harness_root=harness,
                    credential_paths=(credential,), credential_namespace="not/hex",
                    env={"HOME": str(home), "PATH": "/bin", "LANG": "C"},
                )

    def test_credential_mount_uses_add_dir_for_overlap(self):
        with self._outside_tmp("retro2-overlap-") as directory:
            root = Path(directory)
            harness = root / "harness"
            harness.mkdir()
            credential = harness / ".credentials.json"
            credential.write_text("SYNTHETIC-MARKER\n", encoding="utf-8")
            writable = root / "work"
            writable.mkdir()
            with mock.patch.object(host_exits.config, "RETRO2_HOME", harness):
                with self.assertRaisesRegex(host_exits.HostExitError, "overlaps"):
                    host_exits.run_allowlisted(
                        ["true", "--add-dir", "/tmp"], writable, cwd=writable, profile="diagnosis",
                        project_root=writable, harness_root=harness, credential_paths=(credential,),
                        env={"HOME": str(root / "home"), "PATH": "/bin", "LANG": "C"},
                    )


if __name__ == "__main__":
    unittest.main()
