"""Storage, content-addressed archive, and SQL query helpers for claimrev."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from .errors import NotFoundError, ValidationError
from .models import canonical_json, sha256_bytes, sha256_file, utc_now_iso


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def archive_bytes(archive_dir: Path, data: bytes, suffix: str = ".json") -> tuple[str, Path]:
    digest = sha256_bytes(data)
    target = archive_dir / f"{digest}{suffix}"
    if not target.exists():
        tmp = target.with_suffix(target.suffix + f".tmp.{os.getpid()}")
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    return digest, target


def archive_file(archive_dir: Path, src_path: Path) -> tuple[str, Path]:
    src = Path(src_path).expanduser().resolve()
    if not src.is_file():
        raise NotFoundError(f"Input file not found: {src}")
    suffix = src.suffix or ".bin"
    digest = sha256_file(src)
    target = archive_dir / f"{digest}{suffix}"
    if not target.exists():
        tmp = target.with_suffix(target.suffix + f".tmp.{os.getpid()}")
        shutil.copyfile(src, tmp)
        with open(tmp, "rb") as f:
            os.fsync(f.fileno())
        os.replace(tmp, target)
    return digest, target


def append_event(
    conn: sqlite3.Connection,
    task_id: str,
    actor: str,
    event_type: str,
    task_revision: int,
    payload: dict[str, Any],
    created_at: str | None = None,
) -> int:
    ts = created_at or utc_now_iso()
    cur = conn.execute(
        """
        INSERT INTO events (task_id, actor, event_type, task_revision, payload_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (task_id, actor, event_type, task_revision, canonical_json(payload), ts),
    )
    return int(cur.lastrowid)


def resolve_task_id(conn: sqlite3.Connection, task_id: str | None) -> str:
    if task_id:
        row = conn.execute("SELECT id FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            raise NotFoundError(f"Task not found: {task_id!r}", details={"task_id": task_id})
        return str(row["id"])
    rows = conn.execute("SELECT id FROM tasks ORDER BY created_at ASC").fetchall()
    if len(rows) == 1:
        return str(rows[0]["id"])
    if not rows:
        raise NotFoundError("No tasks exist in this claimrev store")
    raise ValidationError(
        "Multiple tasks exist; specify task_id explicitly",
        details={"available_task_ids": [str(r["id"]) for r in rows]},
    )


def get_latest_claim_row(conn: sqlite3.Connection, task_id: str, claim_id: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM claims
        WHERE task_id = ? AND claim_id = ?
        ORDER BY version DESC
        LIMIT 1
        """,
        (task_id, claim_id),
    ).fetchone()


def get_all_latest_claim_rows(conn: sqlite3.Connection, task_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT c.*
        FROM claims c
        INNER JOIN (
            SELECT claim_id, MAX(version) AS max_v
            FROM claims
            WHERE task_id = ?
            GROUP BY claim_id
        ) latest
          ON c.task_id = ? AND c.claim_id = latest.claim_id AND c.version = latest.max_v
        ORDER BY c.claim_id ASC
        """,
        (task_id, task_id),
    ).fetchall()


def get_claim_links(conn: sqlite3.Connection, task_id: str, claim_id: str, version: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT artifact_id, relation, rationale
        FROM claim_links
        WHERE task_id = ? AND claim_id = ? AND claim_version = ?
        ORDER BY relation ASC, artifact_id ASC
        """,
        (task_id, claim_id, version),
    ).fetchall()
    return [dict(r) for r in rows]


def hydrate_claim_row(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    return {
        "task_id": row["task_id"],
        "claim_id": row["claim_id"],
        "version": int(row["version"]),
        "task_revision": int(row["task_revision"]),
        "status": row["status"],
        "action_taken": row["action_taken"],
        "location": row["location"],
        "statement": row["statement"],
        "rationale": row["rationale"],
        "scope": json.loads(row["scope_json"]),
        "bound_metrics": json.loads(row["bound_metrics_json"]),
        "data_series_ids": json.loads(row["data_series_ids_json"]),
        "depends_on_claims": json.loads(row["depends_on_claims_json"]),
        "reopen_conditions": row["reopen_conditions"],
        "links": get_claim_links(conn, row["task_id"], row["claim_id"], int(row["version"])),
        "created_at": row["created_at"],
    }


def load_artifact_json(
    conn: sqlite3.Connection, task_id: str, artifact_id: str
) -> tuple[sqlite3.Row, dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM artifacts WHERE id = ? AND task_id = ?",
        (artifact_id, task_id),
    ).fetchone()
    if not row:
        raise NotFoundError(
            f"Artifact not found in task {task_id!r}: {artifact_id!r}",
            details={"task_id": task_id, "artifact_id": artifact_id},
        )
    p = Path(row["archive_path"])
    if not p.is_file():
        raise ValidationError(
            f"Archived artifact file missing on disk: {p}",
            details={"artifact_id": artifact_id, "archive_path": str(p)},
        )
    actual_sha = sha256_file(p)
    if actual_sha != row["sha256"]:
        raise ValidationError(
            f"Archived artifact checksum mismatch for {artifact_id!r}",
            details={"artifact_id": artifact_id, "expected_sha256": row["sha256"], "actual_sha256": actual_sha},
        )
    payload = json.loads(p.read_text(encoding="utf-8"))
    return row, payload


def find_claims_depending_on_series(conn: sqlite3.Connection, task_id: str, series_id: str) -> list[str]:
    latest = get_all_latest_claim_rows(conn, task_id)
    affected = {
        row["claim_id"]
        for row in latest
        if series_id in json.loads(row["data_series_ids_json"])
    }
    changed = True
    while changed:
        changed = False
        for row in latest:
            cid = row["claim_id"]
            if cid in affected:
                continue
            deps = set(json.loads(row["depends_on_claims_json"]))
            if deps & affected:
                affected.add(cid)
                changed = True
    return sorted(affected)


def recompute_task_status(conn: sqlite3.Connection, task_id: str, now: str) -> str:
    pending_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM decisions WHERE task_id = ? AND status = 'PENDING'",
            (task_id,),
        ).fetchone()[0]
    )
    new_status = "HUMAN_DECISION_REQUIRED" if pending_count > 0 else "ACTIVE"
    conn.execute(
        "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
        (new_status, now, task_id),
    )
    return new_status


def build_evidence_index(
    conn: sqlite3.Connection, task_id: str, artifact_ids: list[str]
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for aid in artifact_ids:
        row = conn.execute(
            "SELECT id, kind, sha256, vintage_tag, archive_path, metadata_json, created_at FROM artifacts WHERE id = ? AND task_id = ?",
            (aid, task_id),
        ).fetchone()
        if row:
            items.append(
                {
                    "id": row["id"],
                    "kind": row["kind"],
                    "sha256": row["sha256"],
                    "vintage_tag": row["vintage_tag"],
                    "archive_path": row["archive_path"],
                    "metadata": json.loads(row["metadata_json"]),
                    "created_at": row["created_at"],
                }
            )
    return items
