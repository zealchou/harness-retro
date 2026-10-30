# Key broker

## Decisions for the chairman

The chairman decides these. Nothing in this design is implemented, and PR #9 does not merge until the broker lands with it.

1. Claude: subscription login or API key?
2. Codex: ChatGPT login or API key?
3. Grant veth admin rights, or use a Unix socket instead?
4. Turn off all of Claude's nonessential background traffic?
5. Broker as a long-running service, or one per task?

## Plain language

A sandbox is a restricted bubble. The `claude` or `codex` command runs inside it and can see only the files we explicitly put there.

A credential is the real login secret: the Claude or Codex password-equivalent that proves who is paying for the model. Today a copy of that file is still placed inside the bubble, which is the residual risk this design removes.

A broker is a small program that stays outside the bubble, on the host. It is the only place that file lives. The bubble never receives it.

A ticket is a random one-time pass for a single run. It is not the login secret. The command inside the bubble shows the ticket to the broker. The broker checks the ticket, attaches the real login, and forwards the request to the model company. When the run ends, that ticket stops working.

The bubble also has no road to the public internet. The only program it can talk to is the broker. Who the broker logs in as, and whether we build the private network with extra admin rights or with a socket file, are chairman decisions 1–3 above.

This is a design only. It is not implemented.

## What changes

Today, diagnosis and candidate generation bind the Claude login file read-only at `$CLAUDE_CONFIG_DIR/.credentials.json` on the sandbox `/tmp`. The model can read that file. Codex is disabled because its `auth.json` would have the same problem.

After the broker:

- No credential file is mounted in any sandbox.
- The child environment has no API key, no refresh token, and no `CLAUDE_CONFIG_DIR` pointing at a real login file.
- The CLI is given one per-run ticket and a base URL that points at the broker.
- The sandbox has no route to the public internet. The only network path is to the broker. That limit comes from the network namespace and the veth or Unix-socket layout, not from seccomp.
- If the broker is down, the ticket is wrong, or the network setup fails, the stage stops. It does not mount the credential file and it does not open the public network.

## Threat model

The sandboxed model can read and write only what the allow-list mounts, and it can send HTTP only to the broker.

| It can | It cannot |
|---|---|
| Read the project or worktree that the stage mounted | Read `~/.claude/.credentials.json`, `~/.codex/auth.json`, or `~/.ssh` on the host |
| Write its scratch directory or candidate test worktree | Read a credential file under the sandbox `/tmp`, because none is mounted |
| Send model-API HTTP to the broker, using the per-run ticket | Open a TCP connection to `api.anthropic.com`, `api.openai.com`, `chatgpt.com`, or any other host |
| See the ticket in its environment | See the refresh token, the API key, or the OAuth access token |
| See its own process in its private `/proc` | See the broker process, ptrace it, or read its memory or token |

`/tmp` inside the sandbox stays a private tmpfs. Creating a file there does not reveal host files and does not survive the process. The broker does not put a login file on that tmpfs.

The environment is still an allow-list (`--clearenv` and `--setenv`). `SSH_AUTH_SOCK` stays out. The ticket is the only new value, and it is not the real credential. The broker's pid is not placed in the child environment.

A symlink inside the project still does not expose `$HOME`, because `$HOME` is not mounted. That does not change.

### Process isolation

The broker is a different process from the sandbox. The sandboxed CLI must not be able to see it, attach to it, or read the token out of its memory.

`bwrap --unshare-all` already includes `--unshare-pid` and `--unshare-user-try`. The broker design keeps that:

