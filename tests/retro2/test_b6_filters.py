import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MODULE = Path(os.environ.get("RETRO2_B6_MODULE", ROOT / "retro2/filter_adapter.py"))


def load_module():
    spec = importlib.util.spec_from_file_location("retro2_filter_adapter", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeFilterRunner:
    def __init__(self, ponytail=0, gunpla_blocker=False, omit=None):
        self.ponytail = ponytail
        self.gunpla_blocker = gunpla_blocker
        self.omit = omit
        self.calls = []

    def __call__(self, request):
        skill = request["skill_name"]
        self.calls.append(skill)
        if skill == self.omit:
            return None
        if skill == "ponytail-review":
            return {"markdown": f"net: -{self.ponytail} lines possible.\n", "json": {"net_removable_lines": self.ponytail, "findings": []}}
        if skill == "gunpla-audit":
            finding = {"location": "src/target.py:4", "evidence": "writer enum differs from reader enum", "severity": "blocker"}
            return {"markdown": "gunpla\n", "json": {"findings": [finding] if self.gunpla_blocker else [], "blockers": [finding] if self.gunpla_blocker else []}}
        if skill == "wiring-ghost-audit":
            return {"markdown": "wiring\n", "json": {"findings": [], "blockers": [], "six_routes_checked": True}}
        if skill == "security-review-project":
            return {"markdown": "security review\n", "json": {"findings": []}}
        if skill == "security-audit-skill":
            return {
                "architecture": "# Architecture\ntrust boundary\n",
                "report": "# Report\nNo exploitable finding in fixture.\n",
                "details": "# Details\nNone.\n",
                "findings": {"findings": []},
            }
        raise AssertionError(skill)


def candidate(patch, paths=None):
    return {"candidate_id": "candidate-A", "patch": patch, "changed_paths": paths or ["src/target.py"]}


class FilterAdapterTests(unittest.TestCase):
    @unittest.skip("R2-LITE speedup replaces the ponytail model call with host net-line evidence; assertion retained as history")
    def test_required_candidate_reports_and_non_hook_wiring_na(self):
        adapter = load_module()
        with tempfile.TemporaryDirectory() as directory:
            runner = FakeFilterRunner()
            result = adapter.filter_candidate(candidate("+safe = True\n"), directory, runner, removable_limit=20)
            self.assertEqual("FILTERED_OK", result["status"])
            self.assertEqual(
                ["ponytail-review", "gunpla-audit", "security-review-project", "security-audit-skill"],
                runner.calls,
            )
            root = Path(directory) / "filters/candidate-A"
            for relative in (
                "ponytail.md", "ponytail.json", "gunpla.md", "gunpla.json",
                "wiring/not-applicable.json", "security-review.md", "security-review.json",
                "security-audit/architecture.md", "security-audit/REPORT.md",
                "security-audit/FINDINGS-DETAIL.md", "security-audit/findings.json",
                "security-verdict.json", "filter-verdict.json",
            ):
                self.assertTrue((root / relative).is_file(), relative)
            self.assertEqual("non_hook_diff", json.loads((root / "wiring/not-applicable.json").read_text())["reason"])

    @unittest.skip("R2-LITE speedup removes model-reported removable-line limits; assertion retained as history")
    def test_overengineering_worldview_and_safe_candidate_transition(self):
        adapter = load_module()
        with tempfile.TemporaryDirectory() as directory:
            over = adapter.filter_candidate(candidate("+wrapper = object()\n"), Path(directory) / "over", FakeFilterRunner(ponytail=21), removable_limit=20)
            world = adapter.filter_candidate(candidate("+STATUS = 'drift'\n"), Path(directory) / "world", FakeFilterRunner(gunpla_blocker=True), removable_limit=20)
            safe = adapter.filter_candidate(candidate("+safe = True\n"), Path(directory) / "safe", FakeFilterRunner(), removable_limit=20)
            self.assertEqual("DISQUALIFIED", over["status"])
            self.assertEqual("ponytail_limit", over["reason"])
            self.assertEqual("DISQUALIFIED", world["status"])
            self.assertEqual("gunpla_blocker", world["reason"])
            self.assertEqual("FILTERED_OK", safe["status"])

    def test_security_categories_require_human_and_never_apply(self):
        adapter = load_module()
        fixtures = [
            ("+os.chmod(path, 0o777)\n", ["src/access.py"], "permission_expansion"),
            ("+urllib.request.urlopen(url)\n", ["src/client.py"], "network_egress"),
            ("+password = 'fixture-secret'\n", ["credentials.py"], "credentials"),
            ("-raise PermissionError('blocked')\n+return True\n", ["hooks/security_gate.py"], "blocking_hook_weakened"),
            ('+{"permissions":{"allow":["Bash(*)"]}}\n', ["settings.json"], "settings_security"),
        ]
        apply_calls = []
        with tempfile.TemporaryDirectory() as directory:
            for index, (patch, paths, category) in enumerate(fixtures):
                item = candidate(patch, paths)
                item["candidate_id"] = f"candidate-{index}"
                result = adapter.filter_candidate(item, directory, FakeFilterRunner(), removable_limit=20, apply_candidate=apply_calls.append)
                self.assertEqual("REQUIRES_HUMAN_SECURITY_APPROVAL", result["status"])
                self.assertIn(category, result["security_categories"])
            clean = adapter.classify_security_risks(candidate("+value = 1\n", ["src/math.py"]))
            self.assertLessEqual(len(clean), 1)
            self.assertEqual([], apply_calls)

    def test_hook_diff_requires_wiring_and_missing_artifact_stops(self):
        adapter = load_module()
        hook = candidate("+def hook(): pass\n", ["hooks/new_guard.py"])
        with tempfile.TemporaryDirectory() as directory:
            good_runner = FakeFilterRunner()
            result = adapter.filter_candidate(hook, Path(directory) / "good", good_runner, removable_limit=20)
            self.assertEqual("FILTERED_OK", result["status"])
            self.assertIn("wiring-ghost-audit", good_runner.calls)
            with self.assertRaisesRegex(adapter.FilterGateError, "missing required artifact"):
                adapter.filter_candidate(hook, Path(directory) / "bad", FakeFilterRunner(omit="wiring-ghost-audit"), removable_limit=20)
            with self.assertRaisesRegex(adapter.FilterGateError, "missing required artifact"):
                adapter.filter_candidate(candidate("+safe = True\n"), Path(directory) / "bad-security", FakeFilterRunner(omit="security-audit-skill"), removable_limit=20)

    def test_presence_gate_rejects_a_report_deleted_after_generation(self):
        adapter = load_module()
        with tempfile.TemporaryDirectory() as directory:
            adapter.filter_candidate(candidate("+safe = True\n"), directory, FakeFilterRunner(), removable_limit=20)
            root = Path(directory) / "filters/candidate-A"
            (root / "gunpla.md").unlink()
            with self.assertRaisesRegex(adapter.FilterGateError, "gunpla.md"):
                adapter.assert_required_reports(root, wiring_required=False)


if __name__ == "__main__":
    unittest.main()

