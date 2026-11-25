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

OUT_DIR="${CLAIMREV_DEMO_DIR:-$SCRIPT_DIR/test_runs/latest}"
STORE_DIR="$OUT_DIR/store"
WORKSPACE_DIR="${CLAIMREV_TEST_WORKSPACE:-$SCRIPT_DIR/test_runs/claimrev-test-workspace}"
DSH_LOG="$OUT_DIR/dsh_events.jsonl"
MCP_LOG="$OUT_DIR/mcp_calls.jsonl"
QUERY="${1:-如果去掉对照组里线宽最大的两个天体，报告里的哪句话需要改？}"

rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR" "$WORKSPACE_DIR"
rm -f "$WORKSPACE_DIR/report_v1.md" "$WORKSPACE_DIR/regression_inputs.json"

echo "=== [1/4] Environment ==="
echo "Project dir   : $SCRIPT_DIR"
echo "dsh binary    : $DSH"
echo -n "dsh version   : "
all_proxy= ALL_PROXY= "$DSH" --version
echo "Store dir     : $STORE_DIR"
echo "dsh workspace : $WORKSPACE_DIR (add this folder in DeepSeek Harness.app to view test trajectories under its own group)"

echo ""
echo "=== [2/4] Import initial Revision 1 report from public regression_inputs.json ==="
"$CLAIMREV_BIN" --store "$STORE_DIR" seed-module --reset \
  --spec claimrev.examples.lrd_width.seed:seed_initial_report \
  > "$OUT_DIR/seed_result.json"

# Stage ONLY the old report and public input data in the clean dsh workspace
# (expected reference intervals remain strictly separated under tests/fixtures/)
cp "$STORE_DIR/pub/lrd_width_luminosity/revision_v1.md" "$WORKSPACE_DIR/report_v1.md"
cp "$SCRIPT_DIR/src/claimrev/examples/lrd_width/data/regression_inputs.json" "$WORKSPACE_DIR/regression_inputs.json"
echo "Seeded Revision 1 -> $STORE_DIR/pub/lrd_width_luminosity/revision_v1.md"

echo ""
echo "=== [3/4] Run dsh headless with claimrev MCP plugin (clean workspace) ==="
echo "User prompt : $QUERY"
(
  cd "$WORKSPACE_DIR"
  all_proxy= ALL_PROXY= \
    CLAIMREV_MCP_BIN="$CLAIMREV_MCP_BIN" \
    CLAIMREV_STORE="$STORE_DIR" \
    CLAIMREV_PLUGINS="claimrev.examples.lrd_width.computations:register" \
    CLAIMREV_MCP_LOG="$MCP_LOG" \
    CLAIMREV_DSH_SESSIONS_DIR="${CLAIMREV_DSH_SESSIONS_DIR:-}" \
    "$DSH" headless \
      --patch "$SCRIPT_DIR/dsh/claimrev.patch.yml" \
      --json \
      "$QUERY" \
    > "$DSH_LOG"
)

echo ""
echo "=== [4/4] Summary of dsh tool calls & published report diff ==="
"$VENV_BIN/python" - "$DSH_LOG" "$STORE_DIR" <<'PY'
import json, sys
from pathlib import Path

dsh_log = Path(sys.argv[1])
store_dir = Path(sys.argv[2])

session_id = None
calls = []
final_text = ""
for line in dsh_log.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line:
        continue
    ev = json.loads(line)
    t = ev.get("type")
    if t == "session":
        session_id = ev.get("sessionId")
    elif t == "tool_call":
        calls.append({"callId": ev.get("callId"), "tool": ev.get("tool"), "input": ev.get("input"), "status": None})
    elif t == "tool_result":
        cid = ev.get("callId")
        for c in calls:
            if c["callId"] == cid:
                c["status"] = ev.get("status")
                break
    elif t == "final":
        final_text = ev.get("text", "")

print(f"dsh session : {session_id}")
print(f"Tool calls  : {len(calls)}")
for i, c in enumerate(calls, 1):
    inp = json.dumps(c["input"], ensure_ascii=False)
    if len(inp) > 160:
        inp = inp[:157] + "..."
    print(f"  {i}. {c['tool']} [{c['status']}] {inp}")

print("\n--- dsh final response ---")
print(final_text)

pub_dir = store_dir / "pub" / "lrd_width_luminosity"
latest_md = pub_dir / "latest_report.md"
print("\n--- Published artifacts ---")
for p in sorted(pub_dir.glob("*")):
    print(f"  - {p}")
if latest_md.is_file():
    print("\n--- Published Markdown diff (latest_report.md) ---")
    print(latest_md.read_text(encoding="utf-8"))
PY
