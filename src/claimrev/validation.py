"""Host-guard validators: metric semantics, evidence grounding, scope diffing, and fine-grained freshness."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .errors import (
    MetricSemanticMismatchError,
    NotFoundError,
    ValidationError,
)
from .models import diff_scopes, normalize_scope
from .store import get_latest_claim_row, load_artifact_json

ACTION_TO_STATUS: dict[str, str | None] = {
    "KEEP": None,  # preserves current status
    "NARROW": "NARROWED",
    "REVISE": "ACTIVE",
    "REFUTE": "REFUTED",
    "CONDITIONAL": "CONDITIONAL",
}

VALID_RELATIONS = {"SUPPORTS", "REFUTES", "DEFENDS_AGAINST", "TRIGGERED_BY"}


def validate_metric_bindings(
    conn: sqlite3.Connection,
    task_id: str,
    bindings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Verify every metric binding exists in an archived METRICS_JSON artifact and matches unit & stat_meaning."""
    resolved: list[dict[str, Any]] = []
    cache: dict[str, dict[str, Any]] = {}
    for idx, b in enumerate(bindings):
        if not isinstance(b, dict):
            raise ValidationError(f"metric_bindings[{idx}] must be an object")
        aid = b.get("artifact_id")
        mkey = b.get("metric_key")
        if not aid or not mkey:
            raise ValidationError(
                f"metric_bindings[{idx}] requires artifact_id and metric_key",
                details={"binding": b},
            )
        if aid not in cache:
            art_row, payload = load_artifact_json(conn, task_id, aid)
            if art_row["kind"] != "METRICS_JSON":
                raise ValidationError(
                    f"Metric binding artifact {aid!r} has kind {art_row['kind']!r}, expected 'METRICS_JSON'",
                    details={"artifact_id": aid, "kind": art_row["kind"]},
                )
            cache[aid] = payload
        payload = cache[aid]
        metrics_map = payload.get("metrics", {})
        if mkey not in metrics_map:
            raise ValidationError(
                f"Metric key {mkey!r} not found in artifact {aid!r}",
                details={"artifact_id": aid, "metric_key": mkey, "available_keys": sorted(metrics_map.keys())},
            )
        rec = dict(metrics_map[mkey])

        exp_meaning = b.get("expected_stat_meaning")
        if exp_meaning is not None and rec["stat_meaning"] != exp_meaning:
            raise MetricSemanticMismatchError(
                f"Metric {mkey!r} has stat_meaning={rec['stat_meaning']!r} "
                f"(value={rec['display_value']} {rec['unit']}), which does not match "
                f"expected_stat_meaning={exp_meaning!r}",
                details={
                    "artifact_id": aid,
                    "metric_key": mkey,
                    "actual_stat_meaning": rec["stat_meaning"],
                    "expected_stat_meaning": exp_meaning,
                    "display_value": rec["display_value"],
                    "unit": rec["unit"],
                },
            )

        exp_unit = b.get("expected_unit")
        if exp_unit is not None and rec["unit"] != exp_unit:
            raise MetricSemanticMismatchError(
                f"Metric {mkey!r} has unit={rec['unit']!r}, expected_unit={exp_unit!r}",
                details={
                    "artifact_id": aid,
                    "metric_key": mkey,
                    "actual_unit": rec["unit"],
                    "expected_unit": exp_unit,
                },
            )

        rec["artifact_id"] = aid
        if b.get("role"):
            rec["role"] = str(b["role"])
        resolved.append(rec)
    return resolved


