import json
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
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

    def purge(self, before: datetime) -> None:
        self._execute("DELETE FROM history WHERE at < ?", (before.isoformat(),))
        self._execute(
            "DELETE FROM operations WHERE status != 'running' AND started_at < ?",
            (before.isoformat(),),
        )
