import importlib.util
import json
import os
import statistics
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MODULE = Path(os.environ.get("RETRO2_B1_MODULE", ROOT / "retro2/queue_adapter.py"))
GAUGE = ROOT / "hooks/context_pressure_gauge.py"


def load_module():
    spec = importlib.util.spec_from_file_location("retro2_queue_adapter", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_gauge():
    spec = importlib.util.spec_from_file_location("retro2_b1_gauge", GAUGE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class QueueAdapterTests(unittest.TestCase):
    def test_gauge_adapter_is_opt_in_and_uses_90_percent_boundary(self):
        gauge = load_gauge()
        with tempfile.TemporaryDirectory() as directory:
            old_enabled = os.environ.get("RETRO2_QUEUE_ENABLED")
            old_queue = os.environ.get("RETRO2_QUEUE_DIR")
            try:
                os.environ.pop("RETRO2_QUEUE_ENABLED", None)
                self.assertIsNone(gauge.enqueue_retro2_if_enabled({"session_id": "s"}, 0.95))
                os.environ["RETRO2_QUEUE_ENABLED"] = "1"
                os.environ["RETRO2_QUEUE_DIR"] = directory
                gauge.enqueue_retro2_if_enabled({"session_id": "s"}, 0.899, lambda _: 1)
                gauge.enqueue_retro2_if_enabled({"session_id": "s"}, 0.90, lambda _: 1)
                gauge.enqueue_retro2_if_enabled({"session_id": "s"}, 0.95, lambda _: 1)
                self.assertEqual(1, len(list(Path(directory).glob("*.ticket.json"))))
            finally:
                if old_enabled is None:
                    os.environ.pop("RETRO2_QUEUE_ENABLED", None)
                else:
                    os.environ["RETRO2_QUEUE_ENABLED"] = old_enabled
                if old_queue is None:
                    os.environ.pop("RETRO2_QUEUE_DIR", None)
                else:
                    os.environ["RETRO2_QUEUE_DIR"] = old_queue
    def test_one_ticket_and_worker_for_100_parallel_events(self):
        adapter = load_module()
        with tempfile.TemporaryDirectory() as directory:
            queue = Path(directory)
            calls = []

            def launcher(ticket):
                calls.append(ticket)
                return 123

            def send(_):
                return adapter.enqueue_pressure_event(
                    "session-a", "cycle-1", 0.90, queue, launcher=launcher
                )

            with ThreadPoolExecutor(max_workers=32) as pool:
                results = list(pool.map(send, range(100)))
            self.assertEqual(1, sum(result.created for result in results))
            self.assertEqual(1, len(list(queue.glob("*.ticket.json"))))
            self.assertEqual(1, len(calls))

    def test_boundary_idempotency_latency_and_ticket_shape(self):
        adapter = load_module()
        with tempfile.TemporaryDirectory() as directory:
            queue = Path(directory)
            durations = []
            for ratio in (0.899, 0.90, 0.95):
                started = time.perf_counter()
                adapter.enqueue_pressure_event(
                    "session-b", "cycle-1", ratio, queue, launcher=lambda _ticket: 7
                )
                durations.append((time.perf_counter() - started) * 1000)
            tickets = list(queue.glob("*.ticket.json"))
            self.assertEqual(1, len(tickets))
            payload = json.loads(tickets[0].read_text())
            self.assertEqual("session-b", payload["session_id"])
            self.assertNotIn("incident", payload)
            self.assertLess(statistics.quantiles(durations * 34, n=100)[94], 100)

    def test_default_detached_worker_writes_heartbeat(self):
        adapter = load_module()
        with tempfile.TemporaryDirectory() as directory:
            queue = Path(directory)
            result = adapter.enqueue_pressure_event("session-c", "cycle-1", 0.91, queue)
            heartbeat = Path(str(result.ticket) + ".heartbeat.json")
            deadline = time.time() + 3
            while time.time() < deadline and not heartbeat.exists():
                time.sleep(0.02)
            self.assertTrue(heartbeat.exists())
            self.assertEqual(result.key, json.loads(heartbeat.read_text())["idempotency_key"])

    def test_launcher_failure_is_fail_open_and_ticket_records_error(self):
        adapter = load_module()
        with tempfile.TemporaryDirectory() as directory:
            result = adapter.enqueue_pressure_event(
                "session-d",
                "cycle-1",
                0.90,
                Path(directory),
                launcher=lambda _ticket: (_ for _ in ()).throw(OSError("boom")),
            )
            self.assertTrue(result.created)
            self.assertEqual("enqueue_error", json.loads(result.ticket.read_text())["status"])


if __name__ == "__main__":
    unittest.main()

