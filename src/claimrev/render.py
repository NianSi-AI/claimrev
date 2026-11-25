"""Deterministic read-only Markdown and HTML report diff renderer.

Designed with human-first ergonomics (人机工程):
  - First screen presents a clean executive decision brief: question, verdict,
    per-claim cards with clear badges (including Correct Defense vs Narrow vs Keep),
    plain-language rationale, and sample-labeled metric pills.
  - All technical provenance (internal IDs, agent diagnostic scripts, archived
    JSON payloads, scope contracts, and UTC audit events) live inside collapsed
    <details> panels below the fold.
  - 100% deterministic from committed SQLite rows + immutable archive files.
"""

from __future__ import annotations

import html
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .models import sha256_bytes

ACTION_BADGE = {
    "INITIAL": ("初始基线", "badge-init"),
    "KEEP": ("保留 · 无需修改", "badge-keep"),
    "DEFEND": ("捍卫原结论 · 已驳回质疑", "badge-defend"),
    "NARROW": ("收窄表述", "badge-narrow"),
    "REVISE": ("更新表述", "badge-revise"),
    "REFUTE": ("撤回结论", "badge-refute"),
    "CONDITIONAL": ("附条件保留", "badge-cond"),
}


def _atomic_write_text(path: Path, text: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode("utf-8")
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return sha256_bytes(data)


def _format_readable_time(iso_utc: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
        bj = dt.astimezone(timezone(timedelta(hours=8)))
        return bj.strftime("%Y-%m-%d %H:%M") + " (UTC+8)"
    except ValueError:
        return iso_utc


def _effective_action_key(row: dict[str, Any]) -> str:
    act = row["action_taken"]
    if act == "KEEP" and any(lk.get("relation") == "DEFENDS_AGAINST" for lk in row.get("links", [])):
        return "DEFEND"
    return act


def _summarize_actions(rows: list[dict[str, Any]]) -> str:
    order = ["DEFEND", "KEEP", "NARROW", "REVISE", "REFUTE", "CONDITIONAL", "INITIAL"]
    short_labels = {
        "DEFEND": "捍卫（驳回质疑）",
        "KEEP": "保留",
        "NARROW": "收窄",
        "REVISE": "更新",
        "REFUTE": "撤回",
        "CONDITIONAL": "附条件",
        "INITIAL": "初始基线",
    }
    counts: dict[str, int] = {}
    for row in rows:
        k = _effective_action_key(row)
        counts[k] = counts.get(k, 0) + 1
    parts = [f"{short_labels.get(k, k)} {counts[k]} 条" for k in order if k in counts]
    return " · ".join(parts)


def _format_metric_line(m: dict[str, Any]) -> str:
    disp = m.get("display_value", m.get("value"))
    unit = m.get("unit", "")
    meaning = m.get("stat_meaning", "")
    sample_n = m.get("sample_n")
    label = m.get("label", m.get("metric_key"))
    u_part = f" {unit}" if unit else ""
    return f"{label}：**{disp}{u_part}**（`{meaning}`, N={sample_n}）"


def render_revision_markdown(context: dict[str, Any]) -> str:
    """Render deterministic Markdown report diff from committed snapshot context."""
    task = context["task"]
    rev = context["revision"]
    prev_rev = context.get("prev_revision")
    published_at = context["published_at"]
    readable_time = _format_readable_time(published_at)
    summary = context["summary"]
    decision = context.get("decision")
    rows = context["claim_rows"]
    evidence_items = context["evidence_items"]
    diagnostic_scripts = context.get("diagnostic_scripts", [])
    shocks = context["shocks"]
    events = context["events"]

    rev_header = f"第 {prev_rev} 版 → 第 {rev} 版" if prev_rev else f"第 {rev} 版（初始版本）"
    verdict_line = _summarize_actions(rows)
    question_text = shocks[0].get("content") if shocks else ""

    lines: list[str] = [
        f"# {task['title']} — 改稿差异（{rev_header}）",
        "",
        f"- **发布时间**：{readable_time}",
        f"- **核查结果**：{verdict_line}",
    ]
    if decision:
        lines.append(
            f"- **人工拍板**：已确认选项 `{decision.get('selected_option')}`（操作通道：`{decision.get('resolved_by')}`）"
        )
    if question_text:
        lines.append(f"- **本次提问 / 质疑**：{question_text}")
    if summary:
        lines.append(f"- **处理摘要**：{summary}")

    lines.extend(
        [
            "",
            "## 报告结论核查结果",
            "",
            "| 报告中的原判断 | 本次处理与依据 |",
            "| --- | --- |",
        ]
    )

    for idx, row in enumerate(rows, 1):
        eff = _effective_action_key(row)
        action_label, _ = ACTION_BADGE.get(eff, (eff, "badge-init"))
        old_stmt = row.get("previous_statement") or row["statement"]
        new_stmt = row["statement"]
        loc = (row.get("location") or "").strip()
        loc_part = f"（{loc}）" if loc else ""
        claim_cell = f"**结论 {idx}**{loc_part}：{old_stmt}"
        if row["action_taken"] in ("NARROW", "REVISE", "REFUTE", "CONDITIONAL") and new_stmt != old_stmt:
            result_cell = f"**{action_label}。** {row['rationale']}<br>**建议改为：** {new_stmt}"
        else:
            result_cell = f"**{action_label}。** {row['rationale']}"
        claim_cell = claim_cell.replace("|", "\\|").replace("\n", " ")
        result_cell = result_cell.replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {claim_cell} | {result_cell} |")

    lines.extend(["", "## 绑定指标与证据索引（技术明细）", ""])
    for idx, row in enumerate(rows, 1):
        lines.append(f"### 结论 {idx}（内部编号 `{row['claim_id']}` · v{row['version']} · {row['status']}）")
        if row["bound_metrics"]:
            for bm in row["bound_metrics"]:
                lines.append(f"- {_format_metric_line(bm)} · 产物 `{bm['artifact_id']}`")
        else:
            lines.append("- （无绑定数值指标）")
        for lk in row["links"]:
            lines.append(f"- 证据链接：`[{lk['relation']}]` `{lk['artifact_id']}` — {lk['rationale']}")
        lines.append(f"- 样本/适用口径：`{json.dumps(row['scope'], ensure_ascii=False, sort_keys=True)}`")
        if row.get("reopen_conditions"):
            lines.append(f"- 重开条件：{row['reopen_conditions']}")
        lines.append("")

    if diagnostic_scripts:
        lines.extend(["## Agent 现场诊断脚本（归档源码）", ""])
        for sc in diagnostic_scripts:
            title = sc.get("metadata", {}).get("title") or sc["id"]
            lines.append(f"### `{sc['id']}` — {title}（sha256=`{sc['sha256'][:16]}`）")
            lines.append("```python")
            lines.append(sc.get("source_code", "").rstrip())
            lines.append("```")
            lines.append("")

    if shocks:
        lines.extend(["## 关联质疑 / 冲击记录", ""])
        for sh in shocks:
            lines.append(f"- `{sh['artifact_id']}` (`{sh['sha256'][:12]}`) — {sh.get('summary', '')}")
            if sh.get("content"):
                lines.append(f"  > {sh['content']}")
        lines.append("")

    if evidence_items:
        lines.extend(["## 计算与归档记录", ""])
        for ev in evidence_items:
            payload = ev.get("payload")
            src_tag = ""
            if isinstance(payload, dict) and payload.get("evidence_source") == "agent_diagnostic":
                src_tag = " · [Agent 现场诊断]"
            elif isinstance(payload, dict) and payload.get("evidence_source") == "controlled_computation":
                src_tag = " · [受控基线复算]"
            lines.append(
                f"- `{ev['id']}` · `{ev['kind']}`{src_tag} · sha256=`{ev['sha256'][:16]}` · 归档：`{ev['archive_path']}`"
            )
            if isinstance(payload, dict):
                alg = payload.get("algorithm_version")
                if alg:
                    lines.append(
                        f"  - 算法版本：`{alg}`；输入批次：`{payload.get('batch_id')}` (`{str(payload.get('input_sha256', ''))[:16]}`)"
                    )
                dropped = payload.get("summary", {}).get("dropped_comparison_sources")
                if dropped:
                    d_txt = "、".join(
                        f"`{d['source_id']}` ({d.get('width_kms_rounded', d.get('width_kms'))} km/s)"
                        for d in dropped
                    )
                    lines.append(f"  - 移除对照源：{d_txt}")
        lines.append("")

    lines.extend(["## 审计事件", ""])
    for evt in events:
        lines.append(
            f"- `#{evt['seq']}` `rev={evt['task_revision']}` `{evt['actor']}` `{evt['event_type']}` ({evt['created_at']})"
        )
    lines.append("")
    return "\n".join(lines)


def render_revision_html(context: dict[str, Any]) -> str:
    """Render deterministic single-page read-only HTML diff with executive-ready first screen."""
    task = context["task"]
    rev = context["revision"]
    prev_rev = context.get("prev_revision")
    published_at = context["published_at"]
    readable_time = _format_readable_time(published_at)
    summary = context["summary"]
    decision = context.get("decision")
    rows = context["claim_rows"]
    evidence_items = context["evidence_items"]
    diagnostic_scripts = context.get("diagnostic_scripts", [])
    shocks = context["shocks"]
    events = context["events"]

    rev_label = f"第 {prev_rev} 版 → 第 {rev} 版" if prev_rev else f"第 {rev} 版"
    verdict_line = _summarize_actions(rows)
    question_text = shocks[0].get("content") if shocks else ""

    if decision:
        mode_pill = '<span class="mode-pill mode-human">已由人工拍板发布</span>'
    elif any(_effective_action_key(r) == "DEFEND" for r in rows):
        mode_pill = '<span class="mode-pill mode-defend">自主核查 · 已驳回无效质疑</span>'
    elif prev_rev:
        mode_pill = '<span class="mode-pill mode-auto">自主核查 · 局部更新</span>'
    else:
        mode_pill = '<span class="mode-pill mode-init">初始基线报告</span>'

    hero_lines: list[str] = []
    if question_text:
        hero_lines.append(
            f'<div class="hero-row"><span class="hero-tag">本次提问</span><span class="hero-q">{html.escape(question_text)}</span></div>'
        )
    hero_lines.append(
        f'<div class="hero-row"><span class="hero-tag">核查结果</span><span class="hero-verdict">{html.escape(verdict_line)}</span></div>'
    )
    if summary and (not question_text or summary != question_text):
        hero_lines.append(
            f'<div class="hero-row"><span class="hero-tag">处理建议</span><span class="hero-summary">{html.escape(summary)}</span></div>'
        )

    claim_cards_html: list[str] = []
    for idx, row in enumerate(rows, 1):
        eff = _effective_action_key(row)
        action_label, badge_cls = ACTION_BADGE.get(eff, (eff, "badge-init"))
        old_stmt = row.get("previous_statement") or row["statement"]
        new_stmt = row["statement"]
        claim_id_esc = html.escape(row["claim_id"])
        loc = (row.get("location") or "").strip()
        loc_badge = f'<span class="claim-loc">{html.escape(loc)}</span>' if loc else ""

        updated_block = ""
        if row["action_taken"] in ("NARROW", "REVISE", "REFUTE", "CONDITIONAL") and new_stmt != old_stmt:
            updated_block = (
                f'<div class="updated-stmt"><span class="updated-label">建议改为：</span>'
                f"{html.escape(new_stmt)}</div>"
            )

        metric_pills: list[str] = []
        for bm in row["bound_metrics"]:
            disp = html.escape(str(bm.get("display_value", bm.get("value"))))
            unit = html.escape(str(bm.get("unit", "")))
            meaning = html.escape(str(bm.get("stat_meaning", "")))
            label = html.escape(str(bm.get("label", bm.get("metric_key"))))
            aid = html.escape(str(bm.get("artifact_id", "")))
            metric_pills.append(
                f'<span class="metric-pill" title="统计含义: {meaning} | 证据编号: {aid}">'
                f'{label}：<strong>{disp}{(" " + unit) if unit else ""}</strong>'
                f"</span>"
            )
        metrics_html = f'<div class="metric-pills">{"".join(metric_pills)}</div>' if metric_pills else ""

        claim_cards_html.append(
            f"""      <tr data-claim-id="{claim_id_esc}" data-action="{html.escape(row['action_taken'])}">
        <td class="col-claim">
          <div class="claim-head">
            <span class="claim-id" title="内部编号：{claim_id_esc}">结论 {idx}</span>
            {loc_badge}
          </div>
          <div class="claim-stmt">{html.escape(old_stmt)}</div>
        </td>
        <td class="col-result">
          <div class="result-top">
            <span class="badge {badge_cls}">{html.escape(action_label)}</span>
          </div>
          <div class="rationale">{html.escape(row['rationale'])}</div>
          {updated_block}
          {metrics_html}
        </td>
      </tr>"""
        )

    diag_panel_html = ""
    if diagnostic_scripts:
        sc_blocks = []
        for sc in diagnostic_scripts:
            title = html.escape(str(sc.get("metadata", {}).get("title") or sc["id"]))
            sc_blocks.append(
                f"""      <details class="sub-details">
        <summary><code>{html.escape(sc['id'])}</code> — {title}（sha256: <code>{html.escape(sc['sha256'][:12])}…</code>）</summary>
        <pre class="json-pre">{html.escape(sc.get('source_code', ''))}</pre>
      </details>"""
            )
        diag_panel_html = f"""
    <details class="panel">
      <summary>展开查看：Agent 现场编写并运行的诊断脚本（{len(diagnostic_scripts)} 份归档源码）</summary>
{chr(10).join(sc_blocks)}
    </details>"""

    evidence_blocks: list[str] = []
    for ev in evidence_items:
        ev_id = html.escape(ev["id"])
        ev_kind = html.escape(ev["kind"])
        ev_sha = html.escape(ev["sha256"])
        ev_path = html.escape(ev["archive_path"])
        payload = ev.get("payload")
        pretty_payload = html.escape(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)) if payload is not None else ""
        src_badge = ""
        if isinstance(payload, dict) and payload.get("evidence_source") == "agent_diagnostic":
            src_badge = ' <span class="src-tag src-diag">Agent 现场诊断</span>'
        elif isinstance(payload, dict) and payload.get("evidence_source") == "controlled_computation":
            src_badge = ' <span class="src-tag src-ctrl">受控基线复算</span>'
        meta_lines: list[str] = [
            f"<div><strong>类型：</strong> <code>{ev_kind}</code> · <strong>SHA-256：</strong> <code>{ev_sha[:16]}…</code></div>",
            f"<div><strong>归档路径：</strong> <code>{ev_path}</code></div>",
        ]
        if isinstance(payload, dict) and payload.get("algorithm_version"):
            meta_lines.append(
                f"<div><strong>算法版本：</strong> <code>{html.escape(str(payload['algorithm_version']))}</code> · "
                f"<strong>输入批次：</strong> <code>{html.escape(str(payload.get('batch_id', '')))}</code></div>"
            )
            dropped = payload.get("summary", {}).get("dropped_comparison_sources")
            if dropped:
                d_items = "、".join(
                    f"{d['source_id']} ({d.get('width_kms_rounded', d.get('width_kms'))} km/s)"
                    for d in dropped
                )
                meta_lines.append(f"<div><strong>移除的对照天体：</strong> <code>{html.escape(d_items)}</code></div>")
        evidence_blocks.append(
            f"""      <details class="sub-details">
        <summary><code>{ev_id}</code> ({ev_kind}){src_badge}</summary>
        <div class="ev-meta">{"".join(meta_lines)}</div>
        <pre class="json-pre">{pretty_payload}</pre>
      </details>"""
        )

    claim_detail_blocks: list[str] = []
    for idx, row in enumerate(rows, 1):
        scope_pretty = html.escape(json.dumps(row["scope"], ensure_ascii=False, indent=2, sort_keys=True))
        metrics_pretty = html.escape(json.dumps(row["bound_metrics"], ensure_ascii=False, indent=2, sort_keys=True))
        links_html = "".join(
            f"<li><code>{html.escape(lk['relation'])}</code> → <code>{html.escape(lk['artifact_id'])}</code>: {html.escape(lk['rationale'])}</li>"
            for lk in row["links"]
        )
        claim_detail_blocks.append(
            f"""      <details class="sub-details">
        <summary>结论 {idx}（内部编号 <code>{html.escape(row['claim_id'])}</code> · v{row['version']} · {html.escape(row['status'])}）— 口径与指标元数据</summary>
        <div class="ev-meta"><strong>证据链接：</strong><ul>{links_html or '<li>无</li>'}</ul></div>
        <div class="ev-meta"><strong>样本与拟合口径 (scope)：</strong></div>
        <pre class="json-pre">{scope_pretty}</pre>
        <div class="ev-meta"><strong>绑定指标（含单位与统计含义）：</strong></div>
        <pre class="json-pre">{metrics_pretty}</pre>
      </details>"""
        )

    shock_blocks = "".join(
        f"""      <li><code>{html.escape(sh['artifact_id'])}</code> — <strong>{html.escape(sh.get('summary', ''))}</strong>：{html.escape(sh.get('content', ''))}</li>"""
        for sh in shocks
    )
    event_rows = "".join(
        f"""      <li><code>#{evt['seq']}</code> <code>rev={evt['task_revision']}</code> <code>{html.escape(evt['actor'])}</code> <strong>{html.escape(evt['event_type'])}</strong> ({html.escape(evt['created_at'])})</li>"""
        for evt in events
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{html.escape(task['title'])} — 改稿差异 ({html.escape(rev_label)})</title>
  <style>
    :root {{
      --bg: #f8fafc;
      --card: #ffffff;
      --ink: #0f172a;
      --muted: #475569;
      --border: #e2e8f0;
      --keep-bg: #ecfdf5;
      --keep-fg: #065f46;
      --defend-bg: #eef2ff;
      --defend-fg: #3730a3;
      --narrow-bg: #fffbeb;
      --narrow-fg: #92400e;
      --revise-bg: #eff6ff;
      --revise-fg: #1e40af;
      --refute-bg: #fef2f2;
      --refute-fg: #991b1b;
      --init-bg: #f1f5f9;
      --init-fg: #334155;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      padding: 28px 20px 64px;
      background: var(--bg);
      color: var(--ink);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Noto Sans CJK SC", sans-serif;
      line-height: 1.65;
    }}
    .container {{ max-width: 980px; margin: 0 auto; }}
    header {{
      margin-bottom: 16px;
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      align-items: center;
      gap: 10px;
    }}
    h1 {{ font-size: 1.32rem; margin: 0; font-weight: 680; letter-spacing: -0.01em; }}
    .meta-right {{ display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }}
    .meta {{ color: var(--muted); font-size: 0.85rem; }}
    .mode-pill {{
      font-size: 0.76rem; font-weight: 650; padding: 2px 10px; border-radius: 999px;
    }}
    .mode-auto {{ background: #eff6ff; color: #1d4ed8; }}
    .mode-defend {{ background: var(--defend-bg); color: var(--defend-fg); }}
    .mode-human {{ background: #ecfdf5; color: #047857; }}
    .mode-init {{ background: var(--init-bg); color: var(--init-fg); }}
    .hero-card {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 15px 18px;
      margin-bottom: 16px;
      display: flex;
      flex-direction: column;
      gap: 8px;
      box-shadow: 0 1px 2px rgba(15, 23, 42, 0.03);
    }}
    .hero-row {{ display: flex; align-items: baseline; gap: 10px; font-size: 0.94rem; }}
    .hero-tag {{
      flex-shrink: 0; font-size: 0.76rem; font-weight: 650;
      padding: 2px 8px; border-radius: 6px; background: #f1f5f9; color: #334155;
      min-width: 68px; text-align: center;
    }}
    .hero-q {{ font-weight: 620; color: var(--ink); }}
    .hero-verdict {{ font-weight: 600; color: #0f172a; }}
    .hero-summary {{ color: var(--muted); }}
    .card {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 12px;
      overflow: hidden;
      box-shadow: 0 1px 3px rgba(15, 23, 42, 0.04);
    }}
    table.diff-table {{ width: 100%; border-collapse: collapse; }}
    table.diff-table th {{
      text-align: left; font-size: 0.84rem; font-weight: 600; color: var(--muted);
      background: #f1f5f9; padding: 12px 18px; border-bottom: 1px solid var(--border);
    }}
    table.diff-table td {{
      padding: 20px 18px; vertical-align: top; border-bottom: 1px solid var(--border);
    }}
    table.diff-table tr:last-child td {{ border-bottom: none; }}
    .col-claim {{ width: 38%; }}
    .col-result {{ width: 62%; }}
    .claim-head {{
      display: flex; flex-wrap: wrap; align-items: center; gap: 6px; margin-bottom: 7px;
    }}
    .claim-id {{ font-size: 0.78rem; font-weight: 650; color: var(--muted); }}
    .claim-loc {{
      font-size: 0.75rem; color: #334155; background: #f1f5f9;
      border: 1px solid var(--border); border-radius: 5px; padding: 1px 7px;
    }}
    .claim-stmt {{ font-weight: 620; font-size: 0.99rem; color: var(--ink); }}
    .result-top {{ margin-bottom: 8px; }}
    .rationale {{ font-size: 0.94rem; color: #1e293b; }}
    .badge {{
      display: inline-block; padding: 3px 11px; border-radius: 999px;
      font-size: 0.8rem; font-weight: 650;
    }}
    .badge-keep {{ background: var(--keep-bg); color: var(--keep-fg); }}
    .badge-defend {{ background: var(--defend-bg); color: var(--defend-fg); border: 1px solid #c7d2fe; }}
    .badge-narrow {{ background: var(--narrow-bg); color: var(--narrow-fg); }}
    .badge-revise {{ background: var(--revise-bg); color: var(--revise-fg); }}
    .badge-refute {{ background: var(--refute-bg); color: var(--refute-fg); }}
    .badge-cond {{ background: var(--narrow-bg); color: var(--narrow-fg); }}
    .badge-init {{ background: var(--init-bg); color: var(--init-fg); }}
    .updated-stmt {{
      margin-top: 11px; padding: 10px 13px; background: #fffbeb;
      border-left: 3px solid #d97706; border-radius: 0 8px 8px 0; font-size: 0.93rem;
    }}
    .updated-label {{ font-weight: 650; color: #92400e; }}
    .metric-pills {{ margin-top: 12px; display: flex; flex-wrap: wrap; gap: 7px; }}
    .metric-pill {{
      font-size: 0.81rem; background: #f8fafc; border: 1px solid var(--border);
      border-radius: 7px; padding: 3px 10px; color: #1e293b;
    }}
    .src-tag {{
      font-size: 0.72rem; font-weight: 600; padding: 1px 7px; border-radius: 999px; margin-left: 6px;
    }}
    .src-diag {{ background: #eef2ff; color: #3730a3; }}
    .src-ctrl {{ background: #ecfdf5; color: #065f46; }}
    details.panel {{
      margin-top: 14px; background: var(--card); border: 1px solid var(--border);
      border-radius: 10px; padding: 12px 16px;
    }}
    details.panel > summary {{ cursor: pointer; font-weight: 600; font-size: 0.9rem; color: var(--muted); }}
    details.sub-details {{ margin-top: 10px; border-top: 1px solid var(--border); padding-top: 10px; }}
    details.sub-details > summary {{ cursor: pointer; font-size: 0.86rem; color: #1e293b; }}
    .ev-meta {{ font-size: 0.84rem; color: var(--muted); margin: 6px 0; }}
    pre.json-pre {{
      background: #0f172a; color: #f8fafc; padding: 12px; border-radius: 8px;
      overflow-x: auto; font-size: 0.78rem; line-height: 1.48;
    }}
    ul.compact-list {{ margin: 8px 0 0 18px; padding: 0; font-size: 0.85rem; color: var(--muted); }}
    code {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.85em; }}
  </style>
</head>
<body>
  <div class="container">
    <header>
      <h1>{html.escape(task['title'])}</h1>
      <div class="meta-right">
        {mode_pill}
        <span class="meta">{html.escape(rev_label)} · {html.escape(readable_time)}</span>
      </div>
    </header>
    <section class="hero-card" aria-label="本次核查概览">
      {"".join(hero_lines)}
    </section>
    <section class="card" aria-label="报告结论核查结果">
      <table class="diff-table">
        <thead>
          <tr>
            <th>报告中的原判断</th>
            <th>本次处理与依据</th>
          </tr>
        </thead>
        <tbody>
{chr(10).join(claim_cards_html)}
        </tbody>
      </table>
    </section>
{diag_panel_html}
    <details class="panel">
      <summary>展开查看：计算记录与归档证据（{len(evidence_items)} 份产物）</summary>
{chr(10).join(evidence_blocks) if evidence_blocks else '      <p class="ev-meta">无归档计算产物。</p>'}
    </details>

    <details class="panel">
      <summary>展开查看：内部编号、样本口径与指标元数据（{len(rows)} 条结论）</summary>
{chr(10).join(claim_detail_blocks)}
    </details>

    <details class="panel">
      <summary>展开查看：原始提问与审计日志（{len(shocks)} 条记录 · {len(events)} 条审计事件）</summary>
      <div class="ev-meta"><strong>关联记录：</strong></div>
      <ul class="compact-list">{shock_blocks or '<li>无</li>'}</ul>
      <div class="ev-meta" style="margin-top:10px;"><strong>审计事件流（UTC）：</strong></div>
      <ul class="compact-list">{event_rows}</ul>
    </details>
  </div>
</body>
</html>
"""


def write_revision_exports(pub_dir: Path, context: dict[str, Any]) -> dict[str, Any]:
    """Deterministically write Markdown diff, HTML diff, latest pointers, and evidence manifest."""
    rev = context["revision"]
    prev_rev = context.get("prev_revision")

    md_text = render_revision_markdown(context)
    html_text = render_revision_html(context)
    manifest_text = json.dumps(
        {
            "task_id": context["task"]["id"],
            "revision": rev,
            "prev_revision": prev_rev,
            "published_at": context["published_at"],
            "summary": context["summary"],
            "decision": context.get("decision"),
            "claims": context["claim_rows"],
            "evidence_items": [
                {k: v for k, v in ev.items() if k != "payload"}
                for ev in context["evidence_items"]
            ],
            "diagnostic_scripts": [
                {k: v for k, v in sc.items() if k != "source_code"}
                for sc in context.get("diagnostic_scripts", [])
            ],
            "shocks": context["shocks"],
        },
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )

    md_name = f"revision_v{rev}.md"
    html_name = f"diff_v{prev_rev}_to_v{rev}.html" if prev_rev else f"revision_v{rev}.html"
    manifest_name = f"evidence_v{rev}.json"

    md_path = pub_dir / md_name
    html_path = pub_dir / html_name
    manifest_path = pub_dir / manifest_name

    md_sha = _atomic_write_text(md_path, md_text)
    html_sha = _atomic_write_text(html_path, html_text)
    manifest_sha = _atomic_write_text(manifest_path, manifest_text)

    _atomic_write_text(pub_dir / "latest_report.md", md_text)
    _atomic_write_text(pub_dir / "latest_diff.html", html_text)
    _atomic_write_text(pub_dir / "latest_evidence.json", manifest_text)

    return {
        "md_path": str(md_path),
        "html_path": str(html_path),
        "manifest_path": str(manifest_path),
        "latest_md_path": str(pub_dir / "latest_report.md"),
        "latest_html_path": str(pub_dir / "latest_diff.html"),
        "md_sha256": md_sha,
        "html_sha256": html_sha,
        "manifest_sha256": manifest_sha,
    }
