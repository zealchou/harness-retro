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

    def test_restricted_git_view_mounts_only_a_read_only_scaffold_and_named_metadata(self):
        roots = station_host.PythonStationRunner._git_metadata_roots(Path.home() / ".claude")
        observed = {}
        completed = subprocess.CompletedProcess(["bwrap"], 0, "R2_CHILD_STARTED\nok", "")
        with mock.patch.object(host_exits.shutil, "which", return_value="/usr/bin/bwrap"), \
                mock.patch.object(host_exits.subprocess, "run", side_effect=lambda argv, **_kwargs: observed.update(argv=argv) or completed):
            result = host_exits.run_restricted(["true"], ROOT, git_metadata_roots=roots)
        self.assertTrue(result.child_started)
        command = observed["argv"]
        home_claude = str(Path.home() / ".claude")
        mounts = [command[index:index + 3] for index in range(len(command) - 2)]
        self.assertIn(home_claude, command)
        self.assertTrue(any(mount[0] == "--ro-bind" and mount[2] == home_claude for mount in mounts))
        for root in roots:
            self.assertIn(["--ro-bind", str(root), str(root)], mounts)
        self.assertNotIn(["--bind", str(Path.home() / ".claude/.git"), str(Path.home() / ".claude/.git")],
                         mounts)

    @unittest.skipUnless(supports_bwrap_namespace(), "requires a usable bwrap network namespace")
    def test_restricted_child_cannot_write_host_git_metadata_view(self):
        """當 retro 測試改法時，測試程式改不到 Claude 的版本紀錄。"""
        metadata_root = Path.home() / ".claude"
        roots = station_host.PythonStationRunner._git_metadata_roots(metadata_root)
        self.assertTrue(roots)
        target = metadata_root / ".git" / f"retro2-write-denied-{uuid.uuid4().hex}"
        original = "host-original\n"
        target.write_text(original, encoding="utf-8")
        self.addCleanup(target.unlink, missing_ok=True)
        script = "import pathlib,sys;\ntry: pathlib.Path(sys.argv[1]).write_text('bad')\nexcept OSError: print('DENIED')\nelse: print('WROTE')"
        with tempfile.TemporaryDirectory() as directory:
            result = host_exits.run_restricted(
                [sys.executable, "-B", "-c", script, str(target)], directory,
                git_metadata_roots=roots,
            )
        self.assertTrue(result.child_started, result.stderr)
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
            result = host_exits.run_restricted(
                [sys.executable, "-c", script, str(protected), str(common / "HEAD"), str(subject)], worktree
            )
            if result.returncode == 0:
                self.assertEqual("host\n", protected.read_text(encoding="utf-8"))
            else:
                # 子程序有啟動、寫入被唯讀擋下；或核心不允許建網路命名空間而整個沒啟動（fail-closed）。
                self.assertTrue("Read-only file system" in result.stderr or "NETLINK_ROUTE" in result.stderr, result.stderr)
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
