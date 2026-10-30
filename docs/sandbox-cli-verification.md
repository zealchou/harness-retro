# Real CLI sandbox verification

Claude only. Codex selection is refused and no Codex auth file is copied or mounted.
The process HOME is a throwaway directory. Marker bodies are not printed.
Each probe uses `build_allowlisted_argv` with the same profile, mounts, and
`runtime_argv` as that stage's `claude --version` command. The bwrap argv
through `r2-child` is equal; only the child program differs.

```
$ /var/tmp/cli-prefix/bin/claude --version
2.1.314 (Claude Code)
```

## claude diagnosis --version
profile: diagnosis
child_started=True exit=0
bwrap argv:
```
/usr/bin/bwrap
--die-with-parent
--new-session
--unshare-all
--clearenv
--share-net
--tmpfs
/
--ro-bind
/usr
/usr
--symlink
usr/bin
/bin
--symlink
usr/sbin
/sbin
--symlink
usr/lib
/lib
--symlink
usr/lib64
/lib64
--ro-bind
/etc/resolv.conf
/etc/resolv.conf
--ro-bind
/etc/hosts
/etc/hosts
--ro-bind
/etc/nsswitch.conf
/etc/nsswitch.conf
--ro-bind
/etc/passwd
/etc/passwd
--ro-bind
/etc/group
/etc/group
--symlink
/usr/share/zoneinfo/Etc/UTC
/etc/localtime
--ro-bind
/etc/ssl
/etc/ssl
--ro-bind
/etc/ca-certificates
/etc/ca-certificates
--tmpfs
/tmp
--ro-bind
/var/tmp/retro2-real-cli-j_hosjlz/project
/var/tmp/retro2-real-cli-j_hosjlz/project
--ro-bind
/var/tmp/retro2-real-cli-j_hosjlz/home/.claude/CLAUDE.md
/var/tmp/retro2-real-cli-j_hosjlz/home/.claude/CLAUDE.md
--ro-bind
/var/tmp/retro2-real-cli-j_hosjlz/home/.claude/settings.json
/var/tmp/retro2-real-cli-j_hosjlz/home/.claude/settings.json
--ro-bind
/var/tmp/retro2-real-cli-j_hosjlz/home/.claude/hooks
/var/tmp/retro2-real-cli-j_hosjlz/home/.claude/hooks
--ro-bind
/var/tmp/retro2-real-cli-j_hosjlz/home/.claude/scripts/logs
/var/tmp/retro2-real-cli-j_hosjlz/home/.claude/scripts/logs
--ro-bind
/var/tmp/cli-prefix/lib/node_modules/@anthropic-ai/claude-code
/var/tmp/cli-prefix/lib/node_modules/@anthropic-ai/claude-code
--ro-bind
/exec-daemon/node
/exec-daemon/node
--dir
/tmp/retro2-claude-auth-d36064a5053a5d43
--ro-bind
/var/tmp/retro2-real-cli-j_hosjlz/home/.claude/.credentials.json
/tmp/retro2-claude-auth-d36064a5053a5d43/.credentials.json
--setenv
PATH
/exec-daemon:/usr/bin:/bin
--setenv
HOME
/var/tmp/retro2-real-cli-j_hosjlz/home
--setenv
LANG
C
--setenv
CLAUDE_CONFIG_DIR
/tmp/retro2-claude-auth-d36064a5053a5d43
--setenv
CLAUDE_CREDENTIAL_FILE
/tmp/retro2-claude-auth-d36064a5053a5d43/.credentials.json
--setenv
RETRO2_DIAGNOSIS_SCRATCH
/var/tmp/retro2-real-cli-j_hosjlz/scratch
--bind
/var/tmp/retro2-real-cli-j_hosjlz/scratch
/var/tmp/retro2-real-cli-j_hosjlz/scratch
--proc
/proc
--dev
/dev
--chdir
/var/tmp/retro2-real-cli-j_hosjlz/project
--
/bin/sh
-c
printf "R2_CHILD_STARTED\n"; exec "$@"
r2-child
/var/tmp/cli-prefix/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe
--version
```
stdout:
2.1.314 (Claude Code)
stderr:
(empty)

