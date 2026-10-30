# Writing incidents

The public catalog contains synthetic examples only. Build a private catalog from evidence you are authorized to retain and disclose.

## Directory shape

```text
catalog-root/
├── incidents/
│   └── INC-001.json
└── evidence/
    └── INC-001.txt
```

Point relative evidence paths at a base with `RETRO2_INCIDENT_EVIDENCE_ROOT`. Keep private catalogs outside a public checkout.

## Required fields

Each incident needs:

- unique `incident_id` and short `title`;
- `source.kind`, `source.path`, line range, and exact `evidence_sha256`;
- a risk class and root-cause family;
- a minimal replay fixture and observable result;
- at least one mechanical check;
- one target question and one positive-control question;
- severity (`major` or `blocker`);
- human curator metadata and an explicit maintainer confirmation reference.

See [`examples/incidents/SYN-001.json`](../examples/incidents/SYN-001.json) for the complete shape.

## Curation procedure

1. Reduce the incident to the smallest evidence excerpt that still proves the observable failure.
2. Remove names, absolute home paths, repository identifiers, credentials, customer text, message identifiers, and unrelated context.
3. Prefer a newly written synthetic reproduction when disclosure of the original excerpt is unnecessary.
4. Save the evidence with a final newline.
5. Compute `sha256sum evidence-file` and copy the exact digest into `source.evidence_sha256`.
6. Set the line range to the lines actually hashed and replayed.
7. Add a positive control that proves the evaluator can recognize an obvious fact in the same evidence.
8. Have a human curate and confirm the item. Automated discovery alone remains quarantined.
9. Run `python3 -m pytest -q` and the repository sanitization scan.

## Verify a digest

```bash
sha256sum examples/evidence/01-missing-runtime-wire.txt
python3 - <<'PY'
from retro2.immutable_adapter import load_catalog, verify_source_evidence
for item in load_catalog("examples"):
    assert verify_source_evidence(item), item["incident_id"]
print("all evidence digests verified")
PY
```

## Diversity and leakage checks

Avoid a catalog where one failure family dominates. Include correctness, confidentiality, wiring, rollback, and cost cases. Search for personal paths, emails, real message references, long hexadecimal identifiers, and credential patterns before sharing.

Do not “sanitize” by changing only a person's name while preserving a distinctive customer story. When in doubt, replace the incident with a synthetic equivalent.

