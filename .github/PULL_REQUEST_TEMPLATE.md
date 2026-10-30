## Summary

Describe the problem and the smallest change that solves it.

## Safety impact

- [ ] No safety boundary changes
- [ ] Approval binding changed
- [ ] Isolation changed
- [ ] Protected paths or provenance changed
- [ ] Rollback/application changed

Explain every checked boundary.

## Verification

```text
Paste python3 -m pytest -q output here.
```

- [ ] Clean-home `--help` and `enqueue` smoke tests pass without traceback
- [ ] Skipped tests and reasons are listed
- [ ] Documentation station order matches `ALL_STATIONS`
- [ ] Fixtures contain only synthetic or explicitly releasable data
- [ ] Sanitization scan was run

## Rollback or migration

Describe compatibility changes and how to revert them.

