# Key broker

## Decisions for the chairman

The chairman decides these. Nothing in this design is implemented, and PR #9 does not merge until the broker lands with it.

1. Claude: a subscription login, which has the support and terms risk in U1, or an API key, which is billed per token ([Manage costs effectively](https://code.claude.com/docs/en/costs))? The subscription option keeps using the plan allowance the operator already pays for, rather than a separate per-token bill.
2. Codex: a ChatGPT login, which has the support gap in U2, or an API key, which the provider bills per token ([OpenAI pricing](https://platform.openai.com/docs/pricing))?
3. The transport is a Unix domain socket. It does not need a network-admin capability, and the sandbox is not started as root. An administrator still creates the broker account once. See rollout step 1.
4. Turn off all of Claude's nonessential background traffic?
5. Broker as a long-running service, or one per task?

## Plain language

A sandbox is a restricted bubble. The `claude` or `codex` command runs inside it and can see only the files we explicitly put there.

A credential is the real login secret: the Claude or Codex password-equivalent that proves who is paying for the model. Today a copy of that file is still placed inside the bubble, which is the residual risk this design removes.

A broker is a small program that stays outside the bubble, on the host. It is the only place that file lives. The bubble never receives it.

A ticket is a random one-time pass for a single run. It is not the login secret. The command inside the bubble shows the ticket to the broker. The broker checks the ticket, attaches the real login, and forwards the request to the model company. When the run ends, that ticket stops working.

The bubble also has no network. The only program it can talk to is the broker, through one socket file that we place inside the bubble. Who the broker logs in as is chairman decisions 1 and 2.

This is a design only. It is not implemented.

## What changes

Today, diagnosis and candidate generation bind the Claude login file read-only at `$CLAUDE_CONFIG_DIR/.credentials.json` on the sandbox `/tmp`. The model can read that file. Codex is disabled because its `auth.json` would have the same problem.

After the broker:

- No credential file is mounted in any sandbox.
- The child environment has no API key, no refresh token, and no `CLAUDE_CONFIG_DIR` pointing at a real login file.
- The CLI is given one per-run ticket and a base URL that points at the broker.
- The sandbox has no network (`--unshare-net`). The only route to credentials is the broker's Unix socket, which is bind-mounted in. That limit is not a seccomp rule.
- If the broker is down, the ticket is wrong, or the broker socket is missing, the stage stops. It does not mount the credential file and it does not open the public network.

## Threat model

The sandboxed model can read and write only what the allow-list mounts, and its only route to a model API is the broker's Unix socket.

| It can | It cannot |
|---|---|
| Read the project or worktree that the stage mounted | Read `~/.claude/.credentials.json`, `~/.codex/auth.json`, or `~/.ssh` on the host |
| Write its scratch directory or candidate test worktree | Read a credential file under the sandbox `/tmp`, because none is mounted |
| Send a model request to the broker on the bind-mounted socket, using the per-run ticket | Open a TCP connection to `api.anthropic.com`, `api.openai.com`, `chatgpt.com`, or any other host |
| See the ticket in its environment | See the refresh token, the API key, or the OAuth access token |
| See its own process in its private `/proc` | See the broker process, ptrace it, or read its memory or token |

`/tmp` inside the sandbox stays a private tmpfs. Creating a file there does not reveal host files and does not survive the process. The broker does not put a login file on that tmpfs.

The environment is still an allow-list (`--clearenv` and `--setenv`). `SSH_AUTH_SOCK` stays out. The ticket is the only new value, and it is not the real credential. The broker's pid is not placed in the child environment.

A symlink inside the project still does not expose `$HOME`, because `$HOME` is not mounted. That does not change.

### Process isolation

The broker is a different process from the sandbox. The sandboxed CLI must not be able to see it, attach to it, or read the token out of its memory.

`bwrap --unshare-all` already includes `--unshare-pid` and `--unshare-user-try`. The broker design keeps that:

- **PID namespace.** The sandbox is in its own PID namespace. A process can see, and so can target with `kill` or `ptrace`, only processes in its own PID namespace and in descendants of that namespace. The broker stays in the host PID namespace, which is an ancestor, so the sandbox cannot see it, attach to it, or open its `/proc/<pid>/mem` or `environ`. See [pid_namespaces(7)](https://man7.org/linux/man-pages/man7/pid_namespaces.7.html).
- **Private `/proc`.** The sandbox gets bubblewrap's `--proc` mount, a procfs for that PID namespace. `/proc/<broker-pid>/mem` and `/proc/<broker-pid>/environ` are not there, because the broker's pid is not a pid in that namespace. Guessing the host pid number does not make those files appear.
- **Different uid.** The broker runs as its own host account, not as the user inside the sandbox. Reading another user's `/proc/<pid>/environ` is a `PTRACE_MODE_READ_FSCREDS` check, not `PTRACE_MODE_ATTACH`. In [fs/proc/base.c](https://github.com/torvalds/linux/blob/master/fs/proc/base.c), `environ_open` calls `__mem_open` with `PTRACE_MODE_READ`, and `proc_mem_open` passes that mode to `mm_access` OR'd with `PTRACE_MODE_FSCREDS`. The same `mm_access` path covers the other read-only memory-adjacent files. A different uid fails that check. `/proc/<pid>/mem` is the separate `mem_open` path in the same file, which passes `PTRACE_MODE_ATTACH`; a different uid fails that too. The conclusion does not change: the sandbox user cannot read the broker's environment or memory. The token stays in the broker's address space. This does not rely on Yama `ptrace_scope`. The r6 audit checked that file only on the audit host, where `/proc/sys/kernel/yama/ptrace_scope` was missing. That result is not a claim about every machine. The deploy host must be checked with `cat /proc/sys/kernel/yama/ptrace_scope` (a missing file means Yama is not there). An administrator creates the broker account once, before the broker is started. See rollout step 1.
- **No shared credentials in memory maps.** The sandbox does not receive the broker's file descriptors. The ticket in the child environment is the random pass, not the OAuth token or API key.

## Pointing the CLIs at the broker

The broker speaks the vendor's HTTP API from the host. Inside the sandbox the CLI reaches the broker only through the bind-mounted Unix socket. The CLI never receives the real token.

### Claude Code

Official environment variables, from [Claude Code environment variables](https://code.claude.com/docs/en/env-vars) and [Connect Claude Code to an LLM gateway](https://code.claude.com/docs/en/llm-gateway-connect):

- `ANTHROPIC_BASE_URL` overrides the API endpoint so requests go through a gateway.
- `ANTHROPIC_AUTH_TOKEN` is sent as `Authorization: Bearer <value>`.
- `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`, set to any non-empty value such as `1`, disables nonessential network traffic: auto-updates, telemetry, error reporting, feedback, release notes, status checks, and feature-flag fetching. Unset it to allow that traffic again. The same page documents that setting it to `0` or `false` still disables the traffic.

The child gets:

```text
ANTHROPIC_BASE_URL=<broker endpoint on the bind-mounted socket>
ANTHROPIC_AUTH_TOKEN=<per-run ticket>
CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
```

`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` is the proposed default. Whether to keep it is chairman decision 4.

It does not get `ANTHROPIC_API_KEY`. It does not get a `CLAUDE_CONFIG_DIR` that contains `.credentials.json`. Proxy variables are documented in [Enterprise network configuration](https://code.claude.com/docs/en/network-config) (`HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY`). This design does not use them. A proxy would be a second hop. The base URL is enough if the only route is the broker.

Claude Code also calls hosts other than the model API. With the variable above, the documented nonessential calls are turned off. Anything still attempted fails because `--unshare-net` leaves no network. Whether the CLI still completes a normal diagnosis is chairman decision 4, with the official variable as the switch.

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
base_url = "<broker endpoint on the bind-mounted socket>"
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

The chairman has chosen a Unix domain socket. There is no virtual Ethernet device and no address or route to configure.

`bwrap --share-net` is removed for model stages. The sandbox is started with `--unshare-net`, so it has no network at all. It cannot open a TCP connection to the public internet or to a local IP. Seccomp is not what enforces that. A seccomp BPF program cannot dereference pointer arguments, so it cannot read the `sockaddr` passed to `connect` or `bind`, and it cannot tell one Unix socket path from another. The kernel documents that limit in [Seccomp BPF (SECure COMPuting with filters)](https://docs.kernel.org/userspace-api/seccomp_filter.html). See also [seccomp(2)](https://man7.org/linux/man-pages/man2/seccomp.2.html).

The broker runs outside the bubblewrap sandbox. It listens on a Unix socket on the host. Bubblewrap bind-mounts that socket's directory and nothing else from the host's socket locations:

```text
--bind <host socket dir> <sandbox path>
```

That mounted socket is the only route to credentials. The model CLI reaches `api.anthropic.com`, `api.openai.com`, or `chatgpt.com/backend-api/codex` only because the broker, on the host, opens those connections. The sandbox does not.

The socket directory is mode `0700`. The socket is owned by the user who runs the sandbox. The broker checks each connection with `SO_PEERCRED` and accepts the peer only when that uid is the sandbox user. No `CAP_NET_ADMIN` is required, and the sandbox is not started with `--uid 0`.

`--unshare-net` also isolates abstract Unix sockets, which have no filesystem path, so they are not the host's abstract sockets ([network_namespaces(7)](https://man7.org/linux/man-pages/man7/network_namespaces.7.html)). A path that was not bind-mounted is not visible. Together, the mount list and `--unshare-net` are what limit the sandbox to the broker socket.

`/tmp` is unchanged: a private tmpfs, with no credential file on it.

## seccomp

Seccomp is syscall-level hardening only. It does not enforce which socket the sandbox may use. That is the mount list plus `--unshare-net`, above.

Bubblewrap loads the filter. From [bwrap(1)](https://manpages.ubuntu.com/manpages/resolute/man1/bwrap.1.html), `--add-seccomp-fd FD` reads a compiled classic BPF program, as produced by `seccomp_export_bpf`, from that file descriptor. The option can be repeated. Filters are installed in the order given, and the kernel evaluates them in reverse order, so the last program on the command line runs first. Every filter except possibly the last must allow `prctl(PR_SET_SECCOMP)`. `--add-seccomp-fd` cannot be combined with `--seccomp`. In [bubblewrap.c](https://github.com/containers/bubblewrap/blob/master/bubblewrap.c) the early setup does `prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)` and exits if that fails (currently lines 2927–2929, comment "Never gain any more privs during exec"). That call is what lets the filter be installed. See [seccomp(2)](https://man7.org/linux/man-pages/man2/seccomp.2.html).

The filter we would load is small, and it does not look at addresses or paths:

- Deny `ptrace`, so the sandbox cannot attach to a process even if a later bug shared a PID namespace.
- Do not allow or deny one Unix socket by path. `socket` takes a domain, a type, and a protocol, all integers. The path arrives later as a pointer argument to `connect` or `bind`, and a BPF filter cannot dereference that pointer ([Seccomp BPF](https://docs.kernel.org/userspace-api/seccomp_filter.html)). Seccomp therefore does not try to recognize the broker socket. The mount list bind-mounts only that one socket file, and `--unshare-net` isolates abstract sockets. Together those two decide which socket the sandbox can reach.
- Do not allow `connect` only for one address or one socket path. The filter cannot see either value. `--unshare-net` leaves no IP network. The bind-mounted socket is the only broker path.

This is not a full syscall allow-list. File access stays the job of the mount list. Having no network, and reaching only the broker socket, stay the job of `--unshare-net` plus that one bind mount.

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
| The broker socket is missing, or seccomp setup fails | `HostExitError`. No unconfined child. |
| Ticket missing or already revoked | Broker returns 401 and does not forward. The stage fails. |
| Refresh fails | Broker returns an error. The refresh token is not sent to the child. |
| CLI tries any host except the broker | `--unshare-net` leaves no network, so the connection fails. The run does not open the public network instead. |
| Operator points `ANTHROPIC_BASE_URL` at a public host | The child env is set by the allow-list, not copied from the operator. The sandbox still has no route to that host. |

## Test plan

Use a throwaway `HOME` and synthetic markers. Do not print credential bodies. Record exit codes, tool verdicts, and the bwrap argv.

1. Unit: a credential path under `~/.claude` or `~/.codex` is not a `--ro-bind` source. `CLAUDE_CONFIG_DIR` is unset or points at an empty directory.
2. Unit: child env contains the ticket and the broker base URL, and does not contain `ANTHROPIC_API_KEY` or a refresh token.
3. Unit: after the stage exits, the broker rejects the ticket.
4. Real CLI, Claude, logged-in machine: `claude -p --model haiku` through the broker socket completes one short prompt. A Python probe with the same mounts cannot read the host credential, `~/.ssh`, `~/.codex/auth.json`, or a symlink into `$HOME`. `connect` to `1.1.1.1:443` fails because `--unshare-net` leaves no network, not because a seccomp filter read the address.
5. Real CLI, Claude Read tool: an allow-listed file is readable. The host credential path, `~/.ssh`, and a HOME symlink are not. Confirm the relocated `/tmp/.../.credentials.json` path is absent.
6. Same sandbox: `/proc` does not list the broker pid, and opening that pid's `mem` or `environ` fails.
7. Real CLI, Codex, after re-enable: one `codex exec` prompt through the broker. The same probe cannot read `~/.codex/auth.json`.
8. Fail closed: stop the broker and confirm the stage errors without mounting a credential file.

The logged-in Read run at `f942d1b2` stays the record of the current mount behavior. It is not a broker test.

## Rollout

PR #9 does not merge by itself. The broker implementation is the next change on this line of work, and the two merge together.

1. An administrator, the person who can create a system account, creates the broker's dedicated user before the first broker process starts. That is a one-time host setup step. The sandbox does not create this account. The same step creates the socket directory, mode `0700`, owned so the broker can listen and the sandbox user can connect. No network-admin capability is required.
2. Add the broker, running as that user and outside the sandbox, listening on the Unix socket. Bind-mount only that socket directory into the sandbox. Delete the credential `--ro-bind` in the same change. The sandbox keeps `--unshare-net`. Bubblewrap loads the seccomp program with `--add-seccomp-fd`. The filter does not select a socket path or an address.
3. Run the Claude real-CLI plan above on a logged-in machine.
4. Re-enable Codex only after its real-CLI plan passes and the chairman has chosen the Codex account type.
5. Update `docs/sandbox-allowlist.md`, both READMEs, and `docs/safety-model.md` so they no longer say the login file is mounted.
6. Merge that change with PR #9. Do not merge earlier.

## Open questions

1. Which Claude login should the broker hold: the subscription OAuth file, an `ANTHROPIC_API_KEY`, or both? **Chairman decides** (account and policy). The subscription file has no official broker support and the terms limit in U1. An API key is the documented gateway credential and is billed per token ([Manage costs effectively](https://code.claude.com/docs/en/costs)).
2. Which Codex login should it hold: ChatGPT OAuth in `auth.json`, or an API key? **Chairman decides** (account and policy). The ChatGPT login has no official broker support (U2). An API key matches `env_key` and is billed per token ([OpenAI pricing](https://platform.openai.com/docs/pricing)).
3. The transport is a Unix domain socket. **Chairman has decided** (card t693s3282). It does not need a network-admin capability, and the sandbox is not started as root. An administrator still creates the dedicated broker account once, before the first start (rollout step 1).
4. Claude Code calls more than the model API. Which of those calls, if any, must succeed for diagnosis to be usable? **Chairman decides** (policy). The official switch is `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` ([environment variables](https://code.claude.com/docs/en/env-vars)). Setting it disables auto-updates, telemetry, error reporting, feedback, release notes, status checks, and feature-flag fetching.
5. Should one broker process serve every run, or should each run start a short-lived broker? **Chairman decides** (policy). Either way the ticket is revoked when the stage exits, and the broker stays outside the sandbox PID namespace.

## Unverified / policy risk

- **U1.** Having the broker hold a Claude subscription login (`~/.claude/.credentials.json`) has no official support. [Legal and compliance](https://code.claude.com/docs/en/legal-and-compliance) says: "developers may not collect, store, or intermediate Claude.ai credentials or session tokens." The supported route is an API key with `ANTHROPIC_BASE_URL`, documented in [Connect Claude Code to an LLM gateway](https://code.claude.com/docs/en/llm-gateway-connect). An API key is billed per token.
- **U2.** Having the broker hold Codex's ChatGPT login has no official support and no support in the provider source cited above. That source tells Codex to take an API key from `env_key` when `requires_openai_auth` is false. `chatgpt.com/backend-api/codex` is the host the broker would have to call for a ChatGPT login. It is not a documented extension point for a third-party broker.
- **U3.** Closed for the transport. The chairman chose a Unix socket, so the design does not create a virtual Ethernet device and does not ask for a network-admin capability. Creating the dedicated broker account still needs an administrator. That step is rollout step 1, not an untested network setup.
