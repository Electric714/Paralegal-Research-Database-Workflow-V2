from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"
IMPORT_DIR = DATA_DIR / "imports"
DB_PATH = DATA_DIR / "paralegal_research.db"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    IMPORT_DIR.mkdir(parents=True, exist_ok=True)


@contextmanager
def connect():
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS imports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL,
                imported_at TEXT NOT NULL,
                row_count INTEGER NOT NULL,
                columns_json TEXT NOT NULL,
                snapshot_path TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS bidders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                import_id INTEGER NOT NULL,
                external_id TEXT,
                contractor_name TEXT NOT NULL,
                related_companies TEXT,
                city TEXT,
                state TEXT,
                row_json TEXT NOT NULL,
                FOREIGN KEY(import_id) REFERENCES imports(id)
            );
            CREATE INDEX IF NOT EXISTS idx_bidders_name ON bidders(contractor_name);
            CREATE INDEX IF NOT EXISTS idx_bidders_external_id ON bidders(external_id);
            CREATE TABLE IF NOT EXISTS research_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                bidder_scope_json TEXT NOT NULL,
                source_keys_json TEXT NOT NULL,
                bidder_count INTEGER NOT NULL,
                source_count INTEGER NOT NULL,
                message TEXT
            );
            CREATE TABLE IF NOT EXISTS review_proposals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bidder_id INTEGER NOT NULL,
                source_key TEXT NOT NULL,
                field_name TEXT NOT NULL,
                current_value TEXT,
                proposed_value TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                evidence_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                reviewed_at TEXT,
                FOREIGN KEY(bidder_id) REFERENCES bidders(id)
            );
            CREATE TABLE IF NOT EXISTS diagnostics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                severity TEXT NOT NULL,
                source_key TEXT,
                bidder_name TEXT,
                stage TEXT,
                message TEXT NOT NULL,
                details_json TEXT NOT NULL DEFAULT '{}'
            );
            """
        )


def add_diagnostic(severity: str, message: str, *, source_key: str | None = None, bidder_name: str | None = None, stage: str | None = None, details: dict | None = None) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO diagnostics(timestamp, severity, source_key, bidder_name, stage, message, details_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (utcnow(), severity, source_key, bidder_name, stage, message, json.dumps(details or {}, sort_keys=True)),
        )


def replace_master_database(filename: str, columns: list[str], rows: list[dict[str, str]], snapshot_path: str) -> int:
    with connect() as conn:
        conn.execute("UPDATE imports SET active = 0 WHERE active = 1")
        cur = conn.execute(
            "INSERT INTO imports(filename, imported_at, row_count, columns_json, snapshot_path, active) VALUES (?, ?, ?, ?, ?, 1)",
            (filename, utcnow(), len(rows), json.dumps(columns), snapshot_path),
        )
        import_id = int(cur.lastrowid)
        conn.execute("DELETE FROM review_proposals")
        conn.execute("DELETE FROM bidders")
        conn.executemany(
            "INSERT INTO bidders(import_id, external_id, contractor_name, related_companies, city, state, row_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(
                import_id,
                row.get("id", ""),
                row.get("contractor_name", ""),
                row.get("related_companies", ""),
                row.get("city", ""),
                row.get("state", ""),
                json.dumps(row, ensure_ascii=False),
            ) for row in rows],
        )
        return import_id


def active_import() -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM imports WHERE active = 1 ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            return None
        item = dict(row)
        item["columns"] = json.loads(item.pop("columns_json"))
        return item


def list_bidders(search: str = "", limit: int = 100, offset: int = 0) -> tuple[list[dict], int]:
    search = search.strip()
    where = ""
    params: list[object] = []
    if search:
        where = "WHERE contractor_name LIKE ? OR related_companies LIKE ? OR external_id LIKE ? OR city LIKE ? OR state LIKE ?"
        q = f"%{search}%"
        params.extend([q, q, q, q, q])
    with connect() as conn:
        total = int(conn.execute(f"SELECT COUNT(*) FROM bidders {where}", params).fetchone()[0])
        rows = conn.execute(
            f"SELECT id, row_json FROM bidders {where} ORDER BY contractor_name LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        result = []
        for row in rows:
            item = json.loads(row["row_json"])
            item["_internal_id"] = row["id"]
            result.append(item)
        return result, total


def get_bidder(bidder_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT id, row_json FROM bidders WHERE id = ?", (bidder_id,)).fetchone()
        if not row:
            return None
        item = json.loads(row["row_json"])
        item["_internal_id"] = row["id"]
        return item


def all_bidder_rows() -> list[dict[str, str]]:
    with connect() as conn:
        return [json.loads(row["row_json"]) for row in conn.execute("SELECT row_json FROM bidders ORDER BY id").fetchall()]


def count_bidders() -> int:
    with connect() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM bidders").fetchone()[0])


def create_run(bidder_ids: list[int] | None, source_keys: list[str], bidder_count: int) -> dict:
    scope = {"type": "selected" if bidder_ids else "all", "bidder_ids": bidder_ids or []}
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO research_runs(status, created_at, bidder_scope_json, source_keys_json, bidder_count, source_count, message) VALUES ('planned', ?, ?, ?, ?, ?, ?)",
            (utcnow(), json.dumps(scope), json.dumps(source_keys), bidder_count, len(source_keys), "Research framework ready; source collectors are not implemented yet."),
        )
        run_id = int(cur.lastrowid)
    add_diagnostic("INFO", "Research run created", stage="research", details={"run_id": run_id, "sources": source_keys, "bidder_count": bidder_count})
    return get_run(run_id)


def _run_row(row: sqlite3.Row) -> dict:
    item = dict(row)
    item["bidder_scope"] = json.loads(item.pop("bidder_scope_json"))
    item["source_keys"] = json.loads(item.pop("source_keys_json"))
    return item


def get_run(run_id: int) -> dict:
    with connect() as conn:
        return _run_row(conn.execute("SELECT * FROM research_runs WHERE id = ?", (run_id,)).fetchone())


def list_runs(limit: int = 50) -> list[dict]:
    with connect() as conn:
        return [_run_row(row) for row in conn.execute("SELECT * FROM research_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]


def list_review_proposals() -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT rp.*, b.contractor_name FROM review_proposals rp JOIN bidders b ON b.id = rp.bidder_id ORDER BY rp.id DESC").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["evidence"] = json.loads(item.pop("evidence_json"))
            result.append(item)
        return result


def list_diagnostics(limit: int = 500) -> list[dict]:
    with connect() as conn:
        result = []
        for row in conn.execute("SELECT * FROM diagnostics ORDER BY id DESC LIMIT ?", (limit,)).fetchall():
            item = dict(row)
            item["details"] = json.loads(item.pop("details_json"))
            result.append(item)
        return result


def clear_diagnostics() -> None:
    with connect() as conn:
        conn.execute("DELETE FROM diagnostics")
