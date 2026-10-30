import importlib.util
import json
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def load_python(name, path):
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class ReportAndMaintenanceTests(unittest.TestCase):
    def test_four_terminal_states_have_distinct_first_screen_conclusions(self):
        reporting = load_python("retro2_b10_reporting", ROOT / "retro2/reporting.py")
        conclusions = []
        with tempfile.TemporaryDirectory() as directory:
            for state in ("CANARY_OK", "NO_CHANGE", "RESTORE_VERIFIED", "REQUIRES_HUMAN_SECURITY_APPROVAL"):
                summary = reporting.empty_trace(state, "run-" + state.lower())
                paths = reporting.write_report(summary, Path(directory) / state)
                markdown = paths["markdown"].read_text()
                html = paths["html"].read_text()
                conclusions.append(markdown.splitlines()[6])
                self.assertIn("lang=\"zh-Hant\"", html)
                self.assertIn("診斷索引", markdown)
                self.assertIn("資安摘要", markdown)
                self.assertIn("套用後機械回歸", markdown)
            self.assertEqual(4, len(set(conclusions)))

    def test_report_rejects_missing_trace_field(self):
        reporting = load_python("retro2_b10_reporting_bad", ROOT / "retro2/reporting.py")
        summary = reporting.empty_trace("NO_CHANGE", "run-bad")
        summary.pop("post_apply_mechanical_regression")
        with self.assertRaisesRegex(ValueError, "post_apply_mechanical_regression"):
            reporting.validate_trace(summary)

    def test_maintenance_is_advisory_only_and_rejects_apply_shape(self):
        maintenance = load_python("retro2_b10_maintenance", ROOT / "retro2/maintenance.py")
        calls = []

        def runner(name, request):
            calls.append((name, request))
            return {"recommendations": [{"action": "KEEP", "reason": "fixture"}], "evidence": ["fixture"]}

        with tempfile.TemporaryDirectory() as directory:
            result = maintenance.run_maintenance(Path(directory), {"rules": "snapshot"}, runner)
            self.assertEqual("HUMAN_DECISION_REQUIRED", result["status"])
            self.assertEqual(["rulebook", "overlap", "trigger"], [name for name, _ in calls])
            self.assertEqual(6, len(result["reports"]))  # three reports, each in Markdown and HTML
            with self.assertRaisesRegex(ValueError, "advisory"):
                maintenance.run_maintenance(
                    Path(directory) / "bad", {}, lambda _name, _request: {"patch": "forbidden"}
                )


class ProductionWiringTests(unittest.TestCase):
    def test_gauge_is_enabled_only_at_existing_hook_mounts(self):
        hooks = json.loads((ROOT / "examples/settings.json").read_text())["hooks"]
        commands = [
            hook["command"]
            for event in ("UserPromptSubmit", "PostToolUse")
            for group in hooks[event]
            for hook in group["hooks"]
            if "retro-2-gauge" in hook.get("command", "")
        ]
        self.assertEqual(2, len(commands))
        self.assertTrue(all(command.startswith("RETRO2_QUEUE_ENABLED=1 ") for command in commands))

    def test_manual_entry_is_thin_and_wiring_registry_tracks_core(self):
        agents = (ROOT / "AGENTS.md").read_text()
        self.assertIn("retro2/controller/run.py", agents)
        self.assertIn("Do not call internal stations directly", agents)
        registry = (ROOT / "hooks/wiring_map_registry.txt").read_text()
        for path in ("hooks/context_pressure_gauge.py", "retro2/controller/run.py", "retro2/worker.py", "retro2/reporting.py"):
            self.assertIn(path, registry)
        wiring = (ROOT / "docs/architecture.md").read_text()
        self.assertIn("Single entry point", wiring)
        self.assertIn("winner package", wiring)
        self.assertIn("No secondary entry point", wiring)


if __name__ == "__main__":
    unittest.main()
