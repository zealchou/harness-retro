# Diagnosis sandbox allow-list

Diagnosis does not start from the host filesystem and then hide secrets. The bubblewrap root is an empty tmpfs. A path is visible only when this list mounts it.

Candidate tests and apply checks still use the older restricted helper. This page is the diagnosis mount list.

## Both backends

| Mount | Mode | Why |
|---|---|---|
| `/usr`, `/lib`, `/lib64`, `/bin`, `/sbin` when present | read-only | The `claude` or `codex` binary and its loader and libraries. |
| `/etc/resolv.conf`, `/etc/hosts`, `/etc/nsswitch.conf`, `/etc/passwd`, `/etc/group`, `/etc/localtime`, `/etc/ssl`, `/etc/ca-certificates` when present | read-only | Name service and TLS material so the CLI can reach the model endpoint. |
| `/proc`, `/dev` | runtime | Process and device nodes bubblewrap gives the CLI. |
| The CLI executable, if it is not already under those system directories | read-only | A checker or a binary installed outside `/usr` still has to start. Only that file is mounted, not its parent tree. |
| `CLAUDE.md`, `settings.json`, `hooks`, `scripts/logs` under the harness (`~/.claude` by default, or `RETRO2_HOME`) when each path exists | read-only | Diagnosis reads these rule, settings, hook, and log files. |
| Diagnostics directory (`/tmp/retro2-diagnostics` by default) when it exists | read-only | Optional diagnostic evidence. |
| One scratch directory outside the home, harness, and project | read-write | Claude needs a writable directory. Codex writes its schema and message file here. |
| Project directory | read-only | Diagnosis reads the project it was pointed at. |

The project mount is the whole project only when that directory does not contain `$HOME`, `~/.claude`, `~/.codex`, the other credential homes, or `RETRO2_HOME`. A project nested in `$HOME`, and a project outside `$HOME`, are in that set.

These layouts do not get the project root as one mount:

- the project is `$HOME`;
- the project contains `$HOME` or `~/.claude`;
- the project contains `RETRO2_HOME`, or is `RETRO2_HOME`.

In those layouts the sandbox mounts only the subtrees that contain none of those directories, plus the harness files named above. `$HOME` itself is not mounted, so a file that merely sits in the home directory is not visible. When the project is the harness directory, the only project files mounted are `CLAUDE.md`, `settings.json`, `hooks`, and `scripts/logs`.

Nothing else from `$HOME`, `RETRO2_HOME`, or the project is mounted. Run directories (`retro-2/runs`), queue state, `projects`, `history`, `todos`, and `memory` are not on this list.

## Claude

| Mount | Mode | Why |
|---|---|---|
| `$CLAUDE_CONFIG_DIR/.credentials.json` | read-only | The Claude CLI reads this file to authenticate. The directory is a random path under the sandbox `/tmp`, not `~/.claude/.credentials.json`. |

`~/.claude/.credentials.json` is not mounted. Claude's `--restricted` file tools are also limited to the working directory and `--add-dir`. Those directories are the project (when it is mounted), the harness root, and the diagnostics directory. The `/tmp` credential directory is not one of them. This repository has not executed the real `claude` binary to confirm that flag.

## Codex

| Mount | Mode | Why |
|---|---|---|
| `$CODEX_HOME/auth.json` | read-only | The Codex CLI reads `$CODEX_HOME/auth.json`. The directory is a random path under the sandbox `/tmp` and must exist before the process starts. `~/.codex/auth.json` is not mounted. |

Codex's read-only sandbox blocks writes, not reads. The model can still read `auth.json` if it reaches the `CODEX_HOME` path, for example from the process environment or by listing `/tmp`. That is the residual exposure. It is not inside the project tree, including when the project directory is `$HOME`. The Claude backend does not mount this file.

## Writes

The scratch directory is the only read-write mount. Project files, harness files, and credential files are read-only. A write anywhere else fails because the path is absent or mounted read-only.
