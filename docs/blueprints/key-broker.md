# Key broker

The sandbox should not be able to read a login file. A small program on the host, the broker, keeps the real Claude or Codex credential. The sandboxed CLI talks only to that program. The broker adds the real credential and forwards the request to the model API. When the run ends, the sandbox's ticket stops working.

This is a design only. It is not implemented. PR #9 stays unmerged until this broker lands with it.

## What changes

Today, diagnosis and candidate generation bind the Claude login file read-only at `$CLAUDE_CONFIG_DIR/.credentials.json` on the sandbox `/tmp`. The model can read that file. Codex is disabled because its `auth.json` would have the same problem.

After the broker:

- No credential file is mounted in any sandbox.
- The child environment has no API key, no refresh token, and no `CLAUDE_CONFIG_DIR` pointing at a real login file.
- The CLI is given one per-run ticket and a base URL that points at the broker.
- The sandbox has no route to the public internet. The only network path is to the broker.
- If the broker is down, the ticket is wrong, or the network setup fails, the stage stops. It does not mount the credential file and it does not open the public network.

## Threat model

The sandboxed model can read and write only what the allow-list mounts, and it can send HTTP only to the broker.

| It can | It cannot |
|---|---|
| Read the project or worktree that the stage mounted | Read `~/.claude/.credentials.json`, `~/.codex/auth.json`, or `~/.ssh` on the host |
| Write its scratch directory or candidate test worktree | Read a credential file under the sandbox `/tmp`, because none is mounted |
| Send model-API HTTP to the broker, using the per-run ticket | Open a TCP connection to `api.anthropic.com`, `api.openai.com`, or any other host |
| See the ticket in its environment | See the refresh token, the API key, or the OAuth access token |

`/tmp` inside the sandbox stays a private tmpfs. Creating a file there does not reveal host files and does not survive the process. The broker does not put a login file on that tmpfs.

The environment is still an allow-list (`--clearenv` and `--setenv`). `SSH_AUTH_SOCK` stays out. The ticket is the only new value, and it is not the real credential.

A symlink inside the project still does not expose `$HOME`, because `$HOME` is not mounted. That does not change.

## Pointing the CLIs at the broker

The broker speaks the vendor's HTTP API on a private address, for example `http://10.88.0.1:8080`. The sandbox can reach only that address. The CLI never receives the real token.

### Claude Code

