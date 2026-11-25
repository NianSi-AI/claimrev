"""Core ClaimRevService facade: task lifecycle, proposals, human decisions, diagnostics, and recovery.

Cleanly separated from storage/SQL helpers (`store.py`), host-guard validators (`validation.py`),
diagnostic script execution (`diagnostics.py`), and rendering (`render.py`).
Completely domain-agnostic: zero domain constants or math live here.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from .computations import ComputationInput, ComputationRegistry
from .db import init_db, open_connection, write_tx
from .diagnostics import execute_diagnostic_script
from .errors import (
    DecisionConflictError,
    DecisionRequiredError,
    ExportInterruptedError,
    IdempotencyConflictError,
    NotFoundError,
    StaleClaimVersionError,
    StaleDataBatchError,
    ValidationError,
)
from .models import canonical_json, normalize_scope, sha256_file, sha256_json, utc_now_iso
from .render import write_revision_exports
from .store import (
    append_event,
    archive_bytes,
    archive_file,
    build_evidence_index,
    find_claims_depending_on_series,
    get_all_latest_claim_rows,
    get_claim_links,
    get_latest_claim_row,
    hydrate_claim_row,
    new_id,
    recompute_task_status,
    resolve_task_id,
)
from .validation import (
    check_freshness,
    collect_dependency_closure,
    validate_change_list,
    validate_evidence_links,
    validate_metric_bindings,
)


class ClaimRevService:
    """Single-writer service managing a claimrev store directory."""

    def __init__(self, store_dir: str | Path, registry: ComputationRegistry | None = None) -> None:
        self.store_dir = Path(store_dir).expanduser().resolve()
        self.db_path = self.store_dir / "state.db"
        self.archive_dir = self.store_dir / "archive"
        self.pub_dir = self.store_dir / "pub"
        self.registry = registry if registry is not None else ComputationRegistry()

        self.store_dir.mkdir(parents=True, exist_ok=True)
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self.pub_dir.mkdir(parents=True, exist_ok=True)
        init_db(self.db_path)

    def _connect(self) -> sqlite3.Connection:
        return open_connection(self.db_path)

    # ------------------------------------------------------------------
    # Task, Data Series/Batch, Initial Import, and Shocks
    # ------------------------------------------------------------------

    def create_task(self, *, task_id: str | None = None, title: str, description: str = "") -> dict[str, Any]:
        tid = task_id or new_id("task")
        now = utc_now_iso()
        conn = self._connect()
        try:
            with write_tx(conn):
                if conn.execute("SELECT id FROM tasks WHERE id = ?", (tid,)).fetchone():
                    raise ValidationError(f"Task already exists: {tid!r}")
                conn.execute(
                    """
                    INSERT INTO tasks (id, title, description, current_revision, status, created_at, updated_at)
                    VALUES (?, ?, ?, 0, 'ACTIVE', ?, ?)
                    """,
                    (tid, title, description, now, now),
                )
                append_event(conn, tid, "SYSTEM", "TASK_CREATED", 0, {"title": title}, created_at=now)
        finally:
            conn.close()
        return {"task_id": tid, "title": title, "current_revision": 0, "status": "ACTIVE"}

    def ingest_data_batch(
        self,
        *,
        task_id: str | None = None,
        series_id: str,
        file_path: str | Path,
        vintage_tag: str | None = None,
        release_key: str | None = None,
        description: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Register an immutable data batch for a logical data_series.

        Updates data_series.current_batch_id without advancing task_revision,
        so pending approvals depending on this series are immediately invalidated.
        """
        digest, archived_path = archive_file(self.archive_dir, Path(file_path))
        now = utc_now_iso()
        conn = self._connect()
        try:
            with write_tx(conn):
                tid = resolve_task_id(conn, task_id)
                task_rev = int(conn.execute("SELECT current_revision FROM tasks WHERE id = ?", (tid,)).fetchone()[0])

                if release_key is not None:
                    dup = conn.execute(
                        """
                        SELECT b.batch_id, b.artifact_id, a.sha256, b.vintage_tag
                        FROM data_batches b
                        JOIN artifacts a ON a.id = b.artifact_id
                        WHERE b.task_id = ? AND b.series_id = ? AND b.release_key = ?
                        """,
                        (tid, series_id, release_key),
                    ).fetchone()
                    if dup:
                        return {
                            "deduplicated": True,
                            "task_id": tid,
                            "series_id": series_id,
                            "batch_id": dup["batch_id"],
                            "artifact_id": dup["artifact_id"],
                            "sha256": dup["sha256"],
                            "vintage_tag": dup["vintage_tag"],
                        }

                srow = conn.execute(
                    "SELECT current_batch_id FROM data_series WHERE task_id = ? AND series_id = ?",
                    (tid, series_id),
                ).fetchone()
                supersedes = str(srow["current_batch_id"]) if srow and srow["current_batch_id"] else None
                if not srow:
                    conn.execute(
                        "INSERT INTO data_series (task_id, series_id, description, current_batch_id, updated_at) VALUES (?, ?, ?, NULL, ?)",
                        (tid, series_id, description, now),
                    )

                art_id = new_id("art_data")
                batch_id = new_id("batch")
                meta = dict(metadata or {})
                meta["series_id"] = series_id
                meta["source_filename"] = Path(file_path).name

                conn.execute(
                    """
                    INSERT INTO artifacts (id, task_id, kind, sha256, vintage_tag, release_key, archive_path, metadata_json, created_at)
                    VALUES (?, ?, 'DATA', ?, ?, ?, ?, ?, ?)
                    """,
                    (art_id, tid, digest, vintage_tag, release_key, str(archived_path), canonical_json(meta), now),
                )
                conn.execute(
                    """
                    INSERT INTO data_batches (batch_id, task_id, series_id, artifact_id, supersedes_batch_id, vintage_tag, release_key, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (batch_id, tid, series_id, art_id, supersedes, vintage_tag, release_key, now),
                )
                conn.execute(
                    "UPDATE data_series SET current_batch_id = ?, updated_at = ? WHERE task_id = ? AND series_id = ?",
                    (batch_id, now, tid, series_id),
                )

                affected_claims = find_claims_depending_on_series(conn, tid, series_id)
                append_event(
                    conn,
                    tid,
                    "SYSTEM",
                    "DATA_BATCH_INGESTED",
                    task_rev,
                    {
                        "series_id": series_id,
                        "batch_id": batch_id,
                        "supersedes_batch_id": supersedes,
                        "artifact_id": art_id,
                        "sha256": digest,
                        "vintage_tag": vintage_tag,
                        "affected_claims": affected_claims,
                    },
                    created_at=now,
                )
        finally:
            conn.close()

        return {
            "deduplicated": False,
            "task_id": tid,
            "series_id": series_id,
            "batch_id": batch_id,
            "supersedes_batch_id": supersedes,
            "artifact_id": art_id,
            "sha256": digest,
            "archive_path": str(archived_path),
            "vintage_tag": vintage_tag,
            "affected_claims": affected_claims,
        }

    def import_initial_claims(
        self,
        *,
        task_id: str,
        summary: str,
        claims: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Publish Revision 1 of a task from imported baseline claims and verified metrics."""
        if not claims:
            raise ValidationError("import_initial_claims requires at least one claim")
        now = utc_now_iso()
        conn = self._connect()
        try:
            with write_tx(conn):
                tid = resolve_task_id(conn, task_id)
                trow = conn.execute("SELECT current_revision FROM tasks WHERE id = ?", (tid,)).fetchone()
                if int(trow["current_revision"]) != 0:
                    raise ValidationError(
                        f"Task {tid!r} already has revision {trow['current_revision']}; initial import requires revision 0"
                    )

                claim_snapshot: dict[str, int] = {}
                changed_claims: list[dict[str, Any]] = []
                evidence_ids: set[str] = set()

                for item in claims:
                    cid = str(item["claim_id"]).strip()
                    location = str(item.get("location", "")).strip()
                    statement = str(item["statement"]).strip()
                    rationale = str(item.get("rationale", "初始报告导入。")).strip()
                    status = str(item.get("status", "ACTIVE"))
                    scope = normalize_scope(item.get("scope", {}))
                    series_ids = sorted({str(s) for s in item.get("data_series_ids", [])})
                    depends_on = sorted({str(d) for d in item.get("depends_on_claims", [])})
                    reopen = item.get("reopen_conditions")

                    for sid in series_ids:
                        srow = conn.execute(
                            "SELECT current_batch_id FROM data_series WHERE task_id = ? AND series_id = ?",
                            (tid, sid),
                        ).fetchone()
                        if not srow or not srow["current_batch_id"]:
                            raise ValidationError(f"Claim {cid!r} references unknown or empty data series {sid!r}")

                    resolved_metrics = validate_metric_bindings(conn, tid, item.get("metric_bindings", []))
                    validated_links = validate_evidence_links(conn, tid, item.get("evidence_links", []))

                    conn.execute(
                        """
                        INSERT INTO claims (
                            task_id, claim_id, version, task_revision, status, action_taken,
                            location, statement, rationale, scope_json, bound_metrics_json,
                            data_series_ids_json, depends_on_claims_json, reopen_conditions, created_at
                        ) VALUES (?, ?, 1, 1, ?, 'INITIAL', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            tid, cid, status, location, statement, rationale,
                            canonical_json(scope), canonical_json(resolved_metrics),
                            canonical_json(series_ids), canonical_json(depends_on), reopen, now,
                        ),
                    )
                    for lk in validated_links:
                        conn.execute(
                            "INSERT INTO claim_links (task_id, claim_id, claim_version, artifact_id, relation, rationale) VALUES (?, ?, 1, ?, ?, ?)",
                            (tid, cid, lk["artifact_id"], lk["relation"], lk["rationale"]),
                        )
                        evidence_ids.add(lk["artifact_id"])
                    for bm in resolved_metrics:
                        evidence_ids.add(bm["artifact_id"])

                    claim_snapshot[cid] = 1
                    changed_claims.append({"claim_id": cid, "version": 1, "action_taken": "INITIAL", "status": status})

                ev_index = build_evidence_index(conn, tid, sorted(evidence_ids))
                conn.execute(
                    """
                    INSERT INTO revisions (
                        task_id, revision, proposal_id, summary, claim_snapshot_json,
                        changed_claims_json, evidence_index_json, export_status, published_at
                    ) VALUES (?, 1, NULL, ?, ?, ?, ?, 'PENDING', ?)
                    """,
                    (tid, summary, canonical_json(claim_snapshot), canonical_json(changed_claims), canonical_json(ev_index), now),
                )
                conn.execute("UPDATE tasks SET current_revision = 1, updated_at = ? WHERE id = ?", (now, tid))
                append_event(
                    conn, tid, "SYSTEM", "REPORT_IMPORTED", 1,
                    {"summary": summary, "claims": sorted(claim_snapshot.keys())}, created_at=now,
                )
        finally:
            conn.close()

        exports = self._export_revision(tid, 1)
        return {"task_id": tid, "revision": 1, "claims": claim_snapshot, "exports": exports}

    def ingest_shock(
        self,
        *,
        task_id: str | None = None,
        shock_type: str,
        summary: str,
        content: str,
        release_key: str | None = None,
        source_ref: str | None = None,
        affected_series_id: str | None = None,
    ) -> dict[str, Any]:
        """Register an incoming challenge, evidence alert, or objective fork with deduplication."""
        valid_types = {"ADVERSARIAL_SHOCK", "EVIDENCE_SHOCK", "OBJECTIVE_SHOCK"}
        if shock_type not in valid_types:
            raise ValidationError(f"Invalid shock_type {shock_type!r}", details={"valid_shock_types": sorted(valid_types)})

        payload = {
            "shock_type": shock_type,
            "summary": summary.strip(),
            "content": content.strip(),
            "source_ref": source_ref,
            "affected_series_id": affected_series_id,
        }
        digest, archived_path = archive_bytes(self.archive_dir, canonical_json(payload).encode("utf-8"), suffix=".json")
        now = utc_now_iso()

        conn = self._connect()
        try:
            with write_tx(conn):
                tid = resolve_task_id(conn, task_id)
                task_rev = int(conn.execute("SELECT current_revision FROM tasks WHERE id = ?", (tid,)).fetchone()[0])
                affected = (
                    find_claims_depending_on_series(conn, tid, affected_series_id)
                    if affected_series_id
                    else [r["claim_id"] for r in get_all_latest_claim_rows(conn, tid)]
                )
                if release_key is not None:
                    dup = conn.execute(
                        "SELECT id, sha256 FROM artifacts WHERE task_id = ? AND kind = 'EXTERNAL_SHOCK' AND release_key = ?",
                        (tid, release_key),
                    ).fetchone()
                    if dup:
                        return {
                            "deduplicated": True,
                            "task_id": tid,
                            "artifact_id": dup["id"],
                            "sha256": dup["sha256"],
                            "affected_claims": affected,
                        }

                art_id = new_id("art_shock")
                conn.execute(
                    """
                    INSERT INTO artifacts (id, task_id, kind, sha256, vintage_tag, release_key, archive_path, metadata_json, created_at)
                    VALUES (?, ?, 'EXTERNAL_SHOCK', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        art_id, tid, digest, shock_type, release_key, str(archived_path),
                        canonical_json({"shock_type": shock_type, "summary": summary.strip()}), now,
                    ),
                )
                append_event(
                    conn, tid, "AGENT", "SHOCK_RECEIVED", task_rev,
                    {
                        "artifact_id": art_id,
                        "shock_type": shock_type,
                        "summary": summary.strip(),
                        "release_key": release_key,
                        "affected_claims": affected,
                    },
                    created_at=now,
                )
        finally:
            conn.close()

        return {
            "deduplicated": False,
            "task_id": tid,
            "artifact_id": art_id,
            "sha256": digest,
            "archive_path": str(archived_path),
            "shock_type": shock_type,
            "affected_claims": affected,
        }

    # ------------------------------------------------------------------
    # Controlled Computations & Agent-Authored Diagnostics
    # ------------------------------------------------------------------

    def _resolve_batch_input(
        self, task_id: str | None, series_id: str | None, batch_id: str | None
    ) -> tuple[str, str, str, str, Path, str]:
        conn = self._connect()
        try:
            tid = resolve_task_id(conn, task_id)
            if series_id is None:
                srows = conn.execute(
                    "SELECT series_id FROM data_series WHERE task_id = ? ORDER BY series_id", (tid,)
                ).fetchall()
                if len(srows) == 1:
                    series_id = str(srows[0]["series_id"])
                elif not srows:
                    raise NotFoundError(f"No data_series registered for task {tid!r}")
                else:
                    raise ValidationError(
                        "Multiple data_series exist; specify series_id explicitly",
                        details={"available_series_ids": [str(r["series_id"]) for r in srows]},
                    )

            if batch_id is None:
                srow = conn.execute(
                    "SELECT current_batch_id FROM data_series WHERE task_id = ? AND series_id = ?",
                    (tid, series_id),
                ).fetchone()
                if not srow or not srow["current_batch_id"]:
                    raise NotFoundError(f"Data series {series_id!r} has no active batch")
                batch_id = str(srow["current_batch_id"])

            brow = conn.execute(
                """
                SELECT b.batch_id, b.artifact_id, a.sha256, a.archive_path
                FROM data_batches b
                JOIN artifacts a ON a.id = b.artifact_id
                WHERE b.batch_id = ? AND b.task_id = ?
                """,
                (batch_id, tid),
            ).fetchone()
            if not brow:
                raise NotFoundError(f"Data batch {batch_id!r} not found in task {tid!r}")

            input_path = Path(brow["archive_path"])
            if not input_path.is_file():
                raise ValidationError(f"Archived batch file missing: {input_path}")
            actual_sha = sha256_file(input_path)
            if actual_sha != brow["sha256"]:
                raise ValidationError(
                    f"Data batch checksum mismatch for {batch_id!r}",
                    details={"expected_sha256": brow["sha256"], "actual_sha256": actual_sha},
                )
            return tid, series_id, batch_id, str(brow["artifact_id"]), input_path, actual_sha
        finally:
            conn.close()

    def run_computation(
        self,
        *,
        task_id: str | None = None,
        computation_name: str,
        params: dict[str, Any] | None = None,
        series_id: str | None = None,
        batch_id: str | None = None,
    ) -> dict[str, Any]:
        """Run a registered controlled computation on an archived data batch and archive verified metrics."""
        tid, sid, bid, input_art_id, input_path, actual_sha = self._resolve_batch_input(task_id, series_id, batch_id)
        comp_input = ComputationInput(
            task_id=tid, series_id=sid, batch_id=bid,
            input_path=input_path, input_sha256=actual_sha, params=dict(params or {}),
        )
        comp_out = self.registry.execute(computation_name, comp_input)

        doc = {
            "evidence_source": "controlled_computation",
            "computation_name": computation_name,
            "algorithm_version": comp_out.algorithm_version,
            "task_id": tid,
            "series_id": sid,
            "batch_id": bid,
            "input_artifact_id": input_art_id,
            "input_sha256": actual_sha,
            "params": comp_input.params,
            "run_scope": comp_out.run_scope,
            "metrics": comp_out.metrics,
            "summary": comp_out.summary,
            "raw_output": comp_out.raw_output,
        }
        raw_bytes = json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        digest, archived_path = archive_bytes(self.archive_dir, raw_bytes, suffix=".json")
        now = utc_now_iso()

        conn = self._connect()
        try:
            with write_tx(conn):
                task_rev = int(conn.execute("SELECT current_revision FROM tasks WHERE id = ?", (tid,)).fetchone()[0])
                art_id = new_id("art_metrics")
                conn.execute(
                    """
                    INSERT INTO artifacts (id, task_id, kind, sha256, vintage_tag, release_key, archive_path, metadata_json, created_at)
                    VALUES (?, ?, 'METRICS_JSON', ?, ?, NULL, ?, ?, ?)
                    """,
                    (
                        art_id, tid, digest, bid, str(archived_path),
                        canonical_json({
                            "evidence_source": "controlled_computation",
                            "computation_name": computation_name,
                            "algorithm_version": comp_out.algorithm_version,
                            "series_id": sid,
                            "batch_id": bid,
                            "params": comp_input.params,
                        }),
                        now,
                    ),
                )
                append_event(
                    conn, tid, "AGENT", "COMPUTATION_EXECUTED", task_rev,
                    {
                        "artifact_id": art_id,
                        "computation_name": computation_name,
                        "algorithm_version": comp_out.algorithm_version,
                        "series_id": sid,
                        "batch_id": bid,
                        "params": comp_input.params,
                        "sha256": digest,
                    },
                    created_at=now,
                )
        finally:
            conn.close()

        return {
            "task_id": tid,
            "artifact_id": art_id,
            "sha256": digest,
            "archive_path": str(archived_path),
            "computation_name": computation_name,
            "algorithm_version": comp_out.algorithm_version,
            "series_id": sid,
            "batch_id": bid,
            "input_sha256": actual_sha,
            "run_scope": comp_out.run_scope,
            "metrics": comp_out.metrics,
            "summary": comp_out.summary,
        }

    def run_diagnostic(
        self,
        *,
        task_id: str | None = None,
        title: str,
        script_source: str,
        series_id: str | None = None,
        batch_id: str | None = None,
        timeout_sec: int = 30,
    ) -> dict[str, Any]:
        """Execute an agent-authored Python diagnostic/counterexample script in an isolated temp dir.

        Archives both the exact Python source (`DIAGNOSTIC_CODE`) and verified output (`METRICS_JSON`)
        so novel investigations carry full reproducible provenance.
        """
        tid, sid, bid, input_art_id, input_path, actual_sha = self._resolve_batch_input(task_id, series_id, batch_id)
        diag = execute_diagnostic_script(
            script_source=script_source,
            input_path=input_path,
            series_id=sid,
            batch_id=bid,
            input_sha256=actual_sha,
            timeout_sec=timeout_sec,
        )

        code_sha, code_path = archive_bytes(self.archive_dir, diag.script_source.encode("utf-8"), suffix=".py")
        now = utc_now_iso()
        code_art_id = new_id("art_code")
        metrics_art_id = new_id("art_metrics")

        doc = {
            "evidence_source": "agent_diagnostic",
            "title": title.strip(),
            "algorithm_version": f"agent_diagnostic@{code_sha[:12]}",
            "diagnostic_code_artifact_id": code_art_id,
            "diagnostic_code_sha256": code_sha,
            "task_id": tid,
            "series_id": sid,
            "batch_id": bid,
            "input_artifact_id": input_art_id,
            "input_sha256": actual_sha,
            "duration_sec": diag.duration_sec,
            "stdout": diag.stdout,
            "metrics": diag.metrics,
            "summary": diag.summary,
        }
        metrics_bytes = json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        metrics_sha, metrics_path = archive_bytes(self.archive_dir, metrics_bytes, suffix=".json")

        conn = self._connect()
        try:
            with write_tx(conn):
                task_rev = int(conn.execute("SELECT current_revision FROM tasks WHERE id = ?", (tid,)).fetchone()[0])
                conn.execute(
                    """
                    INSERT INTO artifacts (id, task_id, kind, sha256, vintage_tag, release_key, archive_path, metadata_json, created_at)
                    VALUES (?, ?, 'DIAGNOSTIC_CODE', ?, ?, NULL, ?, ?, ?)
                    """,
                    (
                        code_art_id, tid, code_sha, bid, str(code_path),
                        canonical_json({"title": title.strip(), "series_id": sid, "batch_id": bid}), now,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO artifacts (id, task_id, kind, sha256, vintage_tag, release_key, archive_path, metadata_json, created_at)
                    VALUES (?, ?, 'METRICS_JSON', ?, ?, NULL, ?, ?, ?)
                    """,
                    (
                        metrics_art_id, tid, metrics_sha, bid, str(metrics_path),
                        canonical_json({
                            "evidence_source": "agent_diagnostic",
                            "title": title.strip(),
                            "diagnostic_code_artifact_id": code_art_id,
                            "series_id": sid,
                            "batch_id": bid,
                        }),
                        now,
                    ),
                )
                append_event(
                    conn, tid, "AGENT", "DIAGNOSTIC_EXECUTED", task_rev,
                    {
                        "title": title.strip(),
                        "code_artifact_id": code_art_id,
                        "metrics_artifact_id": metrics_art_id,
                        "code_sha256": code_sha,
                        "metrics_sha256": metrics_sha,
                        "duration_sec": diag.duration_sec,
                    },
                    created_at=now,
                )
        finally:
            conn.close()

        return {
            "task_id": tid,
            "artifact_id": metrics_art_id,
            "code_artifact_id": code_art_id,
            "evidence_source": "agent_diagnostic",
            "title": title.strip(),
            "series_id": sid,
            "batch_id": bid,
            "input_sha256": actual_sha,
            "duration_sec": diag.duration_sec,
            "stdout": diag.stdout,
            "metrics": diag.metrics,
            "summary": diag.summary,
        }

    # ------------------------------------------------------------------
    # Query State, Claims & Decisions
    # ------------------------------------------------------------------

    def get_task_state(
        self,
        *,
        task_id: str | None = None,
        include_all_statuses: bool = False,
    ) -> dict[str, Any]:
        conn = self._connect()
        try:
            tid = resolve_task_id(conn, task_id)
            trow = conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
            latest_rows = get_all_latest_claim_rows(conn, tid)
            claims = [
                hydrate_claim_row(conn, r)
                for r in latest_rows
                if include_all_statuses or r["status"] != "REFUTED"
            ]
            series_rows = conn.execute(
                """
                SELECT s.series_id, s.description, s.current_batch_id, b.artifact_id, b.vintage_tag, a.sha256
                FROM data_series s
                LEFT JOIN data_batches b ON b.batch_id = s.current_batch_id
                LEFT JOIN artifacts a ON a.id = b.artifact_id
                WHERE s.task_id = ?
                ORDER BY s.series_id ASC
                """,
                (tid,),
            ).fetchall()

            dec_rows = conn.execute(
                "SELECT * FROM decisions WHERE task_id = ? AND status = 'PENDING' ORDER BY created_at ASC",
                (tid,),
            ).fetchall()
            pending_decisions = []
            for d in dec_rows:
                stale_reasons = check_freshness(
                    conn, tid,
                    json.loads(d["read_claim_versions_json"]),
                    json.loads(d["read_data_batches_json"]),
                )
                pending_decisions.append(
                    {
                        "decision_id": d["id"],
                        "proposal_id": d["proposal_id"],
                        "reason_code": d["reason_code"],
                        "question": d["question"],
                        "options": json.loads(d["options_json"]),
                        "affected_claims": json.loads(d["affected_claims_json"]),
                        "created_at_revision": int(d["created_at_revision"]),
                        "status": d["status"],
                        "freshness": {"valid": len(stale_reasons) == 0, "stale_reasons": stale_reasons},
                    }
                )

            rev_row = conn.execute(
                "SELECT * FROM revisions WHERE task_id = ? ORDER BY revision DESC LIMIT 1", (tid,)
            ).fetchone()
            latest_pub = (
                {
                    "revision": int(rev_row["revision"]),
                    "export_status": rev_row["export_status"],
                    "md_path": rev_row["md_path"],
                    "html_path": rev_row["html_path"],
                    "manifest_path": rev_row["manifest_path"],
                    "published_at": rev_row["published_at"],
                }
                if rev_row
                else None
            )

            return {
                "task": {
                    "id": trow["id"],
                    "title": trow["title"],
                    "description": trow["description"],
                    "current_revision": int(trow["current_revision"]),
                    "status": trow["status"],
                },
                "claims": claims,
                "data_series": [dict(r) for r in series_rows],
                "available_computations": self.registry.list_specs(),
                "pending_decisions": pending_decisions,
                "latest_publication": latest_pub,
            }
        finally:
            conn.close()

    def get_claim(
        self,
        *,
        claim_id: str,
        task_id: str | None = None,
        include_history: bool = True,
    ) -> dict[str, Any]:
        conn = self._connect()
        try:
            tid = resolve_task_id(conn, task_id)
            rows = conn.execute(
                "SELECT * FROM claims WHERE task_id = ? AND claim_id = ? ORDER BY version ASC",
                (tid, claim_id),
            ).fetchall()
            if not rows:
                raise NotFoundError(f"Claim {claim_id!r} not found in task {tid!r}")
            result: dict[str, Any] = {"claim": hydrate_claim_row(conn, rows[-1])}
            if include_history:
                result["history"] = [hydrate_claim_row(conn, r) for r in rows]
            return result
        finally:
            conn.close()

    def get_decision(self, *, decision_id: str) -> dict[str, Any]:
        """Fetch full human-readable decision card context for CLI or local approval web UI."""
        conn = self._connect()
        try:
            drow = conn.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,)).fetchone()
            if not drow:
                raise NotFoundError(f"Decision not found: {decision_id!r}")
            tid = str(drow["task_id"])
            trow = conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
            prow = conn.execute("SELECT * FROM proposals WHERE id = ?", (drow["proposal_id"],)).fetchone()
            assert trow is not None and prow is not None

            rcv = json.loads(drow["read_claim_versions_json"])
            rdb = json.loads(drow["read_data_batches_json"])
            stale_reasons = check_freshness(conn, tid, rcv, rdb)
            options_snapshot = json.loads(prow["options_snapshot_json"])

            shock_info = None
            if prow["shock_artifact_id"]:
                srow = conn.execute("SELECT archive_path FROM artifacts WHERE id = ?", (prow["shock_artifact_id"],)).fetchone()
                if srow and Path(srow["archive_path"]).is_file():
                    shock_info = json.loads(Path(srow["archive_path"]).read_text(encoding="utf-8"))

            # Hydrate current baseline versions of affected claims for side-by-side comparison
            affected_cids = json.loads(drow["affected_claims_json"])
            current_claims = {}
            for cid in affected_cids:
                crow = get_latest_claim_row(conn, tid, cid)
                if crow:
                    current_claims[cid] = hydrate_claim_row(conn, crow)

            committed_pub = None
            if prow["committed_revision"] is not None:
                rrow = conn.execute(
                    "SELECT revision, md_path, html_path, manifest_path FROM revisions WHERE task_id = ? AND revision = ?",
                    (tid, int(prow["committed_revision"])),
                ).fetchone()
                if rrow:
                    committed_pub = dict(rrow)

            return {
                "decision_id": drow["id"],
                "proposal_id": drow["proposal_id"],
                "task": {
                    "id": trow["id"],
                    "title": trow["title"],
                    "current_revision": int(trow["current_revision"]),
                },
                "status": drow["status"],
                "reason_code": drow["reason_code"],
                "question": drow["question"],
                "summary": prow["summary"],
                "shock": shock_info,
                "affected_claims": affected_cids,
                "current_claims": current_claims,
                "scope_diffs": json.loads(prow["scope_diffs_json"]),
                "options": list(options_snapshot.values()),
                "selected_option": drow["selected_option"],
                "resolved_at": drow["resolved_at"],
                "created_at": drow["created_at"],
                "created_at_revision": int(drow["created_at_revision"]),
                "freshness": {
                    "valid": drow["status"] == "PENDING" and len(stale_reasons) == 0,
                    "stale_reasons": stale_reasons if drow["status"] == "PENDING" else json.loads(drow["stale_reason_json"] or "[]"),
                },
                "committed_revision": prow["committed_revision"],
                "committed_publication": committed_pub,
            }
        finally:
            conn.close()

    def list_decisions(self, *, task_id: str | None = None) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            if task_id:
                rows = conn.execute(
                    "SELECT id FROM decisions WHERE task_id = ? ORDER BY created_at DESC", (task_id,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT id FROM decisions ORDER BY created_at DESC").fetchall()
            ids = [str(r["id"]) for r in rows]
        finally:
            conn.close()
        return [self.get_decision(decision_id=did) for did in ids]

    # ------------------------------------------------------------------
    # Propose, Resolve (Human Out-of-Band Only), Commit
    # ------------------------------------------------------------------

    def propose_revision(
        self,
        *,
        task_id: str | None = None,
        summary: str,
        changes: list[dict[str, Any]],
        shock_artifact_id: str | None = None,
        decision_question: str | None = None,
        decision_options: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Create an immutable proposal snapshot; auto-opens a PENDING human decision card on scope shift."""
        now = utc_now_iso()
        conn = self._connect()
        try:
            with write_tx(conn):
                tid = resolve_task_id(conn, task_id)
                task_rev = int(conn.execute("SELECT current_revision FROM tasks WHERE id = ?", (tid,)).fetchone()[0])
                if shock_artifact_id:
                    if not conn.execute(
                        "SELECT id FROM artifacts WHERE id = ? AND task_id = ?", (shock_artifact_id, tid)
                    ).fetchone():
                        raise NotFoundError(f"Shock artifact {shock_artifact_id!r} not found")

                options_snapshot: dict[str, dict[str, Any]] = {}
                all_target_cids: list[str] = []
                combined_scope_diffs: dict[str, dict[str, Any]] = {}

                if decision_options is not None:
                    if not isinstance(decision_options, list) or len(decision_options) < 2:
                        raise ValidationError("decision_options must contain at least 2 options")
                    for opt in decision_options:
                        oid = str(opt.get("option_id", "")).strip()
                        label = str(opt.get("label", "")).strip()
                        desc = str(opt.get("description", "")).strip()
                        if not oid or not label:
                            raise ValidationError("Each decision option requires option_id and label")
                        if oid in options_snapshot:
                            raise ValidationError(f"Duplicate option_id {oid!r}")
                        opt_changes, opt_sdiffs = validate_change_list(conn, tid, opt.get("changes", []))
                        options_snapshot[oid] = {
                            "option_id": oid, "label": label, "description": desc,
                            "changes": opt_changes, "scope_diffs": opt_sdiffs,
                        }
                        for c in opt_changes:
                            if c["claim_id"] not in all_target_cids:
                                all_target_cids.append(c["claim_id"])
                        combined_scope_diffs.update(opt_sdiffs)
                    requires_decision = True
                    reason_code = "UNAPPROVED_SCOPE_SHIFT" if combined_scope_diffs else "OBJECTIVE_FORK"
                else:
                    validated_changes, scope_diffs = validate_change_list(conn, tid, changes)
                    all_target_cids = [c["claim_id"] for c in validated_changes]
                    combined_scope_diffs = scope_diffs
                    if scope_diffs:
                        requires_decision = True
                        reason_code = "UNAPPROVED_SCOPE_SHIFT"
                        keep_changes = []
                        for c in validated_changes:
                            cur_row = get_latest_claim_row(conn, tid, c["claim_id"])
                            assert cur_row is not None
                            keep_changes.append(
                                {
                                    "claim_id": c["claim_id"],
                                    "previous_version": int(cur_row["version"]),
                                    "previous_statement": str(cur_row["statement"]),
                                    "action": "KEEP",
                                    "status": str(cur_row["status"]),
                                    "location": str(cur_row["location"] or ""),
                                    "statement": str(cur_row["statement"]),
                                    "rationale": "用户选择维持原默认样本与分析口径，正式报告主口径不变，本次重算结果留作附注参考。",
                                    "scope": json.loads(cur_row["scope_json"]),
                                    "scope_diff": {},
                                    "bound_metrics": json.loads(cur_row["bound_metrics_json"]),
                                    "evidence_links": get_claim_links(conn, tid, c["claim_id"], int(cur_row["version"])),
                                    "data_series_ids": json.loads(cur_row["data_series_ids_json"]),
                                    "depends_on_claims": json.loads(cur_row["depends_on_claims_json"]),
                                    "reopen_conditions": cur_row["reopen_conditions"],
                                }
                            )
                        options_snapshot["keep_baseline_scope"] = {
                            "option_id": "keep_baseline_scope",
                            "label": "维持原默认口径（推荐先保留基线）",
                            "description": "保留正式报告现有默认样本与筛选口径不变，避免大幅缩减样本量；本次更严格口径的计算结果归档备查。",
                            "changes": keep_changes,
                            "scope_diffs": {},
                        }
                        options_snapshot["approve_scope_change"] = {
                            "option_id": "approve_scope_change",
                            "label": "采纳新默认口径并更新正式报告",
                            "description": "同意更改正式报告默认口径，并按新口径对应的重算指标与新表述发布新版报告。",
                            "changes": validated_changes,
                            "scope_diffs": scope_diffs,
                        }
                    else:
                        requires_decision = False
                        reason_code = ""
                        options_snapshot["default"] = {
                            "option_id": "default",
                            "label": "发布证据驱动的局部修订",
                            "description": summary.strip(),
                            "changes": validated_changes,
                            "scope_diffs": {},
                        }

                read_claim_versions, read_data_batches = collect_dependency_closure(conn, tid, all_target_cids)
                snapshot_sha = sha256_json({
                    "task_id": tid,
                    "options_snapshot": options_snapshot,
                    "read_claim_versions": read_claim_versions,
                    "read_data_batches": read_data_batches,
                    "scope_diffs": combined_scope_diffs,
                })
                proposal_id = new_id("prop")
                prop_status = "AWAITING_DECISION" if requires_decision else "READY"

                conn.execute(
                    """
                    INSERT INTO proposals (
                        id, task_id, created_at_revision, summary, shock_artifact_id,
                        status, requires_decision, options_snapshot_json,
                        read_claim_versions_json, read_data_batches_json,
                        scope_diffs_json, snapshot_sha256, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        proposal_id, tid, task_rev, summary.strip(), shock_artifact_id,
                        prop_status, 1 if requires_decision else 0,
                        canonical_json(options_snapshot), canonical_json(read_claim_versions),
                        canonical_json(read_data_batches), canonical_json(combined_scope_diffs),
                        snapshot_sha, now,
                    ),
                )
                append_event(
                    conn, tid, "AGENT", "PROPOSAL_CREATED", task_rev,
                    {
                        "proposal_id": proposal_id, "status": prop_status, "target_claims": all_target_cids,
                        "read_claim_versions": read_claim_versions, "read_data_batches": read_data_batches,
                        "scope_diffs": combined_scope_diffs, "snapshot_sha256": snapshot_sha,
                    },
                    created_at=now,
                )

                decision_id: str | None = None
                public_options: list[dict[str, Any]] = []
                if requires_decision:
                    decision_id = new_id("dec")
                    question = (
                        decision_question.strip()
                        if decision_question
                        else f"本次修改涉及更改正式报告的默认样本/筛选口径，请确认是否采纳新口径？"
                    )
                    public_options = [
                        {
                            "option_id": o["option_id"],
                            "label": o["label"],
                            "description": o["description"],
                            "scope_diffs": o["scope_diffs"],
                        }
                        for o in options_snapshot.values()
                    ]
                    conn.execute(
                        """
                        INSERT INTO decisions (
                            id, task_id, proposal_id, created_at_revision, reason_code,
                            question, options_json, affected_claims_json,
                            read_claim_versions_json, read_data_batches_json,
                            status, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?)
                        """,
                        (
                            decision_id, tid, proposal_id, task_rev, reason_code,
                            question, canonical_json(public_options), canonical_json(all_target_cids),
                            canonical_json(read_claim_versions), canonical_json(read_data_batches), now,
                        ),
                    )
                    recompute_task_status(conn, tid, now)
                    append_event(
                        conn, tid, "HOST_GUARD", "DECISION_REQUESTED", task_rev,
                        {
                            "decision_id": decision_id, "proposal_id": proposal_id,
                            "reason_code": reason_code, "affected_claims": all_target_cids,
                            "scope_diffs": combined_scope_diffs,
                        },
                        created_at=now,
                    )
        finally:
            conn.close()

        return {
            "task_id": tid,
            "proposal_id": proposal_id,
            "status": prop_status,
            "requires_decision": requires_decision,
            "decision_id": decision_id,
            "reason_code": reason_code or None,
            "scope_diffs": combined_scope_diffs,
            "options": public_options,
            "read_claim_versions": read_claim_versions,
            "read_data_batches": read_data_batches,
            "snapshot_sha256": snapshot_sha,
            "next_step": (
                f"需要人工拍板（已拦截自动发布）。可打开本地可视化审批页："
                f"claimrev approve-web --store {self.store_dir} --decision-id {decision_id} "
                f"（或使用 CLI：claimrev approve --store {self.store_dir} --decision-id {decision_id} --option <option_id>）"
                if requires_decision
                else f"Ready to publish via commit_revision(proposal_id={proposal_id!r})"
            ),
        }

    def resolve_decision(
        self,
        *,
        decision_id: str,
        selected_option: str,
        idempotency_key: str | None = None,
        resolved_by: str = "human-cli",
        auto_commit: bool = True,
        simulate_export_crash: bool = False,
    ) -> dict[str, Any]:
        """Human-only out-of-band approval (CLI or token-protected local web UI; NEVER exposed via MCP)."""
        now = utc_now_iso()
        conn = self._connect()
        proposal_id_to_commit: str | None = None
        replay_result: dict[str, Any] | None = None
        stale_to_raise: list[dict[str, Any]] | None = None
        pid_for_err = ""

        try:
            with write_tx(conn):
                drow = conn.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,)).fetchone()
                if not drow:
                    raise NotFoundError(f"Decision not found: {decision_id!r}")
                tid = str(drow["task_id"])
                pid = str(drow["proposal_id"])
                pid_for_err = pid
                prow = conn.execute("SELECT * FROM proposals WHERE id = ?", (pid,)).fetchone()
                assert prow is not None
                task_rev = int(conn.execute("SELECT current_revision FROM tasks WHERE id = ?", (tid,)).fetchone()[0])

                if idempotency_key is not None:
                    other = conn.execute(
                        "SELECT id FROM decisions WHERE idempotency_key = ? AND id != ?",
                        (idempotency_key, decision_id),
                    ).fetchone()
                    if other:
                        raise IdempotencyConflictError(
                            f"Idempotency key {idempotency_key!r} already used by decision {other['id']!r}",
                            details={"idempotency_key": idempotency_key, "other_decision_id": other["id"]},
                        )

                if drow["status"] == "RESOLVED":
                    prev_opt = str(drow["selected_option"])
                    if selected_option != prev_opt:
                        raise DecisionConflictError(
                            f"Decision {decision_id!r} was already confirmed with option {prev_opt!r}; cannot change to {selected_option!r}",
                            details={"decision_id": decision_id, "confirmed_option": prev_opt, "attempted_option": selected_option},
                        )
                    if auto_commit:
                        proposal_id_to_commit = pid
                        replay_result = {
                            "idempotent_replay": True, "decision_id": decision_id,
                            "proposal_id": pid, "status": "RESOLVED", "selected_option": prev_opt,
                        }
                    else:
                        return {
                            "idempotent_replay": True, "decision_id": decision_id, "proposal_id": pid,
                            "status": "RESOLVED", "selected_option": prev_opt, "committed_revision": prow["committed_revision"],
                        }
                elif drow["status"] == "SUPERSEDED":
                    stale_to_raise = json.loads(drow["stale_reason_json"] or "[]")
                else:
                    options_snapshot = json.loads(prow["options_snapshot_json"])
                    if selected_option not in options_snapshot:
                        raise ValidationError(
                            f"Unknown option {selected_option!r} for decision {decision_id!r}",
                            details={"valid_options": sorted(options_snapshot.keys())},
                        )
                    stale_reasons = check_freshness(
                        conn, tid,
                        json.loads(drow["read_claim_versions_json"]),
                        json.loads(drow["read_data_batches_json"]),
                    )
                    if stale_reasons:
                        self._mark_superseded(conn, tid, pid, decision_id, stale_reasons, task_rev, now)
                        stale_to_raise = stale_reasons
                    else:
                        conn.execute(
                            """
                            UPDATE decisions
                            SET status = 'RESOLVED', selected_option = ?, idempotency_key = ?, resolved_by = ?, resolved_at = ?
                            WHERE id = ?
                            """,
                            (selected_option, idempotency_key, resolved_by, now, decision_id),
                        )
                        recompute_task_status(conn, tid, now)
                        append_event(
                            conn, tid, "HUMAN", "DECISION_RESOLVED", task_rev,
                            {
                                "decision_id": decision_id, "proposal_id": pid,
                                "selected_option": selected_option, "idempotency_key": idempotency_key,
                                "resolved_by": resolved_by,
                            },
                            created_at=now,
                        )
                        if auto_commit:
                            proposal_id_to_commit = pid
                        else:
                            replay_result = {
                                "idempotent_replay": False, "decision_id": decision_id, "proposal_id": pid,
                                "status": "RESOLVED", "selected_option": selected_option, "committed_revision": None,
                            }

            if stale_to_raise:
                self._raise_stale_error(decision_id, pid_for_err, stale_to_raise)
        except DecisionConflictError:
            self._record_conflict_audit(decision_id, selected_option)
            raise
        finally:
            conn.close()

        if proposal_id_to_commit is not None:
            commit_res = self.commit_revision(
                proposal_id=proposal_id_to_commit, simulate_export_crash=simulate_export_crash
            )
            return {
                "idempotent_replay": bool(
                    (replay_result and replay_result.get("idempotent_replay")) or commit_res.get("idempotent_replay")
                ),
                "decision_id": decision_id,
                "proposal_id": proposal_id_to_commit,
                "status": "RESOLVED",
                "selected_option": selected_option,
                "committed_revision": commit_res["revision"],
                "commit_result": commit_res,
            }

        assert replay_result is not None
        return replay_result

    def _record_conflict_audit(self, decision_id: str, attempted_option: str) -> None:
        conn = self._connect()
        try:
            with write_tx(conn):
                drow = conn.execute("SELECT task_id FROM decisions WHERE id = ?", (decision_id,)).fetchone()
                if drow:
                    tid = str(drow["task_id"])
                    rev = int(conn.execute("SELECT current_revision FROM tasks WHERE id = ?", (tid,)).fetchone()[0])
                    append_event(
                        conn, tid, "HOST_GUARD", "DECISION_CONFLICT", rev,
                        {"decision_id": decision_id, "attempted_option": attempted_option},
                    )
        finally:
            conn.close()

    def _mark_superseded(
        self,
        conn: sqlite3.Connection,
        task_id: str,
        proposal_id: str,
        decision_id: str | None,
        stale_reasons: list[dict[str, Any]],
        task_rev: int,
        now: str,
    ) -> None:
        reasons_json = canonical_json(stale_reasons)
        conn.execute(
            "UPDATE proposals SET status = 'SUPERSEDED', stale_reason_json = ? WHERE id = ?",
            (reasons_json, proposal_id),
        )
        if decision_id:
            conn.execute(
                "UPDATE decisions SET status = 'SUPERSEDED', stale_reason_json = ? WHERE id = ?",
                (reasons_json, decision_id),
            )
        recompute_task_status(conn, task_id, now)
        append_event(
            conn, task_id, "HOST_GUARD", "PROPOSAL_INVALIDATED", task_rev,
            {"proposal_id": proposal_id, "decision_id": decision_id, "stale_reasons": stale_reasons},
            created_at=now,
        )

    def _raise_stale_error(
        self, decision_id: str | None, proposal_id: str, stale_reasons: list[dict[str, Any]]
    ) -> None:
        details = {"proposal_id": proposal_id, "decision_id": decision_id, "stale_reasons": stale_reasons}
        if any(r.get("kind") == "DATA_BATCH_CHANGED" for r in stale_reasons):
            raise StaleDataBatchError(
                f"Proposal {proposal_id!r} invalidated because dependent data batch changed", details=details
            )
        raise StaleClaimVersionError(
            f"Proposal {proposal_id!r} invalidated because a referenced claim version changed", details=details
        )

    def commit_revision(
        self,
        *,
        proposal_id: str,
        task_id: str | None = None,
        simulate_export_crash: bool = False,
    ) -> dict[str, Any]:
        """Atomically commit a READY or human-RESOLVED proposal, then export deterministic read-only diffs."""
        now = utc_now_iso()
        conn = self._connect()
        already_committed_rev: int | None = None
        new_rev: int | None = None
        chosen_option = "default"
        stale_to_raise: list[dict[str, Any]] | None = None
        dec_id_for_err: str | None = None

        try:
            with write_tx(conn):
                prow = conn.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,)).fetchone()
                if not prow:
                    raise NotFoundError(f"Proposal not found: {proposal_id!r}")
                tid = str(prow["task_id"])
                if task_id and task_id != tid:
                    raise ValidationError(f"Proposal {proposal_id!r} belongs to task {tid!r}, not {task_id!r}")

                if prow["status"] == "COMMITTED":
                    already_committed_rev = int(prow["committed_revision"])
                    chosen_option = str(prow["committed_option"] or "default")
                elif prow["status"] == "SUPERSEDED":
                    stale_to_raise = json.loads(prow["stale_reason_json"] or "[]")
                    drow = conn.execute("SELECT id FROM decisions WHERE proposal_id = ?", (proposal_id,)).fetchone()
                    dec_id_for_err = str(drow["id"]) if drow else None
                else:
                    drow = conn.execute("SELECT * FROM decisions WHERE proposal_id = ?", (proposal_id,)).fetchone()
                    dec_id_for_err = str(drow["id"]) if drow else None

                    if int(prow["requires_decision"]) == 1:
                        if not drow or drow["status"] == "PENDING":
                            raise DecisionRequiredError(
                                f"Proposal {proposal_id!r} requires human approval on decision {dec_id_for_err!r} before commit",
                                details={"proposal_id": proposal_id, "decision_id": dec_id_for_err},
                            )
                        if drow["status"] == "SUPERSEDED":
                            stale_to_raise = json.loads(drow["stale_reason_json"] or "[]")
                        else:
                            chosen_option = str(drow["selected_option"])

                    if stale_to_raise is None:
                        stale_reasons = check_freshness(
                            conn, tid,
                            json.loads(prow["read_claim_versions_json"]),
                            json.loads(prow["read_data_batches_json"]),
                        )
                        task_rev = int(conn.execute("SELECT current_revision FROM tasks WHERE id = ?", (tid,)).fetchone()[0])
                        if stale_reasons:
                            self._mark_superseded(conn, tid, proposal_id, dec_id_for_err, stale_reasons, task_rev, now)
                            stale_to_raise = stale_reasons
                        else:
                            options_snapshot = json.loads(prow["options_snapshot_json"])
                            if chosen_option not in options_snapshot:
                                raise ValidationError(f"Option {chosen_option!r} not in frozen proposal snapshot")
                            frozen_changes = options_snapshot[chosen_option]["changes"]

                            new_rev = task_rev + 1
                            changed_claims = []
                            shock_aid = prow["shock_artifact_id"]

                            for ch in frozen_changes:
                                cid = ch["claim_id"]
                                cur_row = get_latest_claim_row(conn, tid, cid)
                                assert cur_row is not None
                                next_ver = int(cur_row["version"]) + 1

                                conn.execute(
                                    """
                                    INSERT INTO claims (
                                        task_id, claim_id, version, task_revision, status, action_taken,
                                        location, statement, rationale, scope_json, bound_metrics_json,
                                        data_series_ids_json, depends_on_claims_json, reopen_conditions, created_at
                                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                    """,
                                    (
                                        tid, cid, next_ver, new_rev, ch["status"], ch["action"],
                                        ch.get("location", str(cur_row["location"] or "")),
                                        ch["statement"], ch["rationale"],
                                        canonical_json(ch["scope"]), canonical_json(ch["bound_metrics"]),
                                        canonical_json(ch["data_series_ids"]), canonical_json(ch["depends_on_claims"]),
                                        ch.get("reopen_conditions"), now,
                                    ),
                                )
                                for lk in ch["evidence_links"]:
                                    conn.execute(
                                        "INSERT OR IGNORE INTO claim_links (task_id, claim_id, claim_version, artifact_id, relation, rationale) VALUES (?, ?, ?, ?, ?, ?)",
                                        (tid, cid, next_ver, lk["artifact_id"], lk["relation"], lk["rationale"]),
                                    )
                                if shock_aid:
                                    conn.execute(
                                        "INSERT OR IGNORE INTO claim_links (task_id, claim_id, claim_version, artifact_id, relation, rationale) VALUES (?, ?, ?, ?, 'TRIGGERED_BY', ?)",
                                        (tid, cid, next_ver, shock_aid, "本次修订由已登记的提问/质疑触发"),
                                    )
                                changed_claims.append({
                                    "claim_id": cid, "previous_version": int(cur_row["version"]),
                                    "version": next_ver, "action_taken": ch["action"], "status": ch["status"],
                                })

                            all_latest = get_all_latest_claim_rows(conn, tid)
                            claim_snapshot = {r["claim_id"]: int(r["version"]) for r in all_latest}
                            ev_ids: set[str] = set()
                            if shock_aid:
                                ev_ids.add(str(shock_aid))
                            for r in all_latest:
                                for bm in json.loads(r["bound_metrics_json"]):
                                    ev_ids.add(str(bm["artifact_id"]))
                                for lk in get_claim_links(conn, tid, r["claim_id"], int(r["version"])):
                                    ev_ids.add(str(lk["artifact_id"]))
                            # Also include any DIAGNOSTIC_CODE artifacts referenced by METRICS_JSON artifacts
                            extra_code_ids: set[str] = set()
                            for aid in list(ev_ids):
                                arow = conn.execute("SELECT metadata_json FROM artifacts WHERE id = ?", (aid,)).fetchone()
                                if arow:
                                    meta = json.loads(arow["metadata_json"] or "{}")
                                    if meta.get("diagnostic_code_artifact_id"):
                                        extra_code_ids.add(str(meta["diagnostic_code_artifact_id"]))
                            ev_ids.update(extra_code_ids)

                            ev_index = build_evidence_index(conn, tid, sorted(ev_ids))
                            conn.execute(
                                """
                                INSERT INTO revisions (
                                    task_id, revision, proposal_id, summary, claim_snapshot_json,
                                    changed_claims_json, evidence_index_json, export_status, published_at
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING', ?)
                                """,
                                (
                                    tid, new_rev, proposal_id, prow["summary"],
                                    canonical_json(claim_snapshot), canonical_json(changed_claims),
                                    canonical_json(ev_index), now,
                                ),
                            )
                            conn.execute("UPDATE tasks SET current_revision = ?, updated_at = ? WHERE id = ?", (new_rev, now, tid))
                            conn.execute(
                                "UPDATE proposals SET status = 'COMMITTED', committed_revision = ?, committed_option = ?, committed_at = ? WHERE id = ?",
                                (new_rev, chosen_option, now, proposal_id),
                            )
                            recompute_task_status(conn, tid, now)
                            append_event(
                                conn, tid, "HOST_GUARD", "REVISION_COMMITTED", new_rev,
                                {
                                    "proposal_id": proposal_id, "committed_option": chosen_option,
                                    "changed_claims": changed_claims, "claim_snapshot": claim_snapshot,
                                },
                                created_at=now,
                            )
        finally:
            conn.close()

        if stale_to_raise is not None:
            self._raise_stale_error(dec_id_for_err, proposal_id, stale_to_raise)

        if already_committed_rev is not None:
            exports = self._ensure_revision_exported(tid, already_committed_rev)
            return {
                "idempotent_replay": True, "task_id": tid, "proposal_id": proposal_id,
                "revision": already_committed_rev, "committed_option": chosen_option,
                "exports": exports, "diff_summary": self._get_revision_diff_summary(tid, already_committed_rev),
            }

        assert new_rev is not None
        if simulate_export_crash or os.environ.get("CLAIMREV_SIMULATE_EXPORT_CRASH") == "1":
            task_pub = self.pub_dir / tid
            task_pub.mkdir(parents=True, exist_ok=True)
            (task_pub / f"revision_v{new_rev}.md.tmp.crashed").write_text("partial interrupted write", encoding="utf-8")
            raise ExportInterruptedError(
                f"Simulated export crash after DB commit for revision {new_rev}",
                details={"task_id": tid, "revision": new_rev, "proposal_id": proposal_id},
            )

        exports = self._export_revision(tid, new_rev)
        return {
            "idempotent_replay": False, "task_id": tid, "proposal_id": proposal_id,
            "revision": new_rev, "committed_option": chosen_option,
            "exports": exports, "diff_summary": self._get_revision_diff_summary(tid, new_rev),
        }

    # ------------------------------------------------------------------
    # Deterministic Export & Crash Recovery
    # ------------------------------------------------------------------

    def _build_render_context(self, conn: sqlite3.Connection, task_id: str, revision: int) -> dict[str, Any]:
        trow = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        rrow = conn.execute("SELECT * FROM revisions WHERE task_id = ? AND revision = ?", (task_id, revision)).fetchone()
        if not trow or not rrow:
            raise NotFoundError(f"Revision {revision} not found for task {task_id!r}")

        prev_rev = revision - 1 if revision > 1 else None
        prev_snapshot: dict[str, int] = {}
        if prev_rev is not None:
            prow = conn.execute(
                "SELECT claim_snapshot_json FROM revisions WHERE task_id = ? AND revision = ?", (task_id, prev_rev)
            ).fetchone()
            if prow:
                prev_snapshot = json.loads(prow["claim_snapshot_json"])

        decision_info = None
        if rrow["proposal_id"]:
            drow = conn.execute(
                "SELECT id, reason_code, selected_option, resolved_by, resolved_at FROM decisions WHERE proposal_id = ?",
                (rrow["proposal_id"],),
            ).fetchone()
            if drow:
                decision_info = dict(drow)

        curr_snapshot: dict[str, int] = json.loads(rrow["claim_snapshot_json"])
        claim_rows = []
        for cid in sorted(curr_snapshot.keys()):
            ver = int(curr_snapshot[cid])
            crow = conn.execute(
                "SELECT * FROM claims WHERE task_id = ? AND claim_id = ? AND version = ?", (task_id, cid, ver)
            ).fetchone()
            assert crow is not None
            hydrated = hydrate_claim_row(conn, crow)
            prev_ver = prev_snapshot.get(cid)
            if prev_ver is not None:
                prev_crow = conn.execute(
                    "SELECT statement, status FROM claims WHERE task_id = ? AND claim_id = ? AND version = ?",
                    (task_id, cid, int(prev_ver)),
                ).fetchone()
                hydrated["previous_statement"] = prev_crow["statement"] if prev_crow else hydrated["statement"]
                hydrated["previous_status"] = prev_crow["status"] if prev_crow else hydrated["status"]
                if prev_ver == ver and revision > 1:
                    hydrated["action_taken"] = "KEEP"
            else:
                hydrated["previous_statement"] = hydrated["statement"]
                hydrated["previous_status"] = hydrated["status"]
            claim_rows.append(hydrated)

        ev_index = json.loads(rrow["evidence_index_json"])
        evidence_items = []
        shocks = []
        diagnostic_scripts = []
        for ev in ev_index:
            p = Path(ev["archive_path"])
            if not p.is_file():
                raise ValidationError(f"Cannot export revision {revision}: archived evidence missing at {p}")
            actual_sha = sha256_file(p)
            if actual_sha != ev["sha256"]:
                raise ValidationError(f"Archived evidence checksum mismatch during export for {ev['id']}")
            if ev["kind"] == "DIAGNOSTIC_CODE":
                code_copy = dict(ev)
                code_copy["source_code"] = p.read_text(encoding="utf-8")
                diagnostic_scripts.append(code_copy)
            elif ev["kind"] == "EXTERNAL_SHOCK":
                payload = json.loads(p.read_text(encoding="utf-8"))
                shocks.append({
                    "artifact_id": ev["id"], "sha256": ev["sha256"],
                    "shock_type": payload.get("shock_type"), "summary": payload.get("summary", ""),
                    "content": payload.get("content", ""),
                })
            else:
                ev_copy = dict(ev)
                ev_copy["payload"] = json.loads(p.read_text(encoding="utf-8")) if ev["kind"] == "METRICS_JSON" else None
                evidence_items.append(ev_copy)

        events_rows = conn.execute(
            """
            SELECT seq, actor, event_type, task_revision, payload_json, created_at
            FROM events
            WHERE task_id = ? AND task_revision <= ? AND event_type != 'EXPORT_RECOVERED'
            ORDER BY seq ASC
            """,
            (task_id, revision),
        ).fetchall()
        events = [
            {
                "seq": int(e["seq"]), "actor": e["actor"], "event_type": e["event_type"],
                "task_revision": int(e["task_revision"]), "payload": json.loads(e["payload_json"]),
                "created_at": e["created_at"],
            }
            for e in events_rows
        ]

        return {
            "task": {"id": trow["id"], "title": trow["title"], "description": trow["description"]},
            "revision": revision,
            "prev_revision": prev_rev,
            "published_at": rrow["published_at"],
            "summary": rrow["summary"],
            "decision": decision_info,
            "claim_rows": claim_rows,
            "evidence_items": evidence_items,
            "diagnostic_scripts": diagnostic_scripts,
            "shocks": shocks,
            "events": events,
        }

    def _export_revision(self, task_id: str, revision: int) -> dict[str, Any]:
        conn = self._connect()
        try:
            ctx = self._build_render_context(conn, task_id, revision)
        finally:
            conn.close()

        task_pub_dir = self.pub_dir / task_id
        if task_pub_dir.exists():
            for leftover in task_pub_dir.glob("*.tmp.*"):
                try:
                    leftover.unlink()
                except OSError:
                    pass

        exports = write_revision_exports(task_pub_dir, ctx)
        conn = self._connect()
        try:
            with write_tx(conn):
                conn.execute(
                    """
                    UPDATE revisions
                    SET export_status = 'EXPORTED', md_path = ?, html_path = ?, manifest_path = ?, md_sha256 = ?, html_sha256 = ?
                    WHERE task_id = ? AND revision = ?
                    """,
                    (
                        exports["md_path"], exports["html_path"], exports["manifest_path"],
                        exports["md_sha256"], exports["html_sha256"], task_id, revision,
                    ),
                )
        finally:
            conn.close()
        return exports

    def _ensure_revision_exported(self, task_id: str, revision: int) -> dict[str, Any]:
        conn = self._connect()
        try:
            rrow = conn.execute(
                "SELECT * FROM revisions WHERE task_id = ? AND revision = ?", (task_id, revision)
            ).fetchone()
            assert rrow is not None
            needs_rebuild = rrow["export_status"] != "EXPORTED"
            if not needs_rebuild:
                for pkey, skey in (("md_path", "md_sha256"), ("html_path", "html_sha256")):
                    p = Path(rrow[pkey]) if rrow[pkey] else None
                    if not p or not p.is_file() or sha256_file(p) != rrow[skey]:
                        needs_rebuild = True
                        break
            if not needs_rebuild:
                return {
                    "md_path": rrow["md_path"], "html_path": rrow["html_path"],
                    "manifest_path": rrow["manifest_path"], "md_sha256": rrow["md_sha256"],
                    "html_sha256": rrow["html_sha256"], "rebuilt": False,
                }
        finally:
            conn.close()
        exports = self._export_revision(task_id, revision)
        exports["rebuilt"] = True
        return exports

    def recover_exports(self, *, task_id: str | None = None) -> dict[str, Any]:
        """Rebuild any pending, missing, or corrupted revision exports purely from SQLite + archive."""
        conn = self._connect()
        try:
            if task_id:
                rows = conn.execute(
                    "SELECT task_id, revision, export_status, md_path, html_path, md_sha256, html_sha256 FROM revisions WHERE task_id = ? ORDER BY revision ASC",
                    (task_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT task_id, revision, export_status, md_path, html_path, md_sha256, html_sha256 FROM revisions ORDER BY task_id ASC, revision ASC"
                ).fetchall()
        finally:
            conn.close()

        recovered = []
        for r in rows:
            tid, rev = str(r["task_id"]), int(r["revision"])
            needs = r["export_status"] != "EXPORTED"
            if not needs:
                for pkey, skey in (("md_path", "md_sha256"), ("html_path", "html_sha256")):
                    p = Path(r[pkey]) if r[pkey] else None
                    if not p or not p.is_file() or sha256_file(p) != r[skey]:
                        needs = True
                        break
            if needs:
                exports = self._export_revision(tid, rev)
                conn = self._connect()
                try:
                    with write_tx(conn):
                        append_event(
                            conn, tid, "SYSTEM", "EXPORT_RECOVERED", rev,
                            {"revision": rev, "md_sha256": exports["md_sha256"], "html_sha256": exports["html_sha256"]},
                        )
                finally:
                    conn.close()
                recovered.append({"task_id": tid, "revision": rev, "exports": exports})

        return {"recovered_count": len(recovered), "recovered": recovered}

    def _get_revision_diff_summary(self, task_id: str, revision: int) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            ctx = self._build_render_context(conn, task_id, revision)
        finally:
            conn.close()
        return [
            {
                "claim_id": row["claim_id"],
                "version": row["version"],
                "action_taken": row["action_taken"],
                "status": row["status"],
                "previous_statement": row.get("previous_statement"),
                "statement": row["statement"],
                "rationale": row["rationale"],
                "bound_metrics": [
                    {
                        "metric_key": m["metric_key"],
                        "display_value": m["display_value"],
                        "unit": m["unit"],
                        "stat_meaning": m["stat_meaning"],
                        "sample_n": m["sample_n"],
                        "artifact_id": m["artifact_id"],
                    }
                    for m in row["bound_metrics"]
                ],
            }
            for row in ctx["claim_rows"]
        ]
