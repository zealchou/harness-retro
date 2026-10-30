import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "retro2"))
station_host = importlib.import_module("station_host")
reporting = importlib.import_module("reporting")
queue_adapter = importlib.import_module("queue_adapter")
worker = importlib.import_module("worker")


class ModelBackendTests(unittest.TestCase):
    def runner(self, root, command):
        repo = Path(root) / "repo"
        repo.mkdir(exist_ok=True)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        ticket = {"schema_version": 1, "idempotency_key": "backend-test",
                  "controller_entry": "retro2/controller/run.py", "repo_root": str(repo)}
        return station_host.PythonStationRunner(ticket, Path(root) / "runs", command=command,
                                                claude="fake-claude", codex="fake-codex")

    def test_codex_backend_uses_astra_schema_readonly_and_records_usage(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {"RETRO2_MODEL_BACKEND": "codex"}):
            calls = []

            def fake_codex(argv, **_kwargs):
                calls.append(argv)
                output = Path(argv[argv.index("-o") + 1])
                output.write_text(json.dumps({"answer": "structured"}), encoding="utf-8")
                return 0, json.dumps({"type": "turn.completed", "usage": {"input_tokens": 7, "output_tokens": 3}}), ""

            runner = self.runner(directory, fake_codex)
            self.assertEqual({"answer": "structured"}, runner._structured("review", {}))
            argv = calls[0]
            self.assertEqual(["fake-codex", "exec"], argv[:2])
            self.assertEqual("gpt-6-astra", argv[argv.index("-m") + 1])
            self.assertIn("--output-schema", argv)
            self.assertEqual("read-only", argv[argv.index("-s") + 1])
            self.assertNotIn("--add-dir", argv)
            self.assertEqual(10, json.loads((runner.run_dir / "token-ledger.json").read_text())["used_tokens"])

    def test_strict_free_finding_shape_satisfies_filter_gate(self):
        filter_adapter = importlib.import_module("filter_adapter")
        shape = station_host._strict_schema({"type": "array", "items": {"type": "object"}})["items"]
        self.assertIn("location", shape["required"])
        filled = {key: "x" for key in shape["required"]}
        filter_adapter._validate_findings({"findings": [filled]}, "wiring")

    def test_codex_zero_exit_without_output_or_with_error_is_a_failure(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {"RETRO2_MODEL_BACKEND": "codex"}):
            missing = self.runner(directory, lambda *_a, **_k: (0, "", ""))
            with self.assertRaisesRegex(RuntimeError, "output missing"):
                missing._structured("review", {})

            def error(argv, **_kwargs):
                Path(argv[argv.index("-o") + 1]).write_text("ERROR: unavailable\n", encoding="utf-8")
                return 0, "", ""

            with self.assertRaisesRegex(RuntimeError, "codex"):
                self.runner(directory, error)._structured("review", {})

    def test_codex_unknown_usage_records_zero_without_inventing_a_count(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {"RETRO2_MODEL_BACKEND": "codex"}):
            def fake_codex(argv, **_kwargs):
                Path(argv[argv.index("-o") + 1]).write_text("{}", encoding="utf-8")
                return 0, json.dumps({"type": "turn.completed"}), ""

            runner = self.runner(directory, fake_codex)
            runner._structured("review", {})
            ledger = json.loads((runner.run_dir / "token-ledger.json").read_text())
            self.assertEqual(("codex", 0, True), (ledger["backend"], ledger["used_tokens"], ledger["usage_unknown"]))

    def test_unset_backend_keeps_claude_argv(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {}, clear=True):
            calls = []
            reply = json.dumps({"structured_output": {}, "usage": {}})
            runner = self.runner(directory, lambda argv, **_k: (calls.append(argv) or (0, reply, "")))
            runner._structured("review", {})
            self.assertEqual("fake-claude", calls[0][0])
            self.assertIn("--json-schema", calls[0])

    def test_codex_sandbox_mounts_only_its_credential_file(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {"RETRO2_MODEL_BACKEND": "codex"}):
            repo = Path(directory) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            ticket = {"schema_version": 1, "idempotency_key": "backend-sandbox",
                      "controller_entry": "retro2/controller/run.py", "repo_root": str(repo)}
            runner = station_host.PythonStationRunner(ticket, Path(directory) / "runs", codex="fake-codex")
            completed = subprocess.CompletedProcess(["bwrap"], 0, "", "")
            with mock.patch.object(station_host, "run_restricted", return_value=completed) as restricted:
                with self.assertRaises(RuntimeError):
                    runner._structured("diagnosis", {}, read_only_repo=True)
            credentials = restricted.call_args.kwargs["credential_paths"]
            self.assertEqual(1, len(credentials))
            self.assertTrue(str(credentials[0]).endswith(".codex/auth.json"))

    def test_astra_report_names_the_backend_and_states_claude_cli_was_not_used(self):
        with tempfile.TemporaryDirectory() as directory:
            trace = reporting.empty_trace("NO_CHANGE", "run-astra")
            trace["model_backend"] = "codex/gpt-6-astra"
            report = reporting.write_report(trace, Path(directory) / "report")
            text = report["markdown"].read_text(encoding="utf-8")
            self.assertIn("Astra", text)
            self.assertIn("未使用 Claude CLI", text)

    def test_queue_and_worker_preserve_the_selected_backend_for_controller(self):
        class Host:
            def __init__(self):
                self.calls = []

            def spawn_detached(self, argv, env=None):
                self.calls.append((argv, env))
                return 1

        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {"RETRO2_MODEL_BACKEND": "codex"}):
            ticket = Path(directory) / "ticket.json"
            ticket.write_text(json.dumps({"idempotency_key": "backend", "controller_entry": "retro2/controller/run.py"}))
            queue_host, worker_host = Host(), Host()
            queue_adapter.launch_detached_worker(ticket, process_host=queue_host)
            worker.dispatch_controller(ticket, process_host=worker_host)
            self.assertEqual("codex", queue_host.calls[0][1]["RETRO2_MODEL_BACKEND"])
            self.assertEqual("codex", worker_host.calls[0][1]["RETRO2_MODEL_BACKEND"])


if __name__ == "__main__":
    unittest.main()
