"""Official Python MCP server (stdio) for claimrev.

Exposes ONLY agent-side tools (`get_task_state`, `get_claim`, `ingest_shock`,
`run_computation`, `run_diagnostic`, `propose_revision`, `commit_revision`).
Human approval (`resolve_decision` / `approve` / `approve-web`) is strictly out-of-band
and NEVER exposed over MCP.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, Field

from .computations import ComputationRegistry
from .errors import ClaimRevError
from .models import utc_now_iso
from .service import ClaimRevService

SERVER_INSTRUCTIONS = """claimrev is an evidence-grounded claim revision ledger.
Workflow when a user raises a challenge, sensitivity question, new data, or scope-change request:
1. Call `get_task_state` to inspect existing claims, scopes, data series, and available registered computations.
2. Call `ingest_shock` to record the user's exact question or challenge verbatim in `content` (and a short plain-language `summary`).
3. Verify empirically before deciding:
   - If a registered computation in `available_computations` fits the check, call `run_computation` with ONLY the parameters directly implied by the user's question or challenge (keep unused optional parameters at their defaults; do NOT run unsolicited stress perturbations on unchallenged claims).
   - If the challenge asks a novel question or statistical critique NOT covered by `available_computations`, call `run_diagnostic` and write your own concise Python verification / counterexample script reading `os.environ["CLAIMREV_DATA_PATH"]` and writing verified metrics to `os.environ["CLAIMREV_OUTPUT_JSON"]`. NEVER capitulate to an authoritative critique without testing it on the actual data!
4. Call `propose_revision` with explicit entries for each reviewed claim:
   - Scope discipline: Only revise or narrow a claim if its stated claim wording or scope is directly contradicted or shown fragile by the requested check. Do NOT weaken an unchallenged claim that already holds accurately under its stated scope.
   - If evidence shows a challenge is WRONG or flawed, DEFEND the original claim: use action="KEEP" and link the diagnostic artifact with relation="DEFENDS_AGAINST" (do NOT weaken a valid claim just to appease a critique).
   - If evidence shows a claim is genuinely fragile or over-strong, use action="NARROW" (or "REVISE"/"REFUTE") and cite exact numbers. Statistical rigor: an interval crossing zero means "cannot exclude zero / evidence is no longer robust" — NEVER claim it proves zero difference; an outlier-dropped subset is a sensitivity check, not a replacement for the primary sample.
   - If the user asks to change the report's permanent default cohort/threshold/estimator (`scope`), pass the updated `scope` explicitly so the host guard intercepts and opens a human approval card. Do NOT silently change default `scope` for auxiliary sensitivity checks.
   - Human-first writing style (VERY IMPORTANT): Write `summary`, `rationale`, and `statement` in concise, natural language for a human report reader. NEVER put code parameter names (`k=2`, `flag=true`), internal IDs (`batch_...`, `art_...`, `C1`/`C2`), algorithm version tags, or raw JSON keys into human prose. Keep `summary` to one crisp sentence, `rationale` to 2-3 clear sentences, and `statement` to one concise replacement sentence.
