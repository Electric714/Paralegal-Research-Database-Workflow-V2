from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any


SCHEMA_VERSION = 2


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def apply_research_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            component TEXT PRIMARY KEY,
            version INTEGER NOT NULL,
            applied_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS research_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            research_run_id INTEGER NOT NULL,
            bidder_id INTEGER NOT NULL,
            source_key TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'NOT_CHECKED',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            started_at TEXT,
            completed_at TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(research_run_id) REFERENCES research_runs(id),
            FOREIGN KEY(bidder_id) REFERENCES bidders(id),
            UNIQUE(research_run_id, bidder_id, source_key)
        );

        CREATE TABLE IF NOT EXISTS source_checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            research_task_id INTEGER NOT NULL,
            checked_at TEXT NOT NULL,
            result_status TEXT NOT NULL,
            identity_status TEXT NOT NULL,
            completeness_status TEXT NOT NULL,
            identity_confidence REAL,
            acquisition_method TEXT,
            http_status INTEGER,
            adapter_version TEXT NOT NULL,
            parser_version TEXT NOT NULL,
            warnings_json TEXT NOT NULL DEFAULT '[]',
            FOREIGN KEY(research_task_id) REFERENCES research_tasks(id)
        );

        CREATE TABLE IF NOT EXISTS evidence_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_check_id INTEGER NOT NULL,
            bidder_id INTEGER NOT NULL,
            source_key TEXT NOT NULL,
            research_run_id INTEGER NOT NULL,
            retrieved_at TEXT NOT NULL,
            searched_name TEXT NOT NULL,
            searched_address TEXT,
            source_record_id TEXT,
            source_url TEXT,
            acquisition_method TEXT,
            http_status INTEGER,
            raw_artifact_path TEXT,
            raw_artifact_sha256 TEXT,
            normalized_json TEXT NOT NULL DEFAULT '{}',
            identity_confidence REAL,
            identity_status TEXT NOT NULL,
            completeness_status TEXT NOT NULL,
            result_status TEXT NOT NULL,
            parser_version TEXT NOT NULL,
            adapter_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(source_check_id) REFERENCES source_checks(id),
            FOREIGN KEY(bidder_id) REFERENCES bidders(id),
            FOREIGN KEY(research_run_id) REFERENCES research_runs(id)
        );

        CREATE TABLE IF NOT EXISTS evidence_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            evidence_snapshot_id INTEGER NOT NULL,
            field_name TEXT NOT NULL,
            observed_value TEXT,
            source_record_id TEXT,
            source_url TEXT,
            details_json TEXT NOT NULL DEFAULT '{}',
            FOREIGN KEY(evidence_snapshot_id) REFERENCES evidence_snapshots(id)
        );

        CREATE TABLE IF NOT EXISTS identity_judgments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bidder_id INTEGER NOT NULL,
            source_key TEXT NOT NULL,
            source_record_id TEXT NOT NULL,
            judgment TEXT NOT NULL,
            confidence REAL,
            decided_by TEXT,
            decided_at TEXT NOT NULL,
            notes TEXT,
            FOREIGN KEY(bidder_id) REFERENCES bidders(id),
            UNIQUE(bidder_id, source_key, source_record_id)
        );

        CREATE TABLE IF NOT EXISTS proposed_changes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bidder_id INTEGER NOT NULL,
            evidence_snapshot_id INTEGER NOT NULL,
            source_key TEXT NOT NULL,
            field_name TEXT NOT NULL,
            current_value TEXT,
            proposed_value TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            reviewed_at TEXT,
            reviewed_by TEXT,
            review_note TEXT,
            FOREIGN KEY(bidder_id) REFERENCES bidders(id),
            FOREIGN KEY(evidence_snapshot_id) REFERENCES evidence_snapshots(id)
        );

        CREATE TABLE IF NOT EXISTS master_revisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bidder_id INTEGER NOT NULL,
            field_name TEXT NOT NULL,
            previous_value TEXT,
            new_value TEXT,
            evidence_snapshot_id INTEGER,
            source_key TEXT,
            research_run_id INTEGER,
            approved_by TEXT,
            approved_at TEXT NOT NULL,
            FOREIGN KEY(bidder_id) REFERENCES bidders(id),
            FOREIGN KEY(evidence_snapshot_id) REFERENCES evidence_snapshots(id),
            FOREIGN KEY(research_run_id) REFERENCES research_runs(id)
        );

        CREATE TABLE IF NOT EXISTS audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            actor TEXT,
            bidder_id INTEGER,
            research_run_id INTEGER,
            source_key TEXT,
            entity_type TEXT,
            entity_id INTEGER,
            message TEXT NOT NULL,
            details_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_tasks_run ON research_tasks(research_run_id);
        CREATE INDEX IF NOT EXISTS idx_tasks_source_status ON research_tasks(source_key, status);
        CREATE INDEX IF NOT EXISTS idx_evidence_bidder ON evidence_snapshots(bidder_id, source_key, retrieved_at);
        CREATE INDEX IF NOT EXISTS idx_evidence_records_snapshot ON evidence_records(evidence_snapshot_id);
        CREATE INDEX IF NOT EXISTS idx_changes_status ON proposed_changes(status, bidder_id);
        CREATE INDEX IF NOT EXISTS idx_revisions_bidder ON master_revisions(bidder_id, approved_at);
        CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_events(created_at);

        CREATE TRIGGER IF NOT EXISTS prevent_evidence_snapshot_update
        BEFORE UPDATE ON evidence_snapshots
        BEGIN
            SELECT RAISE(ABORT, 'evidence snapshots are immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS prevent_evidence_snapshot_delete
        BEFORE DELETE ON evidence_snapshots
        BEGIN
            SELECT RAISE(ABORT, 'evidence snapshots are immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS prevent_evidence_record_update
        BEFORE UPDATE ON evidence_records
        BEGIN
            SELECT RAISE(ABORT, 'evidence records are immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS prevent_evidence_record_delete
        BEFORE DELETE ON evidence_records
        BEGIN
            SELECT RAISE(ABORT, 'evidence records are immutable');
        END;
        """
    )
    conn.execute(
        """
        INSERT INTO schema_migrations(component, version, applied_at)
        VALUES ('research_evidence', ?, ?)
        ON CONFLICT(component) DO UPDATE SET version=excluded.version, applied_at=excluded.applied_at
        """,
        (SCHEMA_VERSION, utcnow()),
    )


def add_audit_event(
    conn: sqlite3.Connection,
    event_type: str,
    message: str,
    *,
    actor: str | None = None,
    bidder_id: int | None = None,
    research_run_id: int | None = None,
    source_key: str | None = None,
    entity_type: str | None = None,
    entity_id: int | None = None,
    details: dict[str, Any] | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO audit_events(
            event_type, actor, bidder_id, research_run_id, source_key,
            entity_type, entity_id, message, details_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_type,
            actor,
            bidder_id,
            research_run_id,
            source_key,
            entity_type,
            entity_id,
            message,
            json.dumps(details or {}, ensure_ascii=False, sort_keys=True),
            utcnow(),
        ),
    )
    return int(cur.lastrowid)