## claude diagnosis probe
profile: diagnosis
child_started=True exit=0
bwrap argv:
```
/usr/bin/bwrap
--die-with-parent
--new-session
--unshare-all
--clearenv
--share-net
--tmpfs
/
--ro-bind
/usr
/usr
--symlink
usr/bin
/bin
--symlink
usr/sbin
/sbin
--symlink
usr/lib
/lib
--symlink
usr/lib64
/lib64
--ro-bind
/etc/resolv.conf
/etc/resolv.conf
--ro-bind
/etc/hosts
/etc/hosts
--ro-bind
/etc/nsswitch.conf
/etc/nsswitch.conf
--ro-bind
/etc/passwd
/etc/passwd
--ro-bind
/etc/group
/etc/group
--symlink
/usr/share/zoneinfo/Etc/UTC
/etc/localtime
--ro-bind
/etc/ssl
/etc/ssl
--ro-bind
/etc/ca-certificates
/etc/ca-certificates
--tmpfs
/tmp
--ro-bind
/var/tmp/retro2-real-cli-j_hosjlz/project
/var/tmp/retro2-real-cli-j_hosjlz/project
--ro-bind
/var/tmp/retro2-real-cli-j_hosjlz/home/.claude/CLAUDE.md
/var/tmp/retro2-real-cli-j_hosjlz/home/.claude/CLAUDE.md
--ro-bind
/var/tmp/retro2-real-cli-j_hosjlz/home/.claude/settings.json
/var/tmp/retro2-real-cli-j_hosjlz/home/.claude/settings.json
--ro-bind
/var/tmp/retro2-real-cli-j_hosjlz/home/.claude/hooks
/var/tmp/retro2-real-cli-j_hosjlz/home/.claude/hooks
--ro-bind
/var/tmp/retro2-real-cli-j_hosjlz/home/.claude/scripts/logs
/var/tmp/retro2-real-cli-j_hosjlz/home/.claude/scripts/logs
--ro-bind
/var/tmp/cli-prefix/lib/node_modules/@anthropic-ai/claude-code
/var/tmp/cli-prefix/lib/node_modules/@anthropic-ai/claude-code
--ro-bind
/exec-daemon/node
/exec-daemon/node
--dir
/tmp/retro2-claude-auth-d36064a5053a5d43
--ro-bind
/var/tmp/retro2-real-cli-j_hosjlz/home/.claude/.credentials.json
/tmp/retro2-claude-auth-d36064a5053a5d43/.credentials.json
--setenv
PATH
/exec-daemon:/usr/bin:/bin
--setenv
HOME
/var/tmp/retro2-real-cli-j_hosjlz/home
--setenv
LANG
C
--setenv
CLAUDE_CONFIG_DIR
/tmp/retro2-claude-auth-d36064a5053a5d43
--setenv
CLAUDE_CREDENTIAL_FILE
/tmp/retro2-claude-auth-d36064a5053a5d43/.credentials.json
--setenv
RETRO2_DIAGNOSIS_SCRATCH
/var/tmp/retro2-real-cli-j_hosjlz/scratch
--bind
/var/tmp/retro2-real-cli-j_hosjlz/scratch
/var/tmp/retro2-real-cli-j_hosjlz/scratch
--proc
/proc
--dev
/dev
--chdir
/var/tmp/retro2-real-cli-j_hosjlz/project
--
/bin/sh
-c
printf "R2_CHILD_STARTED\n"; exec "$@"
r2-child
/usr/bin/python3
-c
import json, os
from pathlib import Path
home = Path(os.environ["HOME"])
link = Path("escape")
try:
    leaked = link.read_text(encoding="utf-8")
except OSError:
    leaked = ""
tmp_auth = Path("/tmp/retro2-fake-auth-ba8bb33124fe")
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
```
stdout:
PROBE {"bash_history": false, "claude_cred": false, "codex_auth": false, "codex_home": false, "home_canary": false, "hooks_escape": false, "repo_symlink": false, "ssh": false, "ssh_sock": false, "tmp_auth": false}
stderr:
(empty)

