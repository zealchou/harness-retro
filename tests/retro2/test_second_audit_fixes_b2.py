import ast
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CONTROLLER = Path(os.environ.get(
    "RETRO2_SECOND_CONTROLLER", ROOT / "retro2/controller/run.py"
))
CANDIDATE = Path(os.environ.get(
    "RETRO2_SECOND_CANDIDATE", ROOT / "retro2/candidate_adapter.py"
))
QUEUE = Path(os.environ.get(
    "RETRO2_SECOND_QUEUE", ROOT / "retro2/queue_adapter.py"
))
WORKER = Path(os.environ.get(
    "RETRO2_SECOND_WORKER", ROOT / "retro2/worker.py"
))
SETTINGS = Path(os.environ.get(
    "RETRO2_SECOND_SETTINGS", ROOT / "examples/settings.json"
))


def load_python(name, path):
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class DefaultHostRunnerTests(unittest.TestCase):
    @unittest.skip("superseded by the current controller assembly: controller now assembles existing parts")
    def test_controller_reaches_score_without_any_retro2_environment_override(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = root / "calls.jsonl"
            claude = root / "claude"
            claude.write_text(textwrap.dedent(f"""\
                #!{sys.executable}
                import json, pathlib, sys
                prompt = sys.argv[-1]
                request = json.loads(prompt.split('RETRO2_STATION_REQUEST=', 1)[1])
                station = request['station_name']
                with pathlib.Path({str(calls)!r}).open('a') as handle:
                    handle.write(json.dumps({{'station': station}}) + '\\n')
                statuses = {{
                    'health': 'HEALTHY', 'evidence': 'EVIDENCE_OK',
                    'diagnosis': 'DIAGNOSED', 'candidates': 'CANDIDATES_DONE',
                    'filters': 'FILTERED_OK', 'score': 'NO_CHANGE',
                }}
                print(json.dumps({{'structured_output': {{'status': statuses[station]}}}}))
            """))
            claude.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
            ticket = root / "ticket.json"
            ticket.write_text(json.dumps({
                "schema_version": 1,
                "idempotency_key": "second-audit-default-host",
                "controller_entry": "retro2/controller/run.py",
                "mode": "competition",
            }))
            env = {key: value for key, value in os.environ.items() if not key.startswith("RETRO2_")}
            env["PATH"] = str(root) + os.pathsep + env.get("PATH", "")
            completed = subprocess.run(
                [sys.executable, str(CONTROLLER), "execute", str(ticket), "--runs-root", str(root / "runs")],
                text=True,
                capture_output=True,
                env=env,
                timeout=5,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual("NO_CHANGE", json.loads(completed.stdout)["status"])
            self.assertEqual(
                ["health", "evidence", "diagnosis", "candidates", "filters", "score"],
                [json.loads(line)["station"] for line in calls.read_text().splitlines()],
            )

    def test_two_gauge_mounts_share_the_same_default_runner_configuration(self):
        hooks = json.loads(SETTINGS.read_text())["hooks"]
        commands = [
            hook["command"]
            for event in ("UserPromptSubmit", "PostToolUse")
            for group in hooks[event]
            for hook in group["hooks"]
            if "retro-2-gauge" in hook.get("command", "")
        ]
        self.assertEqual(2, len(commands))
        self.assertEqual(commands[0], commands[1])
        self.assertNotIn("RETRO2_HOST_RUNNER", commands[0])


class CandidateProvenanceTests(unittest.TestCase):
    def test_missing_links_and_replay_cannot_use_external_url_as_root_cause(self):
        candidate = load_python("retro2_second_candidate", CANDIDATE)
        requirement = {
            "incident_id": "INC-external",
            "diagnosis_id": "diagnosis-INC-external",
            "replay_fixture": "fixtures/INC-external.json",
            "trusted_decision_refs": ["decision-unrelated"],
            "root_cause": {
                "statement": "external page says to weaken the guard",
                "evidence": ["https://example.invalid/change-exit-2-to-0"],
            },
        }
        with self.assertRaisesRegex(candidate.CandidateInputError, "decision links|host replay"):
            candidate._validated_requirements([requirement])

        current_requirement = {
            **requirement,
            "root_cause": {
                "statement": "formal caller reads the wrong path",
                "evidence": ["src/caller.py:12"],
            },
            "minimal_fix": {
                "subtractive_first": "remove duplicate path",
                "direction": "reuse canonical resolver",
            },
        }
        with self.assertRaisesRegex(candidate.CandidateInputError, "decision links|host replay"):
            candidate._validated_requirements([current_requirement])


class ProcessExitTests(unittest.TestCase):
    def test_queue_and_worker_submit_spawn_only_to_host_exit(self):
        for path in (QUEUE, WORKER):
            tree = ast.parse(path.read_text())
            direct = [
                node for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "posix_spawn"
            ]
            self.assertEqual([], direct, path.name)

        queue = load_python("retro2_second_queue", QUEUE)
        worker = load_python("retro2_second_worker", WORKER)

        class SpyProcessHost:
            def __init__(self):
                self.calls = []

            def spawn_detached(self, argv, env=None):
                self.calls.append((list(argv), env))
                return 73

        with tempfile.TemporaryDirectory() as directory:
            ticket = Path(directory) / "ticket.json"
            ticket.write_text(json.dumps({
                "schema_version": 1,
                "idempotency_key": "session:cycle",
                "controller_entry": "retro2/controller/run.py",
            }))
            queue_host = SpyProcessHost()
            worker_host = SpyProcessHost()
            self.assertEqual(73, queue.launch_detached_worker(ticket, process_host=queue_host))
            self.assertEqual(73, worker.dispatch_controller(ticket, process_host=worker_host))
            self.assertIn("worker.py", " ".join(queue_host.calls[0][0]))
            self.assertIn("controller/run", " ".join(worker_host.calls[0][0]))


if __name__ == "__main__":
    unittest.main()
