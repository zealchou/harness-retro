# Real CLI sandbox verification

Claude only. Codex selection is refused and no Codex auth file is copied or mounted.
The process HOME is a throwaway directory. Marker bodies are not printed.

```
$ /var/tmp/cli-prefix/bin/claude --version
2.1.314 (Claude Code)
```

## claude diagnosis --version
profile: diagnosis
child_started=True exit=0
stdout:
2.1.314 (Claude Code)
stderr:
(empty)

## claude diagnosis probe
profile: diagnosis
child_started=True exit=0
stdout:
PROBE {"bash_history": false, "claude_cred": false, "codex_auth": false, "codex_home": false, "home_canary": false, "hooks_escape": false, "repo_symlink": false, "ssh": false, "ssh_sock": false, "tmp_auth": false}
stderr:
(empty)

## claude candidate --version
profile: candidate
child_started=True exit=0
stdout:
2.1.314 (Claude Code)
stderr:
(empty)

## claude candidate probe
profile: candidate
child_started=True exit=0
stdout:
PROBE {"bash_history": false, "claude_cred": false, "codex_auth": false, "codex_home": false, "home_canary": false, "hooks_escape": false, "repo_symlink": false, "ssh": false, "ssh_sock": false, "tmp_auth": false}
stderr:
(empty)

## claude apply --version
profile: apply
child_started=True exit=0
stdout:
2.1.314 (Claude Code)
stderr:
(empty)

## claude apply probe
profile: apply
child_started=True exit=0
stdout:
PROBE {"bash_history": false, "claude_cred": false, "codex_auth": false, "codex_home": false, "home_canary": false, "hooks_escape": false, "repo_symlink": false, "ssh": false, "ssh_sock": false, "tmp_auth": false}
stderr:
(empty)

