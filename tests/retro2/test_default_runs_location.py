"""Default run directories must be mountable by candidate and apply."""
import importlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "retro2"))
host_exits = importlib.import_module("host_exits")


def supports_bwrap():
    if not shutil.which("bwrap"):
        return False
    return subprocess.run(
        ["bwrap", "--ro-bind", "/", "/", "--unshare-net", "--", "true"],
        capture_output=True,
    ).returncode == 0


class DefaultRunsLocationTests(unittest.TestCase):
    @unittest.skipUnless(supports_bwrap(), "requires a usable bwrap network namespace")
    def test_default_runs_dir_lets_candidate_and_apply_start(self):
        parent = Path("/var/tmp") if Path("/var/tmp").is_dir() else Path(tempfile.gettempdir())
        with tempfile.TemporaryDirectory(prefix="retro2-default-runs-", dir=parent) as directory:
            home = Path(directory) / "home"
            harness = home / ".claude"
            harness.mkdir(parents=True)
            env = {key: value for key, value in os.environ.items() if not key.startswith("RETRO2_") and key != "XDG_STATE_HOME"}
            env.update({"HOME": str(home), "PYTHONPATH": str(ROOT / "retro2"), "PATH": os.environ.get("PATH", "/usr/bin")})
            printed = subprocess.run(
                [sys.executable, "-c", "import config; print(config.RUNS_DIR)"],
                text=True, capture_output=True, check=True, env=env,
            )
            runs = Path(printed.stdout.strip())
            self.assertEqual(runs, (home / ".local" / "state" / "harness-retro" / "runs").resolve())
            self.assertFalse(str(runs).startswith(str(harness.resolve())))
            old = harness / "retro-2" / "runs" / "run" / "worktrees" / "candidate-1"
            old.mkdir(parents=True)
            child_env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "LANG": "C"}
            with self.assertRaisesRegex(host_exits.HostExitError, "refusing to mount"):
                host_exits.run_allowlisted(
                    ["true"], old, cwd=old, profile="candidate", project_root=old,
                    harness_root=harness, env=child_env,
                )
            work = runs / "run-1" / "worktrees" / "candidate-1"
            work.mkdir(parents=True)
            (work / "target.py").write_text("x = 1\n", encoding="utf-8")
            candidate = host_exits.run_allowlisted(
                [sys.executable, "-c", "print('CANDIDATE_OK')"], work, cwd=work,
                profile="candidate", project_root=work, harness_root=harness, env=child_env,
            )
            self.assertTrue(candidate.child_started, candidate.stderr)
            self.assertEqual(0, candidate.returncode, candidate.stderr)
            self.assertIn("CANDIDATE_OK", candidate.stdout)
            subject = Path(directory) / "subject"
            subject.mkdir()
            (subject / "README").write_text("subject\n", encoding="utf-8")
            scratch = runs / "run-1" / "apply-sandbox" / "canary"
            scratch.mkdir(parents=True)
            applied = host_exits.run_allowlisted(
                [sys.executable, "-c", "print('APPLY_OK')"], scratch, cwd=subject,
                profile="apply", subject_root=subject, harness_root=harness, env=child_env,
            )
            self.assertTrue(applied.child_started, applied.stderr)
            self.assertEqual(0, applied.returncode, applied.stderr)
            self.assertIn("APPLY_OK", applied.stdout)
