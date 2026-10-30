import importlib.util
import json
import os
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUN = ROOT / "retro2/controller/run.py"
QUEUE = Path(os.environ.get("RETRO2_B10_QUEUE_MODULE", ROOT / "retro2/queue_adapter.py"))


def load_python(name, path):
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def winner_package():
    return {
        "schema_version": 1,
        "state": "SCORED",
        "run_id": "run-fixture",
        "winner": {"candidate_id": "safe", "changes": []},
    }


class FakeStationRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, station, request):
        self.calls.append((station, request))
        results = {
            "health": {"status": "HEALTHY"},
            "evidence": {"status": "EVIDENCE_OK", "diagnosis_index": ["INC-1"]},
            "diagnosis": {"status": "DIAGNOSED"},
            "candidates": {"status": "CANDIDATES_DONE"},
            "filters": {"status": "FILTERED_OK", "security_summary": "AUTO_APPLY_ALLOWED"},
            "score": {
                "status": "SCORED",
                "winner_package": winner_package(),
                "jev_probabilities": [0.95, 0.82],
                "metrics": {
                    "precision": {"value": 0.9, "unit": "ratio"},
                    "reproducibility": {"value": 1.0, "unit": "ratio"},
                    "token_cost": {"value": 120, "unit": "tokens"},
                },
            },
            "apply_experience": {
                "status": "CANARY_OK",
                "commit_sha": "a" * 40,
                "revert_sha": None,
            },
        }
        return results[station]


class ControllerBoundaryTests(unittest.TestCase):
    def test_manual_and_automatic_tickets_name_the_same_controller(self):
        controller = load_python("retro2_b10_controller", RUN)
        queue = load_python("retro2_b10_queue", QUEUE)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            automatic = queue.enqueue_pressure_event(
                "auto", "cycle", 0.90, root / "auto", launcher=lambda _ticket: 1
            )
            manual = controller.enqueue_manual(
                "manual", root / "manual", launcher=lambda _ticket: 2
            )
            auto_payload = json.loads(automatic.ticket.read_text())
            manual_payload = json.loads(manual.ticket.read_text())
            self.assertEqual("retro2/controller/run.py", auto_payload["controller_entry"])
            self.assertEqual(auto_payload["controller_entry"], manual_payload["controller_entry"])
            queue.observe_pressure_event("auto", 0.50, root / "auto", launcher=lambda _ticket: 3)
            next_cycle = queue.observe_pressure_event("auto", 0.91, root / "auto", launcher=lambda _ticket: 4)
            self.assertTrue(next_cycle.created)
            self.assertNotEqual(automatic.key, next_cycle.key)

    def test_worker_dispatches_controller_and_has_no_station_entry(self):
        worker = load_python("retro2_b10_worker", ROOT / "retro2/worker.py")
        calls = []

        def spawn(executable, argv, _env, **_kwargs):
            calls.append((executable, argv))
            return 77

        with tempfile.TemporaryDirectory() as directory:
            ticket = Path(directory, "ticket.json")
            ticket.write_text(json.dumps({
                "schema_version": 1,
                "idempotency_key": "s:c",
                "controller_entry": "retro2/controller/run.py",
            }))
            pid = worker.dispatch_controller(ticket, spawn=spawn)
            self.assertEqual(77, pid)
            self.assertIn("execute", calls[0][1])
            self.assertIn(str(ticket), calls[0][1])
            self.assertNotIn("apply", " ".join(calls[0][1]))


if __name__ == "__main__":
    unittest.main()
