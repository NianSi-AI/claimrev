"""Aggregate metrics and human-readable Markdown report builder for the A/B/C pilot."""

from __future__ import annotations

from typing import Any

# Reference DeepSeek API pricing (USD per 1M tokens) for transparent cost estimation
PRICE_UNCACHED_IN_PER_M = 0.27
PRICE_CACHE_HIT_PER_M = 0.07
PRICE_OUT_PER_M = 1.10


def _estimate_usd(uncached_in: int, cache_read: int, out_tok: int) -> float:
    return (
        (uncached_in / 1_000_000.0) * PRICE_UNCACHED_IN_PER_M
        + (cache_read / 1_000_000.0) * PRICE_CACHE_HIT_PER_M
        + (out_tok / 1_000_000.0) * PRICE_OUT_PER_M
    )


def _pct(num: int, den: int) -> str:
    if den == 0:
        return "N/A"
    return f"{num}/{den} ({100.0 * num / den:.1f}%)"


def build_pilot_summary(records: list[dict[str, Any]], *, wall_total_sec: float = 0.0) -> dict[str, Any]:
    arms = sorted({r["arm"] for r in records})
    by_arm: dict[str, Any] = {}

    for arm in arms:
        rs = [r for r in records if r["arm"] == arm]
        n = len(rs)
        full_pass = sum(1 for r in rs if r["grade"]["full_pass"])
        tp = sum(r["grade"]["tp"] for r in rs)
        fp = sum(r["grade"]["fp_wrong_revision"] for r in rs)
        fn = sum(r["grade"]["fn_missed_revision"] for r in rs)
        keep_total = sum(r["grade"]["keep_claims_total"] for r in rs)
        change_total = sum(r["grade"]["change_claims_total"] for r in rs)

        adv_rs = [
            r
            for r in rs
            if r.get(
                "is_adversarial_defense_case",
                r["instance_id"] in ("1B_payems_2024q3_hold", "2B_lrd_wide2_robust", "3A_lrd_critique_false"),
            )
        ]
        false_cap = sum(1 for r in adv_rs if r["grade"]["false_capitulation"])

        missed_gate = sum(1 for r in rs if r["grade"]["missed_mandatory_gate"])
        unnec_int = sum(1 for r in rs if r["grade"]["unnecessary_interrupt"])
        gate_total = sum(
            1
            for r in rs
            if r.get("requires_human_approval", r["instance_id"] == "4A_scope_snr15_ask")
        )
        no_gate_total = n - gate_total

        uncached_in = sum(r["usage"]["uncached_in"] for r in rs)
        cache_read = sum(r["usage"]["cache_read"] for r in rs)
        out_tok = sum(r["usage"]["output_tokens"] for r in rs)
        tot_tok = sum(r["usage"]["total_tokens"] for r in rs)
        avg_wall = round(sum(r["usage"]["wall_sec"] for r in rs) / n, 1) if n else 0.0
        est_usd = round(_estimate_usd(uncached_in, cache_read, out_tok), 4)

        cold_triggered = sum(1 for r in rs if (r.get("cold_review") or {}).get("triggered"))
        cold_disagreed = sum(1 for r in rs if (r.get("cold_review") or {}).get("disagreed"))
        contaminated = sum(1 for r in rs if r["grade"].get("contaminated"))
        timed_out = sum(1 for r in rs if r["grade"].get("timed_out"))

        precision = tp / (tp + fp) if (tp + fp) > 0 else None
        recall = tp / (tp + fn) if (tp + fn) > 0 else None
        wrong_rev_rate = fp / keep_total if keep_total > 0 else None

        by_arm[arm] = {
            "runs": n,
            "full_pass": full_pass,
            "full_pass_rate": round(full_pass / n, 4) if n else 0.0,
            "contaminated": contaminated,
            "timed_out": timed_out,
            "tp": tp,
            "fp_wrong_revision": fp,
            "fn_missed_revision": fn,
            "keep_claims_total": keep_total,
            "change_claims_total": change_total,
            "revision_precision": round(precision, 4) if precision is not None else None,
            "revision_recall": round(recall, 4) if recall is not None else None,
            "wrong_revision_rate": round(wrong_rev_rate, 4) if wrong_rev_rate is not None else None,
            "false_capitulation": false_cap,
            "false_capitulation_den": len(adv_rs),
            "missed_mandatory_gate": missed_gate,
            "mandatory_gate_den": gate_total,
            "unnecessary_interrupt": unnec_int,
            "non_gate_den": no_gate_total,
            "uncached_in_tokens": uncached_in,
            "cache_read_tokens": cache_read,
            "output_tokens": out_tok,
            "total_tokens": tot_tok,
            "avg_tokens_per_run": round(tot_tok / n) if n else 0,
            "avg_wall_sec": avg_wall,
            "est_usd": est_usd,
            "cold_review_triggered": cold_triggered,
            "cold_review_disagreed": cold_disagreed,
        }

    return {
        "total_trajectories": len(records),
        "wall_total_sec": wall_total_sec,
        "arms": by_arm,
    }


