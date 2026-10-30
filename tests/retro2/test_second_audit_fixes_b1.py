import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
GAUGE = Path(os.environ.get(
    "RETRO2_SECOND_GAUGE", ROOT / "hooks/context_pressure_gauge.py"
))
STATE_ROOT = Path(os.environ.get("RETRO2_GAUGE_STATE_DIR", "/tmp/retro2-context-state"))


class GaugeStateLockDeadlineTests(unittest.TestCase):
    def _holder(self, lock_path):
        script = (
            "import fcntl, pathlib, sys, time\n"
            "path = pathlib.Path(sys.argv[1])\n"
            "path.parent.mkdir(parents=True, exist_ok=True)\n"
            "with path.open('w') as handle:\n"
            "    fcntl.flock(handle, fcntl.LOCK_EX)\n"
            "    print('locked', flush=True)\n"
            "    time.sleep(0.8)\n"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(lock_path)],
            stdout=subprocess.PIPE,
            text=True,
        )
        self.assertEqual("locked", process.stdout.readline().strip())
        return process

    def test_full_hook_fails_open_under_held_state_lock_before_100ms(self):
        session_id = "retro2-lock-" + uuid.uuid4().hex
        state_path = STATE_ROOT / f"{session_id}.json"
        lock_path = Path(str(state_path) + ".lock")
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "transcript.jsonl"
            transcript.write_text(json.dumps({
                "type": "assistant",
                "message": {"usage": {
                    "input_tokens": 760_000,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                }},
            }) + "\n")
            payload = {
                "session_id": session_id,
                "hook_event_name": "PostToolUse",
                "transcript_path": str(transcript),
            }
            holder = self._holder(lock_path)
            env = {key: value for key, value in os.environ.items() if not key.startswith("RETRO2_")}
            try:
                started = time.monotonic()
                completed = subprocess.run(
                    [sys.executable, str(GAUGE)],
                    input=json.dumps(payload),
                    text=True,
                    capture_output=True,
                    env=env,
                    timeout=1,
                )
                elapsed = time.monotonic() - started
            finally:
                holder.wait(timeout=2)
                holder.stdout.close()
                state_path.unlink(missing_ok=True)
                lock_path.unlink(missing_ok=True)
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual("", completed.stdout)
            self.assertLess(elapsed, 0.10)


if __name__ == "__main__":
    unittest.main()