- **PID namespace.** The sandbox is in its own PID namespace. A process can ptrace only processes in its own PID namespace or a descendant namespace, not processes in the parent namespace. The broker stays in the host PID namespace, so the sandbox cannot ptrace or attach to it. See [ptrace(2)](https://man7.org/linux/man-pages/man2/ptrace.2.html) (PID namespace restriction).
- **Private `/proc`.** The sandbox gets bubblewrap's `--proc` mount, a procfs for that PID namespace. `/proc/<broker-pid>/mem` and `/proc/<broker-pid>/environ` are not there, because the broker's pid is not a pid in that namespace. Guessing the host pid number does not make those files appear.
- **User namespace and uid.** The sandbox's user namespace means its root is not host root. The broker runs as a different host uid from the sandboxed process. Even if a future bug shared a PID namespace, that uid split plus the kernel Yama `ptrace_scope` (a process may ptrace only its descendants when the scope is 1) would still deny `ptrace` and `/proc/<pid>/mem`. The token stays in the broker's address space.
- **No shared credentials in memory maps.** The sandbox does not receive the broker's file descriptors. The ticket in the child environment is the random pass, not the OAuth token or API key.

## Pointing the CLIs at the broker

The broker speaks the vendor's HTTP API on a private address, for example `http://10.88.0.1:8080`. The sandbox can reach only that address. The CLI never receives the real token.

### Claude Code

Official environment variables, from [Claude Code environment variables](https://code.claude.com/docs/en/env-vars) and [Connect Claude Code to an LLM gateway](https://code.claude.com/docs/en/llm-gateway-connect):

- `ANTHROPIC_BASE_URL` overrides the API endpoint so requests go through a gateway.
- `ANTHROPIC_AUTH_TOKEN` is sent as `Authorization: Bearer <value>`.
- `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`, set to any non-empty value such as `1`, disables nonessential network traffic: auto-updates, telemetry, error reporting, feedback, release notes, status checks, and feature-flag fetching. Unset it to allow that traffic again. The same page documents that setting it to `0` or `false` still disables the traffic.

The child gets:

```text
ANTHROPIC_BASE_URL=http://10.88.0.1:8080
ANTHROPIC_AUTH_TOKEN=<per-run ticket>
CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
```

`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` is the proposed default. Whether to keep it is chairman decision 4.

It does not get `ANTHROPIC_API_KEY`. It does not get a `CLAUDE_CONFIG_DIR` that contains `.credentials.json`. Proxy variables are documented in [Enterprise network configuration](https://code.claude.com/docs/en/network-config) (`HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY`). This design does not use them. A proxy would be a second hop. The base URL is enough if the only route is the broker.

Claude Code also calls hosts other than the model API. With the variable above, the documented nonessential calls are turned off. Anything still attempted fails because those hosts are not routed. Whether the CLI still completes a normal diagnosis is chairman decision 4, with the official variable as the switch.

### Codex

Codex reads provider settings from `config.toml`, not from a mounted `auth.json`, when a custom provider is selected. From the [Codex configuration reference](https://developers.openai.com/codex/config-reference) and the provider struct in [codex-rs/model-provider-info/src/lib.rs](https://github.com/openai/codex/blob/main/codex-rs/model-provider-info/src/lib.rs):

- `model_providers.<id>.base_url` is the API base URL.
- `model_providers.<id>.env_key` names an environment variable that holds the API key.
- `requires_openai_auth` defaults to false. When false, Codex does not ask for ChatGPT login and does not need `~/.codex/auth.json`. The key comes from `env_key`.
- `wire_api` stays `responses`.
- `experimental_bearer_token` in the config file is discouraged. Do not put a token in the file.

For one run the host writes a tiny config into the writable scratch, not into `~/.codex`:

```toml
model_provider = "retro"
model = "gpt-5.4"

[model_providers.retro]
name = "retro broker"
base_url = "http://10.88.0.1:8080/v1"
env_key = "RETRO2_BROKER_TOKEN"
wire_api = "responses"
requires_openai_auth = false
```

The child gets `CODEX_HOME` pointed at that scratch directory (empty except this config) and `RETRO2_BROKER_TOKEN` set to the per-run ticket. `~/.codex` is not mounted.

Holding a ChatGPT login inside the broker has no official support. See unverified item U2. The API-key path above is the one the config reference describes.

## Token refresh

The host broker is the only process that reads the real credential:

- Claude subscription: the OAuth access token and refresh token in the host `~/.claude/.credentials.json`. This route is not officially supported. See U1.
- Claude API key: `ANTHROPIC_API_KEY` in the host environment. This is the officially supported gateway path (`ANTHROPIC_BASE_URL` plus a bearer or API key).
- Codex API key: an environment variable named by `env_key`, held by the broker, not by the child.
- Codex ChatGPT login: the host `~/.codex/auth.json` access token and refresh token. This route has no official support. See U2.

The broker refreshes an access token when the vendor says it is expired or near expiry. The refresh token never enters the sandbox, the child environment, or the broker's HTTP response. If refresh fails, the broker returns an error to the CLI and the stage fails. It does not fall back to mounting the file.

## Per-run ticket and revocation

For each model stage the host creates a random ticket, stores it in broker memory with the run id and the backend, and puts only that ticket in the child. The broker accepts a request only when the ticket matches an open run.

The ticket is dropped when the stage process exits, when the run is cancelled, or after the stage timeout. A later request with the same ticket gets 401 and is not forwarded. Tickets are not written to the runs directory.

## Network isolation

`bwrap --share-net` is removed for model stages. The sandbox is started with `--unshare-net`, which gives it no host network. Who the sandbox can reach is decided by that namespace and by what we connect to it. Seccomp is not used to inspect destination addresses. A seccomp BPF program cannot dereference pointer arguments, so it cannot read the `sockaddr` passed to `connect`. The kernel documents that limit in [Seccomp BPF (SECure COMPuting with filters)](https://docs.kernel.org/userspace-api/seccomp_filter.html): BPF programs may not dereference pointers, which keeps filters to the register values in `seccomp_data` and avoids time-of-check-time-of-use races. `connect`'s address is a pointer. See also [seccomp(2)](https://man7.org/linux/man-pages/man2/seccomp.2.html).

A veth pair is one concrete path, because both CLIs speak HTTP to a base URL:

1. Create a veth pair. One end stays on the host and is addressed `10.88.0.1`. The other end is moved into the sandbox namespace and addressed `10.88.0.2`.
2. The broker listens on `10.88.0.1:8080` and nowhere else.
3. The sandbox default route is `10.88.0.1`. There is no DNS server and no route to the public internet.
4. The broker, on the host, is the only process that opens connections to `api.anthropic.com`, `api.openai.com`, or `chatgpt.com/backend-api/codex`.

Creating that veth in the host network namespace requires `CAP_NET_ADMIN` in the initial user namespace. In practice that is root, or a helper with file capability `CAP_NET_ADMIN`. Bubblewrap's `--unshare-user` gives the sandbox `CAP_NET_ADMIN` only inside its own empty network namespace. That is enough to bring up loopback in the sandbox. It is not enough to insert one end of a veth into the host namespace. Whether a non-root user on our machines can do the host half is untested. See U3. Chairman decision 3 chooses veth or the socket below.

A Unix socket needs no `CAP_NET_ADMIN`. The broker listens on a socket file. Bubblewrap bind-mounts that one socket into the sandbox and no other host socket. The CLIs' documented base URL is TCP, so a small forwarder inside the sandbox listens on sandbox loopback and forwards bytes to the mounted socket. The forwarder does not hold a credential. The sandbox still has `--unshare-net`, so loopback in the sandbox is not the host's loopback, and there is still no route to the public internet. Do not build both the veth and the socket forwarder. The chairman picks one.

`/tmp` is unchanged: a private tmpfs, with no credential file on it.

## seccomp

Seccomp is syscall-level hardening only. It does not enforce which host the sandbox may contact. That is the network namespace and the veth or Unix-socket layout above.

Bubblewrap can load a filter. From [bwrap(1)](https://manpages.ubuntu.com/manpages/resolute/man1/bwrap.1.html), `--add-seccomp-fd FD` reads a compiled classic BPF program, as produced by `seccomp_export_bpf`, from that file descriptor. The option can be repeated. Filters are installed in the order given, and the kernel evaluates them in reverse order, so the last program on the command line runs first. Every filter except possibly the last must allow `prctl(PR_SET_SECCOMP)`. `--add-seccomp-fd` cannot be combined with `--seccomp`. The host installs the filter with `PR_SET_NO_NEW_PRIVS` already set, which bubblewrap does for an unprivileged sandbox. See [seccomp(2)](https://man7.org/linux/man-pages/man2/seccomp.2.html).

The filter we would load is small:

- Deny `ptrace` so the sandbox cannot attach to anything, including a process that later shared a namespace by mistake.
- Deny `AF_UNIX` `socket` creation except the one broker socket, when the Unix-socket layout is chosen. On the veth layout, deny `AF_UNIX` `socket` outright so the child cannot open `SSH_AUTH_SOCK` or another host socket.
- Do not try to allow `connect` only for `10.88.0.1:8080`. The filter cannot see that address.

This is not a full syscall allow-list. File access stays the job of the mount list. Cutting off the public internet stays the job of `--unshare-net` plus the veth or socket.

## Turning Codex back on

Codex stays disabled until all of the following are true:

1. The broker is running and the stage refuses to start when it is not.
2. A Codex run uses the generated `CODEX_HOME` config above and does not mount `~/.codex/auth.json`.
3. The real-CLI checks below pass for Codex: the CLI reaches the model, and a probe in the same sandbox cannot read the host `auth.json`, `~/.ssh`, or the HOME canary.
4. `RETRO2_MODEL_BACKEND=codex` then stops raising "Codex backend is disabled…".

Until that pass, the current fail-closed error stays. Re-enabling Codex also waits on chairman decision 2, because a ChatGPT login in the broker is unverified (U2) while an API key matches the documented `env_key` path.

## Failure modes

| What goes wrong | What the stage does |
|---|---|
| Broker is not listening | Stop before the model CLI. Do not mount a credential file. Do not use `--share-net`. |
| veth, Unix-socket mount, or seccomp setup fails | `HostExitError`. No unconfined child. |
| Ticket missing or already revoked | Broker returns 401 and does not forward. The stage fails. |
| Refresh fails | Broker returns an error. The refresh token is not sent to the child. |
| CLI tries any host except the broker | The network namespace has no route there, so the connection fails. The run does not retry on the public network. |
| Operator points `ANTHROPIC_BASE_URL` at a public host | The child env is set by the allow-list, not copied from the operator. The sandbox still has no route to that host. |

## Test plan

Use a throwaway `HOME` and synthetic markers. Do not print credential bodies. Record exit codes, tool verdicts, and the bwrap argv.

1. Unit: a credential path under `~/.claude` or `~/.codex` is not a `--ro-bind` source. `CLAUDE_CONFIG_DIR` is unset or points at an empty directory.
2. Unit: child env contains the ticket and the broker base URL, and does not contain `ANTHROPIC_API_KEY` or a refresh token.
3. Unit: after the stage exits, the broker rejects the ticket.
4. Real CLI, Claude, logged-in machine: `claude -p --model haiku` through the broker completes one short prompt. A Python probe with the same mounts cannot read the host credential, `~/.ssh`, `~/.codex/auth.json`, or a symlink into `$HOME`. `connect` to `1.1.1.1:443` fails because the network namespace has no route, not because a seccomp filter read the address.
5. Real CLI, Claude Read tool: an allow-listed file is readable. The host credential path, `~/.ssh`, and a HOME symlink are not. Confirm the relocated `/tmp/.../.credentials.json` path is absent.
6. Same sandbox: `/proc` does not list the broker pid, and opening that pid's `mem` or `environ` fails.
7. Real CLI, Codex, after re-enable: one `codex exec` prompt through the broker. The same probe cannot read `~/.codex/auth.json`.
8. Fail closed: stop the broker and confirm the stage errors without mounting a credential file.

The logged-in Read run at `f942d1b2` stays the record of the current mount behavior. It is not a broker test.

## Rollout

PR #9 does not merge by itself. The broker implementation is the next change on this line of work, and the two merge together.

1. Add the broker and the chairman's chosen network path (veth or Unix socket) behind the existing allow-list. Delete the credential `--ro-bind` in the same change. Load the seccomp program with `--add-seccomp-fd`.
2. Run the Claude real-CLI plan above on a logged-in machine.
3. Re-enable Codex only after its real-CLI plan passes and the chairman has chosen the Codex account type.
4. Update `docs/sandbox-allowlist.md`, both READMEs, and `docs/safety-model.md` so they no longer say the login file is mounted.
5. Merge that change with PR #9. Do not merge earlier.

## Open questions

1. Which Claude login should the broker hold: the subscription OAuth file, an `ANTHROPIC_API_KEY`, or both? **Chairman decides** (account and policy). The API key is the documented path. The subscription file is U1.
2. Which Codex login should it hold: ChatGPT OAuth in `auth.json`, or an API key? **Chairman decides** (account and policy). The API key matches `env_key`. The ChatGPT login is U2.
3. Is a veth pair acceptable on the machines that run harness-retro, or is a Unix socket required? **Chairman decides** (policy). Creating a veth needs `CAP_NET_ADMIN` on the host. A Unix socket does not. Whether a non-root user can create the veth here is U3. Engineering reports that result. The chairman chooses.
4. Claude Code calls more than the model API. Which of those calls, if any, must succeed for diagnosis to be usable? **Chairman decides** (policy). The official switch is `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` ([environment variables](https://code.claude.com/docs/en/env-vars)). Setting it disables auto-updates, telemetry, error reporting, feedback, release notes, status checks, and feature-flag fetching.
5. Should one broker process serve every run, or should each run start a short-lived broker? **Chairman decides** (policy). Either way the ticket is revoked when the stage exits, and the broker stays outside the sandbox PID namespace.

## Unverified / policy risk

- **U1.** Having the broker hold a Claude subscription login (`~/.claude/.credentials.json`) has no official support. Anthropic's terms restrict transferring Claude.ai credentials. The supported route is an API key with `ANTHROPIC_BASE_URL`, documented in [Connect Claude Code to an LLM gateway](https://code.claude.com/docs/en/llm-gateway-connect).
- **U2.** Having the broker hold Codex's ChatGPT login has no official support and no support in the provider source cited above. That source tells Codex to take an API key from `env_key` when `requires_openai_auth` is false. `chatgpt.com/backend-api/codex` is the host the broker would have to call for a ChatGPT login. It is not a documented extension point for a third-party broker.
- **U3.** Whether a non-root user can create the veth is untested. The capability required is `CAP_NET_ADMIN` in the initial user namespace. A user namespace alone does not grant that on the host network namespace.
