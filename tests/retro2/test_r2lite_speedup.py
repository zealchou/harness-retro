import importlib.machinery
import importlib.util
import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]


def load_controller():
    path = ROOT / "retro2/controller/run.py"
    loader = importlib.machinery.SourceFileLoader("retro2_speed_controller", str(path))
    spec = importlib.util.spec_from_loader("retro2_speed_controller", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def load_filter():
    path = ROOT / "retro2/filter_adapter.py"
    spec = importlib.util.spec_from_file_location("retro2_speed_filter", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


from tests.retro2.test_r2lite import LiteFixture  # noqa: E402
import station_host  # noqa: E402


class R2LiteSpeedupTests(unittest.TestCase):
    def _without_semantic_verdicts(self, runner):
        for alias in "ABC":
            path = runner.run_dir / "filters" / alias / "filter-verdict.json"
            path.unlink(missing_ok=True)

    def test_station_order_is_mechanical_before_semantic(self):
        self.assertEqual(
            ("health", "evidence", "diagnosis", "candidates", "test_gate", "filters", "apply_or_report"),
            load_controller().STATIONS,
        )

    def test_candidate_schema_requires_local_replacement_not_whole_file(self):
        change = station_host.CANDIDATE_SCHEMA["properties"]["changes"]["items"]
        self.assertNotIn("content_utf8", change["properties"])
        self.assertIn("old_string", change["properties"])
        self.assertIn("new_string", change["properties"])

    def test_local_replacement_requires_exactly_one_match(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.txt"
            target.write_text("alpha\nbeta\n", encoding="utf-8")
            station_host.PythonStationRunner._install_changes(root, [{
                "path": "target.txt", "old_string": "beta\n", "new_string": "gamma\n",
            }])
            self.assertEqual("alpha\ngamma\n", target.read_text(encoding="utf-8"))
            for old in ("missing", "a"):
                with self.subTest(old=old), self.assertRaisesRegex(ValueError, "exactly once"):
                    station_host.PythonStationRunner._install_changes(root, [{
                        "path": "target.txt", "old_string": old, "new_string": "x",
                    }])

    def test_invalid_model_replacement_is_marked_for_disqualification(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = LiteFixture(directory)
            runner = fx.runner()
            worktree = fx.root / "candidate-worktree"
            worktree.mkdir()
            (worktree / "target.py").write_text("same\nsame\n", encoding="utf-8")
            response = {
                "diagnosis_ids": ["d"], "trusted_decision_refs": ["r"],
                "changes": [{"path": "target.py", "old_string": "same", "new_string": "new"}],
            }
            with mock.patch.object(runner, "_structured", return_value=response):
                result = runner._candidate({"worktree": str(worktree), "allowed_paths": ["target.py"]})
            self.assertIn("old_string_match_count", result["disqualify_reasons"])
            self.assertEqual("same\nsame\n", (worktree / "target.py").read_text(encoding="utf-8"))

    def test_invalid_replacement_manifest_is_disqualified(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = LiteFixture(directory)
            evaluator = fx.root / "evaluator"
            evaluator.mkdir(mode=stat.S_IRUSR | stat.S_IXUSR)

            def invalid(_request):
                return {"diagnosis_ids": ["d"], "trusted_decision_refs": ["r"],
                        "changes": [], "disqualify_reasons": ["old_string_match_count"]}

            result = station_host.candidate_adapter.generate_candidates(
                fx.repo, fx.root / "candidate-run",
                [{"diagnosis_id": "d", "trusted_decision_refs": ["r"],
                  "root_cause": {"evidence": ["target.py:1"]}, "lite": True}],
                ["target.py"], evaluator, invalid,
                {"catalog_version": 1, "seed": 1, "timeout_seconds": 1,
                 "max_changed_files": 3, "max_changed_lines": 200, "lite": True},
            )
            self.assertEqual("NO_CHANGE", result["status"])
            states = [json.loads(path.read_text())["status"] for path in
                      (fx.root / "candidate-run/candidates").glob("*/manifest.json")]
            self.assertEqual(["DISQUALIFIED"] * 3, sorted(states))

    def test_gate_tests_every_eligible_candidate_without_filter_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = LiteFixture(directory)
            calls = []
            runner = fx.runner(lambda argv, **kwargs: (calls.append((argv, kwargs.get("cwd"))) or (0, "green", "")))
            for alias in "ABC":
                fx.candidate(runner, alias=alias)
            self._without_semantic_verdicts(runner)
            result = runner.test_gate({})
            self.assertEqual("TESTS_PASSED", result["status"])
            self.assertEqual(3, len(list((runner.run_dir / "tests").glob("?.json"))))
            self.assertGreaterEqual(len(calls), 1)

    @mock.patch.object(station_host, "AUTO_APPLY_ENABLED", True)
    def test_only_mechanical_winner_gets_semantic_review(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = LiteFixture(directory)
            runner = fx.runner()
            fx.candidate(runner, "A", patch="-one\n-two\n+one\n")
            fx.candidate(runner, "B", patch="-one\n+one\n")
            fx.candidate(runner, "C", patch="-one\n+one\n+two\n")
            self._without_semantic_verdicts(runner)
            with mock.patch.object(runner, "_semantic_review", return_value={"status": "FILTERED_OK"}) as review:
                result = runner.filters({})
            self.assertEqual("FILTERED_OK", result["status"])
            review.assert_called_once_with("A")
            for alias in "ABC":
                self.assertTrue((runner.run_dir / "filters" / alias / "mechanical-verdict.json").is_file())

    def test_no_automatic_winner_runs_zero_semantic_reviews(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = LiteFixture(directory)
            runner = fx.runner()
            for alias in "ABC":
                fx.candidate(runner, alias, patch="-one\n+one\n+two\n")
            self._without_semantic_verdicts(runner)
            with mock.patch.object(runner, "_semantic_review") as review:
                result = runner.filters({})
            self.assertEqual("FILTERED_OK", result["status"])
            review.assert_not_called()
            decision = runner.apply_or_report({})
            self.assertEqual(("REQUIRES_HUMAN_APPROVAL", ["A", "B", "C"]), (decision["status"], decision["candidates"]))

    def test_manual_apply_reviews_unreviewed_candidate_and_blocks_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = LiteFixture(directory)
            runner = fx.runner()
            fx.candidate(runner, patch="-one\n+one\n+two\n")
            report = runner.run_dir / "report/report.md"
            report.parent.mkdir(parents=True)
            report.write_text("report\n", encoding="utf-8")
            fx.transcript.write_text(json.dumps({
                "type": "user", "uuid": "approval", "origin": {"kind": "human"},
                "timestamp_ns": report.stat().st_mtime_ns + 1, "message": {"content": "套 A"},
            }) + "\n", encoding="utf-8")
            self._without_semantic_verdicts(runner)
            with mock.patch.object(runner, "_semantic_review", return_value={"status": "DISQUALIFIED"}) as review, \
                    mock.patch.object(runner, "_apply") as apply:
                result = runner.apply_or_report({"candidate": "A", "approval_ref": "approval"})
            self.assertEqual("REQUIRES_HUMAN_APPROVAL", result["status"])
            review.assert_called_once_with("A")
            apply.assert_not_called()

    def test_manual_apply_continues_after_successful_supplemental_review(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = LiteFixture(directory)
            runner = fx.runner()
            fx.candidate(runner, patch="-one\n+one\n+two\n")
            report = runner.run_dir / "report/report.md"
            report.parent.mkdir(parents=True)
            report.write_text("report\n", encoding="utf-8")
            fx.transcript.write_text(json.dumps({
                "type": "user", "uuid": "approval", "origin": {"kind": "human"},
                "timestamp_ns": report.stat().st_mtime_ns + 1, "message": {"content": "套 A"},
            }) + "\n", encoding="utf-8")
            self._without_semantic_verdicts(runner)
            with mock.patch.object(runner, "_semantic_review", return_value={"status": "FILTERED_OK"}) as review, \
                    mock.patch.object(runner, "_apply", return_value={"status": "CANARY_OK"}) as apply:
                result = runner.apply_or_report({"candidate": "A", "approval_ref": "approval"})
            self.assertEqual("CANARY_OK", result["status"])
            review.assert_called_once_with("A")
            apply.assert_called_once_with("A", approval_ref="approval")

    def test_filter_requests_embed_bounded_skill_and_explicit_schema_without_ponytail_model(self):
        adapter = load_filter()
        requests = []

        def runner(request):
            requests.append(request)
            if request["skill_name"] == "security-audit-skill":
                return {"architecture": "a", "report": "r", "details": "d", "findings": {"findings": []}}
            return {"markdown": "ok", "json": {"findings": [], "blockers": []}}

        evidence = {"passed": True, "commands": [["python", "-m", "unittest"]], "coverage_unknown": False}
        candidate = {
            "candidate_id": "A", "patch": "-old\n+new\n", "changed_paths": ["docs/a.md"],
            "test_evidence": evidence,
        }
        with tempfile.TemporaryDirectory() as directory:
            result = adapter.filter_candidate(candidate, directory, runner, removable_limit=20)
            ponytail = json.loads((Path(directory) / "filters/A/ponytail.json").read_text())
        self.assertEqual("FILTERED_OK", result["status"])
        self.assertNotIn("ponytail-review", [item["skill_name"] for item in requests])
        self.assertEqual(0, ponytail["net_lines"])
        self.assertTrue(all(0 < len(item["skill_content"]) <= 8000 for item in requests))
        self.assertTrue(all(item["response_schema"]["type"] == "object" for item in requests))
        self.assertTrue(all(item["test_evidence"] == evidence for item in requests))
        self.assertTrue(all("blockers 只列能指出具體觸發情境" in item["blocker_rule"] for item in requests))

    def test_semantic_review_forwards_bounded_host_test_evidence_to_filter_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = LiteFixture(directory)
            seen = []

            def structured(kind, request, *_args, **_kwargs):
                seen.append(request)
                if kind == "filter-security-audit-skill":
                    return {"architecture": "a", "report": "r", "details": "d", "findings": {"findings": []}}
                return {"markdown": "ok", "json": {"findings": [], "blockers": []}}

            runner = fx.runner()
            fx.candidate(runner)
            self._without_semantic_verdicts(runner)
            (runner.run_dir / "tests/A.json").write_text(json.dumps({
                "passed": False,
                "commands": [["python", "-m", "unittest", "tests.test_settings"]],
                "coverage_unknown": False,
                "infrastructure_error": "fixture failure",
                "reason": "test_failed",
                "results": [{"exit_code": 1, "stdout": "o" * 100, "stderr": "fixture failure"}],
            }), encoding="utf-8")
            runner._structured = structured

            self.assertEqual("FILTERED_OK", runner._semantic_review("A")["status"])

            expected = {
                "passed": False,
                "commands": [["python", "-m", "unittest", "tests.test_settings"]],
                "coverage_unknown": False,
                "infrastructure_error": "fixture failure",
                "reason": "test_failed",
                "failure_output_tail": "o" * 100 + "\nfixture failure",
            }
            self.assertEqual(3, len(seen))
            self.assertTrue(all(request["test_evidence"] == expected for request in seen))

    def test_unreviewed_candidate_does_not_require_semantic_reports(self):
        adapter = load_filter()
        with tempfile.TemporaryDirectory() as directory:
            adapter.assert_required_reports(directory, wiring_required=False, semantic_reviewed=False)
            with self.assertRaises(adapter.FilterGateError):
                adapter.assert_required_reports(directory, wiring_required=False, semantic_reviewed=True)

    def test_semantic_review_saves_each_raw_model_reply(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = LiteFixture(directory)

            def command(argv, **_kwargs):
                payload = json.loads(argv[-1].split("<external_data>", 1)[1].split("</external_data>", 1)[0])
                request = payload["request"]
                self.assertIn("skill_content", request)
                self.assertIn("response_schema", request)
                if request["skill_name"] == "security-audit-skill":
                    output = {"architecture": "a", "report": "r", "details": "d", "findings": {"findings": []}}
                else:
                    output = {"markdown": "ok", "json": {"findings": [], "blockers": []}}
                return 0, json.dumps({"structured_output": output, "usage": {"input_tokens": 1, "output_tokens": 1}}), ""

            runner = fx.runner(command)
            fx.candidate(runner)
            self._without_semantic_verdicts(runner)
            self.assertEqual("FILTERED_OK", runner._semantic_review("A")["status"])
            raw = list((runner.run_dir / "model-raw").glob("*.json"))
            self.assertEqual(3, len(raw))
            self.assertTrue(all(path.stat().st_size for path in raw))


if __name__ == "__main__":
    unittest.main()


class MaintenanceRunnerTests(unittest.TestCase):
    def test_production_maintenance_runner_survives_first_model_call(self):
        reply = json.dumps({"structured_output": {"recommendations": []}, "usage": {"input_tokens": 3, "cache_read_input_tokens": 5}})
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(station_host.Path, "home", return_value=Path(directory)), \
                mock.patch.object(station_host.shutil, "which", return_value="/usr/bin/true"), \
                mock.patch.object(station_host.config, "MAINTENANCE_DIR", Path(directory) / "maintenance"), \
                mock.patch.object(station_host.PythonStationRunner, "_command", staticmethod(lambda argv, **_: (0, reply, ""))):
            with self.assertRaisesRegex(station_host.HostExitError, "not allow-listed"):
                station_host.maintenance_runner("prune", {})
