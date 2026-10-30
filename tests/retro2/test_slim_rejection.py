import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "retro2"))

import rejection_index
import station_host


class RejectionIndexTests(unittest.TestCase):
    def test_same_patch_rejection_skips_mechanical_retest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subject = root / "subject"
            subject.mkdir()
            subprocess.run(["git", "init", "-q", str(subject)], check=True)
            subprocess.run(["git", "-C", str(subject), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(subject), "config", "user.name", "test"], check=True)
            (subject / "docs").mkdir()
            (subject / "docs/a.md").write_text("same\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(subject), "add", "."], check=True)
            subprocess.run(["git", "-C", str(subject), "commit", "-qm", "baseline"], check=True)
            transcript = root / "session.jsonl"
            transcript.write_text(json.dumps({
                "type": "user", "uuid": "maintainer-1", "origin": {"kind": "human"},
                "message": {"content": "同一個真人事故"},
            }) + "\n", encoding="utf-8")
            ticket = {"schema_version": 1, "idempotency_key": "repeat", "controller_entry": "retro2/controller/run.py", "repo_root": str(subject), "transcript_path": str(transcript)}
            runs = root / "runs"
            runner = station_host.PythonStationRunner(ticket, runs, command=lambda *_a, **_k: (0, "", ""), claude="claude")
            runner.run_dir.mkdir(parents=True)
            manifest = {"patch_sha256": "a" * 64, "changes": [{"path": "docs/a.md", "content_utf8": "same\n"}]}
            key = runner._rejection_key(manifest)
            rejection_index.append(runs / "run-prior", key, "test_failed", "已拒絕", subject)
            self.assertEqual("test_failed", rejection_index.build(runs).lookup(key)["reason"])

    def test_exact_credible_rejection_is_rebuilt_from_ledger_and_skips_model(self):
        with tempfile.TemporaryDirectory() as directory:
            runs = Path(directory) / "runs"
            prior = runs / "run-prior"
            prior.mkdir(parents=True)
            key = rejection_index.make_key(
                [{"source_ref": "maintainer-1", "content": "同一個真人事故"}], "/subject", "baseline-1",
                {"a.py": "a" * 64}, "b" * 64, "strategy-1", "tests-1",
            )
            (prior / "evidence-ledger.jsonl").write_text(json.dumps({
                "kind": "rejection", "key": key, "reason": "test_failed",
                "summary": "候選碰到 allowlist", "stable": True,
            }) + "\n", encoding="utf-8")
            index = rejection_index.build(runs)
            self.assertEqual("test_failed", index.lookup(key)["reason"])
            self.assertEqual([], index.summaries(subject_repo="/subject", limit=3))

    def test_changed_baseline_or_test_version_and_infrastructure_failures_do_not_suppress(self):
        with tempfile.TemporaryDirectory() as directory:
            runs = Path(directory) / "runs"
            run = runs / "run-prior"
            run.mkdir(parents=True)
            key = rejection_index.make_key(
                [{"source_ref": "maintainer-1", "content": "真人事故"}], "/subject", "base-1",
                {"a.py": "a" * 64}, "b" * 64, "strategy", "tests-1"
            )
            rejection_index.append(run, key, "timeout", "暫時 timeout", "/subject")
            rejection_index.append(run, key, "test_failed", "拒絕摘要", "/subject")
            index = rejection_index.build(runs)
            self.assertEqual("test_failed", index.lookup(key)["reason"])
            changed = rejection_index.make_key(
                [{"source_ref": "maintainer-1", "content": "真人事故"}], "/subject", "base-2",
                {"a.py": "a" * 64}, "b" * 64, "strategy", "tests-1"
            )
            changed_tests = rejection_index.make_key(
                [{"source_ref": "maintainer-1", "content": "真人事故"}], "/subject", "base-1",
                {"a.py": "a" * 64}, "b" * 64, "strategy", "tests-2"
            )
            self.assertIsNone(index.lookup(changed))
            self.assertIsNone(index.lookup(changed_tests))


if __name__ == "__main__":
    unittest.main()
