import json
import sqlite3
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS exit_state (
    exit_id TEXT PRIMARY KEY,
    ip TEXT,
    last_rotation_at TEXT
);
CREATE TABLE IF NOT EXISTS operations (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    exit_id TEXT NOT NULL,
    status TEXT NOT NULL,
    params TEXT NOT NULL,
    result TEXT,
    error TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exit_id TEXT NOT NULL,
    at TEXT NOT NULL,
    event TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS history_exit_at ON history (exit_id, at);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    exit_id TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    closed_at TEXT,
    close_reason TEXT
);
CREATE INDEX IF NOT EXISTS sessions_exit ON sessions (exit_id, closed_at);
"""


def utcnow() -> datetime:
    return datetime.now(UTC)


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(SCHEMA)

    def _execute(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock, self._db:
            return self._db.execute(sql, params).fetchall()

    def exit_row(self, exit_id: str) -> dict[str, Any]:
        rows = self._execute(
            "SELECT ip, last_rotation_at FROM exit_state WHERE exit_id = ?", (exit_id,)
        )
        return dict(rows[0]) if rows else {"ip": None, "last_rotation_at": None}

    def save_ip(self, exit_id: str, ip: str) -> None:
        self._execute(
            "INSERT INTO exit_state (exit_id, ip) VALUES (?, ?) "
            "ON CONFLICT (exit_id) DO UPDATE SET ip = excluded.ip",
            (exit_id, ip),
        )

    def save_rotation_time(self, exit_id: str, at: datetime) -> None:
        self._execute(
            "INSERT INTO exit_state (exit_id, last_rotation_at) VALUES (?, ?) "
            "ON CONFLICT (exit_id) DO UPDATE SET last_rotation_at = excluded.last_rotation_at",
            (exit_id, at.isoformat()),
        )

    def create_operation(self, op_type: str, exit_id: str, params: dict) -> dict[str, Any]:
        op_id = uuid.uuid4().hex
        self._execute(
            "INSERT INTO operations (id, type, exit_id, status, params, started_at) "
            "VALUES (?, ?, ?, 'running', ?, ?)",
            (op_id, op_type, exit_id, json.dumps(params), utcnow().isoformat()),
        )
        return self.operation(op_id)

    def finish_operation(
        self, op_id: str, status: str, result: dict | None = None, error: str | None = None
    ) -> None:
        self._execute(
            "UPDATE operations SET status = ?, result = ?, error = ?, finished_at = ? WHERE id = ?",
            (status, json.dumps(result) if result else None, error, utcnow().isoformat(), op_id),
        )

    def operation(self, op_id: str) -> dict[str, Any] | None:
        rows = self._execute("SELECT * FROM operations WHERE id = ?", (op_id,))
        if not rows:
            return None
        row = dict(rows[0])
        return {
            "id": row["id"],
            "type": row["type"],
            "exit": row["exit_id"],
            "status": row["status"],
            "params": json.loads(row["params"]),
            "result": json.loads(row["result"]) if row["result"] else None,
            "error": row["error"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
        }

    def fail_unfinished_operations(self) -> int:
        rows = self._execute(
            "UPDATE operations SET status = 'failed', error = 'interrupted by a controller "
            "restart', finished_at = ? WHERE status = 'running' RETURNING id",
            (utcnow().isoformat(),),
        )
        return len(rows)

    def add_history(self, exit_id: str, event: str, data: dict, at: datetime | None = None) -> None:
        self._execute(
            "INSERT INTO history (exit_id, at, event, data) VALUES (?, ?, ?, ?)",
            (exit_id, (at or utcnow()).isoformat(), event, json.dumps(data)),
        )

    def history(self, exit_id: str, limit: int) -> list[dict[str, Any]]:
        rows = self._execute(
            "SELECT at, event, data FROM history WHERE exit_id = ? ORDER BY id DESC LIMIT ?",
            (exit_id, limit),
        )
        return [{"at": row["at"], "event": row["event"], **json.loads(row["data"])} for row in rows]

    def open_session(self, exit_id: str, ttl_seconds: float) -> dict[str, Any]:
        session_id = uuid.uuid4().hex
        now = utcnow()
        self._execute(
            "INSERT INTO sessions (id, exit_id, opened_at, expires_at) VALUES (?, ?, ?, ?)",
            (
                session_id,
                exit_id,
                now.isoformat(),
                (now + timedelta(seconds=ttl_seconds)).isoformat(),
            ),
        )
        return self.session(session_id)

    def session(self, session_id: str) -> dict[str, Any] | None:
        rows = self._execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
        if not rows:
            return None
        row = dict(rows[0])
        row["exit"] = row.pop("exit_id")
        return row

    def close_session(self, session_id: str) -> dict[str, Any] | None:
        self._execute(
            "UPDATE sessions SET closed_at = ?, close_reason = 'closed' "
            "WHERE id = ? AND closed_at IS NULL",
            (utcnow().isoformat(), session_id),
        )
        return self.session(session_id)

    def expire_sessions(self) -> list[str]:
        now = utcnow().isoformat()
        rows = self._execute(
            "UPDATE sessions SET closed_at = ?, close_reason = 'expired' "
            "WHERE closed_at IS NULL AND expires_at < ? RETURNING exit_id",
            (now, now),
        )
        return [row["exit_id"] for row in rows]

    def active_sessions(self, exit_id: str) -> int:
        rows = self._execute(
            "SELECT COUNT(*) FROM sessions WHERE exit_id = ? AND closed_at IS NULL", (exit_id,)
        )
        return rows[0][0]

    def closed_sessions_since(self, exit_id: str, since: str | None) -> int:
        rows = self._execute(
            "SELECT COUNT(*) FROM sessions WHERE exit_id = ? AND closed_at > ?",
            (exit_id, since or ""),
        )
        return rows[0][0]

    def purge(self, before: datetime) -> None:
        self._execute("DELETE FROM history WHERE at < ?", (before.isoformat(),))
        self._execute(
            "DELETE FROM operations WHERE status != 'running' AND started_at < ?",
            (before.isoformat(),),
        )
        self._execute("DELETE FROM sessions WHERE closed_at < ?", (before.isoformat(),))
