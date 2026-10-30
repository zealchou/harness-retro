import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "retro2"))

import host_exits
import rejection_index
import reporting
import station_host
import worker
from tests.retro2.test_r2lite import LiteFixture, load_controller


class RoundThreeAuditFixes(unittest.TestCase):
    def test_old_approval_for_a_cannot_approve_new_run_a(self):
        # Scenario: 舊的「套 A」不能批准新一輪的 A。
        with tempfile.TemporaryDirectory() as directory:
            fixture = LiteFixture(directory)
            old = fixture.runner()
            new_ticket = {**fixture.ticket, "idempotency_key": "newer-run"}
            newer = station_host.PythonStationRunner(new_ticket, fixture.runs, command=lambda *_a, **_k: (0, "", ""), claude="claude")
            for runner in (old, newer):
                runner.run_dir.joinpath("report").mkdir(parents=True, exist_ok=True)
                runner.run_dir.joinpath("report", "report.md").write_text("report\n", encoding="utf-8")
            os.utime(old.run_dir / "report/report.md", ns=(10_000_000_000, 10_000_000_000))
            os.utime(newer.run_dir / "report/report.md", ns=(30_000_000_000, 30_000_000_000))
            fixture.transcript.write_text(json.dumps({
                "type": "user", "uuid": "approve-old", "origin": {"kind": "human"},
                "timestamp_ns": 20_000_000_000, "message": {"content": "套 A"},
            }) + "\n", encoding="utf-8")
            self.assertFalse(newer._trusted_approval("approve-old", "A"))

    def test_manual_enqueue_captures_external_subject_and_reaches_real_runner_report(self):
        # Scenario: 我在別的專案打 /retro，報告講的是那個專案。
        with tempfile.TemporaryDirectory() as directory:
            fixture = LiteFixture(directory)
            transcript = fixture.transcript
            transcript.write_text(json.dumps({
                "type": "user", "uuid": "maintainer", "origin": {"kind": "human"},
                "message": {"content": "修正 guide"},
            }) + "\n", encoding="utf-8")
            queue = Path(directory) / "queue"
            controller = load_controller("round3_enqueue")
            queued = controller.enqueue_manual("external-session", queue, cwd=fixture.repo, transcript_path=transcript,
                                               launcher=lambda _ticket: 1)
            ticket = json.loads(queued.ticket.read_text(encoding="utf-8"))
            self.assertEqual(str(fixture.repo.resolve()), ticket["repo_root"])
            self.assertEqual(str(transcript.resolve()), ticket["transcript_path"])
            spawned = []
            worker.dispatch_controller(queued.ticket, spawn=lambda _path, argv, _env, **_kwargs: spawned.append(argv) or 2)
            self.assertIn("execute", spawned[0])
            runner = station_host.PythonStationRunner(ticket, Path(directory) / "runs", command=lambda *_a, **_k: (0, "", ""), claude="fake")
            def fake_model(kind, request, *_args, **_kwargs):
                if kind == "diagnosis":
                    return {"status": "DIAGNOSED", "hypotheses": ["fixture"],
                            "root_cause": {"statement": "fixture", "evidence": ["docs/guide.md:1"], "trusted_decision_refs": ["maintainer"]},
                            "minimal_fix": {"subtractive_first": "reuse", "direction": "small", "trusted_decision_refs": ["maintainer"]}}
                return {"diagnosis_ids": ["diagnosis-transcript-recent"], "trusted_decision_refs": ["maintainer"],
                        "changes": [{"path": "docs/guide.md", "old_string": "guide\n", "new_string": "better guide\n"}]}
            runner._structured = fake_model
            result = controller.execute_ticket(queued.ticket, Path(directory) / "runs", runner)
            self.assertTrue(Path(result["report_md"]).is_file())
            self.assertIn(str(fixture.repo.resolve()), Path(result["report_md"]).read_text(encoding="utf-8"))
            self.assertEqual(str(fixture.repo.resolve()), json.loads((runner.run_dir / "ticket.json").read_text())["repo_root"])

    def test_identical_trusted_input_stops_before_diagnosis_model_call(self):
        # Scenario: 同一件事再觸發，幾秒內回報已處理過、不花錢。
        with tempfile.TemporaryDirectory() as directory:
            fixture = LiteFixture(directory)
            fixture.transcript.write_text(json.dumps({
                "type": "user", "uuid": "maintainer", "origin": {"kind": "human"},
                "message": {"content": "同一件事故"},
            }) + "\n", encoding="utf-8")
            runner = fixture.runner()
            runner.evidence({})
            rejection_index.append_input(runner.runs_root / "run-prior", runner._input_rejection_key(), "already_processed", fixture.repo)
            calls = []
            runner._structured = lambda *_a, **_k: calls.append(1) or {}
            self.assertEqual({"status": "NO_CHANGE", "reason": "already_processed"}, runner.diagnosis({}))
            self.assertEqual([], calls)

    def test_hook_test_content_changes_test_version(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LiteFixture(directory)
            hook = fixture.repo / "hooks/test_target.py"
            hook.parent.mkdir()
            hook.write_text("print('first')\n", encoding="utf-8")
            runner = fixture.runner()
            fixture.candidate(runner)
            commands = runner._test_commands(["target.py"])
            before = runner._test_version(commands)
            (runner.run_dir / "tests").mkdir(parents=True, exist_ok=True)
            (runner.run_dir / "tests/A.json").write_text(json.dumps({"passed": True, "commands": commands}), encoding="utf-8")
            runner._write_receipt("A", json.loads((runner.run_dir / "candidates/A/manifest.json").read_text()), {"passed": True, "commands": commands})
            self.assertTrue(runner._receipt_matches("A"))
            hook.write_text("print('second')\n", encoding="utf-8")
            self.assertNotEqual(before, runner._test_version(commands))
            self.assertFalse(runner._receipt_matches("A"))

    def test_host_hook_candidate_uses_only_its_named_tests_or_reports_unknown_coverage(self):
        # Given retro proposes one automatic-check change, show only that check's
        # tests.  This real hook deliberately has no matching Python test.
        with tempfile.TemporaryDirectory() as directory:
            runner = LiteFixture(directory).runner()
            runner.target_repo = ROOT
            content = "#!/bin/sh\necho synthetic automatic check\n"
            candidate = runner.run_dir / "candidates/A"
            candidate.mkdir(parents=True)
            patch = "host hook fixture\n"
            (candidate / "candidate.patch").write_text(patch, encoding="utf-8")
            (candidate / "manifest.json").write_text(json.dumps({
                "status": "ELIGIBLE", "changed_paths": ["hooks/auto_save.sh"],
                "changes": [{"path": "hooks/auto_save.sh", "content_utf8": content}],
                "patch_sha256": hashlib.sha256(patch.encode()).hexdigest(),
            }), encoding="utf-8")
            fake_host = mock.Mock()
            fake_host.baseline.return_value = "baseline"
            def prepare(_repo, worktree, _baseline):
                (worktree / "hooks").mkdir(parents=True)
                (worktree / "hooks/auto_save.sh").write_text(content, encoding="utf-8")
            fake_host.prepare.side_effect = prepare
            fixture = ROOT / "tests/retro2/fixtures/synthetic-candidates/tests/A.json"
            with mock.patch.object(station_host, "GitWorktreeHost", return_value=fake_host), \
                    mock.patch.object(runner, "_run_tests", return_value=json.loads(fixture.read_text(encoding="utf-8"))) as tested:
                outcome = runner.test_gate({})
            self.assertEqual("COVERAGE_UNKNOWN", outcome["status"])
            self.assertEqual([], tested.call_args.args[1])
            result = json.loads((runner.run_dir / "tests/A.json").read_text(encoding="utf-8"))
            self.assertEqual([], result["commands"])
            self.assertTrue(result["coverage_unknown"])
            report = reporting.write_report(
                reporting.empty_trace("REQUIRES_HUMAN_APPROVAL", runner.run_id), runner.run_dir / "report"
            )
            self.assertIn("未經測試，請人工判斷", report["markdown"].read_text(encoding="utf-8"))

    def test_host_retro2_change_keeps_the_retro2_regression_suite(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = LiteFixture(directory).runner()
            runner.target_repo = ROOT
            self.assertEqual(
                [[sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests/retro2", "-t", "."]],
                runner._test_commands(["retro2/station_host.py"]),
            )

    def test_host_settings_candidate_uses_settings_test_module(self):
        # Given a host settings.json change, when the test policy is resolved,
        # then the dedicated settings syntax/path regression test is selected.
        with tempfile.TemporaryDirectory() as directory:
            runner = LiteFixture(directory).runner()
            runner.target_repo = ROOT
            self.assertIn(
                [sys.executable, "-B", "-m", "unittest", "tests.test_settings"],
                runner._test_commands(["settings.json"]),
            )

    def test_bwrap_start_failure_is_retryable_not_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LiteFixture(directory)
            runner = fixture.runner()
            fixture.candidate(runner, patch="-one\n+broken\n")
            runner._uses_platform_sandbox = True
            failed_start = subprocess.CompletedProcess(["bwrap"], 1, "", "NETLINK_ROUTE: Operation not permitted")
            with mock.patch.object(station_host, "run_restricted", return_value=failed_start):
                result = runner.test_gate({})
            self.assertEqual(("NO_CHANGE", "test_infrastructure_error"), (result["status"], result["reason"]))
            ledger = runner.run_dir / "evidence-ledger.jsonl"
            self.assertFalse(ledger.is_file() and '"kind": "rejection"' in ledger.read_text(encoding="utf-8"))

    def test_restricted_child_uses_allowlisted_environment_and_masks_secrets(self):
        observed = {}
        completed = subprocess.CompletedProcess(["bwrap"], 0, "R2_CHILD_STARTED\nok", "")
        with mock.patch.object(host_exits.shutil, "which", return_value="/usr/bin/bwrap"), \
                mock.patch.object(host_exits.subprocess, "run", side_effect=lambda argv, **kwargs: observed.update(argv=argv, env=kwargs["env"]) or completed):
            result = host_exits.run_restricted(["echo", "ok"], Path.cwd(), env={"SECRET_TOKEN": "no", "PATH": "/bin", "LANG": "C"})
        self.assertTrue(result.child_started)
        self.assertNotIn("SECRET_TOKEN", observed["env"])
        self.assertEqual("/bin", observed["env"]["PATH"])
        for secret in (".ssh", ".aws", ".gnupg", ".config/gh", ".docker", ".kube", ".codex", ".claude"):
            if (Path.home() / secret).is_dir():  # 不存在的路徑沒有東西可遮，且唯讀根上無法建掛載點
                self.assertIn(str(Path.home() / secret), observed["argv"])


if __name__ == "__main__":
    unittest.main()
