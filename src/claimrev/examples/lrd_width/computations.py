"""Controlled astronomy computations for the LRD width-luminosity example.

Separated cleanly in the example layer (`claimrev.examples.lrd_width`).
Reads the input data batch at runtime and computes all posteriors from scratch.
"""

from __future__ import annotations

import json
from typing import Any

from claimrev.computations import ComputationInput, ComputationOutput, ComputationRegistry, ComputationSpec
from claimrev.errors import ValidationError

from .regression import fit, pair
from .stats import ALGORITHM_COMMIT, ALGORITHM_VERSION


def _fmt_dex(v: float, signed: bool = False) -> str:
    if signed:
        if v < 0:
            return f"−{abs(v):.3f}"
        return f"+{v:.3f}"
    return f"{v:.3f}"


def _fmt_ci(lo: float, hi: float, signed: bool = False) -> str:
    return f"{_fmt_dex(lo, signed=signed)} 至 {_fmt_dex(hi, signed=signed)}"


def _load_rows(comp_input: ComputationInput, width_measure: str) -> list[dict[str, Any]]:
    payload = json.loads(comp_input.input_path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        if width_measure not in payload:
            raise ValidationError(
                f"Width measure {width_measure!r} not found in input dataset",
                details={"available_keys": sorted(payload.keys())},
            )
        rows = payload[width_measure]
    elif isinstance(payload, list):
        rows = payload
    else:
        raise ValidationError("Unsupported regression_inputs.json format")
    if not isinstance(rows, list) or not rows:
        raise ValidationError("Input dataset contains no rows")
    return rows


def run_lrd_width_sensitivity(comp_input: ComputationInput) -> ComputationOutput:
    """Recompute LRD vs comparison width-luminosity scatter and sensitivity checks from raw input JSON.

    Follows NianSi-AI/lrd-width-luminosity/analysis/02_robustness.py lines 15-28:
      - Filter single_gaussian (or selected width_measure) and broad_SNR >= broad_snr_min
      - Split into target (`cls == 'LRD'`) and comparison (`cls != 'LRD'`)
      - Optionally drop top-k widest comparison objects sorted by `-r['y']`
      - Optionally drop top-k brightest targets or absorption targets
      - Run `pair(L, O)` (or target-only `fit(L)` if comparison sample < 6, e.g. at SNR >= 15)
    """
    params = comp_input.params or {}
    width_measure = str(params.get("width_measure", "single_gaussian"))
    broad_snr_min = float(params.get("broad_snr_min", 5.0))
    drop_comparison_widest_k = int(params.get("drop_comparison_widest_k", 0))
    drop_target_brightest_k = int(params.get("drop_target_brightest_k", 0))
    exclude_absorption = bool(params.get("exclude_absorption", False))

    if drop_comparison_widest_k < 0 or drop_target_brightest_k < 0:
        raise ValidationError("drop counts must be non-negative")

    all_rows = _load_rows(comp_input, width_measure)

    # Apply broad_SNR threshold (baseline in paper §II.3 is >= 5 with quality cuts already reflected in table)
    eligible = [r for r in all_rows if float(r.get("broad_SNR", 0.0)) >= broad_snr_min]
    targets = [r for r in eligible if r.get("cls") == "LRD"]
    comparison = [r for r in eligible if r.get("cls") != "LRD"]

    dropped_targets: list[dict[str, Any]] = []
    if exclude_absorption:
        dropped_abs = [r for r in targets if r.get("absorption")]
        for r in dropped_abs:
            dropped_targets.append(
                {
                    "source_id": r["source_id"],
                    "reason": "absorption",
                    "width_kms_rounded": round(10 ** float(r["y"])),
                }
            )
        targets = [r for r in targets if not r.get("absorption")]

    if drop_target_brightest_k > 0:
        top_t = sorted(targets, key=lambda r: -float(r["x"]))[:drop_target_brightest_k]
        top_ids = {r["source_id"] for r in top_t}
        for r in top_t:
            dropped_targets.append(
                {
                    "source_id": r["source_id"],
                    "reason": "top_luminosity",
                    "logL_Ha_erg_s": round(float(r["x"]) + 43.0, 2),
                }
            )
        targets = [r for r in targets if r["source_id"] not in top_ids]

    dropped_comparison: list[dict[str, Any]] = []
    if drop_comparison_widest_k > 0:
        wid = sorted(comparison, key=lambda r: -float(r["y"]))[:drop_comparison_widest_k]
        wid_ids = {r["source_id"] for r in wid}
        for r in wid:
            dropped_comparison.append(
                {
                    "source_id": r["source_id"],
                    "width_kms_rounded": round(10 ** float(r["y"])),
                    "width_kms": float(r.get("width_kms", 10 ** float(r["y"]))),
                    "log10_width": float(r["y"]),
                    "reason": "widest_comparison",
                }
            )
        comparison = [r for r in comparison if r["source_id"] not in wid_ids]

    if len(targets) < 6:
        raise ValidationError(
            f"Too few target sources after filtering: N={len(targets)} < 6",
            details={"target_n": len(targets), "broad_snr_min": broad_snr_min},
        )

    snr_tag = f"SNge{int(broad_snr_min) if broad_snr_min.is_integer() else broad_snr_min}"
    target_sample_id = f"LRD_{width_measure}_{snr_tag}_N{len(targets)}"
    comp_suffix = f"_noWide{drop_comparison_widest_k}" if drop_comparison_widest_k > 0 else ""
    comp_sample_id = f"comparison_{width_measure}_{snr_tag}{comp_suffix}_N{len(comparison)}"
    pair_sample_id = f"{target_sample_id}_vs_{comp_sample_id}"

    run_scope = {
        "width_measure": width_measure,
        "broad_snr_min": int(broad_snr_min) if broad_snr_min.is_integer() else broad_snr_min,
        "quality_conditions": "baseline_table_quality_cuts",
        "drop_comparison_widest_k": drop_comparison_widest_k,
        "drop_target_brightest_k": drop_target_brightest_k,
        "exclude_absorption": exclude_absorption,
        "target_n": len(targets),
        "comparison_n": len(comparison),
        "target_source_ids": [r["source_id"] for r in targets],
        "comparison_source_ids": [r["source_id"] for r in comparison],
    }

    metrics: dict[str, dict[str, Any]] = {}
    raw_output: dict[str, Any] = {}
    comparison_fitted = len(comparison) >= 6

    if comparison_fitted:
        res = pair(targets, comparison)
        rl = res["LRD"]
        ro = res["other"]
        diff = res["diff"]
        raw_output["pair"] = res
    else:
        rl, _ = fit(targets)
        ro = None
        diff = None
        raw_output["LRD_only"] = rl

    # Target metrics — explicitly distinguishing point estimate vs one-sided 95% upper bound vs central 95% CI
    metrics["target_sigma_median"] = {
        "metric_key": "target_sigma_median",
        "label": f"目标组本征散度中位数（{rl['N']} 例）",
        "value": rl["sigma"][2],
        "display_value": _fmt_dex(rl["sigma"][2]),
        "unit": "dex",
        "stat_meaning": "point_estimate_posterior_median",
        "confidence_level": None,
        "sample_id": target_sample_id,
        "sample_n": rl["N"],
        "population": "target_LRD",
        "algorithm_version": ALGORITHM_VERSION,
    }
    metrics["target_sigma_upper95_onesided"] = {
        "metric_key": "target_sigma_upper95_onesided",
        "label": f"目标组单侧 95% 上限（{rl['N']} 例）",
        "value": rl["sigma_upper95"],
        "display_value": _fmt_dex(rl["sigma_upper95"]),
        "unit": "dex",
        "stat_meaning": "one_sided_95_upper_bound",
        "confidence_level": 0.95,
        "sample_id": target_sample_id,
        "sample_n": rl["N"],
        "population": "target_LRD",
        "algorithm_version": ALGORITHM_VERSION,
    }
    metrics["target_sigma_ci95"] = {
        "metric_key": "target_sigma_ci95",
        "label": f"目标组本征散度 95% 区间（{rl['N']} 例）",
        "value": [rl["sigma"][0], rl["sigma"][4]],
        "display_value": _fmt_ci(rl["sigma"][0], rl["sigma"][4], signed=False),
        "unit": "dex",
        "stat_meaning": "central_95_credible_interval",
        "confidence_level": 0.95,
        "sample_id": target_sample_id,
        "sample_n": rl["N"],
        "population": "target_LRD",
        "algorithm_version": ALGORITHM_VERSION,
    }

    summary: dict[str, Any] = {
        "algorithm_commit": ALGORITHM_COMMIT,
        "algorithm_version": ALGORITHM_VERSION,
        "input_batch_id": comp_input.batch_id,
        "input_sha256": comp_input.input_sha256,
        "width_measure": width_measure,
        "broad_snr_min": run_scope["broad_snr_min"],
        "target_n": len(targets),
        "comparison_n": len(comparison),
        "comparison_fitted": comparison_fitted,
        "dropped_target_sources": dropped_targets,
        "dropped_comparison_sources": dropped_comparison,
        "target_sigma_median_dex": _fmt_dex(rl["sigma"][2]),
        "target_sigma_upper95_onesided_dex": _fmt_dex(rl["sigma_upper95"]),
    }

    if comparison_fitted and ro is not None and diff is not None:
        contains_zero = bool(diff[0] <= 0.0 <= diff[4])
        strictly_negative = bool(diff[4] < 0.0)
        comp_tag = (
            f"去最宽 {drop_comparison_widest_k} 例，剩 {ro['N']} 例对照"
            if drop_comparison_widest_k > 0
            else f"完整 {ro['N']} 例对照"
        )

        metrics["comparison_sigma_median"] = {
            "metric_key": "comparison_sigma_median",
            "label": f"对照组散度中位数（{comp_tag}）",
            "value": ro["sigma"][2],
            "display_value": _fmt_dex(ro["sigma"][2]),
            "unit": "dex",
            "stat_meaning": "point_estimate_posterior_median",
            "confidence_level": None,
            "sample_id": comp_sample_id,
            "sample_n": ro["N"],
            "population": "comparison_non_LRD",
            "algorithm_version": ALGORITHM_VERSION,
        }
        metrics["comparison_sigma_ci95"] = {
            "metric_key": "comparison_sigma_ci95",
            "label": f"对照组散度 95% 区间（{comp_tag}）",
            "value": [ro["sigma"][0], ro["sigma"][4]],
            "display_value": _fmt_ci(ro["sigma"][0], ro["sigma"][4], signed=False),
            "unit": "dex",
            "stat_meaning": "central_95_credible_interval",
            "confidence_level": 0.95,
            "sample_id": comp_sample_id,
            "sample_n": ro["N"],
            "population": "comparison_non_LRD",
            "algorithm_version": ALGORITHM_VERSION,
        }
        metrics["sigma_diff_median"] = {
            "metric_key": "sigma_diff_median",
            "label": f"散度差中位数（{comp_tag}）",
            "value": diff[2],
            "display_value": _fmt_dex(diff[2], signed=True),
            "unit": "dex",
            "stat_meaning": "point_estimate_posterior_median",
            "confidence_level": None,
            "sample_id": pair_sample_id,
            "sample_n": {"target": rl["N"], "comparison": ro["N"]},
            "population": "target_minus_comparison",
            "algorithm_version": ALGORITHM_VERSION,
        }
        metrics["sigma_diff_ci95"] = {
            "metric_key": "sigma_diff_ci95",
            "label": f"散度差 95% 区间（{comp_tag}）",
            "value": [diff[0], diff[4]],
            "display_value": _fmt_ci(diff[0], diff[4], signed=True),
            "unit": "dex",
            "stat_meaning": "central_95_credible_interval",
            "confidence_level": 0.95,
            "contains_zero": contains_zero,
            "sample_id": pair_sample_id,
            "sample_n": {"target": rl["N"], "comparison": ro["N"]},
            "population": "target_minus_comparison",
            "algorithm_version": ALGORITHM_VERSION,
        }

        summary["comparison_sigma_median_dex"] = _fmt_dex(ro["sigma"][2])
        summary["sigma_diff_ci95_dex"] = _fmt_ci(diff[0], diff[4], signed=True)
        summary["sigma_diff_ci95_contains_zero"] = contains_zero
        summary["sigma_diff_ci95_strictly_negative"] = strictly_negative
    else:
        summary["comparison_fit_skipped_reason"] = (
            f"comparison_n={len(comparison)} < 6 minimum required for regression fit"
        )

    return ComputationOutput(
        algorithm_version=ALGORITHM_VERSION,
        run_scope=run_scope,
        metrics=metrics,
        summary=summary,
        raw_output=raw_output,
    )


def register(registry: ComputationRegistry) -> None:
    """Register LRD width-luminosity controlled computations into a ComputationRegistry."""
    registry.register(
        ComputationSpec(
            name="lrd_width_sensitivity",
            description=(
                "Recompute intrinsic line-width scatter at fixed Halpha luminosity for target (LRD) "
                "and comparison (non-LRD) groups directly from the archived regression_inputs JSON batch "
                "using the requested filtering parameters."
            ),
            algorithm_version=ALGORITHM_VERSION,
            param_schema={
                "type": "object",
                "properties": {
                    "width_measure": {
                        "type": "string",
                        "default": "single_gaussian",
                        "description": "Line width measurement table key in regression_inputs.json",
                    },
                    "broad_snr_min": {
                        "type": "number",
                        "default": 5,
                        "description": "Minimum broad-component S/N threshold (default: 5; keep at baseline unless explicitly asked to test another threshold)",
                    },
                    "drop_comparison_widest_k": {
                        "type": "integer",
                        "default": 0,
                        "minimum": 0,
                        "description": "Number of widest comparison sources to exclude (default: 0; leave at 0 unless an outlier-exclusion check is requested)",
                    },
                    "drop_target_brightest_k": {
                        "type": "integer",
                        "default": 0,
                        "minimum": 0,
                        "description": "Number of brightest target sources to exclude (default: 0; leave at 0 unless requested)",
                    },
                    "exclude_absorption": {
                        "type": "boolean",
                        "default": False,
                        "description": "Exclude target sources flagged with absorption (default: false; leave false unless requested)",
                    },
                },
            },
            fn=run_lrd_width_sensitivity,
        )
    )
