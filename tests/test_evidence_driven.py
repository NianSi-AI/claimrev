"""Acceptance 4: Evidence-driven outcomes, input-variant flipping, metric semantics, and core isolation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claimrev.errors import MetricSemanticMismatchError
from claimrev.examples.lrd_width.seed import (
    DEFAULT_DATA_PATH,
    DEFAULT_SERIES_ID,
    DEFAULT_TASK_ID,
    build_registry,
    seed_initial_report,
)
from claimrev.service import ClaimRevService


def test_core_modules_contain_zero_astronomy_domain_coupling() -> None:
    core_dir = Path(__file__).parent.parent / "src" / "claimrev"
    core_files = [
        "db.py",
        "errors.py",
        "models.py",
        "store.py",
        "validation.py",
        "diagnostics.py",
        "computations.py",
        "render.py",
        "approval_ui.py",
        "service.py",
        "mcp_server.py",
        "cli.py",
    ]
    forbidden_tokens = [
        "claimrev.examples",
        "lrd_width",
        "alfred_payems",
        "PAYEMS",
        "RUBIES-EGS-15825",
        "Skyfire-504101",
        "single_gaussian",
        "0.054",
        "-0.119",
    ]
    for fname in core_files:
        text = (core_dir / fname).read_text(encoding="utf-8")
        for tok in forbidden_tokens:
            assert tok not in text, f"Core file {fname} unexpectedly references domain token {tok!r}"


def _run_evidence_driven_decision(store_dir: Path, data_path: Path) -> dict:
    """Apply the exact same generic evidence rule on two different datasets."""
    seed_initial_report(store_dir, data_path=data_path)
    svc = ClaimRevService(store_dir, registry=build_registry())

    check = svc.run_computation(
        task_id=DEFAULT_TASK_ID,
        computation_name="lrd_width_sensitivity",
        series_id=DEFAULT_SERIES_ID,
        params={"width_measure": "single_gaussian", "broad_snr_min": 5, "drop_comparison_widest_k": 2},
    )
    ci_metric = check["metrics"]["sigma_diff_ci95"]
    contains_zero = bool(ci_metric["contains_zero"])

    if contains_zero:
        c2_change = {
            "claim_id": "C2",
            "action": "NARROW",
            "statement": "小对照组比较对最宽两例敏感，不能独立提供稳健的组间差异证据",
            "rationale": f"去掉最宽两例后，散度差 95% 可信区间为 {ci_metric['display_value']} dex，包含零。",
            "metric_bindings": [
                {
                    "artifact_id": check["artifact_id"],
                    "metric_key": "sigma_diff_ci95",
                    "expected_stat_meaning": "central_95_credible_interval",
                    "expected_unit": "dex",
                }
            ],
            "evidence_links": [
                {"artifact_id": check["artifact_id"], "relation": "REFUTES", "rationale": "95% CI 包含零"}
            ],
        }
    else:
        c2_change = {
            "claim_id": "C2",
            "action": "KEEP",
            "rationale": f"去掉最宽两例后，散度差 95% 可信区间仍为 {ci_metric['display_value']} dex，严格小于零且不含零，结论保持稳健。",
            "metric_bindings": [
                {
                    "artifact_id": check["artifact_id"],
                    "metric_key": "sigma_diff_ci95",
                    "expected_stat_meaning": "central_95_credible_interval",
                    "expected_unit": "dex",
                }
            ],
            "evidence_links": [
                {"artifact_id": check["artifact_id"], "relation": "DEFENDS_AGAINST", "rationale": "95% CI 仍不含零"}
            ],
        }

    prop = svc.propose_revision(
        task_id=DEFAULT_TASK_ID,
        summary="按去最宽两例复算证据决定 C2 处理意见",
        changes=[c2_change],
    )
    commit = svc.commit_revision(proposal_id=prop["proposal_id"])
    st = svc.get_task_state(task_id=DEFAULT_TASK_ID)
    c2 = next(c for c in st["claims"] if c["claim_id"] == "C2")
    return {
        "contains_zero": contains_zero,
        "summary_contains_zero": check["summary"]["sigma_diff_ci95_contains_zero"],
        "ci95_display": ci_metric["display_value"],
        "c2_action": c2["action_taken"],
        "c2_status": c2["status"],
        "revision": commit["revision"],
    }


def test_input_variant_flips_plugin_outcome_from_narrow_to_keep(tmp_path: Path) -> None:
    # 1. Real public input -> drops 2 widest -> CI crosses zero -> NARROW
    real_out = _run_evidence_driven_decision(tmp_path / "real_store", DEFAULT_DATA_PATH)
    assert real_out["contains_zero"] is True
    assert real_out["summary_contains_zero"] is True
    assert real_out["ci95_display"] == "−0.119 至 +0.002"
    assert real_out["c2_action"] == "NARROW"
    assert real_out["c2_status"] == "NARROWED"

    # 2. Construct an input variant where comparison group scatter remains genuinely large
    #    even after removing its 2 widest sources (spread remaining comparison widths symmetrically).
    raw = json.loads(DEFAULT_DATA_PATH.read_text(encoding="utf-8"))
    comp_rows = [r for r in raw["single_gaussian"] if r["cls"] != "LRD"]
    comp_sorted = sorted(comp_rows, key=lambda r: -float(r["y"]))
    # Keep top 2 widest intact, widen scatter among the remaining 13 comparison sources
    offsets = [0.26, -0.24, 0.22, -0.22, 0.20, -0.20, 0.18, -0.18, 0.16, -0.16, 0.14, -0.14, 0.12]
    for r, delta in zip(comp_sorted[2:], offsets):
        r["y"] = float(r["y"]) + delta
        r["width_kms"] = float(10 ** r["y"])

    variant_path = tmp_path / "regression_inputs_robust_variant.json"
    variant_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")

    variant_out = _run_evidence_driven_decision(tmp_path / "variant_store", variant_path)
    assert variant_out["contains_zero"] is False
    assert variant_out["summary_contains_zero"] is False
    assert variant_out["c2_action"] == "KEEP"
    assert variant_out["c2_status"] == "ACTIVE"


def test_metric_semantics_guard_rejects_point_estimate_vs_upper_bound_confusion(tmp_path: Path) -> None:
    seed_initial_report(tmp_path)
    svc = ClaimRevService(tmp_path, registry=build_registry())
    state = svc.get_task_state(task_id=DEFAULT_TASK_ID)
    c1 = next(c for c in state["claims"] if c["claim_id"] == "C1")
    art_id = c1["bound_metrics"][0]["artifact_id"]

    # Confirm both 0.054 (point estimate) and 0.075 (one-sided 95% upper bound) exist with distinct meanings
    by_key = {m["metric_key"]: m for m in c1["bound_metrics"]}
    assert by_key["target_sigma_median"]["display_value"] == "0.054"
    assert by_key["target_sigma_median"]["stat_meaning"] == "point_estimate_posterior_median"
    assert by_key["target_sigma_upper95_onesided"]["display_value"] == "0.075"
    assert by_key["target_sigma_upper95_onesided"]["stat_meaning"] == "one_sided_95_upper_bound"

    # Attempt 1: Bind 0.054 median into an upper-bound slot -> MUST REJECT!
    with pytest.raises(MetricSemanticMismatchError) as exc1:
        svc.propose_revision(
            task_id=DEFAULT_TASK_ID,
            summary="错误地把点估计 0.054 当作单侧 95% 上限绑定",
            changes=[
                {
                    "claim_id": "C1",
                    "action": "KEEP",
                    "rationale": "测试语义错配拦截",
                    "metric_bindings": [
                        {
                            "artifact_id": art_id,
                            "metric_key": "target_sigma_median",
                            "expected_stat_meaning": "one_sided_95_upper_bound",
                            "expected_unit": "dex",
                        }
                    ],
                }
            ],
        )
    assert exc1.value.code == "ERR_METRIC_SEMANTIC_MISMATCH"
    assert exc1.value.details["actual_stat_meaning"] == "point_estimate_posterior_median"
    assert exc1.value.details["expected_stat_meaning"] == "one_sided_95_upper_bound"

    # Attempt 2: Bind 0.075 upper bound into a point-estimate slot -> MUST REJECT!
    with pytest.raises(MetricSemanticMismatchError) as exc2:
        svc.propose_revision(
            task_id=DEFAULT_TASK_ID,
            summary="错误地把单侧 95% 上限 0.075 当作点估计绑定",
            changes=[
                {
                    "claim_id": "C1",
                    "action": "KEEP",
                    "rationale": "测试语义错配拦截",
                    "metric_bindings": [
                        {
                            "artifact_id": art_id,
                            "metric_key": "target_sigma_upper95_onesided",
                            "expected_stat_meaning": "point_estimate_posterior_median",
                            "expected_unit": "dex",
                        }
                    ],
                }
            ],
        )
    assert exc2.value.code == "ERR_METRIC_SEMANTIC_MISMATCH"

    # Attempt 3: Wrong unit -> MUST REJECT!
    with pytest.raises(MetricSemanticMismatchError) as exc3:
        svc.propose_revision(
            task_id=DEFAULT_TASK_ID,
            summary="错误单位测试",
            changes=[
                {
                    "claim_id": "C1",
                    "action": "KEEP",
                    "rationale": "测试单位拦截",
                    "metric_bindings": [
                        {
                            "artifact_id": art_id,
                            "metric_key": "target_sigma_median",
                            "expected_stat_meaning": "point_estimate_posterior_median",
                            "expected_unit": "km/s",
                        }
                    ],
                }
            ],
        )
    assert exc3.value.code == "ERR_METRIC_SEMANTIC_MISMATCH"