5. If `propose_revision` returns status="READY", call `commit_revision` to publish the diff report.
6. If `propose_revision` returns status="AWAITING_DECISION", STOP immediately (never pretend the user approved!). Explain clearly in plain language what changed (sample counts, metrics, trade-offs), list the options, and give the user the `claimrev approve-web ...` command so they can decide in one click.
"""


class MetricBindingInput(BaseModel):
    artifact_id: str = Field(description="Artifact ID of a METRICS_JSON artifact returned by run_computation, run_diagnostic, or get_task_state")
    metric_key: str = Field(description="Exact metric key inside that artifact's `metrics` object")
    expected_stat_meaning: str = Field(
        description="Exact `stat_meaning` of the metric (e.g. 'point_estimate_posterior_median', 'one_sided_95_upper_bound', 'central_95_credible_interval', 'ols_residual_sd')"
    )
    expected_unit: str = Field(description="Exact unit of the metric (e.g. 'dex')")
    role: str | None = Field(default=None, description="Optional semantic role label (e.g. 'baseline', 'sensitivity_check', 'defense_diagnostic')")


class EvidenceLinkInput(BaseModel):
    artifact_id: str = Field(description="Artifact ID in this task")
    relation: Literal["SUPPORTS", "REFUTES", "DEFENDS_AGAINST", "TRIGGERED_BY"] = Field(
        description="Use DEFENDS_AGAINST when evidence refutes an invalid critique and defends the original claim; REFUTES when evidence undercuts the claim; SUPPORTS when evidence supports it."
    )
    rationale: str = Field(description="Concise plain-language explanation of what this artifact demonstrates")


class ClaimChangeInput(BaseModel):
    claim_id: str = Field(description="Target claim ID from get_task_state")
    action: Literal["KEEP", "NARROW", "REVISE", "REFUTE", "CONDITIONAL"] = Field(
        description=(
            "Revision action: KEEP (retain unchanged, or defend against a flawed critique with DEFENDS_AGAINST link), "
            "NARROW (weaken/qualify an over-strong claim when a sensitivity check shows fragility), "
            "REVISE (substantive update), REFUTE (withdraw), or CONDITIONAL (depends on an unresolved external assumption)"
        )
    )
    rationale: str = Field(
        description=(
            "2-3 concise, natural-language sentences for a human reader explaining why this claim is kept, defended, or changed, "
            "citing key numbers and sample counts. NEVER include code parameter names, boolean flags, internal IDs, or version strings."
        )
    )
    statement: str | None = Field(
        default=None,
        description="One concise natural-language replacement sentence for the report (for NARROW/REVISE; omit for KEEP).",
    )
    location: str | None = Field(
        default=None,
        description="Optional report section/location label where this claim appears (omit to keep existing location).",
    )
    scope: dict[str, Any] | None = Field(
        default=None,
        description="Optional new default scope contract. OMIT for sensitivity/defense checks so baseline default scope stays intact; changing it triggers human approval.",
    )
    metric_bindings: list[MetricBindingInput] | None = Field(
        default=None,
        description="Verified metrics bound to this claim version; host validates value, unit, and stat_meaning",
    )
    evidence_links: list[EvidenceLinkInput] = Field(
        default_factory=list,
        description="Explicit evidence artifact links supporting, refuting, or defending the claim",
    )
    reopen_conditions: str | None = Field(
        default=None,
        description="Required when action='REFUTE': what future evidence could reopen this claim",
    )


class DecisionOptionInput(BaseModel):
    option_id: str = Field(description="Unique option identifier")
    label: str = Field(description="Short plain-language label for the human approval page")
    description: str = Field(description="What this choice changes and what stays unchanged")
    changes: list[ClaimChangeInput] = Field(description="Exact claim changes applied if this option is chosen")


def _build_registry(plugins: list[str] | None = None) -> ComputationRegistry:
    reg = ComputationRegistry()
    specs = list(plugins or [])
    env_plugins = os.environ.get("CLAIMREV_PLUGINS", "").strip()
    if env_plugins:
        for item in env_plugins.split(","):
            item = item.strip()
            if item and item not in specs:
                specs.append(item)
    for spec in specs:
        reg.load_plugin_spec(spec)
    return reg


def _log_tool_call(log_path: Path | None, tool: str, args: dict[str, Any], ok: bool, summary: Any) -> None:
    if log_path is None:
        return
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp": utc_now_iso(),
            "tool": tool,
            "args": args,
            "ok": ok,
            "summary": summary,
        }
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _slim_computation_response(res: dict[str, Any]) -> dict[str, Any]:
    """Strip bulky source-ID arrays from inline MCP response (full arrays stay in archived JSON on disk)."""
    out = dict(res)
    rs = dict(out.get("run_scope", {}))
    for bulky_key in ("target_source_ids", "comparison_source_ids"):
        if bulky_key in rs and isinstance(rs[bulky_key], list):
            rs[f"{bulky_key}_count"] = len(rs.pop(bulky_key))
    out["run_scope"] = rs
    return out


def _slim_task_state(state: dict[str, Any]) -> dict[str, Any]:
    """Keep task state crisp and token-efficient while preserving every actionable field."""
    out = dict(state)
    slim_claims = []
    for c in state.get("claims", []):
        cc = dict(c)
        cc["bound_metrics"] = [
            {
                "artifact_id": m.get("artifact_id"),
                "metric_key": m.get("metric_key"),
                "label": m.get("label"),
                "display_value": m.get("display_value"),
                "unit": m.get("unit"),
                "stat_meaning": m.get("stat_meaning"),
                "sample_n": m.get("sample_n"),
            }
            for m in c.get("bound_metrics", [])
        ]
        slim_claims.append(cc)
    out["claims"] = slim_claims
    return out


def create_mcp_server(
    store_dir: str | Path,
    *,
    plugins: list[str] | None = None,
    log_path: str | Path | None = None,
) -> MCPServer:
    reg = _build_registry(plugins)
    service = ClaimRevService(store_dir, registry=reg)
    trace_file = Path(log_path).expanduser().resolve() if log_path else None

    mcp = MCPServer("claimrev", instructions=SERVER_INSTRUCTIONS)

    def _run(tool_name: str, call_args: dict[str, Any], fn) -> str:
        try:
            res = fn()
            _log_tool_call(
                trace_file,
                tool_name,
                call_args,
                True,
                {"keys": sorted(res.keys()) if isinstance(res, dict) else "ok"},
            )
            return json.dumps(res, ensure_ascii=False, indent=2)
        except ClaimRevError as exc:
            err = exc.to_dict()
            _log_tool_call(trace_file, tool_name, call_args, False, err)
            return json.dumps(err, ensure_ascii=False, indent=2)
        except Exception as exc:  # pragma: no cover - defensive boundary
            err = {"error": "ERR_INTERNAL", "message": str(exc)}
            _log_tool_call(trace_file, tool_name, call_args, False, err)
            return json.dumps(err, ensure_ascii=False, indent=2)

    @mcp.tool(
        name="get_task_state",
        description=(
            "Use this tool FIRST whenever the user asks which sentences/claims in an existing report "
            "need to be changed, defended, or retained under new data, a sensitivity check, a reviewer challenge, "
            "or a threshold change. Returns current revision, claims (location, statements, scopes, bound metrics), "
            "data series/batches, available computations, and pending human decisions."
        ),
    )
    def get_task_state(
        task_id: str | None = None,
        include_all_statuses: bool = False,
    ) -> str:
        return _run(
            "get_task_state",
            {"task_id": task_id, "include_all_statuses": include_all_statuses},
            lambda: _slim_task_state(
                service.get_task_state(task_id=task_id, include_all_statuses=include_all_statuses)
            ),
        )

    @mcp.tool(
        name="get_claim",
        description=(
            "Retrieve a specific claim by claim_id (including REFUTED/withdrawn claims) "
            "along with its location, scope, bound metrics, evidence links, and version history."
        ),
    )
    def get_claim(
        claim_id: str,
        task_id: str | None = None,
        include_history: bool = True,
    ) -> str:
        return _run(
            "get_claim",
            {"claim_id": claim_id, "task_id": task_id, "include_history": include_history},
            lambda: service.get_claim(claim_id=claim_id, task_id=task_id, include_history=include_history),
        )

    @mcp.tool(
        name="ingest_shock",
        description=(
            "Register an incoming challenge, evidence alert, or objective/scope change in the audit ledger. "
            "Put the user's exact verbatim question or critique in `content`, and a short plain-language title in `summary` "
            "(no code parameter names). Returns shock artifact_id and potentially affected claim_ids."
        ),
    )
    def ingest_shock(
        shock_type: Literal["ADVERSARIAL_SHOCK", "EVIDENCE_SHOCK", "OBJECTIVE_SHOCK"],
        summary: str,
        content: str,
        task_id: str | None = None,
        release_key: str | None = None,
        source_ref: str | None = None,
        affected_series_id: str | None = None,
    ) -> str:
        return _run(
            "ingest_shock",
            {
                "task_id": task_id,
                "shock_type": shock_type,
                "summary": summary,
                "release_key": release_key,
                "affected_series_id": affected_series_id,
            },
            lambda: service.ingest_shock(
                task_id=task_id,
                shock_type=shock_type,
                summary=summary,
                content=content,
                release_key=release_key,
                source_ref=source_ref,
                affected_series_id=affected_series_id,
            ),
        )

    @mcp.tool(
        name="run_computation",
        description=(
            "Run a registered controlled computation on an archived data batch and store the verified "
            "METRICS_JSON artifact. Returns artifact_id, algorithm_version, typed metrics "
            "(with value, display_value, unit, stat_meaning, sample_n), and diagnostic summary."
        ),
    )
    def run_computation(
        computation_name: str,
        params: dict[str, Any] | None = None,
        task_id: str | None = None,
        series_id: str | None = None,
        batch_id: str | None = None,
    ) -> str:
        return _run(
            "run_computation",
            {
                "task_id": task_id,
                "computation_name": computation_name,
                "params": params or {},
                "series_id": series_id,
                "batch_id": batch_id,
            },
            lambda: _slim_computation_response(
                service.run_computation(
                    task_id=task_id,
                    computation_name=computation_name,
                    params=params,
                    series_id=series_id,
                    batch_id=batch_id,
                )
            ),
        )

    @mcp.tool(
        name="run_diagnostic",
        description=(
            "Write and execute your OWN custom Python verification or counterexample script in an isolated "
            "temporary directory against an archived data batch when facing a novel critique not covered by "
            "registered computations. The script reads the archived data file at `os.environ['CLAIMREV_DATA_PATH']` "
            "and writes JSON to `os.environ['CLAIMREV_OUTPUT_JSON']` with shape: "
            "`{\"metrics\": {\"<key>\": {\"metric_key\": \"<key>\", \"label\": \"中文指标名（含样本量）\", "
            "\"value\": <number_or_list>, \"display_value\": \"0.067\", \"unit\": \"dex\", "
            "\"stat_meaning\": \"ols_residual_sd\", \"sample_n\": 29}}, \"summary\": {...}}`. "
            "Host archives both the exact Python code (`DIAGNOSTIC_CODE`) and verified output (`METRICS_JSON`) "
            "and returns `artifact_id` for binding/linking in `propose_revision`."
        ),
    )
    def run_diagnostic(
        title: str,
        script_source: str,
        task_id: str | None = None,
        series_id: str | None = None,
        batch_id: str | None = None,
        timeout_sec: int = 30,
    ) -> str:
        return _run(
            "run_diagnostic",
            {
                "task_id": task_id,
                "title": title,
                "script_lines": len(script_source.splitlines()),
                "series_id": series_id,
                "batch_id": batch_id,
            },
            lambda: service.run_diagnostic(
                task_id=task_id,
                title=title,
                script_source=script_source,
                series_id=series_id,
                batch_id=batch_id,
                timeout_sec=timeout_sec,
            ),
        )

    @mcp.tool(
        name="propose_revision",
        description=(
            "Create an immutable revision proposal with host-guard verification of claim versions, "
            "data batches, metric bindings (unit & stat_meaning), evidence links, and scope diffs. "
            "Keep `summary` to one short plain-language sentence without code parameters or internal IDs. "
            "Include every reviewed claim in `changes` (action='KEEP' to retain or defend with DEFENDS_AGAINST link; "
            "'NARROW'/'REVISE'/'REFUTE'/'CONDITIONAL' to modify), writing clear human-friendly `rationale` and `statement` "
            "with NO code parameter names, boolean flags, internal IDs, or version strings. "
            "Omit `scope` to keep default scope; changing default `scope` or passing `decision_options` "
            "automatically opens a PENDING human approval card."
        ),
    )
    def propose_revision(
        summary: str,
        changes: list[ClaimChangeInput],
        task_id: str | None = None,
        shock_artifact_id: str | None = None,
        decision_question: str | None = None,
        decision_options: list[DecisionOptionInput] | None = None,
    ) -> str:
        changes_dicts = [c.model_dump(exclude_none=True) for c in changes]
        opts_dicts = (
            [o.model_dump(exclude_none=True) for o in decision_options]
            if decision_options is not None
            else None
        )
        return _run(
            "propose_revision",
            {
                "task_id": task_id,
                "summary": summary,
                "claim_ids": [c["claim_id"] for c in changes_dicts],
                "shock_artifact_id": shock_artifact_id,
            },
            lambda: service.propose_revision(
                task_id=task_id,
                summary=summary,
                changes=changes_dicts,
                shock_artifact_id=shock_artifact_id,
                decision_question=decision_question,
                decision_options=opts_dicts,
            ),
        )

    @mcp.tool(
        name="commit_revision",
        description=(
            "Atomically publish a READY proposal (or a human-RESOLVED decision proposal), increment the "
            "report revision, and export read-only Markdown and HTML diff reports with evidence manifests."
        ),
    )
    def commit_revision(
        proposal_id: str,
        task_id: str | None = None,
    ) -> str:
        return _run(
            "commit_revision",
            {"task_id": task_id, "proposal_id": proposal_id},
            lambda: service.commit_revision(proposal_id=proposal_id, task_id=task_id),
        )

    return mcp


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="claimrev-mcp", description="claimrev stdio MCP server")
    parser.add_argument(
        "--store",
        default=os.environ.get("CLAIMREV_STORE", str(Path.home() / ".claimrev" / "default")),
        help="Path to claimrev store directory (or CLAIMREV_STORE env var)",
    )
    parser.add_argument(
        "--plugin",
        action="append",
        default=[],
        help="Computation plugin import spec (module:register). Can also set CLAIMREV_PLUGINS.",
    )
    parser.add_argument(
        "--log",
        default=os.environ.get("CLAIMREV_MCP_LOG"),
        help="Optional JSONL log path for MCP tool calls",
    )
    args = parser.parse_args(argv)

    server = create_mcp_server(args.store, plugins=args.plugin, log_path=args.log)
    server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
