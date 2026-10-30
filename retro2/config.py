"""Central configuration for portable retro-2 installations."""
from __future__ import annotations

import os
import shlex
from pathlib import Path


def _path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value).expanduser().resolve() if value else default.expanduser().resolve()


def _xdg_state_home() -> Path:
    """XDG state directory. Candidate worktrees must not live under the harness."""
    raw = os.environ.get("XDG_STATE_HOME")
    return Path(raw).expanduser() if raw else Path.home() / ".local" / "state"


RETRO2_HOME = _path("RETRO2_HOME", Path.home() / ".claude")
STATE_DIR = _path("RETRO2_STATE_DIR", RETRO2_HOME / "retro-2")
QUEUE_DIR = _path("RETRO2_QUEUE_DIR", STATE_DIR / "queue")
# Runs, candidate worktrees, and apply scratch sit outside ~/.claude. A worktree
# under RETRO2_HOME is refused by the allow-list, so the default has to be elsewhere.
RUNS_DIR = _path("RETRO2_RUNS_DIR", _xdg_state_home() / "harness-retro" / "runs")
MAINTENANCE_DIR = _path("RETRO2_MAINTENANCE_DIR", STATE_DIR / "maintenance" / "manual")
TRANSCRIPTS_DIR = _path("RETRO2_TRANSCRIPTS_DIR", RETRO2_HOME / "projects")
USAGE_DATA_DIR = _path("RETRO2_USAGE_DATA_DIR", RETRO2_HOME / "usage-data")
DIAGNOSTICS_DIR = _path("RETRO2_DIAGNOSTICS_DIR", Path("/tmp/retro2-diagnostics"))
MANAGED_PROJECTS_FILE = _path(
    "RETRO2_MANAGED_PROJECTS_FILE", RETRO2_HOME / "hooks" / "managed_projects.txt"
)
HOOK_AUDIT_SCRIPT = _path(
    "RETRO2_HOOK_AUDIT_SCRIPT", RETRO2_HOME / "skills" / "hook-doctor" / "scripts" / "hook_audit.py"
)
GLOBAL_RULES_FILE = _path("RETRO2_GLOBAL_RULES_FILE", RETRO2_HOME / "CLAUDE.md")
GLOBAL_EXPERIENCE_FILE = _path(
    "RETRO2_GLOBAL_EXPERIENCE_FILE", RETRO2_HOME / "memory" / "EXPERIENCE.md"
)
JUDGE_SCRIPT = _path("RETRO2_JUDGE_SCRIPT", RETRO2_HOME / "scripts" / "judge.py")
INCIDENT_EVIDENCE_ROOT = _path(
    "RETRO2_INCIDENT_EVIDENCE_ROOT", Path(__file__).resolve().parents[1]
)
CLAUDE_CREDENTIAL_FILE = _path(
    "RETRO2_CLAUDE_CREDENTIAL_FILE", RETRO2_HOME / ".credentials.json"
)
CODEX_CREDENTIAL_FILE = _path(
    "RETRO2_CODEX_CREDENTIAL_FILE", Path.home() / ".codex" / "auth.json"
)


def transcript_path(session_id: str) -> Path:
    """Return the conventional transcript path; callers may always override it."""
    return TRANSCRIPTS_DIR / f"{session_id}.jsonl"


def gate_logs() -> tuple[Path, ...]:
    raw = os.environ.get("RETRO2_GATE_LOGS")
    if raw:
        return tuple(Path(item).expanduser().resolve() for item in raw.split(os.pathsep) if item)
    return (RETRO2_HOME / "scripts" / "logs" / "auto_commit_sweep.log", DIAGNOSTICS_DIR)


def claude_command() -> list[str]:
    return shlex.split(os.environ.get("RETRO2_CLAUDE_COMMAND", "claude"))


def codex_command() -> list[str]:
    return shlex.split(os.environ.get("RETRO2_CODEX_COMMAND", "codex"))