## claude candidate --version
profile: candidate
child_started=True exit=0
bwrap argv:
```
/usr/bin/bwrap
--die-with-parent
--new-session
--unshare-all
--clearenv
--share-net
--tmpfs
/
--ro-bind
/usr
/usr
--symlink
usr/bin
/bin
--symlink
usr/sbin
/sbin
--symlink
usr/lib
/lib
--symlink
usr/lib64
/lib64
--ro-bind
/etc/resolv.conf
/etc/resolv.conf
--ro-bind
/etc/hosts
/etc/hosts
--ro-bind
/etc/nsswitch.conf
/etc/nsswitch.conf
--ro-bind
/etc/passwd
/etc/passwd
--ro-bind
/etc/group
/etc/group
--symlink
/usr/share/zoneinfo/Etc/UTC
/etc/localtime
--ro-bind
/etc/ssl
/etc/ssl
--ro-bind
/etc/ca-certificates
/etc/ca-certificates
--tmpfs
/tmp
--ro-bind
/var/tmp/cli-prefix/lib/node_modules/@anthropic-ai/claude-code
/var/tmp/cli-prefix/lib/node_modules/@anthropic-ai/claude-code
--ro-bind
/exec-daemon/node
/exec-daemon/node
--dir
/tmp/retro2-claude-auth-d36064a5053a5d43
--ro-bind
/var/tmp/retro2-real-cli-j_hosjlz/home/.claude/.credentials.json
/tmp/retro2-claude-auth-d36064a5053a5d43/.credentials.json
--setenv
PATH
/exec-daemon:/usr/bin:/bin
--setenv
HOME
/var/tmp/retro2-real-cli-j_hosjlz/home
--setenv
LANG
C
--setenv
CLAUDE_CONFIG_DIR
/tmp/retro2-claude-auth-d36064a5053a5d43
--setenv
CLAUDE_CREDENTIAL_FILE
/tmp/retro2-claude-auth-d36064a5053a5d43/.credentials.json
--setenv
RETRO2_DIAGNOSIS_SCRATCH
/var/tmp/retro2-real-cli-j_hosjlz/worktree
--bind
/var/tmp/retro2-real-cli-j_hosjlz/worktree
/var/tmp/retro2-real-cli-j_hosjlz/worktree
--proc
/proc
--dev
/dev
--chdir
/var/tmp/retro2-real-cli-j_hosjlz/worktree
--
/bin/sh
-c
printf "R2_CHILD_STARTED\n"; exec "$@"
r2-child
/var/tmp/cli-prefix/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe
--version
```
stdout:
2.1.314 (Claude Code)
stderr:
(empty)

