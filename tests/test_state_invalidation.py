"""Acceptance 2: Only relevant changes invalidate a pending approval card.

1. Unrelated claim publishes first -> global revision advances, original card remains valid and commits cleanly.
2. Dependent data series receives a new batch (even without publishing a new report revision) -> original card MUST be invalidated.
3. Upstream claim version change invalidates downstream card; unrelated data series swap does NOT invalidate card.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claimrev.errors import DecisionRequiredError, StaleClaimVersionError, StaleDataBatchError
from claimrev.examples.lrd_width.seed import (
    DEFAULT_DATA_PATH,
    DEFAULT_SERIES_ID,
    DEFAULT_TASK_ID,
    build_registry,
    seed_initial_report,
)
from claimrev.service import ClaimRevService


def _open_c1_snr15_card(svc: ClaimRevService) -> tuple[dict, dict]:
    snr15 = svc.run_computation(
        task_id=DEFAULT_TASK_ID,
        computation_name="lrd_width_sensitivity",
        series_id=DEFAULT_SERIES_ID,
        params={"width_measure": "single_gaussian", "broad_snr_min": 15, "drop_comparison_widest_k": 0},
    )
    state = svc.get_task_state(task_id=DEFAULT_TASK_ID)
    c1 = next(c for c in state["claims"] if c["claim_id"] == "C1")
    new_scope = json.loads(json.dumps(c1["scope"]))
    new_scope["inclusion_rules"]["broad_snr_min"] = 15

    prop = svc.propose_revision(
        task_id=DEFAULT_TASK_ID,
        summary="提议将 C1 默认宽线信噪比口径提高到 S/N ≥ 15",
        changes=[
            {
                "claim_id": "C1",
                "action": "REVISE",
                "statement": "在宽线 S/N ≥ 15 默认口径下，目标组线宽本征散度保持较小",
                "rationale": f"S/N ≥ 15（N=12）后验中位数为 {snr15['metrics']['target_sigma_median']['display_value']} dex。",
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
                        "rationale": "SNR >= 15 复算指标",
                    }
                ],
            }
        ],
    )
    return prop, snr15


def test_unrelated_claim_publish_keeps_pending_card_valid(tmp_path: Path) -> None:
    seed_initial_report(tmp_path)
    svc = ClaimRevService(tmp_path, registry=build_registry())

    # Step 1: Open approval card for C1 at global rev 1
    prop_c1, _ = _open_c1_snr15_card(svc)
    dec_id = prop_c1["decision_id"]
    assert prop_c1["status"] == "AWAITING_DECISION"
    assert prop_c1["read_claim_versions"] == {"C1": 1}

    # Cannot commit C1 before human resolves decision
    with pytest.raises(DecisionRequiredError):
        svc.commit_revision(proposal_id=prop_c1["proposal_id"])

    # Step 2: Unrelated claim C2 runs sensitivity check on SAME data batch and publishes global rev 2!
    nw2 = svc.run_computation(
        task_id=DEFAULT_TASK_ID,
        computation_name="lrd_width_sensitivity",
        series_id=DEFAULT_SERIES_ID,
        params={"width_measure": "single_gaussian", "broad_snr_min": 5, "drop_comparison_widest_k": 2},
    )
    prop_c2 = svc.propose_revision(
        task_id=DEFAULT_TASK_ID,
        summary="根据去最宽两例敏感性检验收窄 C2",
        changes=[
            {
                "claim_id": "C2",
                "action": "NARROW",
                "statement": "小对照组的组间散度差对最宽两例极端源敏感，不能独立提供稳健的组间差异证据",
                "rationale": "去掉最宽两例后，散度差 95% 可信区间为 −0.119 至 +0.002 dex，包含零。",
                "metric_bindings": [
                    {
                        "artifact_id": nw2["artifact_id"],
                        "metric_key": "sigma_diff_ci95",
                        "expected_stat_meaning": "central_95_credible_interval",
                        "expected_unit": "dex",
                    }
                ],
                "evidence_links": [
                    {
                        "artifact_id": nw2["artifact_id"],
                        "relation": "REFUTES",
                        "rationale": "去最宽两例后可信区间跨过零",
                    }
                ],
            }
        ],
    )
    assert prop_c2["status"] == "READY"
    commit_c2 = svc.commit_revision(proposal_id=prop_c2["proposal_id"])
    assert commit_c2["revision"] == 2

    # Step 3: Verify C1's pending card is STILL valid despite global revision advancing 1 -> 2
    state_after_c2 = svc.get_task_state(task_id=DEFAULT_TASK_ID)
    assert state_after_c2["task"]["current_revision"] == 2
    pending = [d for d in state_after_c2["pending_decisions"] if d["decision_id"] == dec_id]
    assert len(pending) == 1
    assert pending[0]["freshness"]["valid"] is True

    # Step 4: Human approves original card D1 -> succeeds and publishes global rev 3!
    resolved = svc.resolve_decision(
        decision_id=dec_id,
        selected_option="approve_scope_change",
        idempotency_key="approve-c1-after-c2",
        auto_commit=True,
    )
    assert resolved["committed_revision"] == 3
    final_state = svc.get_task_state(task_id=DEFAULT_TASK_ID)
    assert final_state["task"]["current_revision"] == 3
    c1_final = next(c for c in final_state["claims"] if c["claim_id"] == "C1")
    c2_final = next(c for c in final_state["claims"] if c["claim_id"] == "C2")
    assert c1_final["version"] == 2
    assert c1_final["scope"]["inclusion_rules"]["broad_snr_min"] == 15
    assert c2_final["version"] == 2
    assert c2_final["status"] == "NARROWED"


def test_dependent_data_batch_swap_invalidates_card_without_new_report_revision(tmp_path: Path) -> None:
    seed_initial_report(tmp_path)
    svc = ClaimRevService(tmp_path, registry=build_registry())

    # Open approval card for C1 at rev 1
    prop_c1, _ = _open_c1_snr15_card(svc)
    dec_id = prop_c1["decision_id"]
    orig_batch = prop_c1["read_data_batches"][DEFAULT_SERIES_ID]

    # Swap data batch for lrd_regression_inputs WITHOUT publishing any new report revision!
    new_data_file = tmp_path / "regression_inputs_batch2.json"
    raw = json.loads(DEFAULT_DATA_PATH.read_text(encoding="utf-8"))
    raw["single_gaussian"][0]["broad_SNR"] = float(raw["single_gaussian"][0]["broad_SNR"]) + 0.01
    new_data_file.write_text(json.dumps(raw), encoding="utf-8")

    batch2 = svc.ingest_data_batch(
        task_id=DEFAULT_TASK_ID,
        series_id=DEFAULT_SERIES_ID,
        file_path=new_data_file,
        vintage_tag="paper-v2-recalibrated-batch2",
        release_key="lrd_regression_inputs@batch2",
    )
    assert batch2["batch_id"] != orig_batch

    # Confirm global report revision is STILL 1!
    state = svc.get_task_state(task_id=DEFAULT_TASK_ID)
    assert state["task"]["current_revision"] == 1
    card = next(d for d in state["pending_decisions"] if d["decision_id"] == dec_id)
    assert card["freshness"]["valid"] is False
    assert card["freshness"]["stale_reasons"][0]["kind"] == "DATA_BATCH_CHANGED"
    assert card["freshness"]["stale_reasons"][0]["expected_batch_id"] == orig_batch
    assert card["freshness"]["stale_reasons"][0]["current_batch_id"] == batch2["batch_id"]

    # Attempting to approve MUST fail with StaleDataBatchError and mark card SUPERSEDED
    with pytest.raises(StaleDataBatchError) as exc_info:
        svc.resolve_decision(
            decision_id=dec_id,
            selected_option="approve_scope_change",
            idempotency_key="attempt-after-batch-swap",
            auto_commit=True,
        )
    assert exc_info.value.code == "ERR_STALE_DATA_BATCH"
    assert exc_info.value.details["stale_reasons"][0]["current_batch_id"] == batch2["batch_id"]

    # Global revision is still 1, and card is no longer pending
    state_after = svc.get_task_state(task_id=DEFAULT_TASK_ID)
    assert state_after["task"]["current_revision"] == 1
    assert state_after["pending_decisions"] == []


def test_upstream_claim_change_invalidates_downstream_card_while_unrelated_series_does_not(tmp_path: Path) -> None:
    seed_initial_report(tmp_path)
    svc = ClaimRevService(tmp_path, registry=build_registry())

    # Open scope-shift card on C2 (which depends on C1!)
    nw2 = svc.run_computation(
        task_id=DEFAULT_TASK_ID,
        computation_name="lrd_width_sensitivity",
        series_id=DEFAULT_SERIES_ID,
        params={"width_measure": "single_gaussian", "broad_snr_min": 5, "drop_comparison_widest_k": 2},
    )
    state = svc.get_task_state(task_id=DEFAULT_TASK_ID)
    c2 = next(c for c in state["claims"] if c["claim_id"] == "C2")
    new_c2_scope = json.loads(json.dumps(c2["scope"]))
    new_c2_scope["inclusion_rules"]["drop_comparison_widest_k"] = 2

    prop_c2 = svc.propose_revision(
        task_id=DEFAULT_TASK_ID,
        summary="提议将 C2 默认对照组口径改为剔除最宽两例",
        changes=[
            {
                "claim_id": "C2",
                "action": "NARROW",
                "statement": "剔除最宽两例后，小对照组不能独立提供稳健组间差异证据",
                "rationale": "95% 可信区间为 −0.119 至 +0.002 dex。",
                "scope": new_c2_scope,
                "metric_bindings": [
                    {
                        "artifact_id": nw2["artifact_id"],
                        "metric_key": "sigma_diff_ci95",
                        "expected_stat_meaning": "central_95_credible_interval",
                        "expected_unit": "dex",
                    }
                ],
                "evidence_links": [
                    {"artifact_id": nw2["artifact_id"], "relation": "REFUTES", "rationale": "敏感性检验"}
                ],
            }
        ],
    )
    assert prop_c2["read_claim_versions"] == {"C1": 1, "C2": 1}

    # Ingesting an UNRELATED data series does NOT invalidate C2's card
    aux_file = tmp_path / "aux.json"
    aux_file.write_text('{"unrelated": true}', encoding="utf-8")
    svc.ingest_data_batch(task_id=DEFAULT_TASK_ID, series_id="unrelated_aux_series", file_path=aux_file)
    st_mid = svc.get_task_state(task_id=DEFAULT_TASK_ID)
    assert st_mid["pending_decisions"][0]["freshness"]["valid"] is True

    # Now update upstream C1 -> rev 2; this MUST invalidate C2's pending card because C2 depends on C1!
    base_metrics_id = st_mid["claims"][0]["bound_metrics"][0]["artifact_id"]
    prop_c1 = svc.propose_revision(
        task_id=DEFAULT_TASK_ID,
        summary="复核 C1 并刷新版本",
        changes=[
            {
                "claim_id": "C1",
                "action": "KEEP",
                "rationale": "复核确认 29 个目标源本征散度中位数仍为 0.054 dex。",
                "metric_bindings": [
                    {
                        "artifact_id": base_metrics_id,
                        "metric_key": "target_sigma_median",
                        "expected_stat_meaning": "point_estimate_posterior_median",
                        "expected_unit": "dex",
                    }
                ],
            }
        ],
    )
    svc.commit_revision(proposal_id=prop_c1["proposal_id"])

    with pytest.raises(StaleClaimVersionError) as exc_info:
        svc.resolve_decision(
            decision_id=prop_c2["decision_id"],
            selected_option="approve_scope_change",
            auto_commit=True,
        )
    assert exc_info.value.code == "ERR_STALE_CLAIM_VERSION"
    assert exc_info.value.details["stale_reasons"][0]["claim_id"] == "C1"