def validate_evidence_links(
    conn: sqlite3.Connection,
    task_id: str,
    links: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    validated: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for idx, lk in enumerate(links):
        if not isinstance(lk, dict):
            raise ValidationError(f"evidence_links[{idx}] must be an object")
        aid = lk.get("artifact_id")
        rel = lk.get("relation")
        rat = str(lk.get("rationale", "")).strip()
        if not aid or not rel:
            raise ValidationError(f"evidence_links[{idx}] requires artifact_id and relation")
        if rel not in VALID_RELATIONS:
            raise ValidationError(
                f"Invalid evidence relation {rel!r}",
                details={"valid_relations": sorted(VALID_RELATIONS)},
            )
        row = conn.execute(
            "SELECT id, kind FROM artifacts WHERE id = ? AND task_id = ?",
            (aid, task_id),
        ).fetchone()
        if not row:
            raise NotFoundError(
                f"Evidence artifact {aid!r} not found in task {task_id!r}",
                details={"artifact_id": aid, "task_id": task_id},
            )
        key = (aid, rel)
        if key not in seen:
            seen.add(key)
            validated.append(
                {
                    "artifact_id": aid,
                    "kind": row["kind"],
                    "relation": rel,
                    "rationale": rat or f"Linked artifact {aid}",
                }
            )
    return validated


def collect_dependency_closure(
    conn: sqlite3.Connection,
    task_id: str,
    target_claim_ids: list[str],
) -> tuple[dict[str, int], dict[str, str]]:
    """Collect exact latest versions of target + upstream claims, and current batches of their data series."""
    claim_versions: dict[str, int] = {}
    series_ids: set[str] = set()
    queue = list(target_claim_ids)
    visited: set[str] = set()

    while queue:
        cid = queue.pop(0)
        if cid in visited:
            continue
        visited.add(cid)
        row = get_latest_claim_row(conn, task_id, cid)
        if not row:
            raise NotFoundError(
                f"Claim {cid!r} not found in task {task_id!r}",
                details={"claim_id": cid, "task_id": task_id},
            )
        claim_versions[cid] = int(row["version"])
        for sid in json.loads(row["data_series_ids_json"]):
            series_ids.add(str(sid))
        for dep_cid in json.loads(row["depends_on_claims_json"]):
            if dep_cid not in visited:
                queue.append(str(dep_cid))

    data_batches: dict[str, str] = {}
    for sid in sorted(series_ids):
        srow = conn.execute(
            "SELECT current_batch_id FROM data_series WHERE task_id = ? AND series_id = ?",
            (task_id, sid),
        ).fetchone()
        if not srow or not srow["current_batch_id"]:
            raise ValidationError(
                f"Data series {sid!r} referenced by claim has no active batch",
                details={"series_id": sid, "task_id": task_id},
            )
        data_batches[sid] = str(srow["current_batch_id"])

    return claim_versions, data_batches


def check_freshness(
    conn: sqlite3.Connection,
    task_id: str,
    read_claim_versions: dict[str, int],
    read_data_batches: dict[str, str],
) -> list[dict[str, Any]]:
    """Check whether any relevant claim version or dependent data batch changed since proposal creation.

    Unrelated claim updates (and global task_revision advances) do NOT trigger staleness.
    """
    stale_reasons: list[dict[str, Any]] = []
    for sid, expected_batch in sorted(read_data_batches.items()):
        srow = conn.execute(
            "SELECT current_batch_id FROM data_series WHERE task_id = ? AND series_id = ?",
            (task_id, sid),
        ).fetchone()
        actual_batch = str(srow["current_batch_id"]) if srow and srow["current_batch_id"] else None
        if actual_batch != expected_batch:
            stale_reasons.append(
                {
                    "kind": "DATA_BATCH_CHANGED",
                    "series_id": sid,
                    "expected_batch_id": expected_batch,
                    "current_batch_id": actual_batch,
                }
            )
    for cid, expected_ver in sorted(read_claim_versions.items()):
        crow = get_latest_claim_row(conn, task_id, cid)
        actual_ver = int(crow["version"]) if crow else None
        if actual_ver != int(expected_ver):
            stale_reasons.append(
                {
                    "kind": "CLAIM_VERSION_CHANGED",
                    "claim_id": cid,
                    "expected_version": int(expected_ver),
                    "current_version": actual_ver,
                }
            )
    return stale_reasons


def validate_change_list(
    conn: sqlite3.Connection,
    task_id: str,
    changes: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    if not isinstance(changes, list) or not changes:
        raise ValidationError("changes must be a non-empty list")

    validated_changes: list[dict[str, Any]] = []
    all_scope_diffs: dict[str, dict[str, Any]] = {}
    seen_cids: set[str] = set()

    for idx, ch in enumerate(changes):
        if not isinstance(ch, dict):
            raise ValidationError(f"changes[{idx}] must be an object")
        cid = str(ch.get("claim_id", "")).strip()
        if not cid:
            raise ValidationError(f"changes[{idx}] missing claim_id")
        if cid in seen_cids:
            raise ValidationError(f"Duplicate claim_id {cid!r} in changes")
        seen_cids.add(cid)

        action = str(ch.get("action", "")).strip().upper()
        if action not in ACTION_TO_STATUS:
            raise ValidationError(
                f"Invalid action {action!r} for claim {cid!r}",
                details={"valid_actions": sorted(ACTION_TO_STATUS.keys())},
            )

        cur_row = get_latest_claim_row(conn, task_id, cid)
        if not cur_row:
            raise NotFoundError(f"Claim {cid!r} not found in task {task_id!r}")

        cur_scope = json.loads(cur_row["scope_json"])
        cur_location = str(cur_row["location"] or "")
        cur_stmt = str(cur_row["statement"])
        cur_status = str(cur_row["status"])
        cur_series = json.loads(cur_row["data_series_ids_json"])
        cur_deps = json.loads(cur_row["depends_on_claims_json"])
        cur_metrics = json.loads(cur_row["bound_metrics_json"])

        new_location = str(ch.get("location", "")).strip() or cur_location
        new_stmt = str(ch.get("statement", "")).strip() or cur_stmt
        rationale = str(ch.get("rationale", "")).strip()
        if not rationale:
            raise ValidationError(f"Claim {cid!r} change requires a non-empty rationale")

        if action == "REFUTE" and not str(ch.get("reopen_conditions", "")).strip():
            raise ValidationError(
                f"REFUTE action on {cid!r} requires non-empty reopen_conditions"
            )

        new_status = cur_status if ACTION_TO_STATUS[action] is None else str(ACTION_TO_STATUS[action])
        new_scope = normalize_scope(ch["scope"]) if "scope" in ch and ch["scope"] is not None else cur_scope
        sdiff = diff_scopes(cur_scope, new_scope)
        if sdiff:
            all_scope_diffs[cid] = sdiff

        raw_bindings = ch.get("metric_bindings")
        if raw_bindings is not None:
            resolved_metrics = validate_metric_bindings(conn, task_id, raw_bindings)
        else:
            resolved_metrics = cur_metrics

        validated_links = validate_evidence_links(conn, task_id, ch.get("evidence_links", []))

        has_metrics_artifact = bool(resolved_metrics) or any(
            lk["kind"] == "METRICS_JSON" for lk in validated_links
        )
        if not has_metrics_artifact:
            raise ValidationError(
                f"Claim {cid!r} change must cite at least one verified METRICS_JSON artifact",
                details={"claim_id": cid},
            )

        linked_aids = {lk["artifact_id"] for lk in validated_links}
        for bm in resolved_metrics:
            if bm["artifact_id"] not in linked_aids:
                validated_links.append(
                    {
                        "artifact_id": bm["artifact_id"],
                        "kind": "METRICS_JSON",
                        "relation": "SUPPORTS",
                        "rationale": f"Bound metric {bm['metric_key']} ({bm['display_value']} {bm['unit']})",
                    }
                )
                linked_aids.add(bm["artifact_id"])

        validated_changes.append(
            {
                "claim_id": cid,
                "previous_version": int(cur_row["version"]),
                "previous_statement": cur_stmt,
                "action": action,
                "status": new_status,
                "location": new_location,
                "statement": new_stmt,
                "rationale": rationale,
                "scope": new_scope,
                "scope_diff": sdiff,
                "bound_metrics": resolved_metrics,
                "evidence_links": validated_links,
                "data_series_ids": cur_series,
                "depends_on_claims": cur_deps,
                "reopen_conditions": ch.get("reopen_conditions"),
            }
        )

    return validated_changes, all_scope_diffs
