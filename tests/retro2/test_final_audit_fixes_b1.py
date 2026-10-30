import importlib.util
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
QUEUE = Path(os.environ.get("RETRO2_FINAL_B1_QUEUE", ROOT / "retro2/queue_adapter.py"))


def load_queue():
    spec = importlib.util.spec_from_file_location("retro2_final_b1_queue", QUEUE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class QueueLockDeadlineTests(unittest.TestCase):
    def _holder(self, lock_path):
        script = (
            "import fcntl, pathlib, sys, time\n"
            "path = pathlib.Path(sys.argv[1])\n"
            "path.parent.mkdir(parents=True, exist_ok=True)\n"
            "with path.open('a+') as handle:\n"
            "    fcntl.flock(handle, fcntl.LOCK_EX)\n"
            "    print('locked', flush=True)\n"
            "    time.sleep(0.35)\n"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(lock_path)],
            stdout=subprocess.PIPE,
            text=True,
        )
        self.assertEqual("locked", process.stdout.readline().strip())
        return process

    def test_held_enqueue_lock_fails_open_before_deadline(self):
        queue = load_queue()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            holder = self._holder(root / ".enqueue.lock")
            calls = []
            try:
                started = time.monotonic()
                result = queue.enqueue_pressure_event(
                    "session", "cycle", 0.95, root, launcher=calls.append
                )
                elapsed = time.monotonic() - started
            finally:
                holder.wait(timeout=2)
                holder.stdout.close()
            self.assertFalse(result.created)
            self.assertEqual([], calls)
            self.assertLess(elapsed, 0.10)

    def test_held_pressure_lock_fails_open_before_deadline(self):
        queue = load_queue()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            holder = self._holder(root / ".pressure.lock")
            calls = []
            try:
                started = time.monotonic()
                result = queue.observe_pressure_event(
                    "session", 0.95, root, launcher=calls.append
                )
                elapsed = time.monotonic() - started
            finally:
                holder.wait(timeout=2)
                holder.stdout.close()
            self.assertFalse(result.created)
            self.assertEqual([], calls)
            self.assertLess(elapsed, 0.10)


if __name__ == "__main__":
    unittest.main()
