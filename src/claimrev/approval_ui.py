"""Thin local Human Decision Surface (127.0.0.1 + random one-time management token).

Pure Python stdlib `http.server` — zero external frontend build chain.
Provides a calm, plain-language browser approval card so the human user does NOT
have to manually type CLI flags to resolve scope-shift or objective-fork cards,
while keeping approval strictly out-of-band from the MCP tool surface.
"""

from __future__ import annotations

import hmac
import html
import json
import secrets
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .errors import (
    ClaimRevError,
    DecisionConflictError,
    IdempotencyConflictError,
    NotFoundError,
    StaleClaimVersionError,
    StaleDataBatchError,
)
from .service import ClaimRevService

REASON_LABELS = {
    "UNAPPROVED_SCOPE_SHIFT": "涉及修改正式报告的默认样本 / 筛选口径（AI 已暂停，需你亲自确认）",
    "OBJECTIVE_FORK": "存在目标取舍分叉（需你亲自拍板）",
}

SCOPE_KEY_LABELS = {
    "population": "研究总体",
    "estimator": "统计估计方法",
    "unit": "指标单位",
    "inclusion_rules.quality_conditions": "样本质量筛选条件",
}


def _friendly_scope_key(path: str) -> str:
    if path in SCOPE_KEY_LABELS:
        return SCOPE_KEY_LABELS[path]
    if path.startswith("inclusion_rules."):
        return f"筛选规则 · {path.split('.', 1)[1]}"
    return path


