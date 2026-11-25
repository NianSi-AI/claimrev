"""Independent numerical verification against public reference outputs from
NianSi-AI/lrd-width-luminosity@8e892e19b58189b239cc9d4a5fb0d813072e4b37.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claimrev.examples.lrd_width.seed import DEFAULT_SERIES_ID, DEFAULT_TASK_ID, build_registry, seed_initial_report
from claimrev.service import ClaimRevService

FIXTURES = Path(__file__).parent / "fixtures"


def test_recompute_frozen_main_and_no_wide2_matches_reference(tmp_path: Path) -> None:
    ref = json.loads((FIXTURES / "reference_robustness.json").read_text(encoding="utf-8"))
    seed_initial_report(tmp_path)
    svc = ClaimRevService(tmp_path, registry=build_registry())

    # 1. Baseline (frozen_main)
    main_res = svc.run_computation(
        task_id=DEFAULT_TASK_ID,
        computation_name="lrd_width_sensitivity",
        series_id=DEFAULT_SERIES_ID,
        params={"width_measure": "single_gaussian", "broad_snr_min": 5, "drop_comparison_widest_k": 0},
    )
    m_main = main_res["metrics"]
    assert m_main["target_sigma_median"]["sample_n"] == 29
    assert m_main["target_sigma_median"]["display_value"] == "0.054"
    assert m_main["target_sigma_median"]["stat_meaning"] == "point_estimate_posterior_median"
    assert m_main["target_sigma_upper95_onesided"]["display_value"] == "0.075"
    assert m_main["target_sigma_upper95_onesided"]["stat_meaning"] == "one_sided_95_upper_bound"

    assert m_main["comparison_sigma_median"]["sample_n"] == 15
    assert m_main["sigma_diff_ci95"]["display_value"] == "−0.199 至 −0.043"
    assert m_main["sigma_diff_ci95"]["contains_zero"] is False

    # Exact float agreement with public robustness.json
    assert abs(m_main["target_sigma_median"]["value"] - ref["frozen_main"]["LRD"]["sigma"][2]) < 1e-12
    assert abs(m_main["sigma_diff_ci95"]["value"][0] - ref["frozen_main"]["diff"][0]) < 1e-12
    assert abs(m_main["sigma_diff_ci95"]["value"][1] - ref["frozen_main"]["diff"][4]) < 1e-12

    # 2. Sensitivity check dropping 2 widest comparison sources (frozen_noWide2)
    nw2_res = svc.run_computation(
        task_id=DEFAULT_TASK_ID,
        computation_name="lrd_width_sensitivity",
        series_id=DEFAULT_SERIES_ID,
        params={"width_measure": "single_gaussian", "broad_snr_min": 5, "drop_comparison_widest_k": 2},
    )
    m_nw2 = nw2_res["metrics"]
    assert m_nw2["target_sigma_median"]["sample_n"] == 29
    assert m_nw2["target_sigma_median"]["display_value"] == "0.054"
    assert m_nw2["comparison_sigma_median"]["sample_n"] == 13
    assert m_nw2["sigma_diff_ci95"]["display_value"] == "−0.119 至 +0.002"
    assert m_nw2["sigma_diff_ci95"]["contains_zero"] is True
    assert nw2_res["summary"]["sigma_diff_ci95_contains_zero"] is True

    dropped = [
        [d["source_id"], d["width_kms_rounded"]]
        for d in nw2_res["summary"]["dropped_comparison_sources"]
    ]
    assert dropped == ref["frozen_noWide2"]["dropped"]
    assert dropped == [["RUBIES-EGS-15825", 3612], ["Skyfire-504101", 2915]]

    # Exact float agreement with public robustness.json
    assert abs(m_nw2["target_sigma_median"]["value"] - ref["frozen_noWide2"]["LRD"]["sigma"][2]) < 1e-12
    assert abs(m_nw2["sigma_diff_ci95"]["value"][0] - ref["frozen_noWide2"]["diff"][0]) < 1e-12
    assert abs(m_nw2["sigma_diff_ci95"]["value"][1] - ref["frozen_noWide2"]["diff"][4]) < 1e-12


def test_recompute_snr15_matches_reference_and_triggers_scope_approval(tmp_path: Path) -> None:
    ref_snr = json.loads((FIXTURES / "reference_snr_threshold.json").read_text(encoding="utf-8"))
    seed_initial_report(tmp_path)
    svc = ClaimRevService(tmp_path, registry=build_registry())

    snr15 = svc.run_computation(
        task_id=DEFAULT_TASK_ID,
        computation_name="lrd_width_sensitivity",
        series_id=DEFAULT_SERIES_ID,
        params={"width_measure": "single_gaussian", "broad_snr_min": 15, "drop_comparison_widest_k": 0},
    )
    m = snr15["metrics"]
    assert m["target_sigma_median"]["sample_n"] == 12
    assert snr15["summary"]["comparison_fitted"] is False
    assert abs(m["target_sigma_median"]["value"] - ref_snr["LRD_SNge15"]["sigma"][2]) < 1e-12

    # Adopting broad_snr_min=15 as the new permanent default report scope MUST trigger human decision
    state = svc.get_task_state(task_id=DEFAULT_TASK_ID)
    c1 = next(c for c in state["claims"] if c["claim_id"] == "C1")
    new_scope = json.loads(json.dumps(c1["scope"]))
    new_scope["inclusion_rules"]["broad_snr_min"] = 15

    prop = svc.propose_revision(
        task_id=DEFAULT_TASK_ID,
        summary="将 C1 默认宽线信噪比阈值由 S/N ≥ 5 提高至 S/N ≥ 15",
        changes=[
            {
                "claim_id": "C1",
                "action": "REVISE",
                "statement": "在宽线 S/N ≥ 15 子样本（N=12）中，目标组在固定 Hα 亮度下的线宽本征散度仍较小",
                "rationale": "按 S/N ≥ 15 重算得 12 个目标源散度后验中位数为 0.059 dex。",
                "scope": new_scope,
                "metric_bindings": [
                    {
                        "artifact_id": snr15["artifact_id"],
                        "metric_key": "target_sigma_median",
                        "expected_stat_meaning": "point_estimate_posterior_median",
                        "expected_unit": "dex",
                    }
                ],
                "evidence_links": [
                    {
                        "artifact_id": snr15["artifact_id"],
                        "relation": "SUPPORTS",
                        "rationale": "SNR >= 15 子样本复算结果",
                    }
                ],
            }
        ],
    )
    assert prop["requires_decision"] is True
    assert prop["status"] == "AWAITING_DECISION"
    assert prop["reason_code"] == "UNAPPROVED_SCOPE_SHIFT"
    assert prop["decision_id"] is not None
    assert "inclusion_rules.broad_snr_min" in prop["scope_diffs"]["C1"]
    assert prop["scope_diffs"]["C1"]["inclusion_rules.broad_snr_min"] == {"old": 5, "new": 15}
