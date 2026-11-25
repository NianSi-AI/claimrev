"""Tests for Milestone 1 additions:
1. Isolated `run_diagnostic` script sandbox + `DEFENDS_AGAINST` rendering with archived source code.
2. Local Human Decision Surface HTTP server (`approval_ui.py`): token auth, resolve & publish,
   idempotent replay, changed-option 409 conflict, and stale data batch 409 invalidation.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from claimrev.approval_ui import create_approval_server
from claimrev.errors import ValidationError
from claimrev.examples.lrd_width.seed import (
    DEFAULT_DATA_PATH,
    DEFAULT_SERIES_ID,
    DEFAULT_TASK_ID,
    build_registry,
    seed_initial_report,
)
from claimrev.service import ClaimRevService


def _http_request(url: str, *, method: str = "GET", headers: dict[str, str] | None = None, body: dict | None = None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def test_run_diagnostic_archives_code_and_renders_defense_badge(tmp_path: Path) -> None:
    seed_initial_report(tmp_path)
    svc = ClaimRevService(tmp_path, registry=build_registry())

    diag_script = """
import json, os, numpy as np
data = json.load(open(os.environ["CLAIMREV_DATA_PATH"]))["single_gaussian"]
lrd = [r for r in data if r["cls"] == "LRD"]
x = np.array([r["x"] for r in lrd])
y = np.array([r["y"] for r in lrd])
raw_std = float(np.std(y, ddof=1))
c = np.polyfit(x, y, 1)
ols_sd = float(np.std(y - np.polyval(c, x), ddof=1))
precise15 = sorted(lrd, key=lambda r: r["cov"][1][1])[:15]
xp = np.array([r["x"] for r in precise15])
yp = np.array([r["y"] for r in precise15])
cp = np.polyfit(xp, yp, 1)
ols_sd_p15 = float(np.std(yp - np.polyval(cp, xp), ddof=1))

