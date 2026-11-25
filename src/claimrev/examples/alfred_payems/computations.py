"""Controlled macro computations for ALFRED PAYEMS (US Nonfarm Payrolls) vintage analysis.

Strictly lives in the example layer (`claimrev.examples.alfred_payems`) and plugs into
the exact same domain-agnostic `claimrev` core registry.

Explicitly distinguishes the three macro time concepts:
  1. observation_month (the calendar month of employment measured)
  2. vintage_date (the ALFRED release/as-of date when these numbers were visible)
  3. revision delta (differences between an earlier vintage and a later vintage)
"""

from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Any

from claimrev.computations import ComputationInput, ComputationOutput, ComputationRegistry, ComputationSpec
from claimrev.errors import ValidationError

ALGORITHM_VERSION = "alfred_payems@v1"


def _fmt_signed(v: float, decimals: int = 1) -> str:
    if v < 0:
        return f"−{abs(v):.{decimals}f}"
    return f"+{v:.{decimals}f}"


def parse_alfred_csv(path: Path) -> tuple[str, dict[str, float], dict[str, float]]:
    """Parse an ALFRED CSV (`observation_date,PAYEMS_YYYYMMDD`) into vintage_date, levels, and MoM changes (k)."""
    levels: dict[str, float] = {}
    vintage_date = "unknown"
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if not header or len(header) < 2:
            raise ValidationError(f"Invalid ALFRED CSV header in {path}")
        m = re.search(r"(\d{4})(\d{2})(\d{2})", header[1])
        if m:
            vintage_date = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        for row in reader:
            if not row or len(row) < 2 or not row[0] or row[1].strip() in ("", "."):
                continue
            levels[row[0].strip()] = float(row[1].strip())

    if len(levels) < 2:
        raise ValidationError(f"ALFRED CSV {path} has fewer than 2 valid observations")

    sorted_months = sorted(levels.keys())
    mom_changes: dict[str, float] = {}
    for i in range(1, len(sorted_months)):
        curr_m = sorted_months[i]
        prev_m = sorted_months[i - 1]
        mom_changes[curr_m] = round(levels[curr_m] - levels[prev_m], 3)

    return vintage_date, levels, mom_changes


def _normalize_month(m: str) -> str:
    s = m.strip()
    if re.fullmatch(r"\d{4}-\d{2}", s):
        return f"{s}-01"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s
    raise ValidationError(f"Invalid observation month format: {m!r} (expected YYYY-MM or YYYY-MM-DD)")


