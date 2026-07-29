from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class RuntimeStorage:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._lock = threading.Lock()
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS investigations (
                    investigation_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    subject TEXT NOT NULL DEFAULT 'legacy',
                    role TEXT NOT NULL,
                    query TEXT NOT NULL,
                    context_json TEXT NOT NULL,
                    answer_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS feedback (
                    feedback_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    investigation_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    rating TEXT NOT NULL,
                    comment TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(investigation_id) REFERENCES investigations(investigation_id)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    investigation_id TEXT,
                    created_at TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                """
            )
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(investigations)")}
            if "subject" not in columns:
                connection.execute("ALTER TABLE investigations ADD COLUMN subject TEXT NOT NULL DEFAULT 'legacy'")

    def save_investigation(self, record: dict[str, Any]) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO investigations
                    (investigation_id, created_at, subject, role, query, context_json, answer_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["investigation_id"], record["created_at"], record["subject"], record["role"], record["query"],
                    json.dumps(record["context"], ensure_ascii=False), json.dumps(record["answer"], ensure_ascii=False),
                ),
            )
            connection.execute(
                "INSERT INTO audit_events(investigation_id, created_at, event_type, payload_json) VALUES (?, ?, ?, ?)",
                (
                    record["investigation_id"],
                    record["created_at"],
                    "TRIAGE_CREATED",
                    json.dumps({"subject": record["subject"], "role": record["role"]}, ensure_ascii=False),
                ),
            )

    def list_investigations(self, limit: int = 12, *, subject: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as connection:
            if subject is None:
                rows = connection.execute(
                    """
                    SELECT investigation_id, created_at, subject, role, query, context_json, answer_json
                    FROM investigations ORDER BY created_at DESC LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT investigation_id, created_at, subject, role, query, context_json, answer_json
                    FROM investigations WHERE subject = ? ORDER BY created_at DESC LIMIT ?
                    """,
                    (subject, limit),
                ).fetchall()
        return [self._decode_investigation(row) for row in rows]

    def get_investigation(self, investigation_id: str, *, subject: str | None = None) -> dict[str, Any] | None:
        with self._connect() as connection:
            if subject is None:
                row = connection.execute(
                    """
                    SELECT investigation_id, created_at, subject, role, query, context_json, answer_json
                    FROM investigations WHERE investigation_id = ?
                    """,
                    (investigation_id,),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT investigation_id, created_at, subject, role, query, context_json, answer_json
                    FROM investigations WHERE investigation_id = ? AND subject = ?
                    """,
                    (investigation_id, subject),
                ).fetchone()
        return self._decode_investigation(row) if row else None

    @staticmethod
    def _decode_investigation(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "investigation_id": row["investigation_id"],
            "created_at": row["created_at"],
            "subject": row["subject"],
            "role": row["role"],
            "query": row["query"],
            "context": json.loads(row["context_json"]),
            "answer": json.loads(row["answer_json"]),
        }

    def add_feedback(self, investigation_id: str, created_at: str, rating: str, comment: str) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO feedback(investigation_id, created_at, rating, comment) VALUES (?, ?, ?, ?)",
                (investigation_id, created_at, rating, comment),
            )
            connection.execute(
                "INSERT INTO audit_events(investigation_id, created_at, event_type, payload_json) VALUES (?, ?, ?, ?)",
                (investigation_id, created_at, "ANSWER_FEEDBACK", json.dumps({"rating": rating}, ensure_ascii=False)),
            )
            return {"feedback_id": cursor.lastrowid, "investigation_id": investigation_id, "rating": rating, "comment": comment}
