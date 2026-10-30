import json
import unittest
from pathlib import Path


class ExampleSettingsTests(unittest.TestCase):
    def test_hook_example_is_valid_and_opt_in(self):
        root = Path(__file__).resolve().parents[1]
        hooks = json.loads((root / "examples/settings.json").read_text(encoding="utf-8"))["hooks"]
        commands = [item["command"] for event in hooks.values() for group in event for item in group["hooks"]]
        self.assertEqual(2, len(commands))
        self.assertTrue(all(command.startswith("RETRO2_QUEUE_ENABLED=1 ") for command in commands))


if __name__ == "__main__":
    unittest.main()
