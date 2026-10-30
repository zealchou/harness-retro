#!/usr/bin/env python3
"""Advisory-only station 8. It cannot apply a recommendation."""
from html import escape
import json
from pathlib import Path


KINDS = ("rulebook", "overlap", "trigger")
FORBIDDEN = {"apply", "patch", "changes", "commands", "command"}


def _validate(payload):
    if not isinstance(payload, dict) or FORBIDDEN.intersection(payload):
        raise ValueError("maintenance output must be advisory only")
    if not isinstance(payload.get("recommendations"), list) or not isinstance(payload.get("evidence"), list):
        raise ValueError("maintenance output must contain recommendations and evidence")
    return payload


def run_maintenance(run_dir, readonly_snapshot, runner):
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    reports = []
    for kind in KINDS:
        payload = _validate(runner(kind, {
            "mode": "advisory_only",
            "snapshot": readonly_snapshot,
            "human_approval_required": True,
        }))
        md = run_dir / f"{kind}-audit.md"
        html = run_dir / f"{kind}-audit.html"
        md.write_text(
            f"# {kind} 淘汰建議\n\n狀態：HUMAN_DECISION_REQUIRED\n\n"
            f"## 建議\n\n```json\n{json.dumps(payload['recommendations'], ensure_ascii=False, indent=2)}\n```\n\n"
            f"## 證據\n\n```json\n{json.dumps(payload['evidence'], ensure_ascii=False, indent=2)}\n```\n",
            encoding="utf-8",
        )
        html.write_text(
            '<!doctype html><html lang="zh-Hant"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<style>body{max-width:800px;margin:auto;padding:24px;font:18px/1.7 system-ui}pre{white-space:pre-wrap}</style>'
            f"<h1>{escape(kind)} 淘汰建議</h1><p>等待人工裁決；本報告不會套用變更。</p>"
            f"<pre>{escape(json.dumps(payload, ensure_ascii=False, indent=2))}</pre></html>",
            encoding="utf-8",
        )
        reports.extend([str(md), str(html)])
    return {"status": "HUMAN_DECISION_REQUIRED", "reports": reports}
