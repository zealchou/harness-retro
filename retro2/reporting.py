#!/usr/bin/env python3
"""Traditional-Chinese, self-contained terminal reports for retro-2."""
from datetime import datetime, timezone
from html import escape
import json
from pathlib import Path


REQUIRED = {
    "run_id", "status", "commit_sha", "revert_sha", "diagnosis_index",
    "security_summary", "post_apply_mechanical_regression", "subject_repo",
}
CONCLUSIONS = {
    "CANARY_OK": "已套用唯一勝者，套用後機械回歸通過。",
    "NO_CHANGE": "本輪沒有安全且明確勝出的改動，正式環境未變更。",
    "RESTORE_VERIFIED": "新方案在下一場退步，已留下紀錄並撤回到基準。",
    "REQUIRES_HUMAN_SECURITY_APPROVAL": "候選碰到資安必問邊界，尚未套用，等待人工裁決。",
    "REQUIRES_HUMAN_APPROVAL": "候選已通過機械測試，但不符合自動減法套用條件，等待人工裁決。",
}


def empty_trace(status, run_id):
    return {
        "run_id": run_id,
        "status": status,
        "commit_sha": None,
        "revert_sha": None,
        "diagnosis_index": [],
        "security_summary": "未量測",
        "post_apply_mechanical_regression": "未量測",
        "subject_repo": None,
        "model_backend": "Claude",
        "reason": None,
    }


