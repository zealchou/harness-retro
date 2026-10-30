"""Real claude CLI inside each allow-listed stage. Skips when the CLI is absent."""
import importlib
import json
import os
import secrets
import shutil
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "retro2"))
host_exits = importlib.import_module("host_exits")

CLAUDE = Path("/var/tmp/cli-prefix/bin/claude")
PROBE = textwrap.dedent(
    """\
    import json, os
    from pathlib import Path
    home = Path(os.environ["HOME"])
    link = Path("escape")
    try:
        leaked = link.read_text(encoding="utf-8")
    except OSError:
        leaked = ""
    tmp_auth = Path("__TMP_AUTH__")
    rows = {
        "home_canary": (home / "canary.txt").is_file(),
        "bash_history": (home / ".bash_history").is_file(),
        "ssh": (home / ".ssh" / "id_ed25519").is_file(),
        "codex_auth": (home / ".codex" / "auth.json").is_file(),
        "claude_cred": (home / ".claude" / ".credentials.json").is_file(),
        "hooks_escape": (home / ".claude" / "hooks" / "canary.txt").is_file(),
        "tmp_auth": tmp_auth.is_file(),
        "repo_symlink": leaked.startswith("CANARY"),
        "codex_home": "CODEX_HOME" in os.environ,
        "ssh_sock": "SSH_AUTH_SOCK" in os.environ,
    }
    print("PROBE " + json.dumps(rows, sort_keys=True))
    """
).strip()


def _supports():
    if not CLAUDE.is_file() or not shutil.which("bwrap"):
        return False
    return os.access("/var/tmp", os.W_OK)


@unittest.skipUnless(_supports(), "requires the npm claude CLI under /var/tmp/cli-prefix and bwrap")
class RealClaudeSandboxTests(unittest.TestCase):
    def test_each_stage_starts_claude_and_hides_canaries(self):
        version = __import__("subprocess").run(
            [str(CLAUDE), "--version"], text=True, capture_output=True, check=False,
            env={**os.environ, "PATH": "/exec-daemon:/usr/bin:/bin"},
        )
        self.assertEqual(0, version.returncode, version.stderr)
        parent = Path("/var/tmp")
        with tempfile.TemporaryDirectory(prefix="retro2-real-cli-", dir=parent) as directory:
            root = Path(directory)
            home = root / "home"
            harness = home / ".claude"
            (harness / "scripts" / "logs").mkdir(parents=True)
            (harness / "CLAUDE.md").write_text("# rules\n", encoding="utf-8")
            (harness / "settings.json").write_text("{}\n", encoding="utf-8")
            (harness / "hooks").mkdir()
            (home / "canary.txt").write_text("CANARY-HOME\n", encoding="utf-8")
            (home / ".bash_history").write_text("synthetic-history\n", encoding="utf-8")
            (home / ".ssh").mkdir()
            (home / ".ssh" / "id_ed25519").write_text("SYNTHETIC-SSH\n", encoding="utf-8")
            (home / ".codex").mkdir()
            (home / ".codex" / "auth.json").write_text("SYNTHETIC-CODEX-AUTH\n", encoding="utf-8")
            credential = harness / ".credentials.json"
            credential.write_text("SYNTHETIC-CLAUDE-CRED\n", encoding="utf-8")
            project = root / "project"
            project.mkdir()
            (project / "README").write_text("project\n", encoding="utf-8")
            (project / "escape").symlink_to(home / "canary.txt")
            work = root / "worktree"
            work.mkdir()
            (work / "target.py").write_text("x = 1\n", encoding="utf-8")
            (work / "escape").symlink_to(home / "canary.txt")
            scratch = root / "scratch"
            scratch.mkdir()
            host_tmp = Path("/tmp") / ("retro2-fake-auth-" + secrets.token_hex(6))
            host_tmp.write_text("SYNTHETIC-TMP-AUTH\n", encoding="utf-8")
            self.addCleanup(host_tmp.unlink, missing_ok=True)
            env = {
                "HOME": str(home), "PATH": "/exec-daemon:/usr/bin:/bin", "LANG": "C",
                "SSH_AUTH_SOCK": "/tmp/agent.sock",
            }
            probe = PROBE.replace("__TMP_AUTH__", str(host_tmp))
            reads = (
                harness / "CLAUDE.md", harness / "settings.json",
                harness / "hooks", harness / "scripts" / "logs",
            )
            stages = {
                "diagnosis": dict(
                    argv_cwd=project, profile="diagnosis", project_root=project,
                    writable=scratch, subject_root=None, harness_reads=reads,
                    allow_network=True, credential_paths=(credential,),
                ),
                "candidate": dict(
                    argv_cwd=work, profile="candidate", project_root=work,
                    writable=work, subject_root=None, harness_reads=(),
                    allow_network=True, credential_paths=(credential,),
                ),
                "apply": dict(
                    argv_cwd=project, profile="apply", project_root=None,
                    writable=scratch, subject_root=project, harness_reads=(),
                    allow_network=False, credential_paths=(),
                ),
            }
            note = [
                "# Real CLI sandbox verification",
                "",
                "Claude only. Codex selection is refused and no Codex auth file is copied or mounted.",
                "The process HOME is a throwaway directory. Marker bodies are not printed.",
                "",
                "```",
                f"$ {CLAUDE} --version",
                (version.stdout or version.stderr).strip(),
                "```",
                "",
            ]
            for name, spec in stages.items():
                for label, argv in (
                    (f"claude {name} --version", [str(CLAUDE), "--version"]),
                    (f"claude {name} probe", ["/usr/bin/python3", "-c", probe]),
                ):
                    kwargs = dict(
                        cwd=spec["argv_cwd"], timeout=60, profile=spec["profile"],
                        allow_network=spec["allow_network"], harness_root=harness,
                        harness_reads=spec["harness_reads"], credential_paths=spec["credential_paths"],
                        env=env,
                    )
                    if spec["project_root"] is not None:
                        kwargs["project_root"] = spec["project_root"]
                    if spec["subject_root"] is not None:
                        kwargs["subject_root"] = spec["subject_root"]
                    result = host_exits.run_allowlisted(argv, spec["writable"], **kwargs)
                    self.assertTrue(result.child_started, result.stderr)
                    self.assertEqual(0, result.returncode, result.stderr)
                    shown = (result.stdout or "").strip() or "(empty)"
                    note += [
                        f"## {label}",
                        f"profile: {spec['profile']}",
                        f"child_started=True exit={result.returncode}",
                        "stdout:",
                        shown,
                        "stderr:",
                        (result.stderr or "").strip() or "(empty)",
                        "",
                    ]
                    if label.endswith("probe"):
                        rows = json.loads(shown.removeprefix("PROBE ").strip())
                        for key in (
                            "home_canary", "bash_history", "ssh", "codex_auth",
                            "claude_cred", "tmp_auth", "repo_symlink", "codex_home", "ssh_sock",
                        ):
                            self.assertFalse(rows[key], rows)
            destination = ROOT / "docs" / "sandbox-cli-verification.md"
            destination.write_text("\n".join(note) + "\n", encoding="utf-8")