Official environment variables, from [Claude Code environment variables](https://code.claude.com/docs/en/env-vars) and [Connect Claude Code to an LLM gateway](https://code.claude.com/docs/en/llm-gateway-connect):

- `ANTHROPIC_BASE_URL` overrides the API endpoint so requests go through a gateway.
- `ANTHROPIC_AUTH_TOKEN` is sent as `Authorization: Bearer <value>`.

The child gets:

```text
ANTHROPIC_BASE_URL=http://10.88.0.1:8080
ANTHROPIC_AUTH_TOKEN=<per-run ticket>
```

It does not get `ANTHROPIC_API_KEY`. It does not get a `CLAUDE_CONFIG_DIR` that contains `.credentials.json`. Proxy variables are documented in [Enterprise network configuration](https://code.claude.com/docs/en/network-config) (`HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY`). This design does not use them. A proxy would be a second hop. The base URL is enough if the only route is the broker.

Claude Code also calls hosts other than the model API (telemetry and similar). Those calls fail closed because those hosts are not routed. Whether the CLI still completes a normal diagnosis without them is an open question below.

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

## Token refresh

The host broker is the only process that reads the real credential:

- Claude subscription: the OAuth access token and refresh token in the host `~/.claude/.credentials.json`.
- Claude API key: `ANTHROPIC_API_KEY` in the host environment, if that is how the operator logs in.
- Codex: the host `~/.codex/auth.json` access token and refresh token, or an API key.

The broker refreshes an access token when the vendor says it is expired or near expiry. The refresh token never enters the sandbox, the child environment, or the broker's HTTP response. If refresh fails, the broker returns an error to the CLI and the stage fails. It does not fall back to mounting the file.

## Per-run ticket and revocation

For each model stage the host creates a random ticket, stores it in broker memory with the run id and the backend, and puts only that ticket in the child. The broker accepts a request only when the ticket matches an open run.

The ticket is dropped when the stage process exits, when the run is cancelled, or after the stage timeout. A later request with the same ticket gets 401 and is not forwarded. Tickets are not written to the runs directory.

## Network isolation

`bwrap --share-net` is removed for model stages. The sandbox is started with `--unshare-net`, which gives it no host network.

A veth pair is the concrete path, because both CLIs speak HTTP to a base URL and do not speak HTTP over a Unix socket:

1. Create a veth pair. One end stays on the host and is addressed `10.88.0.1`. The other end is moved into the sandbox namespace and addressed `10.88.0.2`.
2. The broker listens on `10.88.0.1:8080` and nowhere else.
3. The sandbox default route is `10.88.0.1`. There is no DNS server and no route to the public internet.
4. The broker, on the host, is the only process that opens connections to `api.anthropic.com` or `api.openai.com`.

A Unix socket is the alternative if a later change wants to avoid a veth. The CLI would still need a TCP base URL, so a socket-only design adds a forwarder inside the namespace. Do not build both in the first version. Use the veth.

`/tmp` is unchanged: a private tmpfs, with no credential file on it.

## seccomp

Bubblewrap does not apply a syscall filter. The broker change adds a small seccomp filter in the child before `exec`, using libseccomp:

- `connect` is allowed only to `10.88.0.1` port `8080`.
- Other `connect` calls return `EPERM`.
- New `AF_UNIX` sockets are denied, so the child cannot attach to `SSH_AUTH_SOCK` or a host service socket.

This is defense in depth on top of `--unshare-net`. It is not a full syscall allow-list. File access stays the job of the existing mount list.

## Turning Codex back on

Codex stays disabled until all of the following are true:

1. The broker is running and the stage refuses to start when it is not.
2. A Codex run uses the generated `CODEX_HOME` config above and does not mount `~/.codex/auth.json`.
3. The real-CLI checks below pass for Codex: the CLI reaches the model, and a probe in the same sandbox cannot read the host `auth.json`, `~/.ssh`, or the HOME canary.
4. `RETRO2_MODEL_BACKEND=codex` then stops raising "Codex backend is disabled…".

Until that pass, the current fail-closed error stays.

## Failure modes

| What goes wrong | What the stage does |
|---|---|
| Broker is not listening | Stop before the model CLI. Do not mount a credential file. Do not use `--share-net`. |
| veth or seccomp setup fails | `HostExitError`. No unconfined child. |
| Ticket missing or already revoked | Broker returns 401 and does not forward. The stage fails. |
| Refresh fails | Broker returns an error. The refresh token is not sent to the child. |
| CLI tries any host except the broker | Connection fails. The run does not retry on the public network. |
| Operator points `ANTHROPIC_BASE_URL` at a public host | The sandbox route and the seccomp filter still block it. The child env is set by the allow-list, not copied from the operator. |

## Test plan

Use a throwaway `HOME` and synthetic markers. Do not print credential bodies. Record exit codes, tool verdicts, and the bwrap argv.

1. Unit: a credential path under `~/.claude` or `~/.codex` is not a `--ro-bind` source. `CLAUDE_CONFIG_DIR` is unset or points at an empty directory.
2. Unit: child env contains the ticket and the broker base URL, and does not contain `ANTHROPIC_API_KEY` or a refresh token.
3. Unit: after the stage exits, the broker rejects the ticket.
4. Real CLI, Claude, logged-in machine: `claude -p --model haiku` through the broker completes one short prompt. A Python probe with the same mounts cannot read the host credential, `~/.ssh`, `~/.codex/auth.json`, or a symlink into `$HOME`. `connect` to `1.1.1.1:443` fails.
5. Real CLI, Claude Read tool: an allow-listed file is readable. The host credential path, `~/.ssh`, and a HOME symlink are not. Confirm the relocated `/tmp/.../.credentials.json` path is absent.
6. Real CLI, Codex, after re-enable: one `codex exec` prompt through the broker. The same probe cannot read `~/.codex/auth.json`.
7. Fail closed: stop the broker and confirm the stage errors without mounting a credential file.

The logged-in Read run at `f942d1b2` stays the record of the current mount behavior. It is not a broker test.

## Rollout

PR #9 does not merge by itself. The broker implementation is the next change on this line of work, and the two merge together.

1. Add the broker and the veth path behind the existing allow-list. Delete the credential `--ro-bind` in the same change.
2. Run the Claude real-CLI plan above on a logged-in machine.
3. Re-enable Codex only after its real-CLI plan passes.
4. Update `docs/sandbox-allowlist.md`, both READMEs, and `docs/safety-model.md` so they no longer say the login file is mounted.
5. Merge that change with PR #9. Do not merge earlier.

## Open questions

1. Which Claude login should the broker hold: the subscription OAuth file, an `ANTHROPIC_API_KEY`, or both?
2. Which Codex login should it hold: ChatGPT OAuth in `auth.json`, or an API key?
3. Is a veth pair acceptable on the machines that run harness-retro, or is a Unix socket required?
4. Claude Code calls more than the model API. Which of those calls, if any, must succeed for diagnosis to be usable?
5. Should one broker process serve every run, or should each run start a short-lived broker?
