"""Acceptance 3: Idempotent retries, changed-option conflicts, atomic DB commit, and crash recovery.

1. Same choice retry publishes only once (both service API and CLI).
2. Passing a different option after confirmation raises DecisionConflictError (ERR_DECISION_CONFLICT).
3. Simulated export interruption after DB commit leaves committed revision intact; restart rebuilds
   byte-for-byte identical report & diff from SQLite + immutable archive without adding another revision.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from claimrev.errors import DecisionConflictError, ExportInterruptedError
from claimrev.examples.lrd_width.seed import DEFAULT_SERIES_ID, DEFAULT_TASK_ID, build_registry, seed_initial_report
from claimrev.models import sha256_file
from claimrev.service import ClaimRevService


def _create_pending_c1_decision(svc: ClaimRevService) -> dict:
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
    return svc.propose_revision(
        task_id=DEFAULT_TASK_ID,
        summary="提议将 C1 默认口径改为 S/N ≥ 15",
        changes=[
            {
                "claim_id": "C1",
                "action": "REVISE",
                "statement": "在宽线 S/N ≥ 15 口径下，目标组线宽本征散度仍较小",
                "rationale": "N=12 子样本后验中位数为 0.059 dex。",
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
                    {"artifact_id": snr15["artifact_id"], "relation": "SUPPORTS", "rationale": "SNR >= 15"}
                ],
            }
        ],
    )


def test_same_choice_retry_publishes_once_and_different_choice_conflicts(tmp_path: Path) -> None:
    seed_initial_report(tmp_path)
    svc = ClaimRevService(tmp_path, registry=build_registry())
    prop = _create_pending_c1_decision(svc)
    dec_id = prop["decision_id"]

    # First approval publishes rev 2
    r1 = svc.resolve_decision(
        decision_id=dec_id,
        selected_option="approve_scope_change",
        idempotency_key="idem-001",
        auto_commit=True,
    )
    assert r1["idempotent_replay"] is False
    assert r1["committed_revision"] == 2

    # Same choice retry (same idempotency key) -> idempotent replay, STILL rev 2
    r2 = svc.resolve_decision(
        decision_id=dec_id,
        selected_option="approve_scope_change",
        idempotency_key="idem-001",
        auto_commit=True,
    )
    assert r2["idempotent_replay"] is True
    assert r2["committed_revision"] == 2

    # Same choice retry (without key) -> STILL rev 2
    r3 = svc.resolve_decision(
        decision_id=dec_id,
        selected_option="approve_scope_change",
        auto_commit=True,
    )
    assert r3["idempotent_replay"] is True
    assert r3["committed_revision"] == 2

    # Direct commit retry -> STILL rev 2
    c_retry = svc.commit_revision(proposal_id=prop["proposal_id"])
    assert c_retry["idempotent_replay"] is True
    assert c_retry["revision"] == 2

    # Different option after confirmation -> MUST raise ERR_DECISION_CONFLICT
    with pytest.raises(DecisionConflictError) as exc_info:
        svc.resolve_decision(
            decision_id=dec_id,
            selected_option="keep_baseline_scope",
            idempotency_key="idem-002",
            auto_commit=True,
        )
    assert exc_info.value.code == "ERR_DECISION_CONFLICT"
    assert exc_info.value.details["confirmed_option"] == "approve_scope_change"
    assert exc_info.value.details["attempted_option"] == "keep_baseline_scope"

    # Global revision never advanced past 2
    assert svc.get_task_state(task_id=DEFAULT_TASK_ID)["task"]["current_revision"] == 2


def test_human_cli_approve_idempotency_and_conflict(tmp_path: Path) -> None:
    seed_initial_report(tmp_path)
    svc = ClaimRevService(tmp_path, registry=build_registry())
    prop = _create_pending_c1_decision(svc)
    dec_id = prop["decision_id"]

    base_cmd = [
        sys.executable,
        "-m",
        "claimrev.cli",
        "--store",
        str(tmp_path),
        "approve",
        "--decision-id",
        dec_id,
    ]
    p1 = subprocess.run(
        base_cmd + ["--option", "keep_baseline_scope", "--idempotency-key", "cli-k1"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert p1.returncode == 0, p1.stderr
    out1 = json.loads(p1.stdout)
    assert out1["committed_revision"] == 2
    assert out1["idempotent_replay"] is False

    # Retry same option via CLI
    p2 = subprocess.run(
        base_cmd + ["--option", "keep_baseline_scope", "--idempotency-key", "cli-k1"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert p2.returncode == 0, p2.stderr
    out2 = json.loads(p2.stdout)
    assert out2["committed_revision"] == 2
    assert out2["idempotent_replay"] is True

    # Conflicting option via CLI -> non-zero exit + ERR_DECISION_CONFLICT
    p3 = subprocess.run(
        base_cmd + ["--option", "approve_scope_change", "--idempotency-key", "cli-k2"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert p3.returncode == 2
    err3 = json.loads(p3.stderr)
    assert err3["error"] == "ERR_DECISION_CONFLICT"


def test_crash_after_db_commit_recovers_exact_same_revision_deterministically(tmp_path: Path) -> None:
    seed_initial_report(tmp_path)
    svc1 = ClaimRevService(tmp_path, registry=build_registry())

    nw2 = svc1.run_computation(
        task_id=DEFAULT_TASK_ID,
        computation_name="lrd_width_sensitivity",
        series_id=DEFAULT_SERIES_ID,
        params={"width_measure": "single_gaussian", "broad_snr_min": 5, "drop_comparison_widest_k": 2},
    )
    prop = svc1.propose_revision(
        task_id=DEFAULT_TASK_ID,
        summary="去最宽两例后收窄 C2，保留 C1",
        changes=[
            {
                "claim_id": "C1",
                "action": "KEEP",
                "rationale": "29 个目标源样本与拟合口径不变，后验中位数仍为 0.054 dex。",
                "metric_bindings": [
                    {
                        "artifact_id": nw2["artifact_id"],
                        "metric_key": "target_sigma_median",
                        "expected_stat_meaning": "point_estimate_posterior_median",
                        "expected_unit": "dex",
                    }
                ],
            },
            {
                "claim_id": "C2",
                "action": "NARROW",
                "statement": "小对照组比较对最宽两例极端源敏感，不能独立提供稳健的组间差异证据",
                "rationale": "去最宽两例后 95% 可信区间为 −0.119 至 +0.002 dex，包含零。",
                "metric_bindings": [
                    {
                        "artifact_id": nw2["artifact_id"],
                        "metric_key": "sigma_diff_ci95",
                        "expected_stat_meaning": "central_95_credible_interval",
                        "expected_unit": "dex",
                    }
                ],
                "evidence_links": [
                    {"artifact_id": nw2["artifact_id"], "relation": "REFUTES", "rationale": "区间包含零"}
                ],
            },
        ],
    )

    # Simulate crash RIGHT AFTER SQLite commit, BEFORE report files are exported!
    with pytest.raises(ExportInterruptedError):
        svc1.commit_revision(proposal_id=prop["proposal_id"], simulate_export_crash=True)

    pub_dir = tmp_path / "pub" / DEFAULT_TASK_ID
    assert not (pub_dir / "revision_v2.md").exists()
    assert not (pub_dir / "diff_v1_to_v2.html").exists()
    assert (pub_dir / "revision_v2.md.tmp.crashed").exists()

    # Restart: brand-new service instance inspects committed DB state
    svc2 = ClaimRevService(tmp_path, registry=build_registry())
    st = svc2.get_task_state(task_id=DEFAULT_TASK_ID)
    assert st["task"]["current_revision"] == 2
    assert st["latest_publication"]["revision"] == 2
    assert st["latest_publication"]["export_status"] == "PENDING"

    # Run crash recovery
    rec1 = svc2.recover_exports(task_id=DEFAULT_TASK_ID)
    assert rec1["recovered_count"] == 1
    assert rec1["recovered"][0]["revision"] == 2
    assert not (pub_dir / "revision_v2.md.tmp.crashed").exists()

    md_path = pub_dir / "revision_v2.md"
    html_path = pub_dir / "diff_v1_to_v2.html"
    assert md_path.is_file()
    assert html_path.is_file()
    md_sha_1 = sha256_file(md_path)
    html_sha_1 = sha256_file(html_path)

    # Retrying original commit_revision after recovery MUST stay at rev 2 (idempotent, no rev 3!)
    retry = svc2.commit_revision(proposal_id=prop["proposal_id"])
    assert retry["idempotent_replay"] is True
    assert retry["revision"] == 2
    assert svc2.get_task_state(task_id=DEFAULT_TASK_ID)["task"]["current_revision"] == 2

    # Delete exported files and rebuild again -> MUST be byte-for-byte identical!
    md_path.unlink()
    html_path.unlink()
    rec2 = svc2.recover_exports(task_id=DEFAULT_TASK_ID)
    assert rec2["recovered_count"] == 1
    assert sha256_file(md_path) == md_sha_1
    assert sha256_file(html_path) == html_sha_1
