# Contributing

Thank you for helping make harness-retro safer and more portable.

## Before opening a pull request

1. Open an issue for behavior changes or new safety policy.
2. Fork the repository and create a focused branch.
3. Add or update tests without removing existing safety assertions.
4. Run `python3 -m pytest -q` on Linux with Python 3.11 or 3.12.
5. Run `HOME=$(mktemp -d) python3 retro2_cli.py --help` and an `enqueue` smoke test.
6. Check the repository for personal paths, credentials, transcripts, run artifacts, and real incident evidence.

Keep changes small. Explain the failure mode, the safety boundary affected, and the evidence that proves the fix. Tests requiring `bwrap` must skip with a precise reason when the host kernel cannot create the required namespace; do not replace the isolation assertion with a mock-only test.

## Incident examples

Only synthetic or explicitly releasable evidence belongs in this repository. Follow [Writing incidents](docs/writing-incidents.md), update the evidence digest, and never copy a private memory or transcript into a fixture.

## Commit and pull request style

Use an imperative subject line. In the pull request, include:

- problem and reproduction;
- changed safety assumptions;
- test output and skipped-test reasons;
- migration or rollback notes;
- documentation changes.

By participating, you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).

