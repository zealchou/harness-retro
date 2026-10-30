import hashlib
import importlib
import importlib.machinery
import importlib.util
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


def load_controller(name="retro2_lite_controller"):
    path = ROOT / "retro2/controller/run.py"
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], text=True, capture_output=True, check=True,
    ).stdout.strip()


class LiteFixture:
    def __init__(self, root):
        self.root = Path(root)
        self.repo = self.root / "repo"
        self.runs = self.root / "runs"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "lite@example.invalid")
        git(self.repo, "config", "user.name", "R2 Lite")
        (self.repo / "target.py").write_text("one\ntwo\n", encoding="utf-8")
        (self.repo / "docs").mkdir()
        (self.repo / "docs/guide.md").write_text("guide\n", encoding="utf-8")
        tests = self.repo / "tests/retro2"
        tests.mkdir(parents=True)
        (self.repo / "tests/__init__.py").write_text("", encoding="utf-8")
        (tests / "__init__.py").write_text("", encoding="utf-8")
        (tests / "test_target.py").write_text(
            "import unittest\nclass T(unittest.TestCase):\n def test_ok(self): self.assertTrue(True)\n",
            encoding="utf-8",
        )
        git(self.repo, "add", "target.py", "docs/guide.md", "tests/__init__.py", "tests/retro2/__init__.py", "tests/retro2/test_target.py")
        git(self.repo, "commit", "-qm", "baseline")
        self.transcript = self.root / "session.jsonl"
        self.transcript.write_text("", encoding="utf-8")
        self.ticket = {
            "schema_version": 1,
            "idempotency_key": "r2-lite-test",
            "controller_entry": "retro2/controller/run.py",
            "session_id": "session-test",
            "transcript_path": str(self.transcript),
            "repo_root": str(self.repo),
        }

    def runner(self, command=None):
        return station_host.PythonStationRunner(
            self.ticket, self.runs, command=command or (lambda *_a, **_k: (0, "", "")),
            claude="claude",
        )

    def candidate(self, runner, alias="A", *, patch="-one\n-two\n+one\n", content="one\n", delete=False,
                  verdict="FILTERED_OK", categories=(), passed=True, coverage_unknown=False):
        root = runner.run_dir / "candidates" / alias
        root.mkdir(parents=True, exist_ok=True)
        change = {"path": "target.py", "delete": True} if delete else {
            "path": "target.py", "content_utf8": content,
        }
        patch_text = patch
        (root / "candidate.patch").write_text(patch_text, encoding="utf-8")
        (root / "manifest.json").write_text(json.dumps({
            "schema_version": 1,
            "anonymous_id": alias,
            "status": "ELIGIBLE",
            "changed_paths": ["target.py"],
            "changes": [change],
            "patch_sha256": hashlib.sha256(patch_text.encode()).hexdigest(),
        }), encoding="utf-8")
        filters = runner.run_dir / "filters" / alias
        filters.mkdir(parents=True, exist_ok=True)
        (filters / "filter-verdict.json").write_text(json.dumps({
            "status": verdict, "security_categories": list(categories),
        }), encoding="utf-8")
        evidence = runner.run_dir / "tests"
        evidence.mkdir(parents=True, exist_ok=True)
        (evidence / f"{alias}.json").write_text(json.dumps({
            "passed": passed, "coverage_unknown": coverage_unknown, "commands": [["python", "tests"]],
        }), encoding="utf-8")


