import hashlib
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MODULE = Path(os.environ.get("RETRO2_B8_MODULE", ROOT / "retro2/apply/transaction.py"))


def load_module():
    spec = importlib.util.spec_from_file_location("retro2_b8_transaction", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], text=True, capture_output=True, check=True
    ).stdout.strip()


def digest(data):
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()


class FakeRunner:
    def __init__(self, canary_pass=False, canary_session="fresh-session", restore_pass=True):
        self.canary_pass = canary_pass
        self.canary_session = canary_session
        self.restore_pass = restore_pass
        self.calls = []

    def __call__(self, request):
        self.calls.append(dict(request))
        if request["phase"] == "canary":
            return {
                "passed": self.canary_pass,
                "session_id": self.canary_session,
                "observed_commit": request["expected_commit"],
                "settings_version_ok": True,
                "hook_fixture_passed": True,
                "regression_passed": self.canary_pass,
                "duplicate_run_enqueued": False,
                "child_processes_remaining": 0,
            }
        return {
            "passed": self.restore_pass,
            "session_id": "restore-session",
            "observed_commit": request["expected_commit"],
            "baseline_smoke_passed": self.restore_pass,
        }


class ApplyTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.run_dir = self.root / "run"
        self.lock_root = self.root / "locks"
        self.repo.mkdir()
        self.run_dir.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "retro2@example.invalid")
        git(self.repo, "config", "user.name", "Retro 2 Fixture")
        (self.repo / "target.txt").write_text("baseline\n")
        (self.repo / "unrelated.txt").write_text("tracked baseline\n")
        self.protected = {}
        for category in (
            "incident_bank", "scoring_policy", "apply_controller", "revert_controller",
            "settings_security", "credentials", "security_hook_tests",
        ):
            path = self.repo / "protected" / f"{category}.txt"
            path.parent.mkdir(exist_ok=True)
            path.write_text(f"{category}\n")
            self.protected[category] = {path.relative_to(self.repo).as_posix(): digest(path.read_bytes())}
        git(self.repo, "add", "--", "target.txt", "unrelated.txt", "protected")
        git(self.repo, "commit", "-q", "-m", "baseline")
        self.baseline = git(self.repo, "rev-parse", "HEAD")
        for name in ("ponytail", "gunpla", "security-review", "security-audit", "security-verdict"):
            path = self.run_dir / f"{name}.json"
            path.write_text(json.dumps({"name": name, "status": "PASS"}))

    def tearDown(self):
        self.temp.cleanup()

    def package(self, security_status="AUTO_APPLY_ALLOWED"):
        artifacts = {}
        for path in sorted(self.run_dir.glob("*.json")):
            artifacts[path.stem] = {"path": path.name, "sha256": digest(path.read_bytes())}
        return {
            "schema_version": 1,
            "run_id": "run-b8-fixture",
            "state": "SCORED",
            "source_session_id": "source-session",
            "repo_baseline_commit": self.baseline,
            "incident_bank_version": "incidents-v1",
            "scoring_version": "scoring-v1",
            "winner": {
                "candidate_id": "candidate-safe",
                "candidate_sha256": "a" * 64,
                "security_status": security_status,
                "filter_artifacts": artifacts,
                "changes": [
                    {
                        "path": "target.txt",
                        "preimage_sha256": digest("baseline\n"),
                        "content_utf8": "winner\n",
                    }
                ],
            },
            "protected_assets": self.protected,
        }

    def transact(self, package=None, runner=None):
        module = load_module()
        return module.run_transaction(
            self.repo,
            package or self.package(),
            self.run_dir,
            self.lock_root,
            qualification_runner=lambda _repo, _package: {"passed": True},
            session_runner=runner or FakeRunner(),
        )

    def test_failed_canary_creates_exact_commit_then_revert_and_restores_baseline(self):
        (self.repo / "unrelated.txt").write_text("user dirty work\n")
        result = self.transact(runner=FakeRunner(canary_pass=False))
        self.assertEqual("RESTORE_VERIFIED", result["state"])
        self.assertEqual("baseline\n", (self.repo / "target.txt").read_text())
        self.assertEqual("user dirty work\n", (self.repo / "unrelated.txt").read_text())
        self.assertEqual(3, int(git(self.repo, "rev-list", "--count", "HEAD")))
        winner_paths = git(self.repo, "show", "--pretty=format:", "--name-only", result["winner_commit"]).splitlines()
        self.assertEqual(["target.txt"], [path for path in winner_paths if path])
        self.assertTrue(git(self.repo, "log", "-1", "--pretty=%s").startswith("Revert "))
        ledger = [json.loads(line) for line in (self.run_dir / "evidence-ledger.jsonl").read_text().splitlines()]
        self.assertEqual(["APPLIED", "REVERTED"], [row["event"] for row in ledger])
        self.assertEqual(digest("baseline\n"), ledger[0]["preimage_sha256"]["target.txt"])

    def test_passing_fresh_canary_keeps_exact_winner_commit(self):
        result = self.transact(runner=FakeRunner(canary_pass=True))
        self.assertEqual("CANARY_OK", result["state"])
        self.assertEqual("winner\n", (self.repo / "target.txt").read_text())
        self.assertEqual(2, int(git(self.repo, "rev-list", "--count", "HEAD")))
        self.assertEqual(result["winner_commit"], git(self.repo, "rev-parse", "HEAD"))

    def test_human_only_winner_stops_before_commit(self):
        before = git(self.repo, "rev-list", "--count", "HEAD")
        result = self.transact(self.package("REQUIRES_HUMAN_SECURITY_APPROVAL"))
        self.assertEqual("REQUIRES_HUMAN_SECURITY_APPROVAL", result["state"])
        self.assertEqual(before, git(self.repo, "rev-list", "--count", "HEAD"))
        self.assertEqual("baseline\n", (self.repo / "target.txt").read_text())

    def test_target_preimage_changed_by_other_session_stops_without_merge(self):
        (self.repo / "target.txt").write_text("other session\n")
        with self.assertRaisesRegex(Exception, "preimage"):
            self.transact()
        self.assertEqual(1, int(git(self.repo, "rev-list", "--count", "HEAD")))
        self.assertEqual("other session\n", (self.repo / "target.txt").read_text())

    def test_same_session_canary_is_not_fresh_and_is_reverted(self):
        result = self.transact(runner=FakeRunner(canary_pass=True, canary_session="source-session"))
        self.assertEqual("RESTORE_VERIFIED", result["state"])
        self.assertEqual("baseline\n", (self.repo / "target.txt").read_text())

    def test_restore_failure_requires_manual_recovery(self):
        result = self.transact(runner=FakeRunner(canary_pass=False, restore_pass=False))
        self.assertEqual("MANUAL_RECOVERY_REQUIRED", result["state"])
        self.assertEqual("baseline\n", (self.repo / "target.txt").read_text())


if __name__ == "__main__":
    unittest.main()
