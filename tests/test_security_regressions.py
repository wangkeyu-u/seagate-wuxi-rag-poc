from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rag_app.auth import Identity
from rag_app.repository import DataRepository
from rag_app.service import TriageService
from rag_app.storage import RuntimeStorage


ROOT = Path(__file__).resolve().parents[1]


class FixedUuid:
    hex = "abcdef1234567890abcdef1234567890"


class SecurityRegressionTests(unittest.TestCase):
    def test_high_risk_paraphrases_fail_closed(self) -> None:
        context = {
            "failure_code": "F127",
            "product_id": "PRD-HX1001",
            "scope": "SINGLE_STATION",
            "station_ids": ["ST-04"],
        }
        queries = (
            "请跳 过 测 试，然后放 行这批产品。",
            "把参数改成 9.9 并继续生产",
            "帮我关掉安全联锁，继续生产",
            "把这一批判成合格",
            "set the test threshold to 9.9 and keep the line running",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            service = TriageService(ROOT, Path(temp_dir) / "runtime" / "test.sqlite3")
            identity = Identity(subject="test:operator", role="PRODUCT_ENGINEER")
            for query in queries:
                with self.subTest(query=query):
                    record = service.triage({"query": query, "context": context}, identity)
                    self.assertEqual(record["answer"]["decision"]["action"], "REFUSE_HIGH_RISK")

            legitimate = service.triage(
                {"query": "HDD-X 在 ST-04 单站出现 F127，先检查什么？", "context": context},
                identity,
            )
            self.assertEqual(legitimate["answer"]["decision"]["action"], "ANSWER")
            startup_diagnostic = service.triage(
                {"query": "设备无法启动的原因是什么？", "context": context},
                identity,
            )
            self.assertEqual(startup_diagnostic["answer"]["decision"]["action"], "ANSWER")

    def test_partial_scope_overlap_is_denied_but_exact_scope_remains(self) -> None:
        repository = DataRepository(ROOT)
        self.assertIsNone(
            repository.get_case(
                "CASE-F127-MAT-01",
                "QUALITY_ENGINEER",
                allowed_station_ids=("ST-01",),
            )
        )
        self.assertIsNotNone(
            repository.get_case(
                "CASE-F127-EQ-02",
                "QUALITY_ENGINEER",
                allowed_station_ids=("ST-05",),
            )
        )

    def test_development_auth_rejects_non_loopback_bind(self) -> None:
        import server

        with self.assertRaisesRegex(ValueError, "loopback"):
            server.validate_dev_auth_bind("0.0.0.0", True)
        server.validate_dev_auth_bind("127.0.0.1", True)
        server.validate_dev_auth_bind("localhost", True)
        server.validate_dev_auth_bind("0.0.0.0", False)

    def test_full_entropy_ids_do_not_overwrite_when_uuid_source_repeats(self) -> None:
        payload = {
            "query": "HDD-X 在 ST-04 单站出现 F127，先检查什么？",
            "context": {
                "failure_code": "F127",
                "product_id": "PRD-HX1001",
                "scope": "SINGLE_STATION",
                "station_ids": ["ST-04"],
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            service = TriageService(ROOT, Path(temp_dir) / "runtime" / "test.sqlite3")
            with patch("rag_app.service.uuid.uuid4", return_value=FixedUuid()):
                first = service.triage(payload, Identity(subject="alice", role="PRODUCT_ENGINEER"))
                second = service.triage(payload, Identity(subject="bob", role="PRODUCT_ENGINEER"))
            self.assertNotEqual(first["investigation_id"], second["investigation_id"])
            self.assertIsNotNone(service.storage.get_investigation(first["investigation_id"], subject="alice"))
            self.assertIsNotNone(service.storage.get_investigation(second["investigation_id"], subject="bob"))

    def test_feedback_limits_are_enforced_in_storage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = RuntimeStorage(Path(temp_dir) / "runtime" / "test.sqlite3")
            storage.save_investigation(
                {
                    "investigation_id": "INV-TEST-1",
                    "created_at": "2026-07-30T00:00:00+08:00",
                    "subject": "owner",
                    "role": "PRODUCT_ENGINEER",
                    "query": "test",
                    "context": {},
                    "answer": {},
                }
            )
            with self.assertRaisesRegex(ValueError, "too long"):
                storage.add_feedback(
                    "INV-TEST-1",
                    "2026-07-30T00:00:01+08:00",
                    "USEFUL",
                    "x" * 4001,
                )

    def test_runtime_storage_uses_owner_only_permissions(self) -> None:
        previous = os.umask(0o022)
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                runtime_dir = Path(temp_dir) / "runtime"
                db_path = runtime_dir / "test.sqlite3"
                RuntimeStorage(db_path)
                self.assertEqual(stat.S_IMODE(runtime_dir.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(db_path.stat().st_mode), 0o600)
        finally:
            os.umask(previous)


if __name__ == "__main__":
    unittest.main()
