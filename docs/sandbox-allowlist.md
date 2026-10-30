# Sandbox allow-list

Diagnosis, candidate generation, and apply do not start from the host filesystem and then hide secrets. Each stage's bubblewrap root is an empty tmpfs. A path is visible only when this list mounts it. Codex is disabled until a host-side credential broker ships. Selecting it fails closed, and no Codex `auth.json` is copied or mounted.

## Which stage uses which sandbox

| Stage | Claude |
|---|---|
| Diagnosis | `run_allowlisted` profile `diagnosis` (this page). |
| Candidate generation | `run_allowlisted` profile `candidate`. The worktree is read-write. Harness files are not mounted; their text is already in the prompt. The Claude credential is the relocated `/tmp` file. |
| Apply / test commands | `run_allowlisted` profile `apply`. The subject is read-only, the scratch directory is read-write, network is off, and no model CLI and no credential file are mounted. |

There is no Codex column. `RETRO2_MODEL_BACKEND=codex` raises `Codex backend is disabled until the host-side credential broker ships` before a sandbox is built.

## Both backends

| Mount | Mode | Why |
|---|---|---|
| `/usr`, and `/bin`, `/sbin`, `/lib`, `/lib64` when those are real directories | read-only | The Claude binary, its loader, and libraries. A symlink such as `/bin` → `usr/bin` is recreated with `--symlink` and is not bind-mounted from its target. |
| `/etc/resolv.conf`, `/etc/hosts`, `/etc/nsswitch.conf`, `/etc/passwd`, `/etc/group`, `/etc/localtime`, `/etc/ssl`, `/etc/ca-certificates` when present | read-only | Name service and TLS material so the CLI can reach the model endpoint. |
| `/proc`, `/dev` | runtime | Process and device nodes bubblewrap gives the CLI. |
| Claude's npm package directory, plus a `node` binary that lives outside `/usr` | read-only | The global `claude` bin is a symlink. The sandbox follows it once, mounts that package directory when every component is a real path, and executes the real file inside it. `~/.nvm` and `$HOME` are never mounted for this. A checker script outside `/usr` is mounted as that file only. |
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

Every allow-listed path is checked again immediately before that stage's spawn, because an earlier stage can plant a symlink in the worktree. `lstat` walks every component. A symlink, including `hooks` → `$HOME`, is refused (`allowlisted path is a symlink`) and is not mounted. The mount source is that validated path, not the output of `resolve()`. A source equal to `$HOME`, `~/.ssh`, `~/.claude`, `~/.codex`, or `RETRO2_HOME`, a source that contains one of those directories, or a source inside one of them, is refused. The exception is the named harness files above. The check and bubblewrap's mount are not atomic. This assumes no concurrent mutation of those paths in between.

## Claude

| Mount | Mode | Why |
|---|---|---|
| `$CLAUDE_CONFIG_DIR/.credentials.json` | read-only | The Claude CLI reads this file to authenticate. The directory is a random path under the sandbox `/tmp`, not `~/.claude/.credentials.json`. |

`~/.claude/.credentials.json` is not mounted. Claude's `--restricted` file tools are also limited to the working directory and `--add-dir`. Those directories are the project (when it is mounted), the harness root, and the diagnostics directory. The `/tmp` credential directory is not one of them. This repository has not executed the real `claude` binary to confirm that flag.

## Codex

Codex is disabled until a host-side credential broker ships. Selecting `RETRO2_MODEL_BACKEND=codex` fails closed with `Codex backend is disabled until the host-side credential broker ships; see docs/sandbox-allowlist.md#follow-up-credential-broker`. The sandbox does not copy or mount `~/.codex/auth.json`, and it does not set `CODEX_HOME`.

## Follow-up: credential broker

Out of scope for this change: a host-side credential broker that keeps the real token outside the sandbox and relays a dummy token over a Unix socket, and seccomp or `openat2` hardening. Codex stays disabled until that broker ships.

## `/tmp` and the credential directory

`--tmpfs /tmp` hides the host's `/tmp`. Files that were already there, including a fake `*auth*` file, are not in the sandbox. The fresh tmpfs is writable empty space, so creating a new file there does not reveal host files. On it the sandbox creates one directory with a random name and bind-mounts the Claude login file read-only at `$CLAUDE_CONFIG_DIR/.credentials.json`. That file is not a path under `$HOME` or the project. The child environment is an allow-list (`--clearenv` plus `--setenv`). `SSH_AUTH_SOCK` and other agent sockets are not copied. Inherited file descriptors are closed.

## Writes

The scratch directory, and for candidate generation the worktree, are read-write. Allowlisted project files, harness files, and the login file are read-only, so writing them fails. The private `/tmp` is a writable tmpfs; creating a new file there does not reveal host files. A path that was not mounted does not exist, so opening it fails.
