from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from rag_app.storage import RuntimeStorage


class InvestigationWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.storage = RuntimeStorage(Path(self.temp_dir.name) / "runtime" / "workflow.sqlite3")
        self.storage.save_investigation(
            {
                "investigation_id": "INV-WORKFLOW-1",
                "created_at": "2026-07-30T00:00:00+08:00",
                "subject": "owner",
                "role": "PRODUCT_ENGINEER",
                "query": "workflow test",
                "context": {},
                "answer": {"status": "TRIAGE"},
            }
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_investigation_state_machine_and_check_results(self) -> None:
        started = self.storage.transition_investigation(
            "INV-WORKFLOW-1",
            "INVESTIGATING",
            "2026-07-30T00:01:00+08:00",
            actor_subject="owner",
            actor_role="PRODUCT_ENGINEER",
        )
        self.assertEqual(started["status"], "INVESTIGATING")

        check = self.storage.add_check_result(
            "INV-WORKFLOW-1",
            "2026-07-30T00:02:00+08:00",
            actor_subject="owner",
            actor_role="PRODUCT_ENGINEER",
            step_sequence=1,
            outcome="PASS",
            notes="scope confirmed",
            evidence_ids=["DOC-SOP-ST-001-V2_0"],
        )
        self.assertEqual(check["outcome"], "PASS")

        completed = self.storage.transition_investigation(
            "INV-WORKFLOW-1",
            "CHECKED",
            "2026-07-30T00:03:00+08:00",
            actor_subject="owner",
            actor_role="PRODUCT_ENGINEER",
        )
        self.assertEqual(completed["status"], "CHECKED")
        loaded = self.storage.get_investigation("INV-WORKFLOW-1", subject="owner")
        self.assertEqual(len(loaded["check_results"]), 1)

    def test_invalid_transition_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid investigation status transition"):
            self.storage.transition_investigation(
                "INV-WORKFLOW-1",
                "PUBLISHED",
                "2026-07-30T00:01:00+08:00",
                actor_subject="owner",
                actor_role="PRODUCT_ENGINEER",
            )

    def test_checked_status_requires_a_recorded_check(self) -> None:
        self.storage.transition_investigation(
            "INV-WORKFLOW-1",
            "INVESTIGATING",
            "2026-07-30T00:01:00+08:00",
            actor_subject="owner",
            actor_role="PRODUCT_ENGINEER",
        )
        with self.assertRaisesRegex(ValueError, "at least one check result"):
            self.storage.transition_investigation(
                "INV-WORKFLOW-1",
                "CHECKED",
                "2026-07-30T00:02:00+08:00",
                actor_subject="owner",
                actor_role="PRODUCT_ENGINEER",
            )

    def test_review_controls_close_or_reopen_the_investigation(self) -> None:
        self.storage.transition_investigation(
            "INV-WORKFLOW-1",
            "INVESTIGATING",
            "2026-07-30T00:01:00+08:00",
            actor_subject="owner",
            actor_role="PRODUCT_ENGINEER",
        )
        self.storage.add_check_result(
            "INV-WORKFLOW-1",
            "2026-07-30T00:02:00+08:00",
            actor_subject="owner",
            actor_role="PRODUCT_ENGINEER",
            step_sequence=1,
            outcome="PASS",
            notes="scope confirmed",
            evidence_ids=["DOC-SOP-ST-001-V2_0"],
        )
        for status in ("CHECKED", "ROOT_CAUSE_REVIEW"):
            self.storage.transition_investigation(
                "INV-WORKFLOW-1",
                status,
                "2026-07-30T00:03:00+08:00",
                actor_subject="owner",
                actor_role="PRODUCT_ENGINEER",
            )
        review = self.storage.add_review(
            "INV-WORKFLOW-1",
            "2026-07-30T00:04:00+08:00",
            reviewer_subject="quality",
            reviewer_role="QUALITY_ENGINEER",
            decision="APPROVE",
            notes="evidence complete",
        )
        self.assertEqual(review["decision"], "APPROVE")
        loaded = self.storage.get_investigation("INV-WORKFLOW-1", subject="owner")
        self.assertEqual(loaded["status"], "CLOSED")
        self.assertEqual(len(loaded["reviews"]), 1)

    def test_quality_rejection_returns_investigation_to_owner(self) -> None:
        self.storage.transition_investigation(
            "INV-WORKFLOW-1",
            "INVESTIGATING",
            "2026-07-30T00:01:00+08:00",
            actor_subject="owner",
            actor_role="PRODUCT_ENGINEER",
        )
        self.storage.add_check_result(
            "INV-WORKFLOW-1",
            "2026-07-30T00:02:00+08:00",
            actor_subject="owner",
            actor_role="PRODUCT_ENGINEER",
            step_sequence=1,
            outcome="INCONCLUSIVE",
            notes="peer-station comparison incomplete",
            evidence_ids=["DOC-SOP-ST-001-V2_0"],
        )
        for status in ("CHECKED", "ROOT_CAUSE_REVIEW"):
            self.storage.transition_investigation(
                "INV-WORKFLOW-1",
                status,
                "2026-07-30T00:03:00+08:00",
                actor_subject="owner",
                actor_role="PRODUCT_ENGINEER",
            )
        review = self.storage.add_review(
            "INV-WORKFLOW-1",
            "2026-07-30T00:04:00+08:00",
            reviewer_subject="quality",
            reviewer_role="QUALITY_ENGINEER",
            decision="REJECT",
            notes="complete the peer-station comparison",
        )
        self.assertEqual(review["resulting_status"], "INVESTIGATING")
        loaded = self.storage.get_investigation("INV-WORKFLOW-1", subject="owner")
        self.assertEqual(loaded["status"], "INVESTIGATING")
        self.assertEqual(loaded["reviews"][0]["decision"], "REJECT")

    def test_legacy_database_is_migrated_without_losing_records(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "runtime" / "legacy.sqlite3"
            db_path.parent.mkdir()
            with closing(sqlite3.connect(db_path)) as connection:
                connection.executescript(
                    """
                    CREATE TABLE investigations (
                        investigation_id TEXT PRIMARY KEY,
                        created_at TEXT NOT NULL,
                        subject TEXT NOT NULL DEFAULT 'legacy',
                        role TEXT NOT NULL,
                        query TEXT NOT NULL,
                        context_json TEXT NOT NULL,
                        answer_json TEXT NOT NULL
                    );
                    CREATE TABLE feedback (
                        feedback_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        investigation_id TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        rating TEXT NOT NULL,
                        comment TEXT NOT NULL DEFAULT ''
                    );
                    CREATE TABLE audit_events (
                        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        investigation_id TEXT,
                        created_at TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        payload_json TEXT NOT NULL
                    );
                    """
                )
                connection.execute(
                    """
                    INSERT INTO investigations
                        (investigation_id, created_at, subject, role, query, context_json, answer_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "INV-LEGACY-1",
                        "2026-07-29T00:00:00+08:00",
                        "legacy-owner",
                        "PRODUCT_ENGINEER",
                        "legacy query",
                        "{}",
                        '{"status":"TRIAGE"}',
                    ),
                )
                # ``closing`` owns the connection lifetime but does not commit
                # the transaction, so make the legacy fixture durable explicitly.
                connection.commit()
            storage = RuntimeStorage(db_path)
            record = storage.get_investigation("INV-LEGACY-1", subject="legacy-owner")
            self.assertIsNotNone(record)
            self.assertEqual(record["status"], "TRIAGE")
            self.assertEqual(record["updated_at"], record["created_at"])


if __name__ == "__main__":
    unittest.main()