def run_payems_window_summary(comp_input: ComputationInput) -> ComputationOutput:
    """Compute monthly payroll gains and averages over an observation-month window from an ALFRED vintage CSV."""
    params = comp_input.params or {}
    raw_months = params.get("window_months")
    if not isinstance(raw_months, list) or not raw_months:
        raise ValidationError("params.window_months must be a non-empty list of YYYY-MM or YYYY-MM-DD strings")

    window_months = [_normalize_month(str(m)) for m in raw_months]
    window_label = str(params.get("window_label") or f"{window_months[0][:7]} 至 {window_months[-1][:7]}").strip()
    threshold_k = params.get("threshold_k")
    threshold_val = float(threshold_k) if threshold_k is not None else None

    raw_compare = params.get("compare_window_months")
    compare_months = [_normalize_month(str(m)) for m in raw_compare] if raw_compare else None
    compare_label = str(params.get("compare_label") or "对比窗口").strip()

    vintage_date, levels, mom_changes = parse_alfred_csv(comp_input.input_path)

    missing = [m for m in window_months if m not in mom_changes]
    if missing:
        raise ValidationError(
            f"Observation month(s) {missing} not available with prior-month baseline in vintage {vintage_date}",
            details={"missing_months": missing, "available_months": sorted(mom_changes.keys()), "vintage_date": vintage_date},
        )

    w_changes = [mom_changes[m] for m in window_months]
    avg_gain = sum(w_changes) / len(w_changes)
    total_gain = sum(w_changes)
    min_gain = min(w_changes)
    max_gain = max(w_changes)
    all_positive = all(v > 0 for v in w_changes)

    breakdown = [
        {
            "observation_month": m[:7],
            "level_k": levels[m],
            "monthly_change_k": round(mom_changes[m], 1),
        }
        for m in window_months
    ]

    v_tag = f"截至 {vintage_date}"
    sample_id = f"PAYEMS_{vintage_date}_{window_months[0][:7]}_{window_months[-1][:7]}"

    metrics: dict[str, dict[str, Any]] = {
        "window_avg_monthly_gain_k": {
            "metric_key": "window_avg_monthly_gain_k",
            "label": f"{window_label}月均新增非农（{v_tag}）",
            "value": round(avg_gain, 4),
            "display_value": _fmt_signed(avg_gain, 1),
            "unit": "千人/月",
            "stat_meaning": "window_mean_monthly_change",
            "confidence_level": None,
            "sample_id": sample_id,
            "sample_n": len(window_months),
            "vintage_date": vintage_date,
            "algorithm_version": ALGORITHM_VERSION,
        },
        "window_total_gain_k": {
            "metric_key": "window_total_gain_k",
            "label": f"{window_label}累计新增非农（{v_tag}）",
            "value": round(total_gain, 4),
            "display_value": _fmt_signed(total_gain, 1),
            "unit": "千人",
            "stat_meaning": "window_cumulative_change",
            "confidence_level": None,
            "sample_id": sample_id,
            "sample_n": len(window_months),
            "vintage_date": vintage_date,
            "algorithm_version": ALGORITHM_VERSION,
        },
        "window_min_monthly_gain_k": {
            "metric_key": "window_min_monthly_gain_k",
            "label": f"{window_label}单月最低新增（{v_tag}）",
            "value": round(min_gain, 4),
            "display_value": _fmt_signed(min_gain, 1),
            "unit": "千人/月",
            "stat_meaning": "window_minimum_monthly_change",
            "confidence_level": None,
            "sample_id": sample_id,
            "sample_n": len(window_months),
            "vintage_date": vintage_date,
            "algorithm_version": ALGORITHM_VERSION,
        },
    }

    summary: dict[str, Any] = {
        "series": "PAYEMS",
        "vintage_date": vintage_date,
        "latest_observation_month": max(levels.keys())[:7],
        "observation_months": [m[:7] for m in window_months],
        "monthly_breakdown": breakdown,
        "window_avg_monthly_gain_k": round(avg_gain, 1),
        "window_total_gain_k": round(total_gain, 1),
        "window_min_monthly_gain_k": round(min_gain, 1),
        "window_max_monthly_gain_k": round(max_gain, 1),
        "all_months_positive": all_positive,
    }

    if threshold_val is not None:
        above = avg_gain >= threshold_val
        summary["threshold_k"] = threshold_val
        summary["avg_meets_threshold"] = above
        summary["threshold_margin_k"] = round(avg_gain - threshold_val, 1)

    if compare_months:
        c_missing = [m for m in compare_months if m not in mom_changes]
        if c_missing:
            raise ValidationError(f"compare_window_months missing in vintage {vintage_date}: {c_missing}")
        c_changes = [mom_changes[m] for m in compare_months]
        c_avg = sum(c_changes) / len(c_changes)
        diff_avg = avg_gain - c_avg
        c_sample_id = f"PAYEMS_{vintage_date}_{compare_months[0][:7]}_{compare_months[-1][:7]}"

        metrics["compare_window_avg_monthly_gain_k"] = {
            "metric_key": "compare_window_avg_monthly_gain_k",
            "label": f"{compare_label}月均新增非农（{v_tag}）",
            "value": round(c_avg, 4),
            "display_value": _fmt_signed(c_avg, 1),
            "unit": "千人/月",
            "stat_meaning": "window_mean_monthly_change",
            "confidence_level": None,
            "sample_id": c_sample_id,
            "sample_n": len(compare_months),
            "vintage_date": vintage_date,
            "algorithm_version": ALGORITHM_VERSION,
        }
        metrics["avg_gain_diff_vs_compare_k"] = {
            "metric_key": "avg_gain_diff_vs_compare_k",
            "label": f"相对{compare_label}月均增速差（{v_tag}）",
            "value": round(diff_avg, 4),
            "display_value": _fmt_signed(diff_avg, 1),
            "unit": "千人/月",
            "stat_meaning": "window_mean_change_difference",
            "confidence_level": None,
            "sample_id": f"{sample_id}_vs_{c_sample_id}",
            "sample_n": {"window": len(window_months), "compare": len(compare_months)},
            "vintage_date": vintage_date,
            "algorithm_version": ALGORITHM_VERSION,
        }
        summary["compare_observation_months"] = [m[:7] for m in compare_months]
        summary["compare_avg_monthly_gain_k"] = round(c_avg, 1)
        summary["avg_gain_diff_vs_compare_k"] = round(diff_avg, 1)
        summary["faster_than_compare"] = bool(diff_avg > 0)

    run_scope = {
        "series": "PAYEMS",
        "vintage_date": vintage_date,
        "observation_months": [m[:7] for m in window_months],
        "compare_observation_months": [m[:7] for m in compare_months] if compare_months else None,
        "threshold_k": threshold_val,
        "unit": "千人/月",
    }

    return ComputationOutput(
        algorithm_version=ALGORITHM_VERSION,
        run_scope=run_scope,
        metrics=metrics,
        summary=summary,
        raw_output={"vintage_date": vintage_date, "breakdown": breakdown},
    )


def register(registry: ComputationRegistry) -> None:
    """Register ALFRED PAYEMS computations into a generic ComputationRegistry."""
    registry.register(
        ComputationSpec(
            name="payems_window_summary",
            description=(
                "Compute US Total Nonfarm Payrolls (PAYEMS, thousands of persons) monthly job gains, "
                "window average monthly gain, cumulative gain, minimum monthly gain, threshold check, "
                "and comparison against a prior window directly from an archived ALFRED vintage CSV."
            ),
            algorithm_version=ALGORITHM_VERSION,
            param_schema={
                "type": "object",
                "required": ["window_months"],
                "properties": {
                    "window_months": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Observation months to summarize, e.g. ['2025-04', '2025-05', '2025-06']",
                    },
                    "window_label": {
                        "type": "string",
                        "description": "Plain-language Chinese label for the window (e.g. '2025 年二季度')",
                    },
                    "compare_window_months": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional earlier comparison window months (e.g. ['2025-01', '2025-02', '2025-03'])",
                    },
                    "compare_label": {
                        "type": "string",
                        "description": "Plain-language Chinese label for the comparison window (e.g. '2025 年一季度')",
                    },
                    "threshold_k": {
                        "type": "number",
                        "description": "Optional monthly average gain threshold in thousands (e.g. 100 or 150)",
                    },
                },
            },
            fn=run_payems_window_summary,
        )
    )
