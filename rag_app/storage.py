"""Owner-only SQLite persistence for investigations and governed source delivery.

The repository keeps these tables together so a local PoC has one recoverable
audit store. Methods own their transaction boundaries, never retain connections,
and store normalized JSON only after validation/reconciliation. A scaled service
would split workflow and ingestion stores behind the same method contracts.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MAX_FEEDBACK_COMMENT_CHARS = 4_000
MAX_FEEDBACK_PER_INVESTIGATION = 100
MAX_WORKFLOW_NOTES_CHARS = 4_000
VALID_CHECK_OUTCOMES = {"PASS", "FAIL", "INCONCLUSIVE", "NOT_APPLICABLE"}
VALID_REVIEW_DECISIONS = {"APPROVE", "REJECT"}
VALID_QUARANTINE_STAGES = {"MANIFEST", "SCHEMA", "RECONCILIATION", "LEDGER"}
VALID_QUARANTINE_SEVERITIES = {"WARNING", "CRITICAL"}
VALID_QUARANTINE_RESOLUTIONS = {"RETRY", "REJECT"}
DEFAULT_SOURCE_STALE_SECONDS = {
    "SEATRACK_EXPORT": 2 * 60 * 60,
    "APPROVED_DMS_EXPORT": 24 * 60 * 60,
}
INVESTIGATION_TRANSITIONS = {
    "TRIAGE": {"INVESTIGATING"},
    "INVESTIGATING": {"CHECKED"},
    "CHECKED": {"INVESTIGATING", "ROOT_CAUSE_REVIEW"},
    "ROOT_CAUSE_REVIEW": {"INVESTIGATING", "CLOSED"},
    "CLOSED": {"PUBLISHED"},
    "PUBLISHED": set(),
}


class DuplicateInvestigationError(ValueError):
    """Raised when immutable investigation creation receives a duplicate ID."""


class SourceJobLockedError(RuntimeError):
    """Raised when another non-expired controlled source job owns the lease."""


class SourceManifestReplayError(ValueError):
    """Raised when a signed manifest ID is rebound to different delivery metadata."""


class RuntimeStorage:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._prepare_storage_path()
        self._initialize()

    def _prepare_storage_path(self) -> None:
        self.db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.db_path.parent.is_symlink():
            raise ValueError("runtime storage directory must not be a symlink")
        os.chmod(self.db_path.parent, 0o700)
        if self.db_path.exists():
            if self.db_path.is_symlink() or not self.db_path.is_file():
                raise ValueError("runtime database must be a regular file")
        else:
            descriptor = os.open(self.db_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            os.close(descriptor)
        os.chmod(self.db_path, 0o600)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # Each request gets a short-lived connection. WAL plus a bounded busy
        # timeout makes concurrent readers predictable without hiding lock stalls.
        connection = sqlite3.connect(self.db_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
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
            # Idempotent DDL doubles as the PoC migration layer. WAL improves the
            # read-heavy HTTP path while source jobs serialize their own writes.
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS investigations (
                    investigation_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'TRIAGE',
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
                CREATE TABLE IF NOT EXISTS check_results (
                    check_id TEXT PRIMARY KEY,
                    investigation_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    actor_subject TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    step_sequence INTEGER NOT NULL,
                    outcome TEXT NOT NULL,
                    notes TEXT NOT NULL DEFAULT '',
                    evidence_json TEXT NOT NULL DEFAULT '[]',
                    FOREIGN KEY(investigation_id) REFERENCES investigations(investigation_id)
                );
                CREATE TABLE IF NOT EXISTS investigation_reviews (
                    review_id TEXT PRIMARY KEY,
                    investigation_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    reviewer_subject TEXT NOT NULL,
                    reviewer_role TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    notes TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(investigation_id) REFERENCES investigations(investigation_id)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    investigation_id TEXT,
                    created_at TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS source_sync_runs (
                    run_id TEXT PRIMARY KEY,
                    source_system TEXT NOT NULL,
                    previous_cursor TEXT,
                    cursor TEXT NOT NULL,
                    exported_at TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    rolled_back_at TEXT,
                    status TEXT NOT NULL,
                    total_count INTEGER NOT NULL,
                    inserted_count INTEGER NOT NULL DEFAULT 0,
                    updated_count INTEGER NOT NULL DEFAULT 0,
                    unchanged_count INTEGER NOT NULL DEFAULT 0,
                    withdrawn_count INTEGER NOT NULL DEFAULT 0,
                    rejected_count INTEGER NOT NULL DEFAULT 0,
                    errors_json TEXT NOT NULL DEFAULT '[]'
                );
                CREATE TABLE IF NOT EXISTS source_records (
                    source_system TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    source_record_id TEXT NOT NULL,
                    source_updated_at TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    normalized_json TEXT,
                    status TEXT NOT NULL,
                    ingested_at TEXT NOT NULL,
                    sync_run_id TEXT NOT NULL,
                    PRIMARY KEY(source_system, entity_type, source_record_id),
                    FOREIGN KEY(sync_run_id) REFERENCES source_sync_runs(run_id)
                );
                CREATE TABLE IF NOT EXISTS source_record_history (
                    history_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sync_run_id TEXT NOT NULL,
                    source_system TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    source_record_id TEXT NOT NULL,
                    previous_exists INTEGER NOT NULL,
                    previous_source_updated_at TEXT,
                    previous_content_hash TEXT,
                    previous_normalized_json TEXT,
                    previous_status TEXT,
                    previous_ingested_at TEXT,
                    previous_sync_run_id TEXT,
                    FOREIGN KEY(sync_run_id) REFERENCES source_sync_runs(run_id)
                );
                CREATE TABLE IF NOT EXISTS source_cursors (
                    source_system TEXT PRIMARY KEY,
                    cursor TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    sync_run_id TEXT NOT NULL,
                    FOREIGN KEY(sync_run_id) REFERENCES source_sync_runs(run_id)
                );
                CREATE TABLE IF NOT EXISTS source_job_locks (
                    lock_name TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    acquired_at TEXT NOT NULL,
                    acquired_at_epoch INTEGER NOT NULL,
                    expires_at_epoch INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS source_quarantine_events (
                    quarantine_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    source_system TEXT,
                    manifest_id TEXT,
                    artifact_filename TEXT,
                    artifact_sha256 TEXT,
                    stage TEXT NOT NULL,
                    reason_code TEXT NOT NULL,
                    reason_summary TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'OPEN',
                    rejected_count INTEGER NOT NULL DEFAULT 0,
                    resolved_at TEXT,
                    resolved_by TEXT,
                    resolved_by_role TEXT,
                    resolution TEXT,
                    resolution_notes TEXT
                );
                CREATE TABLE IF NOT EXISTS source_verified_manifests (
                    manifest_id TEXT PRIMARY KEY,
                    source_system TEXT NOT NULL,
                    artifact_sha256 TEXT NOT NULL,
                    signing_key_id TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    seen_count INTEGER NOT NULL DEFAULT 1,
                    last_job_id TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_feedback_investigation
                    ON feedback(investigation_id);
                CREATE INDEX IF NOT EXISTS idx_checks_investigation
                    ON check_results(investigation_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_reviews_investigation
                    ON investigation_reviews(investigation_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_source_records_active
                    ON source_records(entity_type, status);
                CREATE INDEX IF NOT EXISTS idx_source_history_run
                    ON source_record_history(sync_run_id, history_id);
                CREATE INDEX IF NOT EXISTS idx_source_runs_source
                    ON source_sync_runs(source_system, started_at);
                CREATE INDEX IF NOT EXISTS idx_source_quarantine_status
                    ON source_quarantine_events(status, created_at);
                """
            )
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(investigations)")}
            if "subject" not in columns:
                connection.execute("ALTER TABLE investigations ADD COLUMN subject TEXT NOT NULL DEFAULT 'legacy'")
            if "status" not in columns:
                connection.execute("ALTER TABLE investigations ADD COLUMN status TEXT NOT NULL DEFAULT 'TRIAGE'")
            if "updated_at" not in columns:
                connection.execute("ALTER TABLE investigations ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''")
                connection.execute("UPDATE investigations SET updated_at = created_at WHERE updated_at = ''")

    # Investigation workflow and audit trail ---------------------------------

    @staticmethod
    def _write_audit(
        connection: sqlite3.Connection,
        investigation_id: str,
        created_at: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        connection.execute(
            "INSERT INTO audit_events(investigation_id, created_at, event_type, payload_json) VALUES (?, ?, ?, ?)",
            (investigation_id, created_at, event_type, json.dumps(payload, ensure_ascii=False)),
        )

    def save_investigation(self, record: dict[str, Any]) -> None:
        status = str(record.get("status") or record.get("answer", {}).get("status") or "TRIAGE")
        if status not in INVESTIGATION_TRANSITIONS:
            raise ValueError("invalid investigation status")
        with self._lock, self._connect() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO investigations
                        (investigation_id, created_at, updated_at, status, subject, role, query, context_json, answer_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record["investigation_id"],
                        record["created_at"],
                        record["created_at"],
                        status,
                        record["subject"],
                        record["role"],
                        record["query"],
                        json.dumps(record["context"], ensure_ascii=False),
                        json.dumps(record["answer"], ensure_ascii=False),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise DuplicateInvestigationError("investigation already exists") from exc
            self._write_audit(
                connection,
                record["investigation_id"],
                record["created_at"],
                "TRIAGE_CREATED",
                {"subject": record["subject"], "role": record["role"], "status": status},
            )

    def list_investigations(self, limit: int = 12, *, subject: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as connection:
            if subject is None:
                rows = connection.execute(
                    """
                    SELECT investigation_id, created_at, updated_at, status, subject, role, query, context_json, answer_json
                    FROM investigations ORDER BY created_at DESC LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT investigation_id, created_at, updated_at, status, subject, role, query, context_json, answer_json
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
                    SELECT investigation_id, created_at, updated_at, status, subject, role, query, context_json, answer_json
                    FROM investigations WHERE investigation_id = ?
                    """,
                    (investigation_id,),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT investigation_id, created_at, updated_at, status, subject, role, query, context_json, answer_json
                    FROM investigations WHERE investigation_id = ? AND subject = ?
                    """,
                    (investigation_id, subject),
                ).fetchone()
            if not row:
                return None
            item = self._decode_investigation(row)
            checks = connection.execute(
                """
                SELECT check_id, investigation_id, created_at, actor_subject, actor_role,
                       step_sequence, outcome, notes, evidence_json
                FROM check_results WHERE investigation_id = ? ORDER BY created_at, check_id
                """,
                (investigation_id,),
            ).fetchall()
            reviews = connection.execute(
                """
                SELECT review_id, investigation_id, created_at, reviewer_subject, reviewer_role, decision, notes
                FROM investigation_reviews WHERE investigation_id = ? ORDER BY created_at, review_id
                """,
                (investigation_id,),
            ).fetchall()
        item["check_results"] = [self._decode_check(row) for row in checks]
        item["reviews"] = [dict(row) for row in reviews]
        return item

    @staticmethod
    def _decode_investigation(row: sqlite3.Row) -> dict[str, Any]:
        answer = json.loads(row["answer_json"])
        answer["status"] = row["status"]
        return {
            "investigation_id": row["investigation_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "status": row["status"],
            "subject": row["subject"],
            "role": row["role"],
            "query": row["query"],
            "context": json.loads(row["context_json"]),
            "answer": answer,
        }

    @staticmethod
    def _decode_check(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["evidence_ids"] = json.loads(item.pop("evidence_json"))
        return item

    def transition_investigation(
        self,
        investigation_id: str,
        target_status: str,
        created_at: str,
        *,
        actor_subject: str,
        actor_role: str,
    ) -> dict[str, Any]:
        target_status = target_status.upper()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM investigations WHERE investigation_id = ?",
                (investigation_id,),
            ).fetchone()
            if not row:
                raise ValueError("investigation not found")
            current_status = row["status"]
            if target_status not in INVESTIGATION_TRANSITIONS.get(current_status, set()):
                raise ValueError(
                    f"invalid investigation status transition: {current_status} -> {target_status}"
                )
            if current_status == "INVESTIGATING" and target_status == "CHECKED":
                check_count = connection.execute(
                    "SELECT COUNT(*) AS count FROM check_results WHERE investigation_id = ?",
                    (investigation_id,),
                ).fetchone()["count"]
                if check_count == 0:
                    raise ValueError("at least one check result is required before CHECKED status")
            connection.execute(
                "UPDATE investigations SET status = ?, updated_at = ? WHERE investigation_id = ?",
                (target_status, created_at, investigation_id),
            )
            self._write_audit(
                connection,
                investigation_id,
                created_at,
                "INVESTIGATION_STATUS_CHANGED",
                {
                    "from": current_status,
                    "to": target_status,
                    "actor_subject": actor_subject,
                    "actor_role": actor_role,
                },
            )
        return {"investigation_id": investigation_id, "status": target_status, "updated_at": created_at}

    def add_check_result(
        self,
        investigation_id: str,
        created_at: str,
        *,
        actor_subject: str,
        actor_role: str,
        step_sequence: int,
        outcome: str,
        notes: str,
        evidence_ids: list[str],
    ) -> dict[str, Any]:
        outcome = outcome.upper()
        if outcome not in VALID_CHECK_OUTCOMES:
            raise ValueError("invalid check outcome")
        if not isinstance(step_sequence, int) or not 1 <= step_sequence <= 100:
            raise ValueError("step_sequence must be between 1 and 100")
        if not isinstance(notes, str) or len(notes) > MAX_WORKFLOW_NOTES_CHARS:
            raise ValueError("check notes too long")
        if (
            not isinstance(evidence_ids, list)
            or len(evidence_ids) > 32
            or any(not isinstance(item, str) or not item or len(item) > 128 for item in evidence_ids)
        ):
            raise ValueError("invalid evidence_ids")
        check_id = f"CHK-{uuid.uuid4().hex.upper()}"
        with self._lock, self._connect() as connection:
            investigation = connection.execute(
                "SELECT status FROM investigations WHERE investigation_id = ?",
                (investigation_id,),
            ).fetchone()
            if not investigation:
                raise ValueError("investigation not found")
            if investigation["status"] != "INVESTIGATING":
                raise ValueError("check results require INVESTIGATING status")
            connection.execute(
                """
                INSERT INTO check_results
                    (check_id, investigation_id, created_at, actor_subject, actor_role,
                     step_sequence, outcome, notes, evidence_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    check_id,
                    investigation_id,
                    created_at,
                    actor_subject,
                    actor_role,
                    step_sequence,
                    outcome,
                    notes,
                    json.dumps(evidence_ids, ensure_ascii=False),
                ),
            )
            self._write_audit(
                connection,
                investigation_id,
                created_at,
                "CHECK_RESULT_RECORDED",
                {
                    "check_id": check_id,
                    "step_sequence": step_sequence,
                    "outcome": outcome,
                    "actor_subject": actor_subject,
                    "actor_role": actor_role,
                },
            )
        return {
            "check_id": check_id,
            "investigation_id": investigation_id,
            "created_at": created_at,
            "actor_subject": actor_subject,
            "actor_role": actor_role,
            "step_sequence": step_sequence,
            "outcome": outcome,
            "notes": notes,
            "evidence_ids": evidence_ids,
        }

    def add_review(
        self,
        investigation_id: str,
        created_at: str,
        *,
        reviewer_subject: str,
        reviewer_role: str,
        decision: str,
        notes: str,
    ) -> dict[str, Any]:
        decision = decision.upper()
        if reviewer_role not in {"QUALITY_ENGINEER", "ADMIN"}:
            raise PermissionError("quality review role required")
        if decision not in VALID_REVIEW_DECISIONS:
            raise ValueError("invalid review decision")
        if not isinstance(notes, str) or len(notes) > MAX_WORKFLOW_NOTES_CHARS:
            raise ValueError("review notes too long")
        review_id = f"REV-{uuid.uuid4().hex.upper()}"
        target_status = "CLOSED" if decision == "APPROVE" else "INVESTIGATING"
        with self._lock, self._connect() as connection:
            investigation = connection.execute(
                "SELECT status FROM investigations WHERE investigation_id = ?",
                (investigation_id,),
            ).fetchone()
            if not investigation:
                raise ValueError("investigation not found")
            if investigation["status"] != "ROOT_CAUSE_REVIEW":
                raise ValueError("review requires ROOT_CAUSE_REVIEW status")
            connection.execute(
                """
                INSERT INTO investigation_reviews
                    (review_id, investigation_id, created_at, reviewer_subject, reviewer_role, decision, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    review_id,
                    investigation_id,
                    created_at,
                    reviewer_subject,
                    reviewer_role,
                    decision,
                    notes,
                ),
            )
            connection.execute(
                "UPDATE investigations SET status = ?, updated_at = ? WHERE investigation_id = ?",
                (target_status, created_at, investigation_id),
            )
            self._write_audit(
                connection,
                investigation_id,
                created_at,
                "INVESTIGATION_REVIEWED",
                {
                    "review_id": review_id,
                    "decision": decision,
                    "reviewer_subject": reviewer_subject,
                    "reviewer_role": reviewer_role,
                    "resulting_status": target_status,
                },
            )
        return {
            "review_id": review_id,
            "investigation_id": investigation_id,
            "created_at": created_at,
            "reviewer_subject": reviewer_subject,
            "reviewer_role": reviewer_role,
            "decision": decision,
            "notes": notes,
            "resulting_status": target_status,
        }

    def add_feedback(self, investigation_id: str, created_at: str, rating: str, comment: str) -> dict[str, Any]:
        if not isinstance(comment, str):
            raise ValueError("feedback comment must be a string")
        if len(comment) > MAX_FEEDBACK_COMMENT_CHARS:
            raise ValueError("feedback comment too long")
        with self._lock, self._connect() as connection:
            count = connection.execute(
                "SELECT COUNT(*) AS count FROM feedback WHERE investigation_id = ?",
                (investigation_id,),
            ).fetchone()["count"]
            if count >= MAX_FEEDBACK_PER_INVESTIGATION:
                raise ValueError("feedback limit reached for investigation")
            cursor = connection.execute(
                "INSERT INTO feedback(investigation_id, created_at, rating, comment) VALUES (?, ?, ?, ?)",
                (investigation_id, created_at, rating, comment),
            )
            self._write_audit(
                connection,
                investigation_id,
                created_at,
                "ANSWER_FEEDBACK",
                {"rating": rating, "comment_length": len(comment)},
            )
            return {
                "feedback_id": cursor.lastrowid,
                "investigation_id": investigation_id,
                "rating": rating,
                "comment": comment,
            }

    # Governed source-delivery operations ------------------------------------

    def acquire_source_job_lock(
        self,
        lock_name: str,
        owner_id: str,
        *,
        acquired_at: str,
        now_epoch: int,
        ttl_seconds: int,
    ) -> dict[str, Any]:
        if not isinstance(lock_name, str) or not lock_name or len(lock_name) > 128:
            raise ValueError("invalid source job lock_name")
        if not isinstance(owner_id, str) or not owner_id or len(owner_id) > 128:
            raise ValueError("invalid source job owner_id")
        if not isinstance(now_epoch, int) or now_epoch < 0:
            raise ValueError("invalid source job lock time")
        if not isinstance(ttl_seconds, int) or not 60 <= ttl_seconds <= 3_600:
            raise ValueError("source job lock TTL must be between 60 and 3600 seconds")
        expires_at_epoch = now_epoch + ttl_seconds
        with self._lock, self._connect() as connection:
            connection.execute(
                "DELETE FROM source_job_locks WHERE lock_name = ? AND expires_at_epoch <= ?",
                (lock_name, now_epoch),
            )
            try:
                connection.execute(
                    """
                    INSERT INTO source_job_locks(
                        lock_name, owner_id, acquired_at, acquired_at_epoch, expires_at_epoch
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (lock_name, owner_id, acquired_at, now_epoch, expires_at_epoch),
                )
            except sqlite3.IntegrityError as exc:
                raise SourceJobLockedError("another source synchronization job is already running") from exc
        return {
            "lock_name": lock_name,
            "owner_id": owner_id,
            "acquired_at": acquired_at,
            "expires_at_epoch": expires_at_epoch,
        }

    def release_source_job_lock(self, lock_name: str, owner_id: str) -> bool:
        with self._lock, self._connect() as connection:
            result = connection.execute(
                "DELETE FROM source_job_locks WHERE lock_name = ? AND owner_id = ?",
                (lock_name, owner_id),
            )
            return result.rowcount == 1

    def create_source_quarantine(
        self,
        *,
        job_id: str,
        created_at: str,
        source_system: str | None,
        manifest_id: str | None,
        artifact_filename: str | None,
        artifact_sha256: str | None,
        stage: str,
        reason_code: str,
        reason_summary: str,
        severity: str,
        rejected_count: int = 0,
    ) -> dict[str, Any]:
        for value, label in ((job_id, "job_id"), (reason_code, "reason_code")):
            if not isinstance(value, str) or not value or len(value) > 128 or "\x00" in value:
                raise ValueError(f"invalid quarantine {label}")
        for value, label, maximum in (
            (source_system, "source_system", 128),
            (manifest_id, "manifest_id", 128),
            (artifact_filename, "artifact_filename", 256),
        ):
            if value is not None and (
                not isinstance(value, str) or not value or len(value) > maximum or "\x00" in value
            ):
                raise ValueError(f"invalid quarantine {label}")
        if artifact_sha256 is not None and (
            not isinstance(artifact_sha256, str)
            or len(artifact_sha256) != 64
            or any(character not in "0123456789abcdef" for character in artifact_sha256)
        ):
            raise ValueError("invalid quarantine artifact_sha256")
        if stage not in VALID_QUARANTINE_STAGES:
            raise ValueError("invalid quarantine stage")
        if severity not in VALID_QUARANTINE_SEVERITIES:
            raise ValueError("invalid quarantine severity")
        if not isinstance(reason_summary, str) or not reason_summary or len(reason_summary) > 500:
            raise ValueError("invalid quarantine reason_summary")
        if "\x00" in reason_summary or "\n" in reason_summary or "\r" in reason_summary:
            raise ValueError("invalid quarantine reason_summary")
        if isinstance(rejected_count, bool) or not isinstance(rejected_count, int) or not 0 <= rejected_count <= 5_000:
            raise ValueError("invalid quarantine rejected_count")
        quarantine_id = f"QUAR-{uuid.uuid4().hex.upper()}"
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO source_quarantine_events(
                    quarantine_id, job_id, created_at, source_system, manifest_id,
                    artifact_filename, artifact_sha256, stage, reason_code,
                    reason_summary, severity, status, rejected_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?)
                """,
                (
                    quarantine_id,
                    job_id,
                    created_at,
                    source_system,
                    manifest_id,
                    artifact_filename,
                    artifact_sha256,
                    stage,
                    reason_code,
                    reason_summary,
                    severity,
                    rejected_count,
                ),
            )
        return {
            "quarantine_id": quarantine_id,
            "job_id": job_id,
            "created_at": created_at,
            "source_system": source_system,
            "manifest_id": manifest_id,
            "artifact_filename": artifact_filename,
            "artifact_sha256": artifact_sha256,
            "stage": stage,
            "reason_code": reason_code,
            "reason_summary": reason_summary,
            "severity": severity,
            "status": "OPEN",
            "rejected_count": rejected_count,
        }

    def record_verified_source_manifest(
        self,
        *,
        manifest_id: str,
        source_system: str,
        artifact_sha256: str,
        signing_key_id: str,
        observed_at: str,
        job_id: str,
    ) -> dict[str, Any]:
        for value, label in (
            (manifest_id, "manifest_id"),
            (source_system, "source_system"),
            (signing_key_id, "signing_key_id"),
            (job_id, "job_id"),
        ):
            if not isinstance(value, str) or not value or len(value) > 128 or "\x00" in value:
                raise ValueError(f"invalid verified manifest {label}")
        if (
            not isinstance(artifact_sha256, str)
            or len(artifact_sha256) != 64
            or any(character not in "0123456789abcdef" for character in artifact_sha256)
        ):
            raise ValueError("invalid verified manifest artifact_sha256")
        with self._lock, self._connect() as connection:
            current = connection.execute(
                """
                SELECT source_system, artifact_sha256, signing_key_id, seen_count
                FROM source_verified_manifests WHERE manifest_id = ?
                """,
                (manifest_id,),
            ).fetchone()
            if current:
                expected = (source_system, artifact_sha256, signing_key_id)
                observed = (
                    current["source_system"],
                    current["artifact_sha256"],
                    current["signing_key_id"],
                )
                if observed != expected:
                    raise SourceManifestReplayError(
                        "verified manifest_id conflicts with an earlier signed delivery"
                    )
                seen_count = current["seen_count"] + 1
                connection.execute(
                    """
                    UPDATE source_verified_manifests SET
                        last_seen_at = ?, seen_count = ?, last_job_id = ?
                    WHERE manifest_id = ?
                    """,
                    (observed_at, seen_count, job_id, manifest_id),
                )
                return {
                    "manifest_id": manifest_id,
                    "replay": True,
                    "seen_count": seen_count,
                }
            connection.execute(
                """
                INSERT INTO source_verified_manifests(
                    manifest_id, source_system, artifact_sha256, signing_key_id,
                    first_seen_at, last_seen_at, seen_count, last_job_id
                ) VALUES (?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    manifest_id,
                    source_system,
                    artifact_sha256,
                    signing_key_id,
                    observed_at,
                    observed_at,
                    job_id,
                ),
            )
        return {"manifest_id": manifest_id, "replay": False, "seen_count": 1}

    def list_source_quarantine(
        self,
        *,
        status: str | None = "OPEN",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        allowed_statuses = {"OPEN", "RESOLVED_RETRY", "RESOLVED_REJECTED"}
        if status is not None and status not in allowed_statuses:
            raise ValueError("invalid quarantine status")
        if not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("quarantine limit must be between 1 and 100")
        query = """
            SELECT quarantine_id, job_id, created_at, source_system, manifest_id,
                   artifact_filename, artifact_sha256, stage, reason_code,
                   reason_summary, severity, status, rejected_count, resolved_at,
                   resolved_by, resolved_by_role, resolution, resolution_notes
            FROM source_quarantine_events
        """
        parameters: tuple[Any, ...]
        if status is None:
            query += " ORDER BY rowid DESC LIMIT ?"
            parameters = (limit,)
        else:
            query += " WHERE status = ? ORDER BY rowid DESC LIMIT ?"
            parameters = (status, limit)
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [dict(row) for row in rows]

    def resolve_source_quarantine(
        self,
        quarantine_id: str,
        *,
        resolved_at: str,
        resolved_by: str,
        resolved_by_role: str,
        resolution: str,
        notes: str,
    ) -> dict[str, Any]:
        if not isinstance(quarantine_id, str) or not quarantine_id or len(quarantine_id) > 128:
            raise ValueError("invalid quarantine_id")
        if not isinstance(resolved_by, str) or not resolved_by or len(resolved_by) > 256:
            raise ValueError("invalid quarantine resolver")
        if not isinstance(resolved_by_role, str) or not resolved_by_role or len(resolved_by_role) > 64:
            raise ValueError("invalid quarantine resolver role")
        if resolution not in VALID_QUARANTINE_RESOLUTIONS:
            raise ValueError("quarantine resolution must be RETRY or REJECT")
        if not isinstance(notes, str) or not notes.strip() or len(notes) > 1_000 or "\x00" in notes:
            raise ValueError("quarantine resolution notes must contain 1 to 1000 characters")
        target_status = "RESOLVED_RETRY" if resolution == "RETRY" else "RESOLVED_REJECTED"
        with self._lock, self._connect() as connection:
            current = connection.execute(
                "SELECT status FROM source_quarantine_events WHERE quarantine_id = ?",
                (quarantine_id,),
            ).fetchone()
            if not current:
                raise ValueError("quarantine event not found")
            if current["status"] != "OPEN":
                raise ValueError("quarantine event is already resolved")
            connection.execute(
                """
                UPDATE source_quarantine_events SET
                    status = ?, resolved_at = ?, resolved_by = ?, resolved_by_role = ?,
                    resolution = ?, resolution_notes = ?
                WHERE quarantine_id = ?
                """,
                (
                    target_status,
                    resolved_at,
                    resolved_by,
                    resolved_by_role,
                    resolution,
                    notes.strip(),
                    quarantine_id,
                ),
            )
        return {
            "quarantine_id": quarantine_id,
            "status": target_status,
            "resolved_at": resolved_at,
            "resolved_by": resolved_by,
            "resolved_by_role": resolved_by_role,
            "resolution": resolution,
            "resolution_notes": notes.strip(),
        }

    @staticmethod
    def _source_time(value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("source ledger timestamp is missing a timezone")
        return parsed

    def source_health(
        self,
        *,
        now: datetime | None = None,
        stale_after_seconds: Mapping[str, int] | None = None,
        recent_run_limit: int = 20,
    ) -> dict[str, Any]:
        """Return redacted operational health for governed source ingestion."""

        observed_at = now or datetime.now(timezone.utc)
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("source health time must include a timezone")
        if not isinstance(recent_run_limit, int) or not 1 <= recent_run_limit <= 100:
            raise ValueError("recent_run_limit must be between 1 and 100")
        thresholds = dict(DEFAULT_SOURCE_STALE_SECONDS)
        if stale_after_seconds is not None:
            if set(stale_after_seconds) != set(DEFAULT_SOURCE_STALE_SECONDS):
                raise ValueError("source stale thresholds must configure every approved source")
            for source_system, seconds in stale_after_seconds.items():
                if not isinstance(seconds, int) or not 60 <= seconds <= 31 * 24 * 60 * 60:
                    raise ValueError(f"invalid stale threshold for {source_system}")
            thresholds = dict(stale_after_seconds)

        alerts: list[dict[str, Any]] = []
        sources: list[dict[str, Any]] = []
        now_epoch = int(observed_at.timestamp())
        with self._connect() as connection:
            lock = connection.execute(
                """
                SELECT lock_name, acquired_at, expires_at_epoch
                FROM source_job_locks WHERE lock_name = 'source-sync'
                """
            ).fetchone()
            quarantine_rows = connection.execute(
                """
                SELECT source_system, severity, COUNT(*) AS count
                FROM source_quarantine_events WHERE status = 'OPEN'
                GROUP BY source_system, severity
                """
            ).fetchall()
            for source_system in sorted(thresholds):
                latest = connection.execute(
                    """
                    SELECT run_id, status, completed_at, total_count, rejected_count
                    FROM source_sync_runs WHERE source_system = ?
                    ORDER BY rowid DESC LIMIT 1
                    """,
                    (source_system,),
                ).fetchone()
                cursor = connection.execute(
                    """
                    SELECT c.cursor, r.completed_at
                    FROM source_cursors AS c
                    JOIN source_sync_runs AS r ON r.run_id = c.sync_run_id
                    WHERE c.source_system = ?
                    """,
                    (source_system,),
                ).fetchone()
                recent = connection.execute(
                    """
                    SELECT total_count, rejected_count FROM source_sync_runs
                    WHERE source_system = ? AND status IN ('COMPLETED', 'PARTIAL')
                    ORDER BY rowid DESC LIMIT ?
                    """,
                    (source_system, recent_run_limit),
                ).fetchall()
                records = connection.execute(
                    """
                    SELECT entity_type, normalized_json FROM source_records
                    WHERE source_system = ? AND status = 'ACTIVE'
                    """,
                    (source_system,),
                ).fetchall()

                total_records = sum(row["total_count"] for row in recent)
                rejected_records = sum(row["rejected_count"] for row in recent)
                rejection_rate = rejected_records / total_records if total_records else 0.0
                questionable_count = 0
                for record in records:
                    if record["entity_type"] != "TEST_OBSERVATION" or not record["normalized_json"]:
                        continue
                    data = json.loads(record["normalized_json"])
                    questionable_count += data.get("quality_status") == "QUESTIONABLE"

                freshness_seconds: int | None = None
                last_successful_at: str | None = None
                if cursor:
                    last_successful_at = cursor["completed_at"]
                    freshness_seconds = max(
                        0,
                        int((observed_at - self._source_time(last_successful_at)).total_seconds()),
                    )
                    if freshness_seconds > thresholds[source_system]:
                        alerts.append(
                            {
                                "code": "STALE_SOURCE",
                                "severity": "CRITICAL",
                                "source_system": source_system,
                                "message": "last successful source synchronization exceeded its freshness threshold",
                            }
                        )
                else:
                    alerts.append(
                        {
                            "code": "NEVER_SYNCED",
                            "severity": "CRITICAL",
                            "source_system": source_system,
                            "message": "source has no successful synchronization cursor",
                        }
                    )
                if latest and latest["status"] == "PARTIAL":
                    alerts.append(
                        {
                            "code": "LAST_RUN_PARTIAL",
                            "severity": "WARNING",
                            "source_system": source_system,
                            "message": "the latest source run rejected one or more records",
                        }
                    )
                if rejected_records:
                    alerts.append(
                        {
                            "code": "HIGH_REJECTION_RATE" if total_records >= 10 and rejection_rate > 0.05 else "REJECTIONS_DETECTED",
                            "severity": "CRITICAL" if total_records >= 10 and rejection_rate > 0.05 else "WARNING",
                            "source_system": source_system,
                            "message": "recent source runs contain rejected records",
                        }
                    )
                if questionable_count:
                    alerts.append(
                        {
                            "code": "QUESTIONABLE_RECORDS",
                            "severity": "WARNING",
                            "source_system": source_system,
                            "message": "active source observations include QUESTIONABLE quality status",
                        }
                    )
                sources.append(
                    {
                        "source_system": source_system,
                        "stale_after_seconds": thresholds[source_system],
                        "cursor": cursor["cursor"] if cursor else None,
                        "last_successful_at": last_successful_at,
                        "freshness_seconds": freshness_seconds,
                        "latest_run": dict(latest) if latest else None,
                        "active_record_count": len(records),
                        "questionable_record_count": questionable_count,
                        "recent_run_count": len(recent),
                        "recent_total_count": total_records,
                        "recent_rejected_count": rejected_records,
                        "recent_rejection_rate": round(rejection_rate, 6),
                    }
                )

        open_quarantine_count = sum(row["count"] for row in quarantine_rows)
        critical_quarantine_count = sum(
            row["count"] for row in quarantine_rows if row["severity"] == "CRITICAL"
        )
        for row in quarantine_rows:
            alerts.append(
                {
                    "code": "OPEN_QUARANTINE",
                    "severity": row["severity"],
                    "source_system": row["source_system"],
                    "message": "source delivery failures require explicit operator disposition",
                    "count": row["count"],
                }
            )

        severities = {alert["severity"] for alert in alerts}
        status = "CRITICAL" if "CRITICAL" in severities else "DEGRADED" if "WARNING" in severities else "HEALTHY"
        return {
            "status": status,
            "observed_at": observed_at.isoformat(timespec="seconds"),
            "sources": sources,
            "alerts": alerts,
            "job_lock": {
                "held": bool(lock and lock["expires_at_epoch"] > now_epoch),
                "acquired_at": lock["acquired_at"] if lock and lock["expires_at_epoch"] > now_epoch else None,
                "expires_at_epoch": lock["expires_at_epoch"] if lock and lock["expires_at_epoch"] > now_epoch else None,
            },
            "quarantine": {
                "open_count": open_quarantine_count,
                "critical_open_count": critical_quarantine_count,
            },
        }

    @staticmethod
    def _record_source_history(
        connection: sqlite3.Connection,
        run_id: str,
        source_system: str,
        entity_type: str,
        source_record_id: str,
        current: sqlite3.Row | None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO source_record_history (
                sync_run_id, source_system, entity_type, source_record_id, previous_exists,
                previous_source_updated_at, previous_content_hash, previous_normalized_json,
                previous_status, previous_ingested_at, previous_sync_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                source_system,
                entity_type,
                source_record_id,
                1 if current else 0,
                current["source_updated_at"] if current else None,
                current["content_hash"] if current else None,
                current["normalized_json"] if current else None,
                current["status"] if current else None,
                current["ingested_at"] if current else None,
                current["sync_run_id"] if current else None,
            ),
        )

    # Versioned source ledger -------------------------------------------------

    @staticmethod
    def _observation_window(data: dict[str, Any] | None) -> tuple[str, str, str, str, str] | None:
        """Return the natural key that prevents duplicate observation windows."""

        if data is None:
            return None
        return (
            data["window_start"],
            data["window_end"],
            data["product_id"],
            data["line_id"],
            data["station_id"],
        )

    @classmethod
    def _active_observation_windows(
        cls,
        connection: sqlite3.Connection,
    ) -> dict[tuple[str, str, str, str, str], str]:
        rows = connection.execute(
            """
            SELECT source_record_id, normalized_json FROM source_records
            WHERE entity_type = 'TEST_OBSERVATION' AND status = 'ACTIVE'
            """
        ).fetchall()
        return {
            cls._observation_window(json.loads(row["normalized_json"])): row["source_record_id"]
            for row in rows
        }

    @staticmethod
    def _source_record_disposition(
        current: sqlite3.Row | None,
        record: dict[str, Any],
        target_status: str,
    ) -> tuple[str, str | None]:
        """Decide whether an incoming version is applicable without mutating state."""

        if current is None:
            return "APPLY", None
        current_time = datetime.fromisoformat(current["source_updated_at"])
        incoming_time = datetime.fromisoformat(record["source_updated_at"])
        if incoming_time < current_time:
            return "REJECT", "source_updated_at is older than the current source record"
        if incoming_time > current_time:
            return "APPLY", None
        if current["content_hash"] == record["content_hash"] and current["status"] == target_status:
            return "UNCHANGED", None
        return "REJECT", "same source_updated_at has conflicting content"

    @staticmethod
    def _upsert_source_record(
        connection: sqlite3.Connection,
        *,
        source_system: str,
        record: dict[str, Any],
        target_status: str,
        created_at: str,
        run_id: str,
    ) -> None:
        normalized_json = (
            json.dumps(record["data"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if record["data"] is not None
            else None
        )
        connection.execute(
            """
            INSERT INTO source_records (
                source_system, entity_type, source_record_id, source_updated_at,
                content_hash, normalized_json, status, ingested_at, sync_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_system, entity_type, source_record_id) DO UPDATE SET
                source_updated_at = excluded.source_updated_at,
                content_hash = excluded.content_hash,
                normalized_json = excluded.normalized_json,
                status = excluded.status,
                ingested_at = excluded.ingested_at,
                sync_run_id = excluded.sync_run_id
            """,
            (
                source_system,
                record["entity_type"],
                record["source_record_id"],
                record["source_updated_at"],
                record["content_hash"],
                normalized_json,
                target_status,
                created_at,
                run_id,
            ),
        )

    def sync_source_records(self, export: dict[str, Any], *, created_at: str) -> dict[str, Any]:
        """Apply one validated export atomically and retain reversible source history."""

        source_system = export["source_system"]
        run_id = f"SYNC-{uuid.uuid4().hex.upper()}"
        records = list(export["records"])
        rejected = list(export.get("rejected", []))
        counts = {"inserted": 0, "updated": 0, "unchanged": 0, "withdrawn": 0}
        with self._lock, self._connect() as connection:
            cursor_row = connection.execute(
                "SELECT cursor FROM source_cursors WHERE source_system = ?",
                (source_system,),
            ).fetchone()
            previous_cursor = cursor_row["cursor"] if cursor_row else None
            connection.execute(
                """
                INSERT INTO source_sync_runs (
                    run_id, source_system, previous_cursor, cursor, exported_at, started_at,
                    completed_at, status, total_count, errors_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'RUNNING', ?, '[]')
                """,
                (
                    run_id,
                    source_system,
                    previous_cursor,
                    export["cursor"],
                    export["exported_at"],
                    created_at,
                    created_at,
                    export["total_count"],
                ),
            )
            observation_windows = self._active_observation_windows(connection)
            for record in records:
                entity_type = record["entity_type"]
                source_record_id = record["source_record_id"]
                current = connection.execute(
                    """
                    SELECT source_updated_at, content_hash, normalized_json, status, ingested_at, sync_run_id
                    FROM source_records
                    WHERE source_system = ? AND entity_type = ? AND source_record_id = ?
                    """,
                    (source_system, entity_type, source_record_id),
                ).fetchone()
                incoming_window = (
                    self._observation_window(record["data"])
                    if entity_type == "TEST_OBSERVATION"
                    else None
                )
                if incoming_window is not None:
                    existing_owner = observation_windows.get(incoming_window)
                    if existing_owner and existing_owner != source_record_id:
                        rejected.append(
                            {
                                "entity_type": entity_type,
                                "source_record_id": source_record_id,
                                "reason": "observation window/product/line/station already exists",
                            }
                        )
                        continue
                target_status = "WITHDRAWN" if record["operation"] == "DELETE" else "ACTIVE"
                disposition, reason = self._source_record_disposition(current, record, target_status)
                if disposition == "REJECT":
                    rejected.append(
                        {
                            "entity_type": entity_type,
                            "source_record_id": source_record_id,
                            "reason": reason,
                        }
                    )
                    continue
                if disposition == "UNCHANGED":
                    counts["unchanged"] += 1
                    continue

                self._record_source_history(
                    connection,
                    run_id,
                    source_system,
                    entity_type,
                    source_record_id,
                    current,
                )
                self._upsert_source_record(
                    connection,
                    source_system=source_system,
                    record=record,
                    target_status=target_status,
                    created_at=created_at,
                    run_id=run_id,
                )
                if entity_type == "TEST_OBSERVATION":
                    if current and current["status"] == "ACTIVE" and current["normalized_json"]:
                        previous_observation = json.loads(current["normalized_json"])
                        previous_window = self._observation_window(previous_observation)
                        if previous_window != incoming_window:
                            observation_windows.pop(previous_window, None)
                    if target_status == "ACTIVE" and incoming_window:
                        observation_windows[incoming_window] = source_record_id
                if target_status == "WITHDRAWN":
                    counts["withdrawn"] += 1
                elif current is None or current["status"] == "WITHDRAWN":
                    counts["inserted"] += 1
                else:
                    counts["updated"] += 1

            status = "PARTIAL" if rejected else "COMPLETED"
            if not rejected:
                connection.execute(
                    """
                    INSERT INTO source_cursors(source_system, cursor, updated_at, sync_run_id)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(source_system) DO UPDATE SET
                        cursor = excluded.cursor,
                        updated_at = excluded.updated_at,
                        sync_run_id = excluded.sync_run_id
                    """,
                    (source_system, export["cursor"], created_at, run_id),
                )
            connection.execute(
                """
                UPDATE source_sync_runs SET
                    completed_at = ?, status = ?, inserted_count = ?, updated_count = ?,
                    unchanged_count = ?, withdrawn_count = ?, rejected_count = ?, errors_json = ?
                WHERE run_id = ?
                """,
                (
                    created_at,
                    status,
                    counts["inserted"],
                    counts["updated"],
                    counts["unchanged"],
                    counts["withdrawn"],
                    len(rejected),
                    json.dumps(rejected, ensure_ascii=False),
                    run_id,
                ),
            )
        return {
            "run_id": run_id,
            "source_system": source_system,
            "status": status,
            "previous_cursor": previous_cursor,
            "cursor": export["cursor"] if not rejected else previous_cursor,
            "requested_cursor": export["cursor"],
            "total_count": export["total_count"],
            "inserted_count": counts["inserted"],
            "updated_count": counts["updated"],
            "unchanged_count": counts["unchanged"],
            "withdrawn_count": counts["withdrawn"],
            "rejected_count": len(rejected),
            "errors": rejected,
            "restart_required": any(counts[key] for key in ("inserted", "updated", "withdrawn")),
        }

    def list_active_source_records(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT source_system, entity_type, source_record_id, source_updated_at,
                       content_hash, normalized_json, ingested_at, sync_run_id
                FROM source_records WHERE status = 'ACTIVE'
                ORDER BY entity_type, source_record_id, source_system
                """
            ).fetchall()
        return [
            {
                "source_system": row["source_system"],
                "entity_type": row["entity_type"],
                "source_record_id": row["source_record_id"],
                "source_updated_at": row["source_updated_at"],
                "content_hash": row["content_hash"],
                "ingested_at": row["ingested_at"],
                "sync_run_id": row["sync_run_id"],
                "data": json.loads(row["normalized_json"]),
            }
            for row in rows
        ]

    def list_sync_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        if not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT run_id, source_system, previous_cursor, cursor, exported_at, started_at,
                       completed_at, rolled_back_at, status, total_count, inserted_count,
                       updated_count, unchanged_count, withdrawn_count, rejected_count, errors_json
                FROM source_sync_runs ORDER BY rowid DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["errors"] = json.loads(item.pop("errors_json"))
            output.append(item)
        return output

    def rollback_sync_run(self, run_id: str, *, created_at: str) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            run = connection.execute(
                "SELECT rowid, * FROM source_sync_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if not run:
                raise ValueError("sync run not found")
            if run["status"] not in {"COMPLETED", "PARTIAL"}:
                raise ValueError("sync run is not rollback-eligible")
            latest = connection.execute(
                """
                SELECT run_id FROM source_sync_runs
                WHERE source_system = ? AND status IN ('COMPLETED', 'PARTIAL')
                ORDER BY rowid DESC LIMIT 1
                """,
                (run["source_system"],),
            ).fetchone()
            if not latest or latest["run_id"] != run_id:
                raise ValueError("only the latest active sync run for a source can be rolled back")
            history = connection.execute(
                """
                SELECT * FROM source_record_history
                WHERE sync_run_id = ? ORDER BY history_id DESC
                """,
                (run_id,),
            ).fetchall()
            for item in history:
                current = connection.execute(
                    """
                    SELECT sync_run_id FROM source_records
                    WHERE source_system = ? AND entity_type = ? AND source_record_id = ?
                    """,
                    (item["source_system"], item["entity_type"], item["source_record_id"]),
                ).fetchone()
                if not current or current["sync_run_id"] != run_id:
                    raise ValueError("source record changed after this sync run")
            for item in history:
                key = (item["source_system"], item["entity_type"], item["source_record_id"])
                if item["previous_exists"]:
                    connection.execute(
                        """
                        UPDATE source_records SET
                            source_updated_at = ?, content_hash = ?, normalized_json = ?, status = ?,
                            ingested_at = ?, sync_run_id = ?
                        WHERE source_system = ? AND entity_type = ? AND source_record_id = ?
                        """,
                        (
                            item["previous_source_updated_at"],
                            item["previous_content_hash"],
                            item["previous_normalized_json"],
                            item["previous_status"],
                            item["previous_ingested_at"],
                            item["previous_sync_run_id"],
                            *key,
                        ),
                    )
                else:
                    connection.execute(
                        """
                        DELETE FROM source_records
                        WHERE source_system = ? AND entity_type = ? AND source_record_id = ?
                        """,
                        key,
                    )
            cursor = connection.execute(
                "SELECT sync_run_id FROM source_cursors WHERE source_system = ?",
                (run["source_system"],),
            ).fetchone()
            if cursor and cursor["sync_run_id"] == run_id:
                if run["previous_cursor"] is None:
                    connection.execute(
                        "DELETE FROM source_cursors WHERE source_system = ?",
                        (run["source_system"],),
                    )
                else:
                    previous_run = connection.execute(
                        """
                        SELECT run_id, completed_at FROM source_sync_runs
                        WHERE source_system = ? AND cursor = ? AND rowid < ? AND status != 'ROLLED_BACK'
                        ORDER BY rowid DESC LIMIT 1
                        """,
                        (run["source_system"], run["previous_cursor"], run["rowid"]),
                    ).fetchone()
                    if not previous_run:
                        raise ValueError("previous cursor provenance is unavailable")
                    connection.execute(
                        """
                        UPDATE source_cursors SET cursor = ?, updated_at = ?, sync_run_id = ?
                        WHERE source_system = ?
                        """,
                        (
                            run["previous_cursor"],
                            created_at,
                            previous_run["run_id"],
                            run["source_system"],
                        ),
                    )
            connection.execute(
                "UPDATE source_sync_runs SET status = 'ROLLED_BACK', rolled_back_at = ? WHERE run_id = ?",
                (created_at, run_id),
            )
        return {
            "run_id": run_id,
            "source_system": run["source_system"],
            "status": "ROLLED_BACK",
            "restored_record_count": len(history),
            "restored_cursor": run["previous_cursor"],
            "restart_required": bool(history),
        }
