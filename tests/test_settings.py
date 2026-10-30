import json
import re
import shlex
import tomllib
import unittest
from pathlib import Path


ENVIRONMENT_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")


def hook_commands(settings_path: Path) -> list[str]:
    hooks = json.loads(settings_path.read_text(encoding="utf-8"))["hooks"]
    return [item["command"] for event in hooks.values() for group in event for item in group["hooks"]]


def command_name(command: str) -> str:
    for word in shlex.split(command):
        if not ENVIRONMENT_ASSIGNMENT.fullmatch(word):
            return word
    raise ValueError(f"command has no executable: {command}")


class ExampleSettingsTests(unittest.TestCase):
    def test_hook_example_is_valid_and_opt_in(self):
        root = Path(__file__).resolve().parents[1]
        commands = hook_commands(root / "examples/settings.json")
        self.assertEqual(2, len(commands))
        self.assertTrue(all(command.startswith("RETRO2_QUEUE_ENABLED=1 ") for command in commands))

    def test_each_example_hook_command_installs_from_project_scripts(self):
        root = Path(__file__).resolve().parents[1]
        scripts = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["scripts"]
        commands = hook_commands(root / "examples/settings.json")
        self.assertTrue(all(command_name(command) in scripts for command in commands))

    def test_stale_gauge_command_is_rejected(self):
        root = Path(__file__).resolve().parents[1]
        scripts = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["scripts"]
        stale = [command.replace("harness-retro-gauge", "retro-2-gauge") for command in hook_commands(root / "examples/settings.json")]
        self.assertEqual(["retro-2-gauge", "retro-2-gauge"], [command_name(command) for command in stale])
        self.assertTrue(any(command_name(command) not in scripts for command in stale))


if __name__ == "__main__":
    unittest.main()
