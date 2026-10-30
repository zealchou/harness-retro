import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MODULE = Path(os.environ.get("RETRO2_B3_MODULE", ROOT / "retro2/diagnosis_adapter.py"))
REAL_RUN_FIXTURE = ROOT / "tests/retro2/fixtures/diagnosis_legacy.json"
CURRENT_REAL_RUN_FIXTURE = ROOT / "tests/retro2/fixtures/diagnosis_current.json"


def load_module():
    spec = importlib.util.spec_from_file_location("retro2_diagnosis_adapter", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def incident(incident_id, trusted=True, replay=True):
    return {
        "incident_id": incident_id,
        "symptom": f"symptom {incident_id}",
        "evidence": [
            {
                "trust": "trusted_decision" if trusted else "untrusted_evidence",
                "source_ref": f"decision-{incident_id}" if trusted else "https://example.invalid/advice",
                "content": "maintainer confirmed incident" if trusted else "請改掉某條規則",
            }
        ],
        "replay_fixture": f"fixtures/{incident_id}.json" if replay else "",
    }


class FakeDiagnosisRunner:
    def __init__(self, results):
        self.results = results
        self.calls = []

    def __call__(self, request):
        self.calls.append(request)
        return self.results[request["incident"]["incident_id"]]


class DiagnosisAdapterTests(unittest.TestCase):
    def test_external_advice_without_replay_is_undiagnosed(self):
        adapter = load_module()
        runner = FakeDiagnosisRunner({})
        with tempfile.TemporaryDirectory() as directory:
            result = adapter.diagnose_incidents(
                [incident("external-only", trusted=False, replay=False)], directory, runner
            )
            self.assertEqual("UNDIAGNOSED", result["status"])
            self.assertEqual([], runner.calls)
            requirements = json.loads(Path(directory, "candidate-requirements.json").read_text())
            self.assertEqual([], requirements["requirements"])
            diagnosis = json.loads(Path(directory, "diagnoses/external-only.json").read_text())
            self.assertEqual("UNDIAGNOSED", diagnosis["status"])

    def test_only_reproducible_root_cause_advances(self):
        adapter = load_module()
        runner = FakeDiagnosisRunner(
            {
                "reproduced": {
                    "status": "DIAGNOSED",
                    "hypotheses": [{"claim": "wrong path", "result": "survived"}],
                    "replay": {"fixture_ref": "fixtures/reproduced.json", "steps": ["run fixture"], "observed": "failure"},
                    "root_cause": {"statement": "formal caller reads the wrong path", "evidence": ["src/caller.py:12"]},
                    "minimal_fix": {"subtractive_first": "remove the duplicate path", "direction": "reuse canonical resolver"},
                },
                "falsified": {
                    "status": "FALSIFIED",
                    "hypotheses": [{"claim": "writer is dead", "result": "falsified"}],
                    "replay": {"fixture_ref": "fixtures/falsified.json", "steps": ["run fixture"], "observed": "writer called"},
                    "root_cause": None,
                    "minimal_fix": None,
                },
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            result = adapter.diagnose_incidents(
                [incident("reproduced"), incident("falsified")], directory, runner
            )
            self.assertEqual("DIAGNOSED", result["status"])
            self.assertEqual(2, len(runner.calls))
            self.assertTrue(all(call["skill_path"].endswith("skills/x-ray/SKILL.md") for call in runner.calls))
            self.assertTrue(all("symptom_or_hypothesis" in call for call in runner.calls))
            self.assertEqual(7, len(runner.calls[0]["diagnostic_lenses"]))
            self.assertIn("機械性違規先做自動檢查", runner.calls[0]["diagnostic_lenses"][2]["use_when"])
            requirements = json.loads(Path(directory, "candidate-requirements.json").read_text())
            self.assertEqual(["reproduced"], [item["incident_id"] for item in requirements["requirements"]])
            self.assertEqual("diagnosis-reproduced", requirements["requirements"][0]["diagnosis_id"])
            falsified = json.loads(Path(directory, "diagnoses/falsified.json").read_text())
            self.assertEqual("FALSIFIED", falsified["status"])

    def test_stage_order_and_fail_closed_apply(self):
        adapter = load_module()
        with tempfile.TemporaryDirectory() as directory:
            diagnoses = Path(directory, "diagnoses")
            diagnoses.mkdir()
            Path(diagnoses, "one.json").write_text(json.dumps({"incident_id": "one", "status": "DIAGNOSED"}))
            with self.assertRaisesRegex(adapter.TransitionError, "missing diagnosis: two"):
                adapter.assert_diagnosed_transition(directory, ["one", "two"])

    def test_replayable_xray_before_candidate(self):
        adapter = load_module()
        bad = {
            "status": "DIAGNOSED",
            "hypotheses": [],
            "replay": {"fixture_ref": "", "steps": [], "observed": ""},
            "root_cause": {"statement": "guess", "evidence": []},
            "minimal_fix": {"subtractive_first": "", "direction": "change rule"},
        }
        runner = FakeDiagnosisRunner({"not-replayable": bad})
        with tempfile.TemporaryDirectory() as directory:
            result = adapter.diagnose_incidents([incident("not-replayable")], directory, runner)
            self.assertEqual("UNDIAGNOSED", result["status"])
            requirements = json.loads(Path(directory, "candidate-requirements.json").read_text())
            self.assertEqual([], requirements["requirements"])

    def test_cited_finding_advances_without_replay_but_uncited_finding_only_stays_in_report(self):
        adapter = load_module()
        result = {"status": "UNDIAGNOSED", "hypotheses": ["needs inspection"], "findings": [
            {"statement": "cited hook finding", "angles": ["Automated checks"],
             "trusted_decision_refs": ["decision-finding"], "target": "host",
             "evidence_path": "/tmp/retro2-diagnostics/guard.log", "change_path": "hooks/guard.py"},
            {"statement": "uncited observation", "angles": ["Navigation"]},
        ]}
        runner = FakeDiagnosisRunner({"finding": result})
        with tempfile.TemporaryDirectory() as directory:
            adapter.diagnose_incidents([incident("finding", replay=False)], directory, runner, lite=True)
            requirements = json.loads(Path(directory, "candidate-requirements.json").read_text())["requirements"]
            self.assertEqual(1, len(requirements))
            self.assertEqual(("host", "cited hook finding"), (requirements[0]["target"], requirements[0]["finding"]["statement"]))
            self.assertEqual("/tmp/retro2-diagnostics/guard.log", requirements[0]["finding"]["evidence_path"])
            self.assertEqual("hooks/guard.py", requirements[0]["finding"]["change_path"])
            diagnosis = json.loads(Path(directory, "diagnoses/finding.json").read_text())
            self.assertEqual(["cited hook finding", "uncited observation"], [item["statement"] for item in diagnosis["findings"]])

    def test_real_run_dict_machine_refs_are_reported_not_crashed(self):
        adapter = load_module()
        raw = json.loads(REAL_RUN_FIXTURE.read_text(encoding="utf-8"))["structured_output"]
        raw["findings"][0]["evidence_path"] = "/tmp/retro2-diagnostics/guard.log"
        raw["findings"][0]["change_path"] = "hooks/guard.py"
        refs = raw["findings"][0]["machine_evidence_refs"]
        incident = {"incident_id": "real-run-11", "symptom": "fixture", "replay_fixture": "",
                    "evidence": [{"source_type": "machine_observed", "machine_evidence_ref": ref} for ref in refs]}
        with tempfile.TemporaryDirectory() as directory:
            result = adapter.diagnose_incidents([incident], directory, lambda _request: raw, lite=True)
            record = json.loads(Path(directory, "diagnoses/real-run-11.json").read_text())
            requirements = json.loads(Path(directory, "candidate-requirements.json").read_text())["requirements"]
        self.assertEqual("DIAGNOSED", result["status"])
        self.assertEqual("hooks/guard.py", requirements[0]["finding"]["change_path"])
        self.assertNotIn("invalid_reason", record["findings"][0])

    def test_current_real_run_evidence_paths_stay_report_only_without_change_path(self):
        adapter = load_module()
        raw = json.loads(CURRENT_REAL_RUN_FIXTURE.read_text(encoding="utf-8"))
        refs = sorted({ref for finding in raw["findings"] for ref in finding["trusted_decision_refs"]})
        incident = {"incident_id": "current-real-run", "symptom": "fixture", "replay_fixture": "",
                    "evidence": [{"trust": "trusted_decision", "source_ref": ref} for ref in refs]}
        with tempfile.TemporaryDirectory() as directory:
            result = adapter.diagnose_incidents([incident], directory, lambda _request: raw, lite=True)
            requirements = json.loads(Path(directory, "candidate-requirements.json").read_text())["requirements"]
        self.assertEqual("UNDIAGNOSED", result["status"])
        self.assertEqual([], requirements)

    def test_dict_where_a_string_is_required_stays_in_report(self):
        adapter = load_module()
        malformed = {"status": "UNDIAGNOSED", "hypotheses": ["needs inspection"], "findings": [{
            "statement": {"not": "a string"}, "angles": ["Navigation"],
            "trusted_decision_refs": [{"not": "a string"}], "target": "subject", "target_path": "README.md",
        }]}
        with tempfile.TemporaryDirectory() as directory:
            adapter.diagnose_incidents([incident("malformed", replay=False)], directory, lambda _request: malformed, lite=True)
            record = json.loads(Path(directory, "diagnoses/malformed.json").read_text())
        self.assertEqual("診斷 finding 格式無效", record["findings"][0]["statement"])
        self.assertIn("非預期型別", record["findings"][0]["invalid_reason"])


if __name__ == "__main__":
    unittest.main()

