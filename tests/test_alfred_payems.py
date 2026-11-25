"""Milestone 2 tests: ALFRED PAYEMS real historical vintage revisions on the unchanged generic core.

Verifies:
  1. Real 2025-07-03 -> 2025-08-01 vintage (-258k May-Jun revision) flips Q2 claim while Q1 stays untouched.
  2. Real 2024-10-04 -> 2024-12-06 mirror (-80k Q3 revision) preserves Q3 claim (>150k and >Q2 both still hold).
  3. Incoming ALFRED vintage batch invalidates pending approval card even before new report publish.
  4. Identical core MCP stdio server runs macro PAYEMS end-to-end with zero core modifications.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from claimrev.errors import StaleDataBatchError
from claimrev.examples.alfred_payems.seed import (
    DATA_DIR,
    DEFAULT_SERIES_ID,
    build_registry,
    seed_payems_2024q3_hold,
    seed_payems_2025q2_flip,
)
from claimrev.service import ClaimRevService


def test_payems_2025q2_flip_exact_vintage_numbers(tmp_path: Path) -> None:
    seeded = seed_payems_2025q2_flip(tmp_path, ingest_t1=True)
    svc = ClaimRevService(tmp_path, registry=build_registry())
    tid = seeded["task_id"]

    # T0 frozen report metrics
    st = svc.get_task_state(task_id=tid)
    assert st["task"]["current_revision"] == 1
    m1_t0 = next(c for c in st["claims"] if c["claim_id"] == "M1")["bound_metrics"][0]
    m2_t0 = next(c for c in st["claims"] if c["claim_id"] == "M2")["bound_metrics"][0]
    assert m1_t0["display_value"] == "+111.0"
    assert m2_t0["display_value"] == "+149.7"

    # Recompute on T1 (2025-08-01) vintage batch
    q1_t1 = svc.run_computation(
        task_id=tid,
        computation_name="payems_window_summary",
        series_id=DEFAULT_SERIES_ID,
        params={"window_months": ["2025-01", "2025-02", "2025-03"], "window_label": "2025 年一季度", "threshold_k": 100},
    )
    assert q1_t1["summary"]["vintage_date"] == "2025-08-01"
    assert q1_t1["metrics"]["window_avg_monthly_gain_k"]["display_value"] == "+111.0"
    assert q1_t1["summary"]["avg_meets_threshold"] is True

    q2_t1 = svc.run_computation(
        task_id=tid,
        computation_name="payems_window_summary",
        series_id=DEFAULT_SERIES_ID,
        params={
            "window_months": ["2025-04", "2025-05", "2025-06"],
            "window_label": "2025 年二季度",
            "compare_window_months": ["2025-01", "2025-02", "2025-03"],
            "compare_label": "2025 年一季度",
            "threshold_k": 100,
        },
    )
    assert q2_t1["metrics"]["window_avg_monthly_gain_k"]["display_value"] == "+63.7"
    assert q2_t1["summary"]["avg_meets_threshold"] is False
    assert q2_t1["summary"]["faster_than_compare"] is False
    assert q2_t1["metrics"]["avg_gain_diff_vs_compare_k"]["display_value"] == "−47.3"
    by_month = {r["observation_month"]: r["monthly_change_k"] for r in q2_t1["summary"]["monthly_breakdown"]}
    assert by_month == {"2025-04": 158.0, "2025-05": 19.0, "2025-06": 14.0}


def test_payems_2024q3_mirror_revision_preserves_claim(tmp_path: Path) -> None:
    seeded = seed_payems_2024q3_hold(tmp_path, ingest_t1=True)
    svc = ClaimRevService(tmp_path, registry=build_registry())
    tid = seeded["task_id"]

    # Recompute on T1 (2024-12-06) vintage: -80k downward revision, yet claim STILL HOLDS!
    q3_t1 = svc.run_computation(
        task_id=tid,
        computation_name="payems_window_summary",
        series_id=DEFAULT_SERIES_ID,
        params={
            "window_months": ["2024-07", "2024-08", "2024-09"],
            "window_label": "2024 年三季度",
            "compare_window_months": ["2024-04", "2024-05", "2024-06"],
            "compare_label": "2024 年二季度",
            "threshold_k": 150,
        },
    )
    assert q3_t1["summary"]["vintage_date"] == "2024-12-06"
    assert q3_t1["metrics"]["window_avg_monthly_gain_k"]["display_value"] == "+159.0"
    assert q3_t1["summary"]["avg_meets_threshold"] is True
    assert q3_t1["summary"]["faster_than_compare"] is True
    assert q3_t1["metrics"]["avg_gain_diff_vs_compare_k"]["display_value"] == "+11.7"
    by_month = {r["observation_month"]: r["monthly_change_k"] for r in q3_t1["summary"]["monthly_breakdown"]}
    assert by_month == {"2024-07": 144.0, "2024-08": 78.0, "2024-09": 255.0}


def test_alfred_new_vintage_invalidates_pending_macro_approval_card(tmp_path: Path) -> None:
    seeded = seed_payems_2025q2_flip(tmp_path, ingest_t1=False)
    svc = ClaimRevService(tmp_path, registry=build_registry())
    tid = seeded["task_id"]

    # Open a scope-shift approval card on M2 (raising threshold from 100k to 120k) while on 2025-07-03 vintage
    c2_t0 = svc.run_computation(
        task_id=tid,
        computation_name="payems_window_summary",
        series_id=DEFAULT_SERIES_ID,
        params={"window_months": ["2025-04", "2025-05", "2025-06"], "threshold_k": 120},
    )
    st = svc.get_task_state(task_id=tid)
    m2 = next(c for c in st["claims"] if c["claim_id"] == "M2")
    new_scope = dict(m2["scope"])
    new_scope["threshold_k"] = 120
    prop = svc.propose_revision(
        task_id=tid,
        summary="提议把二季度稳健门槛从 100 提高到 120 千人/月",
        changes=[
            {
                "claim_id": "M2",
                "action": "REVISE",
                "statement": "2025 年二季度月均新增维持在 120 千人以上",
                "rationale": "按 2025-07-03 冻结版月均 +149.7 千人/月高于 120。",
                "scope": new_scope,
                "metric_bindings": [
                    {
                        "artifact_id": c2_t0["artifact_id"],
                        "metric_key": "window_avg_monthly_gain_k",
                        "expected_stat_meaning": "window_mean_monthly_change",
                        "expected_unit": "千人/月",
                    }
                ],
            }
        ],
    )
    assert prop["requires_decision"] is True

    # New ALFRED vintage 2025-08-01 arrives WITHOUT publishing a new report revision!
    svc.ingest_data_batch(
        task_id=tid,
        series_id=DEFAULT_SERIES_ID,
        file_path=DATA_DIR / "payems_vintage_2025-08-01.csv",
        vintage_tag="2025-08-01",
        release_key="PAYEMS@2025-08-01",
    )

    with pytest.raises(StaleDataBatchError) as exc_info:
        svc.resolve_decision(decision_id=prop["decision_id"], selected_option="approve_scope_change")
    assert exc_info.value.code == "ERR_STALE_DATA_BATCH"


def test_same_mcp_server_executes_payems_revision_end_to_end(tmp_path: Path) -> None:
    seed_payems_2025q2_flip(tmp_path, ingest_t1=True)

    async def _run() -> None:
        params = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "claimrev.mcp_server",
                "--store",
                str(tmp_path),
                "--plugin",
                "claimrev.examples.alfred_payems.computations:register",
            ],
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                st = json.loads((await session.call_tool("get_task_state", {})).content[0].text)
                assert st["data_series"][0]["vintage_tag"] == "2025-08-01"
                assert st["available_computations"][0]["name"] == "payems_window_summary"

                comp = json.loads(
                    (
                        await session.call_tool(
                            "run_computation",
                            {
                                "computation_name": "payems_window_summary",
                                "params": {
                                    "window_months": ["2025-04", "2025-05", "2025-06"],
                                    "window_label": "2025 年二季度",
                                    "compare_window_months": ["2025-01", "2025-02", "2025-03"],
                                    "compare_label": "2025 年一季度",
                                    "threshold_k": 100,
                                },
                            },
                        )
                    ).content[0].text
                )
                assert comp["metrics"]["window_avg_monthly_gain_k"]["display_value"] == "+63.7"

                prop = json.loads(
                    (
                        await session.call_tool(
                            "propose_revision",
                            {
                                "summary": "2025-08-01 历史下修导致二季度增速跌破 100 千人且弱于一季度，修订结论 2。",
                                "changes": [
                                    {
                                        "claim_id": "M1",
                                        "action": "KEEP",
                                        "rationale": "一季度历史数据未修订，月均新增仍为 +111.0 千人/月。",
                                        "metric_bindings": [
                                            {
                                                "artifact_id": comp["artifact_id"],
                                                "metric_key": "compare_window_avg_monthly_gain_k",
                                                "expected_stat_meaning": "window_mean_monthly_change",
                                                "expected_unit": "千人/月",
                                            }
                                        ],
                                    },
                                    {
                                        "claim_id": "M2",
                                        "action": "REVISE",
                                        "statement": "经 2025-08-01 历史修正后，2025 年二季度美国非农就业显著放缓至月均 +63.7 千人，跌破 100 千人且明显弱于一季度",
                                        "rationale": "5 月与 6 月合计下修 258 千人，二季度月均由 +149.7 降至 +63.7 千人/月。",
                                        "metric_bindings": [
                                            {
                                                "artifact_id": comp["artifact_id"],
                                                "metric_key": "window_avg_monthly_gain_k",
                                                "expected_stat_meaning": "window_mean_monthly_change",
                                                "expected_unit": "千人/月",
                                            }
                                        ],
                                        "evidence_links": [
                                            {
                                                "artifact_id": comp["artifact_id"],
                                                "relation": "REFUTES",
                                                "rationale": "新批次月均 +63.7 千人/月推翻原高于 100 千人且回升的结论",
                                            }
                                        ],
                                    },
                                ],
                            },
                        )
                    ).content[0].text
                )
                assert prop["status"] == "READY"
                com = json.loads(
                    (await session.call_tool("commit_revision", {"proposal_id": prop["proposal_id"]})).content[0].text
                )
                assert com["revision"] == 2
                assert "+63.7" in Path(com["exports"]["html_path"]).read_text(encoding="utf-8")

    anyio.run(_run)