def validate_trace(trace):
    missing = sorted(REQUIRED - set(trace))
    if missing:
        raise ValueError("missing report trace fields: " + ", ".join(missing))
    if trace["status"] not in CONCLUSIONS:
        raise ValueError("unsupported terminal status")
    return trace


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _harness_check(output_dir):
    path = Path(output_dir).parent / "health" / "harness-check.json"
    if not path.is_file():
        return {"checks": [], "findings": [], "session_statistics": {}, "rule_file_volumes": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {"checks": [], "findings": [{"component": "harness-check", "symptom": "健檢產物無法解析", "occurrences": 1}], "session_statistics": {}, "rule_file_volumes": []}
    return {"checks": payload.get("checks", []), "findings": payload.get("findings", []),
            "session_statistics": payload.get("session_statistics", {}), "rule_file_volumes": payload.get("rule_file_volumes", [])}


def _machine_candidates(output_dir):
    root = Path(output_dir).parent / "candidates"
    candidates = []
    for path in sorted(root.glob("*/manifest.json")):
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if manifest.get("machine_evidence_refs") and not manifest.get("trusted_decision_refs"):
            candidates.append(manifest.get("anonymous_id", path.parent.name))
    return candidates


def _candidate_disclosures(output_dir):
    root = Path(output_dir).parent
    disclosures = []
    for path in sorted((root / "candidates").glob("*/manifest.json")):
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        alias = manifest.get("anonymous_id", path.parent.name)
        test_path = root / "tests" / f"{alias}.json"
        if test_path.is_file() and _json_load(test_path).get("coverage_unknown") is True:
            disclosures.append(f"候選 {alias}：未經測試，請人工判斷。")
        if manifest.get("duplicate_of"):
            disclosures.append(f"候選 {alias}：與候選 {manifest['duplicate_of']} 改法重複，僅保留前者。")
    return disclosures


def _json_load(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except ValueError:
        return {}


def _seven_lenses(harness):
    stats, volumes = harness.get("session_statistics", {}), harness.get("rule_file_volumes", [])
    findings = harness.get("findings", [])
    components = {item.get("component") for item in findings}
    model_findings = _diagnosis_findings(harness.get("run_dir"))
    by_angle = {angle: [] for angle, _status in [
        ("Navigation", None), ("Automated checks", None), ("Coding standards", None),
        ("Global AGENTS.md／CLAUDE.md", None), ("Tool economy", None), ("No-ops", None),
        ("Information access", None),
    ]}
    for finding in model_findings:
        if not isinstance(finding, dict):
            continue
        for angle in finding.get("angles", []) if isinstance(finding.get("angles"), list) else []:
            if angle in by_angle:
                summary = finding.get("statement", "")
                if finding.get("invalid_reason"):
                    summary += f"（僅報告：{finding['invalid_reason']}）"
                by_angle[angle].append(summary)
    computed = [
        ("Navigation", "有發現" if "重複讀檔" in components else "證據不足"),
        ("Automated checks", "有發現" if "失敗或被拒工具" in components else "證據不足"),
        ("Coding standards", "證據不足"),
        ("Global AGENTS.md／CLAUDE.md", "有發現" if any(item.get("over_limit") for item in volumes) else "無發現"),
        ("Tool economy", "有發現" if stats.get("largest_outputs") else "證據不足"),
        ("No-ops", "證據不足"),
        ("Information access", "有發現" if "失敗或被拒工具" in components else "證據不足"),
    ]
    return [(angle, "有發現" if by_angle[angle] else status,
             by_angle[angle][0] if by_angle[angle] else "") for angle, status in computed]


def _diagnosis_findings(run_dir):
    if not run_dir:
        return []
    findings = []
    for path in sorted(Path(run_dir, "diagnoses").glob("*.json")):
        try:
            findings.extend(json.loads(path.read_text(encoding="utf-8")).get("findings", []))
        except ValueError:
            continue
    return findings


def write_report(trace, output_dir):
    validate_trace(trace)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    generated = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    conclusion = CONCLUSIONS[trace["status"]]
    harness = _harness_check(output_dir)
    harness["run_dir"] = output_dir.parent
    insights = [item for item in harness["findings"] if item.get("component") == "insights"]
    findings = [item for item in harness["findings"] if item.get("component") != "insights"]
    machine_candidates = _machine_candidates(output_dir)
    disclosures = _candidate_disclosures(output_dir)
    rows = [
        ("狀態", trace["status"]),
        ("Commit SHA", trace["commit_sha"]),
        ("Revert SHA", trace["revert_sha"]),
        ("Subject repo", trace["subject_repo"]),
        ("診斷索引", trace["diagnosis_index"]),
        ("資安摘要", trace["security_summary"]),
        ("套用後機械回歸", trace["post_apply_mechanical_regression"]),
        ("模型後端", trace.get("model_backend", "Claude")),
        ("第一個停止原因", trace.get("reason")),
    ]
    markdown = [
        "# Retro-2 執行報告", "", f"日期：{generated}", "", "## 結論", "", conclusion,
        "", f"Run：`{trace['run_id']}`", "", "## 可追溯證據", "",
    ]
    if trace.get("model_backend") == "codex/gpt-6-astra":
        markdown.extend(["", "本輪模型後端：Astra（經 codex；未使用 Claude CLI）。", ""])
    markdown.extend(f"- **{label}**：`{_json(value)}`" for label, value in rows)
    markdown.extend(["", "## Harness 部件健檢", ""])
    markdown.extend(f"- 檢查：{item.get('component')}（{item.get('source')}）— {'已檢查' if item.get('checked') else '未取得'}"
                    + (f"；{item['reason']}" if item.get("reason") else "") for item in harness["checks"])
    if findings:
        markdown.extend(f"- 發現：{item.get('component')}｜{item.get('symptom')}｜{item.get('occurrences', 1)} 次（觀察證據，不是裁決）"
                        for item in findings)
    else:
        markdown.append("- 本輪未觀察到可歸屬的部件問題。")
    if insights:
        markdown.extend(["", "### /insights 使用回顧", ""])
        markdown.extend(f"- {item.get('symptom')}（診斷參考，不是改法依據）" for item in insights)
    stats = harness.get("session_statistics", {})
    if stats:
        markdown.extend(["", "## 本場工作統計", "", f"- 統計範圍：{_json(stats.get('scope', {}))}", f"- 工具呼叫：{stats.get('tool_calls_total', 0)}；分類：{_json(stats.get('tools', {}))}",
                         f"- 最大輸出前五：{_json(stats.get('largest_outputs', []))}",
                         f"- 重複讀檔：{_json(stats.get('repeated_reads', []))}；重複搜尋：{_json(stats.get('repeated_searches', []))}",
                         f"- 失敗／被拒：{stats.get('failed_or_refused_count', 0)} 次 {_json(stats.get('failed_or_refused_tools', []))}；子代理：{stats.get('subagents', 0)}"])
    if harness.get("rule_file_volumes"):
        markdown.extend(["", "## 規則檔體積", ""] + [f"- {item['scope']}／{item['filename']}：{item['characters']} 字元、{item.get('bytes', 0)} 位元組、{item['lines']} 行"
                         + (f"（上限 {item['limit']}；超限）" if item.get("over_limit") else "") for item in harness["rule_file_volumes"]])
    markdown.extend(["", "## 七角度檢查", ""] + [f"- {angle}｜{status}" + (f"｜{summary}" if summary else "")
                    for angle, status, summary in _seven_lenses(harness)])
    if machine_candidates:
        markdown.extend([
            "", "## 機器發現候選", "",
            "- 候選：" + "、".join(machine_candidates) + "（機器發現；不升格為真人裁決）",
            "- 原斷言：machine_observed_requires_maintainer_decision",
            "- 新斷言：機器發現可產生候選並通過測試與過濾；仍須 maintainer 親口「套 X」才套用",
            "- 依據：maintainer decision",
        ])
    if disclosures:
        markdown.extend(["", "## 候選測試揭露", ""] + [f"- {item}" for item in disclosures])
    markdown.extend(["", "## 誠實邊界", "", "本報告只陳述本次可觀測結果；真模型與真人可讀性另行驗收。", ""])
    md_path = output_dir / "report.md"
    md_path.write_text("\n".join(markdown), encoding="utf-8")
    cards = "".join(
        f"<section><h2>{escape(label)}</h2><pre>{escape(_json(value))}</pre></section>"
        for label, value in rows
    )
    harness_html = "".join(f"<li>{escape(str(item.get('component')))}｜{escape(str(item.get('symptom')))}｜{escape(str(item.get('occurrences', 1)))} 次（觀察證據）</li>"
                           for item in findings) or "<li>本輪未觀察到可歸屬的部件問題。</li>"
    insights_html = "" if not insights else "<h3>/insights 使用回顧</h3><ul>" + "".join(
        f"<li>{escape(str(item.get('symptom')))}（診斷參考，不是改法依據）</li>" for item in insights) + "</ul>"
    stats_html = "" if not stats else "<section><h2>本場工作統計</h2><pre>" + escape(_json(stats)) + "</pre></section>"
    volumes_html = "" if not harness.get("rule_file_volumes") else "<section><h2>規則檔體積</h2><pre>" + escape(_json(harness["rule_file_volumes"])) + "</pre></section>"
    machine_html = "" if not machine_candidates else (
        "<section><h2>機器發現候選</h2><p>候選：" + escape("、".join(machine_candidates))
        + "（不升格為真人裁決）。機器發現可產生候選並通過測試與過濾；仍須 maintainer 親口「套 X」才套用。"
        + "依據：maintainer decision。</p></section>"
    )
    disclosure_html = "" if not disclosures else "<section><h2>候選測試揭露</h2><ul>" + "".join(
        f"<li>{escape(item)}</li>" for item in disclosures) + "</ul></section>"
    lens_html = "".join(f"<li>{escape(angle)}｜{escape(status)}" + (f"｜{escape(summary)}" if summary else "") + "</li>"
                        for angle, status, summary in _seven_lenses(harness))
    html = f"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Retro-2 執行報告</title>
<style>body{{margin:auto;max-width:880px;padding:32px 20px;font:18px/1.7 system-ui;color:#20242b;background:#f7f3ea}}main{{background:#fff;padding:clamp(22px,5vw,52px);border-radius:18px}}h1{{font-size:2rem}}h2{{font-size:1.05rem;color:#765b2f}}.lead{{font-size:1.3rem;border-left:5px solid #b68b42;padding-left:16px}}pre{{white-space:pre-wrap;overflow-wrap:anywhere}}</style></head>
<body><main><h1>Retro-2 執行報告</h1><p>{escape(generated)}</p><p class="lead">{escape(conclusion)}</p>
<p>Run：<code>{escape(str(trace['run_id']))}</code></p>{cards}<section><h2>Harness 部件健檢</h2><ul>{harness_html}</ul>{insights_html}</section>{stats_html}{volumes_html}<section><h2>七角度檢查</h2><ul>{lens_html}</ul></section>{machine_html}{disclosure_html}<p>真模型與真人可讀性另行驗收。</p></main></body></html>"""
    html_path = output_dir / "report.html"
    html_path.write_text(html, encoding="utf-8")
    return {"markdown": md_path, "html": html_path}
