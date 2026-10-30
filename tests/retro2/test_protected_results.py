import importlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "retro2"))
host_exits = importlib.import_module("host_exits")
station_host = importlib.import_module("station_host")
from tests.retro2.test_r2lite import LiteFixture


def supports_bwrap_namespace():
    if not shutil.which("bwrap") or not os.access(Path.home(), os.W_OK):
        return False
    return subprocess.run(["bwrap", "--ro-bind", "/", "/", "--unshare-net", "--", "true"], capture_output=True).returncode == 0


class ProtectedResultsTests(unittest.TestCase):
    def _linked_host_worktree(self):
        directory = tempfile.TemporaryDirectory()
        home = Path(directory.name) / "home"
        repository = home / ".claude"
        repository.mkdir(parents=True)
        for argv in (
            ["git", "-C", str(repository), "init"],
            ["git", "-C", str(repository), "config", "user.email", "retro2@example.test"],
            ["git", "-C", str(repository), "config", "user.name", "Retro 2 Test"],
        ):
            subprocess.run(argv, check=True, capture_output=True)
        (repository / "fixture.txt").write_text("fixture\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repository), "add", "fixture.txt"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repository), "commit", "-m", "fixture"], check=True, capture_output=True)
        worktree = Path(directory.name) / "candidate"
        host = host_exits.GitWorktreeHost()
        host.prepare(repository, worktree, host.baseline(repository))
        self.addCleanup(directory.cleanup)
        self.addCleanup(host.remove, repository, worktree)
        return worktree, home

    def test_host_worktree_git_metadata_is_scoped_to_worktree_and_common_read_paths(self):
        worktree, home = self._linked_host_worktree()
        expected = (home / ".claude/.git").resolve()
        with mock.patch.object(station_host.config, "RETRO2_HOME", home / ".claude"):
            roots = station_host.PythonStationRunner._git_metadata_roots(worktree)
        self.assertTrue(roots)
        self.assertTrue(all(path.is_relative_to(expected) for path in roots))
        self.assertTrue(any(path.parent == expected / "worktrees" for path in roots))
        self.assertIn(expected / "objects", roots)
        self.assertIn(expected / "refs", roots)

    def test_host_main_checkout_git_metadata_is_recognized_when_dotgit_is_a_directory(self):
        expected = (Path.home() / ".claude/.git").resolve()
        roots = station_host.PythonStationRunner._git_metadata_roots(Path.home() / ".claude")
        self.assertTrue(roots)
        self.assertTrue(all(path.is_relative_to(expected) for path in roots))
        self.assertIn(expected / "HEAD", roots)
        self.assertIn(expected / "index", roots)
        self.assertIn(expected / "objects", roots)
        self.assertIn(expected / "refs", roots)

    def test_apply_allowlist_does_not_mount_home_or_host_root(self):
        observed = {}
        completed = subprocess.CompletedProcess(["bwrap"], 0, "R2_CHILD_STARTED\nok", "")
        with tempfile.TemporaryDirectory() as directory:
            subject = Path(directory) / "subject"
            scratch = Path(directory) / "scratch"
            subject.mkdir()
            scratch.mkdir()
            with mock.patch.object(host_exits.shutil, "which", return_value="/usr/bin/bwrap"), \
                    mock.patch.object(host_exits.subprocess, "run", side_effect=lambda argv, **_kwargs: observed.update(argv=argv) or completed):
                result = host_exits.run_allowlisted(
                    ["true"], scratch, cwd=subject, profile="apply", subject_root=subject,
                    env={"HOME": str(Path.home()), "PATH": "/bin", "LANG": "C"},
                )
        self.assertTrue(result.child_started)
        command = observed["argv"]
        mounts = [command[index:index + 3] for index in range(len(command) - 2)]
        self.assertIn("--tmpfs", command)
        self.assertEqual("/", command[command.index("--tmpfs") + 1])
        self.assertNotIn(["--ro-bind", "/", "/"], mounts)
        self.assertFalse(any(str(Path.home()) in " ".join(mount) for mount in mounts if mount[0] == "--ro-bind"))
        self.assertIn(["--ro-bind", str(subject), str(subject)], mounts)
        self.assertIn(["--bind", str(scratch), str(scratch)], mounts)

    @unittest.skipUnless(supports_bwrap_namespace(), "requires a usable bwrap network namespace")
    def test_apply_child_cannot_read_or_write_host_git_metadata(self):
        """Apply's allow-list does not mount host git metadata outside the subject."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subject = root / "subject"
            scratch = root / "scratch"
            outside = root / "host-git"
            subject.mkdir()
            scratch.mkdir()
            outside.mkdir()
            target = outside / "HEAD"
            original = "host-original\n"
            target.write_text(original, encoding="utf-8")
            script = (
                "import pathlib,sys\n"
                "target=pathlib.Path(sys.argv[1])\n"
                "print('ABSENT' if not target.exists() else 'PRESENT')\n"
                "try:\n"
                "    target.write_text('bad')\n"
                "    print('WROTE')\n"
                "except OSError:\n"
                "    print('DENIED')\n"
            )
            result = host_exits.run_allowlisted(
                [sys.executable, "-B", "-c", script, str(target)], scratch,
                cwd=subject, profile="apply", subject_root=subject,
                env={"HOME": str(Path.home()), "PATH": os.environ.get("PATH", "/usr/bin"), "LANG": "C"},
            )
            self.assertTrue(result.child_started, result.stderr)
            self.assertIn("ABSENT", result.stdout)
            self.assertIn("DENIED", result.stdout)
            self.assertEqual(original, target.read_text(encoding="utf-8"))

    @unittest.skipUnless(supports_bwrap_namespace(), "requires writable HOME and a usable bwrap network namespace")
    def test_restricted_child_cannot_write_host_receipt_subject_or_git_common_dir(self):
        with tempfile.TemporaryDirectory(dir=Path.home()) as directory:
            root = Path(directory)
            worktree = root / "worktree"
            protected = root / "host-receipt.json"
            common = root / "git-common"
            worktree.mkdir()
            protected.write_text("host\n", encoding="utf-8")
            common.mkdir()
            (common / "HEAD").write_text("host\n", encoding="utf-8")
            subject = root / "formal-subject.txt"
            subject.write_text("subject\n", encoding="utf-8")
            script = "import pathlib,sys; targets=[pathlib.Path(x) for x in sys.argv[1:]]; [p.write_text('mutated') for p in targets]"
            result = host_exits.run_allowlisted(
                [sys.executable, "-c", script, str(protected), str(common / "HEAD"), str(subject)], worktree,
                cwd=worktree, profile="apply", subject_root=worktree,
                env={"HOME": str(Path.home()), "PATH": os.environ.get("PATH", "/usr/bin"), "LANG": "C"},
            )
            if result.returncode == 0:
                self.assertEqual("host\n", protected.read_text(encoding="utf-8"))
            else:
                # The paths are outside the allow-list, so the write fails closed.
                # A kernel that cannot build a network namespace never starts the child.
                self.assertTrue(
                    result.child_started or "NETLINK_ROUTE" in result.stderr,
                    result.stderr,
                )
            self.assertEqual("host\n", protected.read_text(encoding="utf-8"))
            self.assertEqual("host\n", (common / "HEAD").read_text(encoding="utf-8"))
            self.assertEqual("subject\n", subject.read_text(encoding="utf-8"))

    def test_receipt_binds_candidate_and_test_version(self):
        receipt = host_exits.validation_receipt(
            subject_repo="/subject", baseline_commit="a" * 40, preimages={"a.py": "b" * 64},
            patch_sha256="c" * 64, postimages={"a.py": "d" * 64},
            test_commands=[["python", "-m", "unittest"]], result={"passed": True}, approval_ref="maintainer-uuid",
        )
        self.assertEqual("maintainer-uuid", receipt["approval_ref"])
        self.assertNotEqual(receipt["binding_sha256"], host_exits.validation_receipt(
            subject_repo="/subject", baseline_commit="a" * 40, preimages={"a.py": "b" * 64},
            patch_sha256="e" * 64, postimages={"a.py": "d" * 64},
            test_commands=[["python", "-m", "unittest"]], result={"passed": True}, approval_ref="maintainer-uuid",
        )["binding_sha256"])

    def test_human_security_approval_cannot_cross_a_technical_blocker(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LiteFixture(directory)
            runner = fixture.runner()
            fixture.candidate(runner, patch="-one\n+one\n+two\n")
            fixture.transcript.write_text('{"type":"user","uuid":"approval","origin":{"kind":"human"},"message":{"content":"套 A"}}\n', encoding="utf-8")
            with mock.patch.object(runner, "_semantic_review", return_value={
                "status": "REQUIRES_HUMAN_SECURITY_APPROVAL", "technical_blockers": ["test failure"],
            }), mock.patch.object(runner, "_apply") as apply:
                result = runner.apply_or_report({"candidate": "A", "approval_ref": "approval"})
            self.assertEqual(("REQUIRES_HUMAN_APPROVAL", "technical_blocker"), (result["status"], result["reason"]))
            apply.assert_not_called()


if __name__ == "__main__":
    unittest.main()
