"""SQLite persistence layer for claimrev.

Enforces foreign keys (including composite FKs on claim_links), WAL mode,
and single-writer atomic transactions.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    current_revision INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'INVESTIGATING', 'HUMAN_DECISION_REQUIRED')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    kind TEXT NOT NULL CHECK (kind IN ('DATA', 'METRICS_JSON', 'EXTERNAL_SHOCK', 'DIAGNOSTIC_CODE', 'REPORT_IMPORT')),
    sha256 TEXT NOT NULL,
    vintage_tag TEXT,
    release_key TEXT,
    archive_path TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_artifacts_release_dedup
    ON artifacts (task_id, kind, release_key)
    WHERE release_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS data_series (
    task_id TEXT NOT NULL REFERENCES tasks(id),
    series_id TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    current_batch_id TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (task_id, series_id)
);

CREATE TABLE IF NOT EXISTS data_batches (
    batch_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    series_id TEXT NOT NULL,
    artifact_id TEXT NOT NULL REFERENCES artifacts(id),
    supersedes_batch_id TEXT REFERENCES data_batches(batch_id),
    vintage_tag TEXT,
    release_key TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (task_id, series_id) REFERENCES data_series(task_id, series_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_data_batches_release_dedup
    ON data_batches (task_id, series_id, release_key)
    WHERE release_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS claims (
    task_id TEXT NOT NULL REFERENCES tasks(id),
    claim_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    task_revision INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'NARROWED', 'REFUTED', 'CONDITIONAL')),
    action_taken TEXT NOT NULL CHECK (action_taken IN ('INITIAL', 'KEEP', 'NARROW', 'REVISE', 'REFUTE', 'CONDITIONAL')),
    location TEXT NOT NULL DEFAULT '',
    statement TEXT NOT NULL,
    rationale TEXT NOT NULL,
    scope_json TEXT NOT NULL,
    bound_metrics_json TEXT NOT NULL,
    data_series_ids_json TEXT NOT NULL,
    depends_on_claims_json TEXT NOT NULL,
    reopen_conditions TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (task_id, claim_id, version)
);

CREATE TABLE IF NOT EXISTS claim_links (
    task_id TEXT NOT NULL,
    claim_id TEXT NOT NULL,
    claim_version INTEGER NOT NULL,
    artifact_id TEXT NOT NULL REFERENCES artifacts(id),
    relation TEXT NOT NULL CHECK (relation IN ('SUPPORTS', 'REFUTES', 'DEFENDS_AGAINST', 'TRIGGERED_BY')),
    rationale TEXT NOT NULL,
    PRIMARY KEY (task_id, claim_id, claim_version, artifact_id, relation),
    FOREIGN KEY (task_id, claim_id, claim_version) REFERENCES claims(task_id, claim_id, version)
);

CREATE TABLE IF NOT EXISTS proposals (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    created_at_revision INTEGER NOT NULL,
    summary TEXT NOT NULL,
    shock_artifact_id TEXT REFERENCES artifacts(id),
    status TEXT NOT NULL CHECK (status IN ('READY', 'AWAITING_DECISION', 'COMMITTED', 'SUPERSEDED', 'REJECTED')),
    requires_decision INTEGER NOT NULL DEFAULT 0,
    options_snapshot_json TEXT NOT NULL,
    read_claim_versions_json TEXT NOT NULL,
    read_data_batches_json TEXT NOT NULL,
    scope_diffs_json TEXT NOT NULL,
    snapshot_sha256 TEXT NOT NULL,
    committed_revision INTEGER,
    committed_option TEXT,
    stale_reason_json TEXT,
    created_at TEXT NOT NULL,
    committed_at TEXT
);

CREATE TABLE IF NOT EXISTS decisions (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    proposal_id TEXT NOT NULL UNIQUE REFERENCES proposals(id),
    created_at_revision INTEGER NOT NULL,
    reason_code TEXT NOT NULL CHECK (reason_code IN ('UNAPPROVED_SCOPE_SHIFT', 'OBJECTIVE_FORK')),
    question TEXT NOT NULL,
    options_json TEXT NOT NULL,
    affected_claims_json TEXT NOT NULL,
    read_claim_versions_json TEXT NOT NULL,
    read_data_batches_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PENDING', 'RESOLVED', 'SUPERSEDED')),
    selected_option TEXT,
    idempotency_key TEXT,
    resolved_by TEXT,
    resolved_at TEXT,
    stale_reason_json TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS revisions (
    task_id TEXT NOT NULL REFERENCES tasks(id),
    revision INTEGER NOT NULL,
    proposal_id TEXT REFERENCES proposals(id),
    summary TEXT NOT NULL,
    claim_snapshot_json TEXT NOT NULL,
    changed_claims_json TEXT NOT NULL,
    evidence_index_json TEXT NOT NULL,
    export_status TEXT NOT NULL CHECK (export_status IN ('PENDING', 'EXPORTED')),
    md_path TEXT,
    html_path TEXT,
    manifest_path TEXT,
    md_sha256 TEXT,
    html_sha256 TEXT,
    published_at TEXT NOT NULL,
    PRIMARY KEY (task_id, revision)
);

CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    actor TEXT NOT NULL CHECK (actor IN ('AGENT', 'HOST_GUARD', 'HUMAN', 'SYSTEM')),
    event_type TEXT NOT NULL,
    task_revision INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def open_connection(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), isolation_level=None, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = FULL;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    return conn


def init_db(db_path: Path) -> None:
    conn = open_connection(db_path)
    try:
        conn.executescript(SCHEMA_SQL)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(claims)").fetchall()}
        if "location" not in cols:
            conn.execute("ALTER TABLE claims ADD COLUMN location TEXT NOT NULL DEFAULT '';")
    finally:
        conn.close()


@contextmanager
def write_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Single-writer atomic SQLite transaction (BEGIN IMMEDIATE)."""
    conn.execute("BEGIN IMMEDIATE;")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK;")
        raise
    else:
        conn.execute("COMMIT;")