## claude candidate probe
profile: candidate
child_started=True exit=0
bwrap argv:
```
/usr/bin/bwrap
--die-with-parent
--new-session
--unshare-all
--clearenv
--share-net
--tmpfs
/
--ro-bind
/usr
/usr
--symlink
usr/bin
/bin
--symlink
usr/sbin
/sbin
--symlink
usr/lib
/lib
--symlink
usr/lib64
/lib64
--ro-bind
/etc/resolv.conf
/etc/resolv.conf
--ro-bind
/etc/hosts
/etc/hosts
--ro-bind
/etc/nsswitch.conf
/etc/nsswitch.conf
--ro-bind
/etc/passwd
/etc/passwd
--ro-bind
/etc/group
/etc/group
--symlink
/usr/share/zoneinfo/Etc/UTC
/etc/localtime
--ro-bind
/etc/ssl
/etc/ssl
--ro-bind
/etc/ca-certificates
/etc/ca-certificates
--tmpfs
/tmp
--ro-bind
/var/tmp/cli-prefix/lib/node_modules/@anthropic-ai/claude-code
/var/tmp/cli-prefix/lib/node_modules/@anthropic-ai/claude-code
--ro-bind
/exec-daemon/node
/exec-daemon/node
--dir
/tmp/retro2-claude-auth-d36064a5053a5d43
--ro-bind
/var/tmp/retro2-real-cli-j_hosjlz/home/.claude/.credentials.json
/tmp/retro2-claude-auth-d36064a5053a5d43/.credentials.json
--setenv
PATH
/exec-daemon:/usr/bin:/bin
--setenv
HOME
/var/tmp/retro2-real-cli-j_hosjlz/home
--setenv
LANG
C
--setenv
CLAUDE_CONFIG_DIR
/tmp/retro2-claude-auth-d36064a5053a5d43
--setenv
CLAUDE_CREDENTIAL_FILE
/tmp/retro2-claude-auth-d36064a5053a5d43/.credentials.json
--setenv
RETRO2_DIAGNOSIS_SCRATCH
/var/tmp/retro2-real-cli-j_hosjlz/worktree
--bind
/var/tmp/retro2-real-cli-j_hosjlz/worktree
/var/tmp/retro2-real-cli-j_hosjlz/worktree
--proc
/proc
--dev
/dev
--chdir
/var/tmp/retro2-real-cli-j_hosjlz/worktree
--
/bin/sh
-c
printf "R2_CHILD_STARTED\n"; exec "$@"
r2-child
/usr/bin/python3
-c
import json, os
from pathlib import Path
home = Path(os.environ["HOME"])
link = Path("escape")
try:
    leaked = link.read_text(encoding="utf-8")
except OSError:
    leaked = ""
tmp_auth = Path("/tmp/retro2-fake-auth-ba8bb33124fe")
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
```
stdout:
PROBE {"bash_history": false, "claude_cred": false, "codex_auth": false, "codex_home": false, "home_canary": false, "hooks_escape": false, "repo_symlink": false, "ssh": false, "ssh_sock": false, "tmp_auth": false}
stderr:
(empty)

## claude apply --version
profile: apply
child_started=True exit=0
bwrap argv:
```
/usr/bin/bwrap
--die-with-parent
--new-session
--unshare-all
--clearenv
--tmpfs
/
--ro-bind
/usr
/usr
--symlink
usr/bin
/bin
--symlink
usr/sbin
/sbin
--symlink
usr/lib
/lib
--symlink
usr/lib64
/lib64
--ro-bind
/etc/resolv.conf
/etc/resolv.conf
--ro-bind
/etc/hosts
/etc/hosts
--ro-bind
/etc/nsswitch.conf
/etc/nsswitch.conf
--ro-bind
/etc/passwd
/etc/passwd
--ro-bind
/etc/group
/etc/group
--symlink
/usr/share/zoneinfo/Etc/UTC
/etc/localtime
--ro-bind
/etc/ssl
/etc/ssl
--ro-bind
/etc/ca-certificates
/etc/ca-certificates
--tmpfs
/tmp
--ro-bind
/var/tmp/retro2-real-cli-j_hosjlz/project
/var/tmp/retro2-real-cli-j_hosjlz/project
--ro-bind
/var/tmp/cli-prefix/lib/node_modules/@anthropic-ai/claude-code
/var/tmp/cli-prefix/lib/node_modules/@anthropic-ai/claude-code
--ro-bind
/exec-daemon/node
/exec-daemon/node
--setenv
PATH
/exec-daemon:/usr/bin:/bin
--setenv
HOME
/var/tmp/retro2-real-cli-j_hosjlz/home
--setenv
LANG
C
--bind
/var/tmp/retro2-real-cli-j_hosjlz/scratch
/var/tmp/retro2-real-cli-j_hosjlz/scratch
--proc
/proc
--dev
/dev
--chdir
/var/tmp/retro2-real-cli-j_hosjlz/project
--
/bin/sh
-c
printf "R2_CHILD_STARTED\n"; exec "$@"
r2-child
/var/tmp/cli-prefix/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe
--version
```
stdout:
2.1.314 (Claude Code)
stderr:
(empty)

