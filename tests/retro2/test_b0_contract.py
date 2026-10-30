import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "retro2/contract/acceptance.json"
FIXTURE = ROOT / "tests/retro2/fixtures/gauge_behavior.json"
GAUGE = ROOT / "hooks/context_pressure_gauge.py"
REQUIRED_IDS = {f"R{number:02d}" for number in range(1, 20)} - {"R07", "R11", "R12", "R15"}


def load_gauge():
    spec = importlib.util.spec_from_file_location("retro2_gauge_fixture", GAUGE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ContractManifestTests(unittest.TestCase):
    def test_all_blocking_acceptance_ids_are_frozen(self):
        manifest = json.loads(CONTRACT.read_text())
        self.assertEqual(REQUIRED_IDS, {item["id"] for item in manifest["blockers"]})
        self.assertTrue(all(item["batch"].startswith("B") for item in manifest["blockers"]))
        self.assertTrue(all(item["test"].startswith("test_") for item in manifest["blockers"]))

    def test_gauge_75_and_88_outputs_match_frozen_fixture_byte_for_byte(self):
        fixture = json.loads(FIXTURE.read_text())
        gauge = load_gauge()
        with tempfile.TemporaryDirectory() as directory:
            gauge.STATE_DIR = directory
            for index, case in enumerate(fixture["cases"]):
                gauge.read_stdin_payload = lambda case=case, index=index: {
                    "session_id": f"fixture-{index}",
                    "hook_event_name": case["event"],
                    "transcript_path": __file__,
                }
                gauge.measure_usage = lambda _path, case=case: int(
                    fixture["context_window"] * case["ratio"]
                )
                gauge.context_window = lambda: fixture["context_window"]
                captured = io.StringIO()
                with contextlib.redirect_stdout(captured):
                    gauge.main()
                expected = json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": case["event"],
                            "additionalContext": case["expected_context"],
                        }
                    }
                ) + "\n"
                self.assertEqual(expected, captured.getvalue(), msg=case["name"])


class EndToEndContractTests(unittest.TestCase):
    def test_runner_and_immutable_manifest_exist(self):
        manifest = json.loads(CONTRACT.read_text())
        missing = [
            path
            for key in ("runner", "immutable_manifest")
            if not (ROOT / (path := manifest["paths"][key])).exists()
        ]
        self.assertEqual([], missing, f"B0 expected red: missing end-to-end pieces: {missing}")


if __name__ == "__main__":
    unittest.main()
