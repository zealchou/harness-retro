import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MODULE = Path(os.environ.get("RETRO2_B4_MODULE", ROOT / "retro2/immutable_adapter.py"))
IMMUTABLE = ROOT / "examples"
MANIFEST = ROOT / "retro2/immutable/immutable-manifest.json"


def load_module():
    spec = importlib.util.spec_from_file_location("retro2_immutable_adapter", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ImmutableCatalogTests(unittest.TestCase):
    def test_catalog_has_curated_replayable_questions(self):
        adapter = load_module()
        catalog = adapter.load_catalog(IMMUTABLE)
        self.assertGreaterEqual(len(catalog), 10)
        self.assertLessEqual(len(catalog), 20)
        families = {}
        risk_classes = set()
        source_kinds = set()
        for item in catalog:
            self.assertTrue(adapter.validate_incident(item), item["incident_id"])
            self.assertTrue(adapter.verify_source_evidence(item), item["incident_id"])
            self.assertEqual(64, len(item["source"]["evidence_sha256"]))
            self.assertEqual(1, len(item["judge_question"]))
            self.assertEqual(1, len(item["positive_control"]))
            self.assertTrue(item["fixture"])
            self.assertTrue(item["mechanical_checks"])
            families[item["root_cause_family"]] = families.get(item["root_cause_family"], 0) + 1
            risk_classes.add(item["risk_class"])
            source_kinds.add(item["source"]["kind"])
        self.assertLessEqual(max(families.values()), 2)
        self.assertTrue({"confidentiality", "correctness", "rollback", "wiring", "cost"}.issubset(risk_classes))
        self.assertEqual({"memory", "deferred_defect"}, source_kinds)

    def test_immutable_assets_reject_writes(self):
        adapter = load_module()
        guard = adapter.ImmutableGuard(MANIFEST)
        changes = [
            {"path": "examples/incidents/SYN-001.json", "before": "a", "after": "b"},
            {"path": "retro2/immutable/scoring-policy.json", "before": "a", "after": "b"},
            {"path": "retro2/controller/run.py", "before": "", "after": "pass"},
            {"path": "retro2/apply/transaction.py", "before": "", "after": "pass"},
            {"path": "settings.json", "before": '{"permissions":{"allow":[]}}', "after": '{"permissions":{"allow":["Bash(*)"]}}'},
            {"path": "fixtures/client-key.pem", "before": "", "after": "not-a-real-key"},
            {"path": "hooks/test_security_guard.py", "before": "assert deny", "after": "assert allow"},
        ]
        verdicts = [guard.classify(change) for change in changes]
        self.assertTrue(all(item["status"] == "DISQUALIFIED" for item in verdicts))
        self.assertEqual(
            {"incident_bank", "scoring_policy", "apply_controller", "revert_controller", "settings_security", "credentials", "security_hook_tests"},
            {item["category"] for item in verdicts},
        )
        self.assertEqual("ALLOWED", guard.classify({"path": "docs/note.md", "before": "a", "after": "b"})["status"])
        self.assertEqual(
            "ALLOWED",
            guard.classify({"path": "settings.json", "before": '{"theme":"dark"}', "after": '{"theme":"light"}'})["status"],
        )

    def test_manifest_hash_gate_and_candidate_view(self):
        adapter = load_module()
        guard = adapter.ImmutableGuard(MANIFEST)
        snapshot = guard.snapshot(ROOT)
        self.assertEqual([], guard.verify_snapshot(snapshot))
        protected_path = next(iter(snapshot))
        corrupted = dict(snapshot)
        corrupted[protected_path] += b"mutation"
        self.assertEqual([protected_path], guard.verify_snapshot(corrupted))
        candidate_paths = guard.candidate_view(["src/allowed.py", protected_path, "settings.json", "secrets.env"])
        # maintainer decision「settings.json 開放但必問」：候選可看 settings.json，其他受保護路徑仍不可看
        self.assertEqual(["src/allowed.py", "settings.json"], candidate_paths)
        # 但 permissions／sandbox 欄位一改就取消資格（不可改）；只改 hooks 段才放行
        self.assertEqual("DISQUALIFIED", guard.classify({"path": "settings.json", "before": '{"permissions":{}}',
            "after": '{"permissions":{"allow":["Bash(*)"]}}'})["status"])
        self.assertEqual("ALLOWED", guard.classify({"path": "settings.json", "before": '{"hooks":{}}',
            "after": '{"hooks":{"Stop":[]}}'})["status"])


class CuratorAndJudgeTests(unittest.TestCase):
    def test_human_confirmed_admission_deduplicates_otherwise_quarantines(self):
        adapter = load_module()
        candidate = {
            "incident_id": "INC-NEW",
            "title": "confirmed fixture",
            "source": {"kind": "memory", "path": "memory/example.md", "lines": "1-2", "evidence_sha256": "a" * 64},
            "risk_class": "correctness",
            "root_cause_family": "fixture-contract",
            "fixture": {"input": "safe"},
            "expected_observable": "observable result",
            "mechanical_checks": ["check_fixture"],
            "judge_question": [{"code": "target", "text": "證據是否顯示結果正確？"}],
            "positive_control": [{"code": "control", "text": "證據是否顯示 fixture 存在？"}],
            "negative_control": [],
            "severity": "major",
            "admission": {"curator": "human", "maintainer_confirmation_ref": "decision-new", "date": "2026-10-25", "catalog_version": 1},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = adapter.curate_incident(candidate, root / "quarantine", root / "incidents")
            self.assertEqual("QUARANTINED", first["status"])
            self.assertEqual([], list((root / "incidents").glob("*.json")))
            confirmation = {"authority": "maintainer", "confirmed": True, "source_ref": "decision-new"}
            admitted = adapter.curate_incident(
                candidate, root / "quarantine", root / "incidents", confirmation, human_started=True
            )
            duplicate = adapter.curate_incident(
                candidate, root / "quarantine", root / "incidents", confirmation, human_started=True
            )
            self.assertEqual("ADMITTED", admitted["status"])
            self.assertEqual("DUPLICATE", duplicate["status"])
            self.assertEqual(1, len(list((root / "incidents").glob("*.json"))))

    def test_judge_adapter_uses_existing_script_and_positive_control(self):
        adapter = load_module()
        incident_item = json.loads(next((IMMUTABLE / "incidents").glob("*.json")).read_text())
        calls = []

        def passing_runner(argv):
            calls.append(argv)
            return 0, "0.820  target\n0.950  positive_control\n", ""

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory, "state.txt")
            state.write_text("fixture and observed evidence")
            judge = Path(directory, "judge.py")
            judge.write_text("# configured test double\n", encoding="utf-8")
            adapter.JUDGE_SCRIPT = judge
            result = adapter.evaluate_with_judge(incident_item, state, passing_runner)
            self.assertEqual("PASS", result["status"])
            self.assertTrue(Path(calls[0][1]).samefile(judge))
            self.assertEqual(2, calls[0].count("-q"))

            def bad_control_runner(_argv):
                return 0, "0.990  target\n0.500  positive_control\n", ""

            invalid = adapter.evaluate_with_judge(incident_item, state, bad_control_runner)
            self.assertEqual("INVALID_CONTROL", invalid["status"])


if __name__ == "__main__":
    unittest.main()
