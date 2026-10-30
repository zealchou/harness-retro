import hashlib
import importlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "retro2"))
station_host = importlib.import_module("station_host")


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
            # 診斷要呼叫模型；沙盒遮掉 ~/.claude 後只單獨掛回憑證檔（唯讀），且不在可讀工具範圍內
            creds = restricted.call_args.kwargs["credential_paths"]
            self.assertEqual(1, len(creds)); self.assertTrue(str(creds[0]).endswith(".credentials.json"))


if __name__ == "__main__":
    unittest.main()
