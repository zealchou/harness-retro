# Instructions for AI agents

Treat `retro2/controller/run.py`—normally reached through the `harness-retro` console command—as the single runtime entry point. Do not call internal stations directly, invent a second controller, or bypass health, evidence, test, review, approval, or rollback gates.

Before changing behavior:

1. Read `docs/ACCEPTANCE_FIRST.md`, `docs/architecture.md`, and `docs/safety-model.md`.
2. Keep all filesystem defaults in `retro2/config.py`; new paths need a documented `RETRO2_*` override.
3. Never add real transcripts, runtime queues, run artifacts, credentials, customer data, or private incident corpora.
4. Put public teaching incidents in `examples/incidents/` and their synthetic source evidence in `examples/evidence/`; verify every digest.
5. Preserve explicit human approval. A model result is evidence, not authorization.
6. Preserve the bubblewrap fail-closed boundary. Tests that require an unavailable Linux namespace may skip with an explicit reason; do not weaken their assertions.

Run the complete regression suite from the repository root:

```bash
python3 -m pytest -q
```

Also run the clean-home CLI smoke test and the sanitization scan described in `CONTRIBUTING.md`. Do not claim completion from unit tests alone; verify the console entry and station-order documentation as separate checks.