## claude apply probe
profile: apply
child_started=True exit=0
bwrap argv:
```
/usr/bin/bwrap
--die-with-parent
--new-session
--unshare-all
--clearenv
--tmpfs
/
--ro-bind
/usr
/usr
--symlink
usr/bin
/bin
--symlink
usr/sbin
/sbin
--symlink
usr/lib
/lib
--symlink
usr/lib64
/lib64
--ro-bind
/etc/resolv.conf
/etc/resolv.conf
--ro-bind
/etc/hosts
/etc/hosts
--ro-bind
/etc/nsswitch.conf
/etc/nsswitch.conf
--ro-bind
/etc/passwd
/etc/passwd
--ro-bind
/etc/group
/etc/group
--symlink
/usr/share/zoneinfo/Etc/UTC
/etc/localtime
--ro-bind
/etc/ssl
/etc/ssl
--ro-bind
/etc/ca-certificates
/etc/ca-certificates
--tmpfs
/tmp
--ro-bind
/var/tmp/retro2-real-cli-j_hosjlz/project
/var/tmp/retro2-real-cli-j_hosjlz/project
--ro-bind
/var/tmp/cli-prefix/lib/node_modules/@anthropic-ai/claude-code
/var/tmp/cli-prefix/lib/node_modules/@anthropic-ai/claude-code
--ro-bind
/exec-daemon/node
/exec-daemon/node
--setenv
PATH
/exec-daemon:/usr/bin:/bin
--setenv
HOME
/var/tmp/retro2-real-cli-j_hosjlz/home
--setenv
LANG
C
--bind
/var/tmp/retro2-real-cli-j_hosjlz/scratch
/var/tmp/retro2-real-cli-j_hosjlz/scratch
--proc
/proc
--dev
/dev
--chdir
/var/tmp/retro2-real-cli-j_hosjlz/project
--
/bin/sh
-c
printf "R2_CHILD_STARTED\n"; exec "$@"
r2-child
/usr/bin/python3
-c
import json, os
from pathlib import Path
home = Path(os.environ["HOME"])
link = Path("escape")
try:
    leaked = link.read_text(encoding="utf-8")
except OSError:
    leaked = ""
tmp_auth = Path("/tmp/retro2-fake-auth-ba8bb33124fe")
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
```
stdout:
PROBE {"bash_history": false, "claude_cred": false, "codex_auth": false, "codex_home": false, "home_canary": false, "hooks_escape": false, "repo_symlink": false, "ssh": false, "ssh_sock": false, "tmp_auth": false}
stderr:
(empty)

## Real Claude Read tool

`claude auth status` exit=1 loggedIn=False authMethod=none.
No credential file body is recorded.

SKIPPED: no Claude login is available in this VM. The Read tool was not invoked, and this result is not a pass.

## Historical record

This section is a static record of one logged-in run. The test does not rewrite it.

Executed on a logged-in machine at `f942d1b2` (Claude Code 2.1.314, bubblewrap 0.12.0), inside `run_allowlisted` profile `diagnosis`, with `--model haiku` and `--max-budget-usd 1.00`. Session exit 0. No credential body and no canary (`SYNTHETIC-*`, `CANARY-HOME`) appeared in any tool result.

| Check | Expected | Actual | Verdict |
|---|---|---|---|
| Allow-listed project file | READ_OK | READ_OK | PASS |
| Host `~/.claude/.credentials.json` | READ_FAIL | File does not exist | PASS |
| Host `~/.ssh` | READ_FAIL | File does not exist | PASS |
| Synthetic `id_ed25519` in the throwaway HOME | READ_FAIL | File does not exist | PASS |
| Host `~/.codex/auth.json` | READ_FAIL | File does not exist | PASS |
| Repo symlink into HOME | READ_FAIL | File does not exist | PASS |

The repo test without `--model` failed on that machine because the default model exceeded the $1 budget. This branch pins `--model haiku` and keeps the $1 cap. The relocated Claude credential under `/tmp` was not probed.