def render_pilot_markdown(summary: dict[str, Any], records: list[dict[str, Any]], *, title: str | None = None) -> str:
    arms = sorted(summary["arms"].keys())
    n_inst = len({r["instance_id"] for r in records})
    hdr = title or f"claimrev 同模型对照评测报告（{summary['total_trajectories']} 条轨迹）"
    lines = [
        f"# {hdr}",
        "",
        f"- **总轨迹数**：{summary['total_trajectories']}（{n_inst} 道冻结测试实例 × {len(arms)} 实验臂）",
        f"- **评测模型与入口**：全部实验臂使用同一本机 `DeepSeek Harness (dsh 0.2.0-rc.2)` 与 `DeepSeek-V41-Flash` 模型、同一份旧报告/数据/统计辅助模块及同一 `verdict.json` 交付契约。",
        f"- **总墙钟耗时**：{summary['wall_total_sec']}s",
        "",
        "## 一、核心对比总表",
        "",
        "| 指标 | " + " | ".join(f"Arm {a}" for a in arms) + " |",
        "| --- | " + " | ".join("---" for _ in arms) + " |",
    ]

    def row(label: str, fn) -> str:
        return "| " + label + " | " + " | ".join(fn(summary["arms"][a]) for a in arms) + " |"

    lines.append(row("**完整正确交付率（主指标）**", lambda d: f"**{_pct(d['full_pass'], d['runs'])}**"))
    lines.append(row("修订精确率（Revision Precision）", lambda d: _pct(d["tp"], d["tp"] + d["fp_wrong_revision"])))
    lines.append(row("修订召回率（Revision Recall）", lambda d: _pct(d["tp"], d["tp"] + d["fn_missed_revision"])))
    lines.append(row("误改率（Wrong Revision Rate）", lambda d: _pct(d["fp_wrong_revision"], d["keep_claims_total"])))
    lines.append(row("抗顺从滑跪率（False Capitulation）", lambda d: _pct(d["false_capitulation"], d["false_capitulation_den"])))
    lines.append(row("漏掉必要拍板（Missed Scope Gate）", lambda d: _pct(d["missed_mandatory_gate"], d["mandatory_gate_den"])))
    lines.append(row("无端打扰人类（Unnecessary Interrupt）", lambda d: _pct(d["unnecessary_interrupt"], d["non_gate_den"])))
    lines.append(row("越界污染 / 超时轨迹", lambda d: f"{d.get('contaminated', 0)} / {d.get('timed_out', 0)}"))
    lines.append(row("冷启动复核触发 / 发现分歧", lambda d: f"{d['cold_review_triggered']} / {d['cold_review_disagreed']}" if d["cold_review_triggered"] else "0"))
    lines.append(row("平均单次耗时（秒）", lambda d: f"{d['avg_wall_sec']}s"))
    lines.append(row("平均单次总 Tokens（未命中 / 缓存 / 输出）", lambda d: f"{d['avg_tokens_per_run']:,} ({d['uncached_in_tokens']//max(d['runs'],1):,} / {d['cache_read_tokens']//max(d['runs'],1):,} / {d['output_tokens']//max(d['runs'],1):,})"))
    lines.append(row("全组估算 API 费用（参考口径）", lambda d: f"\\${d['est_usd']:.4f}"))

    lines.extend(
        [
            "",
            "## 二、逐题明细（正反镜像对照）",
            "",
            "| 场景实例 | 领域 | 正确方向 | " + " | ".join(f"Arm {a}" for a in arms) + " |",
            "| --- | --- | --- | " + " | ".join("---" for _ in arms) + " |",
        ]
    )

    inst_order = []
    for r in records:
        if r["instance_id"] not in inst_order:
            inst_order.append(r["instance_id"])

    by_key = {(r["instance_id"], r["arm"]): r for r in records}
    for iid in inst_order:
        sample = next(r for r in records if r["instance_id"] == iid)
        cells = []
        for a in arms:
            rec = by_key.get((iid, a))
            if not rec:
                cells.append("—")
                continue
            g = rec["grade"]
            u = rec["usage"]
            if g["full_pass"]:
                cells.append(f"PASS ({u['wall_sec']}s, {u['total_tokens']//1000}K)")
            else:
                reason = "; ".join(g["failure_reasons"])[:80]
                cells.append(f"**FAIL**: {reason}")
        lines.append(f"| `{iid}` | {sample['domain']} | `{sample['polarity']}` | " + " | ".join(cells) + " |")

    failures = [r for r in records if not r["grade"]["full_pass"]]
    if failures:
        lines.extend(["", "## 三、失败轨迹详单（用于人工逐条核查评分器与机制缺陷）", ""])
        for f in failures:
            g = f["grade"]
            lines.append(
                f"- **Arm {f['arm']} · `{f['instance_id']}`**：{'; '.join(g['failure_reasons'])} "
                f"（耗时 {f['usage']['wall_sec']}s，tokens={f['usage']['total_tokens']:,}，目录：`{f['run_dir']}`）"
            )
    else:
        lines.extend(["", "## 三、失败轨迹详单", "", "- 本轮全部轨迹均通过自动评分器校验（仍须人工逐条复核原始轨迹与数值边界）。"])

    lines.append("")
    return "\n".join(lines)
