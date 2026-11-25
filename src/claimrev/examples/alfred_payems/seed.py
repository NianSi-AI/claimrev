"""Seeders for real ALFRED PAYEMS historical vintage revision scenarios.

Provides two mirror pairs strictly respecting real-time historical information sets
(zero look-ahead contamination):
  1. `seed_payems_2025q2_flip`:
     - Frozen T0 vintage: 2025-07-03 (2025 Q1 avg +111.0k, Q2 avg +149.7k)
     - Subsequent T1 vintage: 2025-08-01 (Q1 unchanged +111.0k; Q2 revised down -258k to +63.7k -> FLIPS Q2 claim!)
  2. `seed_payems_2024q3_hold`:
     - Frozen T0 vintage: 2024-10-04 (2024 Q2 avg +147.3k, Q3 avg +185.7k)
     - Subsequent T1 vintage: 2024-12-06 (Q2 unchanged +147.3k; Q3 revised down -80k to +159.0k -> STILL HOLDS >150k & >Q2!)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from claimrev.computations import ComputationRegistry
from claimrev.service import ClaimRevService

from .computations import register as register_payems_computations

DATA_DIR = Path(__file__).parent / "data"
DEFAULT_SERIES_ID = "alfred_payems"


def build_registry() -> ComputationRegistry:
    reg = ComputationRegistry()
    register_payems_computations(reg)
    return reg


def _seed_pair(
    store_dir: str | Path,
    *,
    task_id: str,
    title: str,
    description: str,
    t0_vintage: str,
    q1_months: list[str],
    q1_label: str,
    q1_threshold_k: float,
    q1_statement: str,
    q2_months: list[str],
    q2_label: str,
    q2_threshold_k: float,
    q2_statement: str,
    ingest_t1_vintage: str | None = None,
) -> dict[str, Any]:
    svc = ClaimRevService(store_dir, registry=build_registry())
    t0_file = DATA_DIR / f"payems_vintage_{t0_vintage}.csv"

    svc.create_task(task_id=task_id, title=title, description=description)
    batch_t0 = svc.ingest_data_batch(
        task_id=task_id,
        series_id=DEFAULT_SERIES_ID,
        file_path=t0_file,
        vintage_tag=t0_vintage,
        release_key=f"PAYEMS@{t0_vintage}",
        description=f"美联储 ALFRED 美国非农就业（PAYEMS）截至 {t0_vintage} 冻结历史发布版本",
    )

    c1_comp = svc.run_computation(
        task_id=task_id,
        computation_name="payems_window_summary",
        series_id=DEFAULT_SERIES_ID,
        batch_id=batch_t0["batch_id"],
        params={
            "window_months": q1_months,
            "window_label": q1_label,
            "threshold_k": q1_threshold_k,
        },
    )
    c2_comp = svc.run_computation(
        task_id=task_id,
        computation_name="payems_window_summary",
        series_id=DEFAULT_SERIES_ID,
        batch_id=batch_t0["batch_id"],
        params={
            "window_months": q2_months,
            "window_label": q2_label,
            "compare_window_months": q1_months,
            "compare_label": q1_label,
            "threshold_k": q2_threshold_k,
        },
    )

    m1_avg = c1_comp["metrics"]["window_avg_monthly_gain_k"]
    m2_avg = c2_comp["metrics"]["window_avg_monthly_gain_k"]
    m2_diff = c2_comp["metrics"]["avg_gain_diff_vs_compare_k"]

    imported = svc.import_initial_claims(
        task_id=task_id,
        summary=f"基于 ALFRED 截至 {t0_vintage} 可见数据生成第 1 版宏观研报。",
        claims=[
            {
                "claim_id": "M1",
                "status": "ACTIVE",
                "location": f"§1 · {q1_label}就业基线",
                "statement": q1_statement,
                "rationale": (
                    f"按 {t0_vintage} 当时可见的 ALFRED 数据，{q1_label}月均新增非农为 "
                    f"{m1_avg['display_value']} 千人/月，高于 {int(q1_threshold_k)} 千人/月门槛。"
                ),
                "scope": {
                    "series": "PAYEMS",
                    "observation_months": q1_months,
                    "estimator": "window_mean_monthly_change",
                    "threshold_k": int(q1_threshold_k),
                    "unit": "千人/月",
                },
                "data_series_ids": [DEFAULT_SERIES_ID],
                "depends_on_claims": [],
                "metric_bindings": [
                    {
                        "artifact_id": c1_comp["artifact_id"],
                        "metric_key": "window_avg_monthly_gain_k",
                        "expected_stat_meaning": "window_mean_monthly_change",
                        "expected_unit": "千人/月",
                    }
                ],
                "evidence_links": [
                    {
                        "artifact_id": c1_comp["artifact_id"],
                        "relation": "SUPPORTS",
                        "rationale": f"{t0_vintage} 冻结版本计算得到 {q1_label} 月均新增 {m1_avg['display_value']} 千人/月",
                    }
                ],
            },
            {
                "claim_id": "M2",
                "status": "ACTIVE",
                "location": f"§2 · {q2_label}就业动能",
                "statement": q2_statement,
                "rationale": (
                    f"按 {t0_vintage} 当时可见的 ALFRED 数据，{q2_label}月均新增非农为 "
                    f"{m2_avg['display_value']} 千人/月（高于 {int(q2_threshold_k)} 千人/月），"
                    f"且较{q1_label}高出 {m2_diff['display_value']} 千人/月。"
                ),
                "scope": {
                    "series": "PAYEMS",
                    "observation_months": q2_months,
                    "compare_observation_months": q1_months,
                    "estimator": "window_mean_monthly_change_and_acceleration",
                    "threshold_k": int(q2_threshold_k),
                    "unit": "千人/月",
                },
                "data_series_ids": [DEFAULT_SERIES_ID],
                "depends_on_claims": ["M1"],
                "metric_bindings": [
                    {
                        "artifact_id": c2_comp["artifact_id"],
                        "metric_key": "window_avg_monthly_gain_k",
                        "expected_stat_meaning": "window_mean_monthly_change",
                        "expected_unit": "千人/月",
                    },
                    {
                        "artifact_id": c2_comp["artifact_id"],
                        "metric_key": "avg_gain_diff_vs_compare_k",
                        "expected_stat_meaning": "window_mean_change_difference",
                        "expected_unit": "千人/月",
                    },
                ],
                "evidence_links": [
                    {
                        "artifact_id": c2_comp["artifact_id"],
                        "relation": "SUPPORTS",
                        "rationale": f"{t0_vintage} 冻结版本显示 {q2_label} 动能高于门槛且快于 {q1_label}",
                    }
                ],
            },
        ],
    )

    batch_t1 = None
    if ingest_t1_vintage:
        t1_file = DATA_DIR / f"payems_vintage_{ingest_t1_vintage}.csv"
        batch_t1 = svc.ingest_data_batch(
            task_id=task_id,
            series_id=DEFAULT_SERIES_ID,
            file_path=t1_file,
            vintage_tag=ingest_t1_vintage,
            release_key=f"PAYEMS@{ingest_t1_vintage}",
            description=f"美联储 ALFRED 美国非农就业（PAYEMS）{ingest_t1_vintage} 新发布批次（含历史修正）",
        )

    return {
        "store_dir": str(Path(store_dir).resolve()),
        "task_id": task_id,
        "batch_t0": batch_t0,
        "batch_t1": batch_t1,
        "imported": imported,
    }


def seed_payems_2025q2_flip(store_dir: str | Path, *, ingest_t1: bool = True) -> dict[str, Any]:
    """Scenario M1 (Revision FLIPS claim): 2025-07-03 -> 2025-08-01 (-258k May-Jun revision)."""
    return _seed_pair(
        store_dir,
        task_id="payems_2025q2_revision",
        title="美国非农就业（PAYEMS）2025 年二季度动能跟踪报告",
        description=(
            "基于美联储 ALFRED 实时历史版本（PAYEMS，单位：千人）。"
            "第 1 版严格冻结在 2025-07-03 发布日当时可见的信息集。"
        ),
        t0_vintage="2025-07-03",
        q1_months=["2025-01", "2025-02", "2025-03"],
        q1_label="2025 年一季度",
        q1_threshold_k=100.0,
        q1_statement="2025 年一季度美国非农就业保持温和扩张，月均新增高于 100 千人",
        q2_months=["2025-04", "2025-05", "2025-06"],
        q2_label="2025 年二季度",
        q2_threshold_k=100.0,
        q2_statement="2025 年二季度美国非农就业月均新增维持在 100 千人以上，且增速较一季度明显回升",
        ingest_t1_vintage="2025-08-01" if ingest_t1 else None,
    )


def seed_payems_2024q3_hold(store_dir: str | Path, *, ingest_t1: bool = True) -> dict[str, Any]:
    """Scenario M2 (Mirror — data revised -80k, yet claim STILL HOLDS): 2024-10-04 -> 2024-12-06."""
    return _seed_pair(
        store_dir,
        task_id="payems_2024q3_revision",
        title="美国非农就业（PAYEMS）2024 年三季度动能跟踪报告",
        description=(
            "基于美联储 ALFRED 实时历史版本（PAYEMS，单位：千人）。"
            "第 1 版严格冻结在 2024-10-04 发布日当时可见的信息集。"
        ),
        t0_vintage="2024-10-04",
        q1_months=["2024-04", "2024-05", "2024-06"],
        q1_label="2024 年二季度",
        q1_threshold_k=120.0,
        q1_statement="2024 年二季度美国非农就业月均新增高于 120 千人",
        q2_months=["2024-07", "2024-08", "2024-09"],
        q2_label="2024 年三季度",
        q2_threshold_k=150.0,
        q2_statement="2024 年三季度美国非农就业月均新增超过 150 千人，且增速高于二季度",
        ingest_t1_vintage="2024-12-06" if ingest_t1 else None,
    )