out = {
    "metrics": {
        "lrd_raw_marginal_sd": {
            "metric_key": "lrd_raw_marginal_sd",
            "label": "目标组未固定亮度的总标准差（29 例）",
            "value": raw_std,
            "display_value": f"{raw_std:.3f}",
            "unit": "dex",
            "stat_meaning": "marginal_sample_sd",
            "sample_n": len(lrd),
        },
        "lrd_ols_resid_sd_all": {
            "metric_key": "lrd_ols_resid_sd_all",
            "label": "目标组普通回归残差标准差（未扣误差，29 例）",
            "value": ols_sd,
            "display_value": f"{ols_sd:.3f}",
            "unit": "dex",
            "stat_meaning": "ols_residual_sd",
            "sample_n": len(lrd),
        },
        "lrd_ols_resid_sd_precise15": {
            "metric_key": "lrd_ols_resid_sd_precise15",
            "label": "最高精度 15 例普通回归残差标准差（未扣误差）",
            "value": ols_sd_p15,
            "display_value": f"{ols_sd_p15:.3f}",
            "unit": "dex",
            "stat_meaning": "ols_residual_sd",
            "sample_n": len(precise15),
        },
    },
    "summary": {"critique_refuted": True},
}
json.dump(out, open(os.environ["CLAIMREV_OUTPUT_JSON"], "w"), ensure_ascii=False, indent=2)
print("diagnostic ok")
"""
    shock = svc.ingest_shock(
        task_id=DEFAULT_TASK_ID,
        shock_type="ADVERSARIAL_SHOCK",
        summary="审稿人质疑目标组 0.054 dex 散度是靠大测量误差硬吃出来的",
        content="直接算目标组 y 的标准差就有 0.106 dex，去掉大误差源并直接做 OLS 后散度一定会变大，请撤回结论 1。",
    )
    diag = svc.run_diagnostic(
        task_id=DEFAULT_TASK_ID,
        title="核查固定亮度前后的普通回归残差与高精度子样本散度",
        script_source=diag_script,
    )
    assert diag["evidence_source"] == "agent_diagnostic"
    assert diag["metrics"]["lrd_raw_marginal_sd"]["display_value"] == "0.106"
    assert diag["metrics"]["lrd_ols_resid_sd_all"]["display_value"] == "0.067"
    assert diag["metrics"]["lrd_ols_resid_sd_precise15"]["display_value"] == "0.049"

    prop = svc.propose_revision(
        task_id=DEFAULT_TASK_ID,
        shock_artifact_id=shock["artifact_id"],
        summary="独立诊断脚本证伪了审稿质疑，捍卫结论 1 不变。",
        changes=[
            {
                "claim_id": "C1",
                "action": "KEEP",
                "rationale": "0.106 dex 是未固定亮度的总标准差；固定亮度后未扣任何误差的普通回归残差仅 0.067 dex，最高精度 15 例更缩小至 0.049 dex，反驳了靠大误差压低散度的质疑。",
                "metric_bindings": [
                    {
                        "artifact_id": diag["artifact_id"],
                        "metric_key": "lrd_ols_resid_sd_all",
                        "expected_stat_meaning": "ols_residual_sd",
                        "expected_unit": "dex",
                    },
                    {
                        "artifact_id": diag["artifact_id"],
                        "metric_key": "lrd_ols_resid_sd_precise15",
                        "expected_stat_meaning": "ols_residual_sd",
                        "expected_unit": "dex",
                    },
                ],
                "evidence_links": [
                    {
                        "artifact_id": diag["artifact_id"],
                        "relation": "DEFENDS_AGAINST",
                        "rationale": "现场诊断脚本证实高精度子样本未扣误差残差反而更小（0.049 dex）",
                    }
                ],
            }
        ],
    )
    assert prop["status"] == "READY"
    commit = svc.commit_revision(proposal_id=prop["proposal_id"])
    assert commit["revision"] == 2

    html_text = Path(commit["exports"]["html_path"]).read_text(encoding="utf-8")
    assert "捍卫原结论 · 已驳回质疑" in html_text
    assert "Agent 现场编写并运行的诊断脚本" in html_text
    assert "lrd_ols_resid_sd_precise15" in html_text
    assert "0.049" in html_text


def test_run_diagnostic_rejects_broken_or_incomplete_script(tmp_path: Path) -> None:
    seed_initial_report(tmp_path)
    svc = ClaimRevService(tmp_path, registry=build_registry())

    with pytest.raises(ValidationError):
        svc.run_diagnostic(task_id=DEFAULT_TASK_ID, title="bad exit", script_source="raise RuntimeError('boom')")

    with pytest.raises(ValidationError):
        svc.run_diagnostic(task_id=DEFAULT_TASK_ID, title="no output", script_source="print('forgot to write')")


def _open_snr15_decision(svc: ClaimRevService) -> dict:
    snr15 = svc.run_computation(
        task_id=DEFAULT_TASK_ID,
        computation_name="lrd_width_sensitivity",
        series_id=DEFAULT_SERIES_ID,
        params={"width_measure": "single_gaussian", "broad_snr_min": 15, "drop_comparison_widest_k": 0},
    )
    st = svc.get_task_state(task_id=DEFAULT_TASK_ID)
    c1 = next(c for c in st["claims"] if c["claim_id"] == "C1")
    new_scope = json.loads(json.dumps(c1["scope"]))
    new_scope["inclusion_rules"]["broad_snr_min"] = 15
    return svc.propose_revision(
        task_id=DEFAULT_TASK_ID,
        summary="拟将结论 1 默认信噪比门槛从 5 提高至 15",
        changes=[
            {
                "claim_id": "C1",
                "action": "REVISE",
                "statement": "在高信噪比默认口径下，目标组线宽本征散度保持较小",
                "rationale": "新门槛下目标组由 29 例降至 12 例，散度中位数为 0.059 dex。",
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
                    {"artifact_id": snr15["artifact_id"], "relation": "SUPPORTS", "rationale": "高信噪比复算"}
                ],
            }
        ],
    )


def test_local_approval_web_ui_auth_resolve_idempotency_and_conflict(tmp_path: Path) -> None:
    seed_initial_report(tmp_path)
    svc = ClaimRevService(tmp_path, registry=build_registry())
    prop = _open_snr15_decision(svc)
    did = prop["decision_id"]

    server, url, token = create_approval_server(svc, decision_id=did, port=0)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        # 1. Missing/wrong token MUST be rejected with 403
        code, _ = _http_request(f"{base}/")
        assert code == 403
        code, _ = _http_request(
            f"{base}/api/resolve",
            method="POST",
            body={"decision_id": did, "selected_option": "approve_scope_change"},
        )
        assert code == 403

        # 2. Valid token renders human-friendly decision card
        code, page = _http_request(url)
        assert code == 200
        assert "人工拍板" in page
        assert "approve_scope_change" in page
        assert "keep_baseline_scope" in page

        # 3. Valid approval commits Rev 2
        code, body_txt = _http_request(
            f"{base}/api/resolve",
            method="POST",
            headers={"X-ClaimRev-Token": token},
            body={"decision_id": did, "selected_option": "approve_scope_change", "idempotency_key": "web-k1"},
        )
        assert code == 200
        res1 = json.loads(body_txt)
        assert res1["committed_revision"] == 2
        assert res1["idempotent_replay"] is False

        # 4. Published diff HTML is immediately served over the same local server
        code, diff_html = _http_request(f"{base}/pub/{DEFAULT_TASK_ID}/latest_diff.html")
        assert code == 200
        assert "已由人工拍板发布" in diff_html

        # 5. Same-option retry is idempotent (still Rev 2)
        code, body_txt2 = _http_request(
            f"{base}/api/resolve",
            method="POST",
            headers={"X-ClaimRev-Token": token},
            body={"decision_id": did, "selected_option": "approve_scope_change", "idempotency_key": "web-k1"},
        )
        assert code == 200
        assert json.loads(body_txt2)["committed_revision"] == 2
        assert json.loads(body_txt2)["idempotent_replay"] is True

        # 6. Different option after confirmation returns 409 ERR_DECISION_CONFLICT
        code, err_txt = _http_request(
            f"{base}/api/resolve",
            method="POST",
            headers={"X-ClaimRev-Token": token},
            body={"decision_id": did, "selected_option": "keep_baseline_scope", "idempotency_key": "web-k2"},
        )
        assert code == 409
        assert json.loads(err_txt)["error"] == "ERR_DECISION_CONFLICT"
    finally:
        server.shutdown()
        server.server_close()


def test_local_approval_web_ui_returns_409_when_data_batch_swapped(tmp_path: Path) -> None:
    seed_initial_report(tmp_path)
    svc = ClaimRevService(tmp_path, registry=build_registry())
    prop = _open_snr15_decision(svc)
    did = prop["decision_id"]

    # Swap dependent data batch BEFORE human approves
    raw = json.loads(DEFAULT_DATA_PATH.read_text(encoding="utf-8"))
    raw["single_gaussian"][0]["broad_SNR"] = float(raw["single_gaussian"][0]["broad_SNR"]) + 0.05
    b2_file = tmp_path / "batch2.json"
    b2_file.write_text(json.dumps(raw), encoding="utf-8")
    svc.ingest_data_batch(task_id=DEFAULT_TASK_ID, series_id=DEFAULT_SERIES_ID, file_path=b2_file)

    server, url, token = create_approval_server(svc, decision_id=did, port=0)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        code, page = _http_request(url)
        assert code == 200
        assert "此审批卡已失效" in page

        code, err_txt = _http_request(
            f"{base}/api/resolve",
            method="POST",
            headers={"X-ClaimRev-Token": token},
            body={"decision_id": did, "selected_option": "approve_scope_change"},
        )
        assert code == 409
        assert json.loads(err_txt)["error"] == "ERR_STALE_DATA_BATCH"
    finally:
        server.shutdown()
        server.server_close()