def render_decision_page_html(decision: dict[str, Any], token: str) -> str:
    """Render a clean, human-first single-page approval card."""
    did = decision["decision_id"]
    task = decision["task"]
    status = decision["status"]
    freshness = decision["freshness"]
    reason_text = REASON_LABELS.get(decision["reason_code"], decision["reason_code"])
    shock = decision.get("shock") or {}
    shock_text = shock.get("content") or decision.get("summary") or ""
    scope_diffs = decision.get("scope_diffs") or {}
    current_claims = decision.get("current_claims") or {}
    options = decision.get("options") or []

    # Build readable scope diff table
    diff_rows: list[str] = []
    for cid, fields in scope_diffs.items():
        c_info = current_claims.get(cid, {})
        c_loc = c_info.get("location") or cid
        for fpath, chg in fields.items():
            diff_rows.append(
                f"<tr><td>{html.escape(c_loc)}</td><td>{html.escape(_friendly_scope_key(fpath))}</td>"
                f"<td><span class='val-old'>{html.escape(str(chg.get('old')))}</span></td>"
                f"<td><span class='val-new'>{html.escape(str(chg.get('new')))}</span></td></tr>"
            )
    scope_table_html = ""
    if diff_rows:
        scope_table_html = f"""
      <section class="card">
        <h2>具体规则变动</h2>
        <table class="mini-table">
          <thead><tr><th>受影响位置</th><th>变动项</th><th>当前默认口径</th><th>拟调整口径</th></tr></thead>
          <tbody>{"".join(diff_rows)}</tbody>
        </table>
      </section>"""

    # Build readable option cards with before/after preview
    opt_cards: list[str] = []
    for idx, opt in enumerate(options):
        oid = opt["option_id"]
        label = opt.get("label", oid)
        desc = opt.get("description", "")
        changes = opt.get("changes", [])
        previews: list[str] = []
        for ch in changes:
            c_loc = ch.get("location") or ch.get("claim_id", "")
            metrics_str = " · ".join(
                f"{m.get('label', m['metric_key'])}：{m.get('display_value')} {m.get('unit', '')}".strip()
                for m in ch.get("bound_metrics", [])
            )
            previews.append(
                f"<div class='opt-preview'>"
                f"<div><strong>{html.escape(c_loc)}</strong> → {html.escape(ch.get('statement', ''))}</div>"
                f"<div class='opt-rationale'>{html.escape(ch.get('rationale', ''))}</div>"
                f"{('<div class=\"opt-metrics\">' + html.escape(metrics_str) + '</div>') if metrics_str else ''}"
                f"</div>"
            )
        checked = "checked" if idx == 0 else ""
        disabled_attr = "" if (status == "PENDING" and freshness.get("valid")) else "disabled"
        selected_cls = " opt-selected" if decision.get("selected_option") == oid else ""
        opt_cards.append(
            f"""        <label class="opt-card{selected_cls}">
          <div class="opt-head">
            <input type="radio" name="selected_option" value="{html.escape(oid)}" {checked} {disabled_attr} />
            <span class="opt-title">{html.escape(label)}</span>
          </div>
          <div class="opt-desc">{html.escape(desc)}</div>
          {"".join(previews)}
        </label>"""
        )

    # Status banner & human-readable selected option label
    pub_link_html = ""
    pub_url = ""
    if decision.get("committed_revision") is not None:
        tid_enc = urllib.parse.quote(task["id"])
        pub_url = f"/pub/{tid_enc}/latest_diff.html"
        pub_link_html = f' <a class="pub-btn" href="{pub_url}" target="_blank">查看已发布的第 {decision["committed_revision"]} 版差异页 →</a>'

    selected_id = decision.get("selected_option")
    selected_label = next(
        (o.get("label", selected_id) for o in options if o.get("option_id") == selected_id),
        selected_id,
    )

    if status == "RESOLVED":
        banner_html = (
            f'<div class="banner banner-ok"><strong>已确认并发布第 {decision.get("committed_revision")} 版。</strong> '
            f'你的选择：<strong>{html.escape(str(selected_label))}</strong>。{pub_link_html}</div>'
        )
    elif status == "SUPERSEDED" or not freshness.get("valid"):
        reasons_txt = "；".join(
            f"数据系列 {r.get('series_id')} 已更新批次" if r.get("kind") == "DATA_BATCH_CHANGED"
            else f"结论 {r.get('claim_id')} 已更新至 v{r.get('current_version')}"
            for r in freshness.get("stale_reasons", [])
        )
        banner_html = (
            f'<div class="banner banner-stale"><strong>此审批卡已失效，无法提交：</strong>'
            f'{html.escape(reasons_txt or "关联数据或结论已发生变动")}。请回到对话基于最新数据重新评估。</div>'
        )
    else:
        banner_html = f'<div class="banner banner-wait"><strong>等待你拍板：</strong>{html.escape(reason_text)}</div>'

    can_submit = status == "PENDING" and freshness.get("valid")
    if can_submit:
        submit_html = """      <div class="actions">
        <button id="approve-btn" class="primary-btn" type="button" onclick="submitDecision()">确认选择并发布报告</button>
        <span id="status-msg" class="status-msg"></span>
      </div>"""
    elif status == "RESOLVED":
        open_btn = (
            f'<a class="secondary-btn" href="{pub_url}" target="_blank">打开第 {decision.get("committed_revision")} 版差异页 →</a>'
            if pub_url
            else ""
        )
        submit_html = f"""      <div class="actions locked-bar">
        <span class="locked-note">此审批单已完成拍板并锁定，不可重复提交。如需调整请发起新提案。</span>
        {open_btn}
      </div>"""
    else:
        submit_html = """      <div class="actions locked-bar">
        <span class="locked-note">此审批单已失效并锁定，请回到对话基于最新数据重新生成提案。</span>
      </div>"""

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>人工拍板 — {html.escape(task['title'])}</title>
  <style>
    :root {{
      --bg: #f8fafc; --card: #ffffff; --ink: #0f172a; --muted: #475569; --border: #e2e8f0;
      --accent: #2563eb; --accent-hover: #1d4ed8;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0; padding: 28px 20px 64px; background: var(--bg); color: var(--ink);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Noto Sans CJK SC", sans-serif;
      line-height: 1.65;
    }}
    .container {{ max-width: 860px; margin: 0 auto; }}
    header {{ margin-bottom: 16px; }}
    .eyebrow {{ font-size: 0.78rem; font-weight: 650; color: var(--accent); letter-spacing: 0.03em; margin-bottom: 4px; }}
    h1 {{ font-size: 1.35rem; margin: 0 0 6px; font-weight: 680; }}
    h2 {{ font-size: 1.0rem; margin: 0 0 12px; font-weight: 650; }}
    .meta {{ color: var(--muted); font-size: 0.85rem; }}
    .banner {{
      border-radius: 10px; padding: 13px 16px; margin-bottom: 16px; font-size: 0.93rem;
    }}
    .banner-wait {{ background: #fffbeb; border: 1px solid #fde68a; color: #92400e; }}
    .banner-ok {{ background: #ecfdf5; border: 1px solid #a7f3d0; color: #065f46; }}
    .banner-stale {{ background: #fef2f2; border: 1px solid #fecaca; color: #991b1b; }}
    .card {{
      background: var(--card); border: 1px solid var(--border); border-radius: 12px;
      padding: 18px 20px; margin-bottom: 16px; box-shadow: 0 1px 2px rgba(15,23,42,0.03);
    }}
    .trigger-quote {{
      margin: 0; padding: 10px 14px; background: #f8fafc; border-left: 3px solid #94a3b8;
      border-radius: 0 8px 8px 0; font-size: 0.94rem; color: #1e293b;
    }}
    table.mini-table {{ width: 100%; border-collapse: collapse; font-size: 0.9rem; }}
    table.mini-table th, table.mini-table td {{
      text-align: left; padding: 9px 12px; border-bottom: 1px solid var(--border);
    }}
    table.mini-table th {{ background: #f1f5f9; color: var(--muted); font-weight: 600; font-size: 0.82rem; }}
    .val-old {{ background: #f1f5f9; padding: 2px 8px; border-radius: 5px; font-weight: 600; }}
    .val-new {{ background: #fffbeb; color: #92400e; padding: 2px 8px; border-radius: 5px; font-weight: 650; }}
    .opt-card {{
      display: block; border: 1.5px solid var(--border); border-radius: 10px;
      padding: 14px 16px; margin-bottom: 12px; cursor: pointer; transition: border-color .15s, background .15s;
    }}
    .opt-card:hover {{ border-color: #93c5fd; background: #f8fafc; }}
    .opt-selected {{ border-color: #10b981; background: #ecfdf5; }}
    .opt-head {{ display: flex; align-items: center; gap: 10px; margin-bottom: 6px; }}
    .opt-title {{ font-weight: 650; font-size: 0.98rem; }}
    .opt-desc {{ font-size: 0.88rem; color: var(--muted); margin-bottom: 8px; }}
    .opt-preview {{
      background: #f8fafc; border: 1px solid var(--border); border-radius: 8px;
      padding: 10px 12px; margin-top: 8px; font-size: 0.88rem;
    }}
    .opt-rationale {{ color: var(--muted); margin-top: 4px; }}
    .opt-metrics {{ margin-top: 6px; font-size: 0.82rem; color: #1e293b; font-weight: 500; }}
    .actions {{ display: flex; align-items: center; gap: 14px; margin-top: 16px; flex-wrap: wrap; }}
    .primary-btn {{
      background: var(--accent); color: #fff; border: none; border-radius: 8px;
      padding: 10px 20px; font-size: 0.94rem; font-weight: 620; cursor: pointer;
    }}
    .primary-btn:hover {{ background: var(--accent-hover); }}
    .primary-btn:disabled {{ background: #94a3b8; cursor: not-allowed; }}
    .pub-btn {{
      display: inline-block; margin-left: 8px; color: #065f46; font-weight: 650; text-decoration: underline;
    }}
    .locked-bar {{
      justify-content: space-between; padding-top: 12px; border-top: 1px solid var(--border);
    }}
    .locked-note {{ font-size: 0.88rem; color: #64748b; }}
    .secondary-btn {{
      display: inline-block; background: #f1f5f9; color: #0f172a; border: 1px solid var(--border);
      border-radius: 8px; padding: 7px 14px; font-size: 0.88rem; font-weight: 600; text-decoration: none;
    }}
    .secondary-btn:hover {{ background: #e2e8f0; }}
    .done-box {{
      margin-top: 16px; padding: 16px 18px; border-radius: 10px;
      background: #ecfdf5; border: 1px solid #a7f3d0; color: #065f46;
      display: flex; flex-direction: column; gap: 10px;
    }}
    .done-title {{ font-weight: 650; font-size: 0.96rem; }}
    .done-actions {{ display: flex; gap: 10px; flex-wrap: wrap; }}
    .done-paths {{ font-size: 0.82rem; color: #047857; word-break: break-all; line-height: 1.55; }}
    .status-msg {{ font-size: 0.9rem; color: var(--muted); }}
    .trust-note {{ font-size: 0.78rem; color: #64748b; margin-top: 18px; }}
    code {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.85em; }}
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div class="eyebrow">CLAIMREV · 人工拍板决策面（Human Decision Surface）</div>
      <h1>{html.escape(task['title'])}</h1>
      <div class="meta">当前报告第 {task['current_revision']} 版 · 决策单号 <code>{html.escape(did)}</code></div>
    </header>

    <div id="banner-slot">{banner_html}</div>

    <section class="card">
      <h2>触发原因</h2>
      <p class="trigger-quote">{html.escape(shock_text)}</p>
    </section>
{scope_table_html}
    <section class="card">
      <h2>请选择你认可的处理方案</h2>
      <form id="decision-form">
{chr(10).join(opt_cards)}
      </form>
{submit_html}
    </section>

    <div class="trust-note">
      安全边界说明：本审批页仅监听本机 <code>127.0.0.1</code> 并校验本次启动生成的随机一次性管理令牌；审批接口绝不向 MCP Agent 暴露。
    </div>
  </div>
  <script>
    const DECISION_ID = {json.dumps(did)};
    const MGMT_TOKEN = {json.dumps(token)};
    const IDEM_KEY = "web-" + DECISION_ID + "-" + Math.random().toString(36).slice(2, 10);

    async function submitDecision() {{
      const chosen = document.querySelector('input[name="selected_option"]:checked');
      if (!chosen) return;
      const btn = document.getElementById('approve-btn');
      const msg = document.getElementById('status-msg');
      btn.disabled = true;
      msg.textContent = '正在校验版本与数据批次并发布...';
      try {{
        const res = await fetch('/api/resolve', {{
          method: 'POST',
          headers: {{
            'Content-Type': 'application/json',
            'X-ClaimRev-Token': MGMT_TOKEN
          }},
          body: JSON.stringify({{
            decision_id: DECISION_ID,
            selected_option: chosen.value,
            idempotency_key: IDEM_KEY
          }})
        }});
        const data = await res.json();
        if (!res.ok) {{
          document.getElementById('banner-slot').innerHTML =
            '<div class="banner banner-stale"><strong>提交被拦截（' + (data.error || res.status) + '）：</strong>' + (data.message || '未知错误') + '</div>';
          msg.textContent = '未发布。';
          return;
        }}
        const rev = data.committed_revision;
        const pubUrl = '/pub/' + encodeURIComponent(data.commit_result.task_id) + '/latest_diff.html';
        const mdPath = (data.commit_result.exports && data.commit_result.exports.md_path) || '';
        const htmlPath = (data.commit_result.exports && data.commit_result.exports.html_path) || '';
        document.getElementById('banner-slot').innerHTML =
          '<div class="banner banner-ok"><strong>已确认并发布第 ' + rev + ' 版！</strong> ' +
          '<a class="pub-btn" href="' + pubUrl + '">立即打开第 ' + rev + ' 版差异页 →</a></div>';
        document.querySelectorAll('input[name="selected_option"]').forEach(el => el.disabled = true);
        const actionsEl = btn.parentElement;
        actionsEl.className = 'done-box';
        actionsEl.innerHTML =
          '<div class="done-title">✅ 第 ' + rev + ' 版报告已发布，1 秒后自动为你跳转到差异页…</div>' +
          '<div class="done-actions"><a class="primary-btn" style="text-decoration:none;" href="' + pubUrl + '">立即查看第 ' + rev + ' 版改稿差异页 →</a></div>' +
          '<div class="done-paths">本机保存位置：<br>HTML 差异页：<code>' + htmlPath + '</code><br>Markdown 报告：<code>' + mdPath + '</code></div>';
        setTimeout(() => {{ window.location.href = pubUrl; }}, 1200);
      }} catch (err) {{
        msg.textContent = '请求失败：' + err;
        btn.disabled = false;
      }}
    }}
  </script>
</body>
</html>
"""


def create_approval_server(
    service: ClaimRevService,
    *,
    decision_id: str | None = None,
    host: str = "127.0.0.1",
    port: int = 0,
    token: str | None = None,
) -> tuple[ThreadingHTTPServer, str, str]:
    """Create a local-only HTTP server serving the Human Decision Surface and published diff files.

    Returns (server, url, management_token).
    """
    mgmt_token = token or secrets.token_urlsafe(24)

    class ApprovalHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return  # keep terminal output clean

        def _send_json(self, code: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, code: int, content: str) -> None:
            body = content.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)

            # Serve published read-only diff files directly so user clicks 1 link after approval
            if parsed.path.startswith("/pub/"):
                rel = urllib.parse.unquote(parsed.path[len("/pub/"):]).lstrip("/")
                target = (service.pub_dir / rel).resolve()
                if not str(target).startswith(str(service.pub_dir.resolve())) or not target.is_file():
                    self.send_error(HTTPStatus.NOT_FOUND, "Published file not found")
                    return
                raw = target.read_bytes()
                ctype = "text/html; charset=utf-8" if target.suffix == ".html" else "text/plain; charset=utf-8"
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return

            if parsed.path in ("/", "/decision"):
                req_token = (qs.get("token") or [""])[0]
                if not hmac.compare_digest(req_token, mgmt_token):
                    self._send_html(HTTPStatus.FORBIDDEN, "<h1>403 Forbidden</h1><p>缺少或无效的管理令牌。</p>")
                    return
                did = (qs.get("decision_id") or [decision_id or ""])[0]
                if not did:
                    decs = service.list_decisions()
                    pending = [d for d in decs if d["status"] == "PENDING"]
                    if pending:
                        did = pending[0]["decision_id"]
                    elif decs:
                        did = decs[0]["decision_id"]
                    else:
                        self._send_html(HTTPStatus.OK, "<h1>暂无待审批事项</h1>")
                        return
                try:
                    dec = service.get_decision(decision_id=did)
                except NotFoundError as exc:
                    self._send_html(HTTPStatus.NOT_FOUND, f"<h1>404</h1><p>{html.escape(exc.message)}</p>")
                    return
                self._send_html(HTTPStatus.OK, render_decision_page_html(dec, mgmt_token))
                return

            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            if self.path != "/api/resolve":
                self.send_error(HTTPStatus.NOT_FOUND)
                return

            hdr_token = self.headers.get("X-ClaimRev-Token", "")
            if not hmac.compare_digest(hdr_token, mgmt_token):
                self._send_json(
                    HTTPStatus.FORBIDDEN,
                    {"error": "ERR_FORBIDDEN", "message": "Invalid or missing X-ClaimRev-Token"},
                )
                return

            length = int(self.headers.get("Content-Length", "0"))
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
            except Exception as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "ERR_BAD_REQUEST", "message": str(exc)})
                return

            did = str(body.get("decision_id", "")).strip()
            opt = str(body.get("selected_option", "")).strip()
            idem = body.get("idempotency_key")
            if not did or not opt:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "ERR_VALIDATION", "message": "decision_id and selected_option are required"},
                )
                return

            try:
                res = service.resolve_decision(
                    decision_id=did,
                    selected_option=opt,
                    idempotency_key=str(idem) if idem else None,
                    resolved_by="human-web-ui",
                    auto_commit=True,
                )
                self._send_json(HTTPStatus.OK, res)
            except (DecisionConflictError, IdempotencyConflictError, StaleDataBatchError, StaleClaimVersionError) as exc:
                self._send_json(HTTPStatus.CONFLICT, exc.to_dict())
            except NotFoundError as exc:
                self._send_json(HTTPStatus.NOT_FOUND, exc.to_dict())
            except ClaimRevError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, exc.to_dict())

    server = ThreadingHTTPServer((host, port), ApprovalHandler)
    bound_port = server.server_address[1]
    query = f"token={urllib.parse.quote(mgmt_token)}"
    if decision_id:
        query += f"&decision_id={urllib.parse.quote(decision_id)}"
    url = f"http://{host}:{bound_port}/?{query}"
    return server, url, mgmt_token
