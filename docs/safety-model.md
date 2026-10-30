# Safety model

## Threat model

harness-retro assumes that transcripts, external content, tool output, model output, and candidate patches may be mistaken or adversarial. It also assumes a well-intentioned operator can approve the wrong revision if approval is not bound to a digest.

It does not claim to defend against a malicious host administrator, a compromised Python interpreter, or a hostile kernel.

## Core invariants

### Evidence is not instruction

Model prompts wrap observations as external data. Trusted human decisions and machine observations carry different provenance. An external URL, pasted instruction, or diagnostic sentence cannot become a root-cause authority by itself.

### Test before semantic review

Each candidate is materialized in a disposable worktree. Mechanical tests run before the review filters. Unknown coverage is disclosed and cannot qualify for automatic application.

### Four review surfaces

The filter stage produces independent artifacts for:

1. structural and acceptance integrity;
2. production wiring;
3. project security and privacy;
4. detailed exploit-oriented security review.

The portable repository uses built-in neutral review prompts. Installations may replace model infrastructure, but may not omit required artifacts or weaken reproducible-blocker rules.

### Protected paths

The immutable manifest covers the incident catalog, scoring policy, controller, transaction path, security-sensitive settings, credential-shaped files, and security tests. Both candidate construction and transaction application consult the same manifest.

### Explicit, exact approval

The distributed policy is report-only. Applying a saved candidate requires a maintainer message reference and binds that reference to run ID, candidate ID, and patch SHA-256. Reusing an old approval after a patch changes is rejected.

### Reversible application

Receipts bind baseline, preimages, postimages, tests, and approval. A failed fresh canary creates an explicit revert and verifies the restored baseline. History remains auditable.

### Conservative memory

The rejection index skips repeated mechanical work only when the subject, baseline, inputs, patch, policy, and test version match. Experience notes require observed winning evidence; they are hints, not rules.

## Isolation guarantee

On supported Linux hosts, bubblewrap exposes one writable worktree, selected read-only roots, `/proc`, and `/dev`. After those binds it masks `$HOME/.claude` and `RETRO2_HOME` again, so a project directory that contains either one cannot uncover them, then remounts only the diagnosis allowlist (`CLAUDE.md`, `settings.json`, `hooks`, and `scripts/logs`) plus paths outside the masks. A Claude credential file is rebound read-only under `CLAUDE_CONFIG_DIR`, outside the directories given to the model's file tools, so the CLI can authenticate. The diagnosis scratch directory is outside those directories as well. Tests do not receive model network access.

If bubblewrap or the required namespace is unavailable, safety-sensitive execution stops. A test may skip because the host cannot demonstrate the isolation primitive, but production execution must not silently fall back to an unrestricted subprocess.

## Optional-component behavior

| Component | Missing behavior |
|---|---|
| hook audit | health record says skipped/degraded; core health can proceed |
| managed project list | current project only |
| probability judge | explicit optional-component error; no guessed score |
| diagnostic paths | no observations from that source |
| Codex executable | optional backend fails with a clear unavailable error |
| Claude executable | full default run cannot proceed; help and enqueue remain usable |

## Operational checklist

Before enabling the pressure hook:

- run the suite on the target Linux host;
- verify bubblewrap child-start and network-denial tests do not skip unexpectedly;
- inspect every configured filesystem path;
- keep runtime state outside the code repository;
- start with manual enqueue;
- review a complete report and rollback receipt;
- enable automatic enqueue only after the manual path is understood.

## Residual risk

Review prompts and model diagnoses can still be wrong. A passing local test may share an implementation assumption. Filesystem isolation does not validate business intent. Human approval is therefore a real boundary, not a ceremonial click.