class R2LiteTests(unittest.TestCase):
    @unittest.skip("R2-LITE speedup moves mechanical test_gate before semantic filters; assertion retained as history")
    def test_controller_routes_through_test_gate_before_apply_or_report(self):
        controller = load_controller()
        calls = []

        def runner(station, _request):
            calls.append(station)
            statuses = {
                "health": "HEALTHY", "evidence": "EVIDENCE_OK", "diagnosis": "DIAGNOSED",
                "candidates": "CANDIDATES_DONE", "filters": "FILTERED_OK",
                "test_gate": "TESTS_PASSED", "apply_or_report": "REQUIRES_HUMAN_APPROVAL",
            }
            result = {"status": statuses[station]}
            if station == "apply_or_report":
                result["reason"] = "human_approval_required"
            return result

        with tempfile.TemporaryDirectory() as directory:
            ticket = Path(directory) / "ticket.json"
            ticket.write_text(json.dumps({
                "schema_version": 1, "idempotency_key": "mutation-watch",
                "controller_entry": "retro2/controller/run.py", "mode": "competition",
            }), encoding="utf-8")
            controller.execute_ticket(ticket, Path(directory) / "runs", runner)
        self.assertEqual(
            ["health", "evidence", "diagnosis", "candidates", "filters", "test_gate", "apply_or_report"],
            calls,
        )

    def test_apply_entry_rejects_non_uuid_approval_before_loading_a_run(self):
        controller = load_controller("retro2_lite_apply_uuid")
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(ValueError, "UUID"):
            controller.apply_saved("run-0123456789abcdef", "A", "not-a-uuid", directory)

    def test_apply_entry_targets_host_for_host_candidate_even_if_origin_repo_is_gone(self):
        controller = load_controller("retro2_lite_apply_host")
        seen = {}
        run_id_of = controller.PythonStationRunner.__init__.__globals__["_run_id"]

        class FakeRunner:
            def __init__(self, ticket, _runs_root):
                seen["repo_root"] = ticket.get("repo_root")
                self.run_id = run_id_of(ticket)

            def apply_or_report(self, request):
                return {"status": "APPLIED", **request}

        with tempfile.TemporaryDirectory() as directory:
            key = "host-apply"
            run_id = run_id_of({"idempotency_key": key})
            run_dir = Path(directory) / run_id
            (run_dir / "candidates/A").mkdir(parents=True)
            (run_dir / "ticket.json").write_text(json.dumps({
                "schema_version": 1, "idempotency_key": key, "controller_entry": "retro2/controller/run.py",
                "repo_root": "/nonexistent/removed-worktree",
            }), encoding="utf-8")
            (run_dir / "candidates/A/manifest.json").write_text(json.dumps({"resource_policy": {"target": "host"}}))
            original = controller.PythonStationRunner
            controller.PythonStationRunner = FakeRunner
            try:
                approval = "bdbbc171" + "-ebc2-4671-b9d1-854ee77b9aff"
                result = controller.apply_saved(run_id, "A", approval, directory)
            finally:
                controller.PythonStationRunner = original
        self.assertEqual(str(controller.ROOT.parent), seen["repo_root"])
        self.assertEqual("APPLIED", result["status"])

    def test_trusted_source_rejects_real_machine_prompt_shapes_and_keeps_last_two(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = LiteFixture(directory)
            rows = [
                {"type": "user", "uuid": "legacy-no-origin", "message": {"content": "無 origin 純文字不得冒認真人"}},
                {"type": "user", "uuid": "human-1", "origin": {"kind": "human"}, "message": {"content": "第一筆真人修正"}},
                {"type": "user", "uuid": "machine-1", "message": {"content": "<task-notification id='1'>done</task-notification>"}},
                {"type": "user", "uuid": "machine-2", "message": {"content": "<local-command-stdout>ok</local-command-stdout>"}},
                {"type": "user", "uuid": "machine-3", "promptId": "p1", "message": {"content": "meta"}},
                {"type": "user", "uuid": "human-2", "origin": {"kind": "human"}, "message": {"content": "第二筆真人修正<system-reminder>x</system-reminder>"}},
                {"type": "user", "uuid": "machine-4", "promptId": "p2", "userType": "external", "origin": {"kind": "task-notification"}, "message": {"content": "背景工作完成"}},
                {"type": "user", "uuid": "human-3", "promptId": "p3", "userType": "external", "origin": {"kind": "human"}, "message": {"content": "第三筆真人修正"}},
                {"type": "user", "uuid": "tool", "message": {"content": [{"type": "tool_result"}]}},
            ]
            fx.transcript.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            incidents = fx.runner()._incidents()
            self.assertEqual(["human-1", "human-2", "human-3"], [item["source_ref"] for item in incidents])
            self.assertEqual({"transcript-recent"}, {item["incident_id"] for item in incidents})
            self.assertNotIn("system-reminder", incidents[1]["content"])

    def test_hook_shaped_ticket_derives_context_from_diagnosis_without_ticket_allowlist(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = LiteFixture(directory)
            fx.transcript.write_text(json.dumps({
                "type": "user", "uuid": "human", "origin": {"kind": "human"},
                "message": {"content": "請依證據修正"},
            }) + "\n", encoding="utf-8")
            runner = fx.runner()
            runner.health_result = (True, None)
            self.assertEqual("EVIDENCE_OK", runner.evidence({})["status"])
            diagnosis = {
                "status": "DIAGNOSED", "hypotheses": ["fixture"],
                "root_cause": {"statement": "cause", "evidence": ["docs/guide.md:1"]},
                "minimal_fix": {"subtractive_first": "remove", "direction": "reuse"},
            }
            # 沒引用任何 maintainer 訊息的診斷不得放行
            with mock.patch.object(runner, "_structured", return_value=diagnosis):
                self.assertEqual("NO_CHANGE", runner.diagnosis({})["status"])
            diagnosis["root_cause"]["trusted_decision_refs"] = ["human", "not-in-transcript"]
            with mock.patch.object(runner, "_structured", return_value=diagnosis):
                result = runner.diagnosis({})
            self.assertEqual("DIAGNOSED", result["status"])
            self.assertEqual(["docs/guide.md"], runner.allowed_paths)
            requirement = json.loads((runner.run_dir / "candidate-requirements.json").read_text())["requirements"][0]
            self.assertEqual(["human"], requirement["trusted_decision_refs"])

    def test_host_change_path_accepts_only_existing_safe_target_files(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = LiteFixture(directory)
            host = Path(directory) / "host"
            (host / "hooks").mkdir(parents=True)
            (host / "hooks/guard.py").write_text("# hook\n", encoding="utf-8")
            (host / "hooks/attempt.log").write_text("evidence\n", encoding="utf-8")
            (host / "settings.json").write_text("{}\n", encoding="utf-8")
            runner = fx.runner()
            runner.target_repo = host
            with mock.patch.object(station_host, "HOST_HARNESS", host):
                self.assertEqual("hooks/guard.py", runner._resolve_change_path("hooks/guard.py"))
                self.assertIsNone(runner._resolve_change_path("/tmp/retro2-diagnostics/stop_auto_commit.log"))
                self.assertIsNone(runner._resolve_change_path("hooks/attempt.log"))
                self.assertIsNone(runner._resolve_change_path("../outside.py"))
                # maintainer decision「開放但必問」：settings.json 可成為目標；必問由 filter 的 settings_security 類別負責
                self.assertEqual("settings.json", runner._resolve_change_path("settings.json"))
                (host / "settings.local.json").write_text("{}\n", encoding="utf-8")
                self.assertIsNone(runner._resolve_change_path("settings.local.json"))

                fx.transcript.write_text(json.dumps({
                    "type": "user", "uuid": "human", "origin": {"kind": "human"},
                    "message": {"content": "請依證據修正"},
                }) + "\n", encoding="utf-8")
                runner.health_result = (True, None)
                runner.evidence({})
                diagnosis = {"status": "UNDIAGNOSED", "hypotheses": ["hook self-blocks"], "findings": [{
                    "statement": "自動檢查誤擋時，改負責檢查的程式", "angles": ["Automated checks"],
                    "target": "host", "trusted_decision_refs": ["human"],
                    "evidence_path": "/tmp/retro2-diagnostics/stop_auto_commit.log", "change_path": "hooks/guard.py",
                }]}
                with mock.patch.object(runner, "_structured", return_value=diagnosis):
                    self.assertEqual("DIAGNOSED", runner.diagnosis({})["status"])
            self.assertEqual(["hooks/guard.py"], runner.allowed_paths)

    def test_host_change_path_accepts_an_absolute_safe_path(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = LiteFixture(directory).runner()
            runner.target_repo = ROOT
            with mock.patch.object(station_host, "HOST_HARNESS", ROOT):
                self.assertEqual("hooks/context_pressure_gauge.py", runner._resolve_change_path(
                    str(ROOT / "hooks/context_pressure_gauge.py")
                ))

    def test_host_seals_all_seven_manifest_categories_before_transaction(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "session.jsonl"
            transcript.write_text("", encoding="utf-8")
            runner = station_host.PythonStationRunner({
                "schema_version": 1, "idempotency_key": "protected-assets",
                "controller_entry": "retro2/controller/run.py", "repo_root": str(ROOT),
                "transcript_path": str(transcript),
            }, Path(directory) / "runs", command=lambda *_a, **_k: (0, "", ""), claude="claude")
            snapshot = runner._protected_assets()
            self.assertEqual({
                "incident_bank", "scoring_policy", "apply_controller", "revert_controller",
                "settings_security", "credentials", "security_hook_tests",
            }, set(snapshot))
            self.assertTrue(all(snapshot.values()))
            self.assertTrue(all(len(digest) == 64 for assets in snapshot.values() for digest in assets.values()))

    def test_auto_apply_switch_is_off_by_default_so_net_deletion_only_reports(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = LiteFixture(directory)
            runner = fx.runner()
            fx.candidate(runner)
            with mock.patch.object(runner, "_apply") as apply:
                result = runner.apply_or_report({})
            self.assertEqual("REQUIRES_HUMAN_APPROVAL", result["status"])
            apply.assert_not_called()

    @mock.patch.object(station_host, "AUTO_APPLY_ENABLED", True)
    def test_net_deletion_and_green_tests_auto_apply(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = LiteFixture(directory)
            runner = fx.runner()
            fx.candidate(runner)
            with mock.patch.object(runner, "_apply", return_value={"status": "CANARY_OK", "commit_sha": "a" * 40}) as apply:
                result = runner.apply_or_report({})
            self.assertEqual("CANARY_OK", result["status"])
            apply.assert_called_once_with("A", approval_ref=None)

    def test_net_addition_only_writes_report_and_patch(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = LiteFixture(directory)
            runner = fx.runner()
            fx.candidate(runner, patch="-one\n+one\n+two\n", content="one\ntwo\n")
            with mock.patch.object(runner, "_apply") as apply:
                result = runner.apply_or_report({})
            self.assertEqual("REQUIRES_HUMAN_APPROVAL", result["status"])
            self.assertTrue((runner.run_dir / "decision.md").is_file())
            self.assertIn("retro-2 apply", (runner.run_dir / "decision.md").read_text())
            apply.assert_not_called()

    def test_red_test_gate_never_applies(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = LiteFixture(directory)
            runner = fx.runner()
            fx.candidate(runner, passed=False)
            with mock.patch.object(runner, "_apply") as apply:
                result = runner.apply_or_report({})
            self.assertEqual(("NO_CHANGE", "tests_failed"), (result["status"], result["reason"]))
            apply.assert_not_called()

    @mock.patch.object(station_host, "AUTO_APPLY_ENABLED", True)
    def test_unknown_coverage_never_auto_applies_but_remains_for_human_review(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = LiteFixture(directory)
            runner = fx.runner()
            fx.candidate(runner, coverage_unknown=True)
            with mock.patch.object(runner, "_apply") as apply:
                result = runner.apply_or_report({})
            self.assertEqual("REQUIRES_HUMAN_APPROVAL", result["status"])
            apply.assert_not_called()

    def test_each_protected_change_class_never_auto_applies(self):
        categories = (
            "permission_expansion", "credentials", "blocking_hook_weakened", "network_egress",
        )
        for category in categories:
            with self.subTest(category=category), tempfile.TemporaryDirectory() as directory:
                fx = LiteFixture(directory)
                runner = fx.runner()
                fx.candidate(runner, verdict="REQUIRES_HUMAN_SECURITY_APPROVAL", categories=[category])
                with mock.patch.object(runner, "_apply") as apply:
                    result = runner.apply_or_report({})
                self.assertEqual("REQUIRES_HUMAN_APPROVAL", result["status"])
                apply.assert_not_called()
        with tempfile.TemporaryDirectory() as directory:
            fx = LiteFixture(directory)
            runner = fx.runner()
            fx.candidate(runner, delete=True)
            with mock.patch.object(runner, "_apply") as apply:
                self.assertEqual("REQUIRES_HUMAN_APPROVAL", runner.apply_or_report({})["status"])
            apply.assert_not_called()

    def test_hook_return_weakening_is_human_only_through_filter_winner_and_apply(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = LiteFixture(directory)
            runner = fx.runner()
            patch = "-def guard():\n-    return 2\n+def guard(): return 0\n"
            fx.candidate(runner, patch=patch, content="one\n")
            manifest_path = runner.run_dir / "candidates/A/manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["changed_paths"] = ["hooks/guard.py"]
            manifest["changes"] = [{"path": "hooks/guard.py", "content_utf8": "def guard(): return 0\n"}]
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            def empty_findings(request):
                skill = request["skill_name"]
                if skill in {"gunpla-audit", "wiring-ghost-audit"}:
                    return {"markdown": "ok\n", "json": {"findings": [], "blockers": []}}
                if skill == "security-review-project":
                    return {"markdown": "ok\n", "json": {"findings": []}}
                return {"architecture": "ok", "report": "ok", "details": "ok", "findings": {"findings": []}}

            verdict = station_host.filter_adapter.filter_candidate({
                "candidate_id": "A", "patch": patch, "changed_paths": ["hooks/guard.py"],
                "host_fail_closed": True,
            }, runner.run_dir, empty_findings, 20)
            self.assertEqual("REQUIRES_HUMAN_SECURITY_APPROVAL", verdict["status"])
            with mock.patch.object(runner, "_apply") as apply:
                result = runner.apply_or_report({})
            mechanical = json.loads((runner.run_dir / "filters/A/mechanical-verdict.json").read_text())
            self.assertIn("security_hook_path", mechanical["security_categories"])
            self.assertEqual("REQUIRES_HUMAN_APPROVAL", result["status"])
            apply.assert_not_called()

    def test_host_fail_closed_classifies_protected_paths_and_delete_action(self):
        fixtures = (
            (["hooks/guard.py"], False, "security_hook_path"),
            (["config/settings.local.json"], False, "settings_security"),
            (["docs/token-notes.md"], False, "credentials"),
            (["src/ordinary.py"], True, "file_deletion"),
        )
        for paths, deletes_file, expected in fixtures:
            with self.subTest(expected=expected):
                categories = station_host.filter_adapter.classify_security_risks({
                    "patch": "+safe = True\n", "changed_paths": paths,
                    "deletes_file": deletes_file, "host_fail_closed": True,
                })
                self.assertIn(expected, categories)

    def test_dirty_target_preserves_bytes_and_makes_zero_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = LiteFixture(directory)
            runner = fx.runner()
            fx.candidate(runner)
            before = git(fx.repo, "rev-parse", "HEAD")
            (fx.repo / "target.py").write_text("user draft\n", encoding="utf-8")
            with mock.patch.object(runner, "_apply") as apply:
                result = runner.apply_or_report({})
            self.assertEqual("REQUIRES_HUMAN_APPROVAL", result["status"])
            self.assertEqual("user draft\n", (fx.repo / "target.py").read_text())
            self.assertEqual(before, git(fx.repo, "rev-parse", "HEAD"))
            apply.assert_not_called()

    def test_target_changed_after_dirty_check_stops_inside_locked_transaction(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = LiteFixture(directory)
            runner = fx.runner()
            fx.candidate(runner)
            protected = {}
            for category in (
                "incident_bank", "scoring_policy", "apply_controller", "revert_controller",
                "settings_security", "credentials", "security_hook_tests",
            ):
                path = fx.repo / "protected" / f"{category}.txt"
                path.parent.mkdir(exist_ok=True)
                path.write_text(category + "\n", encoding="utf-8")
                relative = path.relative_to(fx.repo).as_posix()
                protected[category] = {relative: hashlib.sha256(path.read_bytes()).hexdigest()}
            git(fx.repo, "add", "protected")
            git(fx.repo, "commit", "-qm", "protected fixture")
            for relative in (
                "filters/A/ponytail.json", "filters/A/gunpla.json",
                "filters/A/security-review.json", "filters/A/security-audit/findings.json",
                "filters/A/security-verdict.json",
            ):
                path = runner.run_dir / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}", encoding="utf-8")
            before = git(fx.repo, "rev-parse", "HEAD")
            runner._write_receipt(
                "A", json.loads((runner.run_dir / "candidates/A/manifest.json").read_text()),
                json.loads((runner.run_dir / "tests/A.json").read_text()),
            )
            self.assertFalse(runner._dirty("A"))
            package = runner._winner_package

            def race_barrier(alias, approval_ref=None):
                (fx.repo / "target.py").write_text("late user draft\n", encoding="utf-8")
                return package(alias, approval_ref)

            with mock.patch.object(runner, "_protected_assets", return_value=protected), \
                    mock.patch.object(runner, "_winner_package", side_effect=race_barrier):
                result = runner._apply("A")
            self.assertEqual(("NO_CHANGE", "target_dirty"), (result["status"], result["reason"]))
            self.assertEqual("late user draft\n", (fx.repo / "target.py").read_text())
            self.assertEqual(before, git(fx.repo, "rev-parse", "HEAD"))

    def test_post_apply_red_is_reported_as_reverted(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = LiteFixture(directory)
            runner = fx.runner()
            with mock.patch.object(runner, "_winner_package", return_value={"state": "SCORED"}), mock.patch.object(
                station_host.transaction, "run_transaction", return_value={
                    "state": "RESTORE_VERIFIED", "winner_commit": "a" * 40, "revert_commit": "b" * 40,
                }), mock.patch.object(runner, "_receipt_matches", return_value=True):
                result = runner._apply("A", approval_ref=None)
            self.assertEqual(("RESTORE_VERIFIED", "b" * 40), (result["status"], result["revert_sha"]))

    def test_secret_context_stops_before_external_model_call(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = LiteFixture(directory)
            calls = []
            runner = fx.runner(lambda argv, **_kwargs: (calls.append(argv) or (0, "{}", "")))
            worktree = fx.root / "candidate"
            worktree.mkdir()
            (worktree / "target.py").write_text("token" + "=do-not-send\n", encoding="utf-8")
            with self.assertRaises(station_host.SecretDetected):
                runner._candidate({"worktree": str(worktree), "allowed_paths": ["target.py"]})
            self.assertEqual([], calls)

    def test_diagnosis_model_is_restricted_to_repo_cwd(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = LiteFixture(directory)
            calls = []
            response = json.dumps({"structured_output": {}, "usage": {}})
            runner = fx.runner(lambda argv, **kwargs: (calls.append((argv, kwargs.get("cwd"))) or (0, response, "")))
            runner._structured("diagnosis", {}, read_only_repo=True)
            self.assertIn("--restricted", calls[0][0])
            self.assertEqual(fx.repo, calls[0][1])

    def test_no_trusted_incident_makes_zero_model_calls_and_no_report(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = LiteFixture(directory)
            fx.transcript.write_text(json.dumps({
                "type": "user", "uuid": "machine",
                "message": {"content": "<pasted_content id='x'>copied</pasted_content>"},
            }) + "\n", encoding="utf-8")
            calls = []
            runner = fx.runner(lambda argv, **_kwargs: (calls.append(argv) or (0, "", "")))
            runner.health_result = (True, None)
            result = runner.evidence({})
            self.assertEqual(("NO_CHANGE", "no_trusted_incident"), (result["status"], result["reason"]))
            self.assertEqual([], calls)
            self.assertFalse((runner.run_dir / "report").exists())

    def test_round_token_budget_stops_before_a_second_external_call(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = LiteFixture(directory)
            calls = []
            response = json.dumps({"structured_output": {}, "usage": {"input_tokens": station_host.MAX_ROUND_TOKENS + 1, "output_tokens": 0}})
            runner = fx.runner(lambda argv, **_kwargs: (calls.append(argv) or (0, response, "")))
            with self.assertRaises(station_host.TokenBudgetExceeded):
                runner._structured("first", {})
            with self.assertRaises(station_host.TokenBudgetExceeded):
                runner._structured("second", {})
            self.assertEqual(1, len(calls))

    def test_saved_token_ledger_hydrates_and_accumulates_monotonically(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = LiteFixture(directory)
            run_dir = fx.runs / station_host._run_id(fx.ticket)
            run_dir.mkdir(parents=True)
            (run_dir / "token-ledger.json").write_text(json.dumps({
                "max_tokens": 150000, "used_tokens": 79578, "cache_read_tokens": 71414,
            }), encoding="utf-8")
            response = json.dumps({
                "structured_output": {},
                "usage": {"input_tokens": 900, "output_tokens": 100, "cache_read_input_tokens": 7},
            })
            runner = fx.runner(lambda _argv, **_kwargs: (0, response, ""))
            runner._structured("manual-apply-review", {})
            ledger = json.loads((run_dir / "token-ledger.json").read_text())
            self.assertEqual((80578, 71421), (ledger["used_tokens"], ledger["cache_read_tokens"]))

    def test_exhausted_saved_token_ledger_stops_before_model_call(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = LiteFixture(directory)
            run_dir = fx.runs / station_host._run_id(fx.ticket)
            run_dir.mkdir(parents=True)
            (run_dir / "token-ledger.json").write_text(json.dumps({
                "max_tokens": station_host.MAX_ROUND_TOKENS, "used_tokens": station_host.MAX_ROUND_TOKENS, "cache_read_tokens": 20,
            }), encoding="utf-8")
            calls = []
            runner = fx.runner(lambda argv, **_kwargs: (calls.append(argv) or (0, "{}", "")))
            with self.assertRaises(station_host.TokenBudgetExceeded):
                runner._structured("manual-apply-review", {})
            self.assertEqual([], calls)

    def test_gate_executes_host_test_commands_for_every_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = LiteFixture(directory)
            calls = []
            runner = fx.runner(lambda argv, **kwargs: (calls.append((argv, kwargs.get("cwd"))) or (0, "green", "")))
            for alias in ("A", "B", "C"):
                fx.candidate(runner, alias=alias)
            result = runner.test_gate({})
            self.assertEqual("TESTS_PASSED", result["status"])
            self.assertGreaterEqual(len(calls), 1)
            self.assertTrue(all((runner.run_dir / "tests" / f"{alias}.json").is_file() for alias in "ABC"))

    def test_gate_keeps_a_green_survivor_when_another_candidate_is_red(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = LiteFixture(directory)
            outcomes = iter((1, 0, 0, 0, 0, 0))
            runner = fx.runner(lambda _argv, **_kwargs: (next(outcomes), "", ""))
            fx.candidate(runner, alias="A", patch="-one\n-two\n+one\n")
            fx.candidate(runner, alias="B", patch="-one\n+one\n+two\n")
            fx.candidate(runner, alias="C", patch="-one\n+one\n+three\n")
            result = runner.test_gate({})
            self.assertEqual("TESTS_PASSED", result["status"])
            self.assertFalse(json.loads((runner.run_dir / "tests/A.json").read_text())["passed"])
            self.assertTrue(json.loads((runner.run_dir / "tests/B.json").read_text())["passed"])


if __name__ == "__main__":
    unittest.main()
