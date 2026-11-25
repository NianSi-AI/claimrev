"""Seed an initial Revision 1 report for the JWST LRD width-luminosity example.

Computes all baseline metrics from the public regression_inputs.json at runtime
(zero hardcoded reference intervals).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from claimrev.computations import ComputationRegistry
from claimrev.service import ClaimRevService

from .computations import register as register_lrd_computations
from .stats import ALGORITHM_COMMIT

DEFAULT_DATA_PATH = Path(__file__).parent / "data" / "regression_inputs.json"
DEFAULT_TASK_ID = "lrd_width_luminosity"
DEFAULT_SERIES_ID = "lrd_regression_inputs"


def build_registry() -> ComputationRegistry:
    registry = ComputationRegistry()
    register_lrd_computations(registry)
    return registry


def seed_initial_report(
    store_dir: str | Path,
    *,
    data_path: str | Path = DEFAULT_DATA_PATH,
    task_id: str = DEFAULT_TASK_ID,
) -> dict[str, Any]:
    """Import Revision 1 report into store_dir using live baseline computation from data_path."""
    registry = build_registry()
    svc = ClaimRevService(store_dir, registry=registry)

    svc.create_task(
        task_id=task_id,
        title="JWST 小红点（LRD）宽线宽度—光度关系报告",
        description=(
            "基于公开论文 NianSi-AI/lrd-width-luminosity (v2) 的 Hα 宽线本征散度分析报告。"
            "基线口径：single_gaussian，宽线信噪比 S/N ≥ 5（另有基线质量条件），29 个目标源与 15 个对照源。"
            "其中 C2 的初始强表述作为待敏感性检验的演示草稿。"
        ),
    )

    batch = svc.ingest_data_batch(
        task_id=task_id,
        series_id=DEFAULT_SERIES_ID,
        file_path=Path(data_path),
        vintage_tag=f"paper-v2-{ALGORITHM_COMMIT[:8]}",
        release_key=f"lrd_regression_inputs@{ALGORITHM_COMMIT[:12]}",
        description="公开论文 v2 regression_inputs.json（含 single_gaussian 29 个目标源与 15 个对照源）",
    )

    baseline = svc.run_computation(
        task_id=task_id,
        computation_name="lrd_width_sensitivity",
        series_id=DEFAULT_SERIES_ID,
        batch_id=batch["batch_id"],
        params={
            "width_measure": "single_gaussian",
            "broad_snr_min": 5,
            "drop_comparison_widest_k": 0,
        },
    )

    baseline_art_id = baseline["artifact_id"]
    m_target_med = baseline["metrics"]["target_sigma_median"]
    m_target_u95 = baseline["metrics"]["target_sigma_upper95_onesided"]
    m_diff_ci = baseline["metrics"]["sigma_diff_ci95"]

    base_c1_scope = {
        "population": "target_LRD",
        "width_measure": "single_gaussian",
        "inclusion_rules": {
            "broad_snr_min": 5,
            "quality_conditions": "baseline_table_quality_cuts",
        },
        "estimator": "hierarchical_grid_posterior_intrinsic_scatter",
        "unit": "dex",
    }
    base_c2_scope = {
        "population": "target_LRD_vs_small_comparison",
        "width_measure": "single_gaussian",
        "inclusion_rules": {
            "broad_snr_min": 5,
            "quality_conditions": "baseline_table_quality_cuts",
        },
        "estimator": "intrinsic_scatter_difference_ci95",
        "unit": "dex",
    }

    imported = svc.import_initial_claims(
        task_id=task_id,
        summary="导入已有基线报告（29 个目标天体与 15 个对照天体，单高斯拟合）。",
        claims=[
            {
                "claim_id": "C1",
                "status": "ACTIVE",
                "location": "§III.1 主结果 · 目标组本征散度",
                "statement": "在固定 Hα 亮度下，目标组的线宽本征散度很小",
                "rationale": (
                    f"基于 29 个目标天体（单高斯拟合、宽线信噪比 ≥ 5 及质量条件），"
                    f"线宽本征散度的后验中位数为 {m_target_med['display_value']} dex（点估计），"
                    f"单侧 95% 上限为 {m_target_u95['display_value']} dex。"
                ),
                "scope": base_c1_scope,
                "data_series_ids": [DEFAULT_SERIES_ID],
                "depends_on_claims": [],
                "metric_bindings": [
                    {
                        "artifact_id": baseline_art_id,
                        "metric_key": "target_sigma_median",
                        "expected_stat_meaning": "point_estimate_posterior_median",
                        "expected_unit": "dex",
                        "role": "primary_point_estimate",
                    },
                    {
                        "artifact_id": baseline_art_id,
                        "metric_key": "target_sigma_upper95_onesided",
                        "expected_stat_meaning": "one_sided_95_upper_bound",
                        "expected_unit": "dex",
                        "role": "baseline_upper_limit",
                    },
                ],
                "evidence_links": [
                    {
                        "artifact_id": baseline_art_id,
                        "relation": "SUPPORTS",
                        "rationale": "基线 29 个目标源回归后验支持低本征散度估计",
                    }
                ],
            },
            {
                "claim_id": "C2",
                "status": "ACTIVE",
                "location": "§III.3 与表 2 · 小对照组比较（演示草稿）",
                "statement": "小对照组能独立提供稳健的组间差异证据",
                "rationale": (
                    f"完整 15 个对照源时，“目标组减对照组”的散度差 95% 可信区间为 "
                    f"{m_diff_ci['display_value']} dex（不含零），演示草稿据此作出强表述。"
                ),
                "scope": base_c2_scope,
                "data_series_ids": [DEFAULT_SERIES_ID],
                "depends_on_claims": ["C1"],
                "metric_bindings": [
                    {
                        "artifact_id": baseline_art_id,
                        "metric_key": "sigma_diff_ci95",
                        "expected_stat_meaning": "central_95_credible_interval",
                        "expected_unit": "dex",
                        "role": "baseline_group_diff_ci95",
                    }
                ],
                "evidence_links": [
                    {
                        "artifact_id": baseline_art_id,
                        "relation": "SUPPORTS",
                        "rationale": "基线 15 个对照源散度差 95% 可信区间不含零（待极端值敏感性检验）",
                    }
                ],
            },
        ],
    )

    return {
        "store_dir": str(Path(store_dir).resolve()),
        "task_id": task_id,
        "batch": batch,
        "baseline_computation": {
            "artifact_id": baseline_art_id,
            "summary": baseline["summary"],
        },
        "imported": imported,
    }
