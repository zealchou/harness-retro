# Example run: background auto-save re-entry

This example is a de-identified reconstruction. It contains no real transcript, repository, message identifier, customer content, or private path.

## Incident

An automatic save hook was intended to run after an interactive response. A detached background worker inherited the same hook environment, so the hook also fired inside the worker and attempted an unrelated save.

Observed evidence:

- the background job started normally;
- the save hook appeared in the job's diagnostic record;
- the attempted save had no explicit human approval;
- the safety gate rejected the change before the real branch moved.

## Three candidates

The diagnosis produced three bounded alternatives against the same baseline:

- **A — context guard:** return early when the hook event is marked as detached work;
- **B — invocation allowlist:** run the save hook only for two named interactive events;
- **C — environment split:** remove the hook variable from the detached worker environment.

All three were created in disposable worktrees. Candidate C failed an integration test because a separate, legitimate background notifier required the same environment. Candidate B passed local tests but the wiring review found that one registered alias was not in the allowlist.

## Review stop

Candidate A passed its initial tests. The security review still stopped the run because the evidence did not prove the event marker could not be supplied by untrusted project content. The outcome was a report, not an applied patch.

## Evidence repair

The maintainer added a synthetic integration fixture showing that the marker is created by the host launcher after project input is parsed. The candidate was regenerated, its patch digest changed, and the earlier approval was therefore invalid.

The rerun produced:

- a new candidate A digest;
- isolated regression and integration results;
- wiring and security artifacts with no reproducible blocker;
- a report asking for approval of that exact digest.

## Human approval and rollback readiness

The maintainer inspected the patch and report, then explicitly approved candidate A for that run. The transaction verified the baseline and protected assets, applied one commit, ran a fresh canary, and wrote a receipt binding the approval to the patch.

Had the canary failed, retro-2 would have created a revert commit and verified restoration. The important outcome is not that A eventually won; it is that the first plausible A was blocked, new evidence changed the candidate, and only the newly reviewed digest could be approved.

