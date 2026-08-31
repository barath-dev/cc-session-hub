"""SQLite persistence for cc-session-hub."""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "sessions.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    account TEXT NOT NULL,
    cwd TEXT,
    project TEXT,
    state TEXT NOT NULL,
    notification_type TEXT,
    current_tool TEXT,
    last_message TEXT,
    session_start_reason TEXT,
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    ended_at TEXT
);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(SCHEMA)
    return conn


def upsert_session(conn: sqlite3.Connection, row: dict) -> dict:
    existing = conn.execute(
        "SELECT * FROM sessions WHERE session_id = ?", (row["session_id"],)
    ).fetchone()

    if existing is None:
        merged = {
            "session_id": row["session_id"],
            "account": row.get("account", "unknown"),
            "cwd": row.get("cwd"),
            "project": row.get("project"),
            "state": row.get("state", "starting"),
            "notification_type": row.get("notification_type"),
            "current_tool": row.get("current_tool"),
            "last_message": row.get("last_message"),
            "session_start_reason": row.get("session_start_reason"),
            "started_at": row["updated_at"],
            "updated_at": row["updated_at"],
            "ended_at": row.get("ended_at"),
        }
        conn.execute(
            """INSERT INTO sessions
               (session_id, account, cwd, project, state, notification_type,
                current_tool, last_message, session_start_reason, started_at,
                updated_at, ended_at)
               VALUES (:session_id, :account, :cwd, :project, :state,
                       :notification_type, :current_tool, :last_message,
                       :session_start_reason, :started_at, :updated_at, :ended_at)""",
            merged,
        )
    else:
        merged = dict(existing)
        for key in (
            "account",
            "cwd",
            "project",
            "state",
            "notification_type",
            "current_tool",
            "last_message",
            "session_start_reason",
            "ended_at",
        ):
            # A key's mere presence in `row` means "set this field" (even to
            # None, to explicitly clear it) — absence means "leave as-is".
            if key in row:
                merged[key] = row[key]
        merged["updated_at"] = row["updated_at"]
        conn.execute(
            """UPDATE sessions SET
               account=:account, cwd=:cwd, project=:project, state=:state,
               notification_type=:notification_type, current_tool=:current_tool,
               last_message=:last_message, session_start_reason=:session_start_reason,
               updated_at=:updated_at, ended_at=:ended_at
               WHERE session_id=:session_id""",
            merged,
        )
    conn.commit()
    return merged


def list_sessions(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM sessions ORDER BY updated_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]
