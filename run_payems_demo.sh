#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_BIN="$SCRIPT_DIR/.venv/bin"
CLAIMREV_BIN="$VENV_BIN/claimrev"
CLAIMREV_MCP_BIN="$VENV_BIN/claimrev-mcp"

if [[ -n "${DSH_BIN:-}" ]]; then
  DSH="$DSH_BIN"
elif command -v dsh >/dev/null 2>&1; then
  DSH="$(command -v dsh)"
elif [[ -x "/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh" ]]; then
  DSH="/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh"
else
  echo "ERROR: Could not find DeepSeek Harness (dsh). Set DSH_BIN=/path/to/dsh" >&2
  exit 1
fi

WORKSPACE_DIR="${CLAIMREV_TEST_WORKSPACE:-$SCRIPT_DIR/test_runs/claimrev-test-workspace}"
mkdir -p "$WORKSPACE_DIR"

run_payems_case() {
  local tag="$1"
  local label="$2"
  local seed_spec="$3"
  local task_id="$4"
  local t0_csv="$5"
  local t1_csv="$6"
  local query="$7"

  local out_dir="$SCRIPT_DIR/test_runs/$tag"
  local store_dir="$out_dir/store"
  local dsh_log="$out_dir/dsh_events.jsonl"
  local mcp_log="$out_dir/mcp_calls.jsonl"

  rm -rf "$out_dir"
  mkdir -p "$out_dir"

  echo ""
  echo "=================================================================="
  echo "  [$tag] $label"
  echo "=================================================================="

  "$CLAIMREV_BIN" --store "$store_dir" seed-module --reset \
    --spec "$seed_spec" > "$out_dir/seed_result.json"

  rm -f "$WORKSPACE_DIR"/report_v1.md "$WORKSPACE_DIR"/regression_inputs.json "$WORKSPACE_DIR"/payems_vintage_*.csv
  cp "$store_dir/pub/$task_id/revision_v1.md" "$WORKSPACE_DIR/report_v1.md"
  cp "$SCRIPT_DIR/src/claimrev/examples/alfred_payems/data/$t0_csv" "$WORKSPACE_DIR/$t0_csv"
  cp "$SCRIPT_DIR/src/claimrev/examples/alfred_payems/data/$t1_csv" "$WORKSPACE_DIR/$t1_csv"

  (
    cd "$WORKSPACE_DIR"
    all_proxy= ALL_PROXY= \
      CLAIMREV_MCP_BIN="$CLAIMREV_MCP_BIN" \
      CLAIMREV_STORE="$store_dir" \
      CLAIMREV_PLUGINS="claimrev.examples.alfred_payems.computations:register" \
      CLAIMREV_MCP_LOG="$mcp_log" \
      CLAIMREV_DSH_SESSIONS_DIR="${CLAIMREV_DSH_SESSIONS_DIR:-}" \
      "$DSH" headless \
        --patch "$SCRIPT_DIR/dsh/claimrev.patch.yml" \
        --json \
        "$query" \
      > "$dsh_log"
  )

  "$VENV_BIN/python" - "$dsh_log" "$store_dir" "$task_id" <<'PY'
import json, sys
from pathlib import Path
from claimrev.examples.alfred_payems.seed import build_registry
from claimrev.service import ClaimRevService

dsh_log, store_dir, task_id = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
session_id = None
calls = []
final_text = ""
in_tok = cache_tok = out_tok = steps = 0
for line in dsh_log.read_text(encoding="utf-8").splitlines():
    if not line.strip():
        continue
    ev = json.loads(line)
    t = ev.get("type")
    if t == "session":
        session_id = ev.get("sessionId")
    elif t == "tool_call":
        calls.append({"callId": ev.get("callId"), "tool": ev.get("tool"), "status": None, "chars": 0})
    elif t == "tool_result":
        for c in calls:
            if c["callId"] == ev.get("callId"):
                c["status"] = ev.get("status")
                c["chars"] = len(str(ev.get("result", "")))
                break
    elif t == "status" and ev.get("phase") == "step_end":
        steps += 1
        u = ev.get("usage", {})
        in_tok += int(u.get("inputTokens", 0))
        cache_tok += int(u.get("cacheReadTokens", 0))
        out_tok += int(u.get("outputTokens", 0))
    elif t == "final":
        final_text = ev.get("text", "")

tot_in = in_tok + cache_tok
pct = 100.0 * cache_tok / tot_in if tot_in else 0.0
print(f"Session  : {session_id}")
print(f"Steps    : {steps} | Tool calls: {len(calls)}")
print(f"Tokens   : uncached_in={in_tok:,} | cache_read={cache_tok:,} ({pct:.1f}%) | out={out_tok:,} | total={tot_in + out_tok:,}")
for i, c in enumerate(calls, 1):
    print(f"  {i}. {c['tool']} [{c['status']}] (resp={c['chars']} chars)")

svc = ClaimRevService(store_dir, registry=build_registry())
st = svc.get_task_state(task_id=task_id)
print(f"Ledger   : rev={st['task']['current_revision']} | status={st['task']['status']}")
for cl in st["claims"]:
    print(f"  - {cl['claim_id']} (v{cl['version']}, {cl['status']}, action={cl['action_taken']})")
pub = st.get("latest_publication") or {}
print(f"Diff HTML: {pub.get('html_path')}")
print(f"Diff MD  : {pub.get('md_path')}")
print("\n--- dsh final reply ---")
print(final_text)
PY
}

MODE="${1:-all}"
if [[ "$MODE" == "all" || "$MODE" == "flip" ]]; then
  run_payems_case \
    "payems_2025q2_flip" \
    "Macro Case 1 · 历史下修推翻结论（ALFRED 2025-07-03 → 2025-08-01）" \
    "claimrev.examples.alfred_payems.seed:seed_payems_2025q2_flip" \
    "payems_2025q2_revision" \
    "payems_vintage_2025-07-03.csv" \
    "payems_vintage_2025-08-01.csv" \
    "美联储 ALFRED 刚发布了 2025-08-01 最新一期美国非农就业（PAYEMS）数据批次，包含对此前月份的历史修正。请核对原报告（基于 2025-07-03 当时可见数据撰写）的两条结论，重算最新数据后判断哪句话必须修改、哪句话仍然成立，并发布第 2 版报告差异。"
fi

if [[ "$MODE" == "all" || "$MODE" == "hold" ]]; then
  run_payems_case \
    "payems_2024q3_hold" \
    "Macro Case 2 · 镜像案例：历史下修但核心结论仍成立（ALFRED 2024-10-04 → 2024-12-06）" \
    "claimrev.examples.alfred_payems.seed:seed_payems_2024q3_hold" \
    "payems_2024q3_revision" \
    "payems_vintage_2024-10-04.csv" \
    "payems_vintage_2024-12-06.csv" \
    "美联储 ALFRED 发布了 2024-12-06 新一期美国非农就业（PAYEMS）历史修订批次，有评论称 2024 年 8 月被大幅下修，原报告关于 2024 年三季度的判断已经站不住脚、应该立刻撤回。请不要盲从评论，用最新批次重算后客观判断原报告的两条结论究竟要不要改，并发布第 2 版报告差异。"
fi
