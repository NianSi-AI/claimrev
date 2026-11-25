"""End-to-end stdio MCP protocol test using the official Python MCP client SDK."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from claimrev.examples.lrd_width.seed import seed_initial_report


def test_mcp_stdio_end_to_end_and_human_approval_boundary(tmp_path: Path) -> None:
    seed_initial_report(tmp_path)
    mcp_log = tmp_path / "mcp_calls.jsonl"

    async def _run() -> None:
        params = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "claimrev.mcp_server",
                "--store",
                str(tmp_path),
                "--plugin",
                "claimrev.examples.lrd_width.computations:register",
                "--log",
                str(mcp_log),
            ],
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                # 1. Verify exact tool surface (NO resolve_decision / approve!)
                tools_res = await session.list_tools()
                tool_names = [t.name for t in tools_res.tools]
                assert tool_names == [
                    "get_task_state",
                    "get_claim",
                    "ingest_shock",
                    "run_computation",
                    "run_diagnostic",
                    "propose_revision",
                    "commit_revision",
                ]
                assert "resolve_decision" not in tool_names
                assert "approve" not in tool_names

                # 2. Read task state
                st_res = await session.call_tool("get_task_state", {})
                state = json.loads(st_res.content[0].text)
                assert state["task"]["current_revision"] == 1
                assert [c["claim_id"] for c in state["claims"]] == ["C1", "C2"]
                baseline_art = state["claims"][1]["bound_metrics"][0]["artifact_id"]

                # 3. Register shock
                sh_res = await session.call_tool(
                    "ingest_shock",
                    {
                        "shock_type": "ADVERSARIAL_SHOCK",
                        "summary": "去掉对照组最宽两例后的敏感性检验",
                        "content": "如果去掉对照组里线宽最大的两个天体，报告里的哪句话需要改？",
                        "release_key": "reviewer-drop-wide2",
                    },
                )
                shock = json.loads(sh_res.content[0].text)
                assert shock["deduplicated"] is False

                # 4. Run computation
                comp_res = await session.call_tool(
                    "run_computation",
                    {
                        "computation_name": "lrd_width_sensitivity",
                        "params": {"width_measure": "single_gaussian", "broad_snr_min": 5, "drop_comparison_widest_k": 2},
                    },
                )
                comp = json.loads(comp_res.content[0].text)
                assert comp["metrics"]["target_sigma_median"]["display_value"] == "0.054"
                assert comp["metrics"]["sigma_diff_ci95"]["display_value"] == "−0.119 至 +0.002"
                assert comp["metrics"]["sigma_diff_ci95"]["contains_zero"] is True

                # 5. Propose local revision
                prop_res = await session.call_tool(
                    "propose_revision",
                    {
                        "summary": "去掉对照组线宽最大两例后的局部修订：保留 C1，收窄 C2",
                        "shock_artifact_id": shock["artifact_id"],
                        "changes": [
                            {
                                "claim_id": "C1",
                                "action": "KEEP",
                                "rationale": "29 个目标的样本和拟合口径均相同，散度的后验中位数仍为 0.054 dex。",
                                "metric_bindings": [
                                    {
                                        "artifact_id": comp["artifact_id"],
                                        "metric_key": "target_sigma_median",
                                        "expected_stat_meaning": "point_estimate_posterior_median",
                                        "expected_unit": "dex",
                                    }
                                ],
                            },
                            {
                                "claim_id": "C2",
                                "action": "NARROW",
                                "statement": "完整 15 个对照时组间散度差 95% 可信区间不含零，但去掉最宽两例后区间包含零，因此小对照组不能独立提供稳健的组间差异证据",
                                "rationale": (
                                    "用完整的 15 个对照时，“目标组减对照组”的散度差 95% 可信区间为 −0.199 至 −0.043 dex。"
                                    "去掉最宽两例（RUBIES-EGS-15825、Skyfire-504101）、剩 13 个对照时，区间为 −0.119 至 +0.002 dex，包含零。"
                                    "这项组间比较对两例极端值敏感。"
                                ),
                                "metric_bindings": [
                                    {
                                        "artifact_id": baseline_art,
                                        "metric_key": "sigma_diff_ci95",
                                        "expected_stat_meaning": "central_95_credible_interval",
                                        "expected_unit": "dex",
                                        "role": "baseline_15_comparison",
                                    },
                                    {
                                        "artifact_id": comp["artifact_id"],
                                        "metric_key": "sigma_diff_ci95",
                                        "expected_stat_meaning": "central_95_credible_interval",
                                        "expected_unit": "dex",
                                        "role": "no_wide2_13_comparison",
                                    },
                                ],
                                "evidence_links": [
                                    {
                                        "artifact_id": comp["artifact_id"],
                                        "relation": "REFUTES",
                                        "rationale": "去掉最宽两例后散度差 95% 可信区间包含零",
                                    }
                                ],
                            },
                        ],
                    },
                )
                prop = json.loads(prop_res.content[0].text)
                assert prop["status"] == "READY"
                assert prop["requires_decision"] is False

                # 6. Commit revision
                com_res = await session.call_tool("commit_revision", {"proposal_id": prop["proposal_id"]})
                commit = json.loads(com_res.content[0].text)
                assert commit["revision"] == 2
                assert Path(commit["exports"]["md_path"]).is_file()
                assert Path(commit["exports"]["html_path"]).is_file()
                html_text = Path(commit["exports"]["html_path"]).read_text(encoding="utf-8")
                assert "0.054" in html_text
                assert "−0.199 至 −0.043" in html_text
                assert "−0.119 至 +0.002" in html_text
                assert "RUBIES-EGS-15825" in html_text
                assert "Skyfire-504101" in html_text

    anyio.run(_run)
    assert mcp_log.is_file()
    lines = [json.loads(line) for line in mcp_log.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert [x["tool"] for x in lines] == [
        "get_task_state",
        "ingest_shock",
        "run_computation",
        "propose_revision",
        "commit_revision",
    ]
