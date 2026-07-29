from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from rag_app.ingestion import load_export_file, validate_export
from rag_app.reconciliation import MasterDataCatalog, reconcile_export
from rag_app.source_operations import run_source_sync_job
from rag_app.storage import RuntimeStorage, SourceJobLockedError
from server import parse_source_stale_thresholds
from tests.test_ingestion import document_export, observation_export


ROOT = Path(__file__).resolve().parents[1]
CATALOG = MasterDataCatalog.from_file(ROOT / "data" / "master_data.json")


class MasterDataReconciliationTests(unittest.TestCase):
    def test_valid_examples_reconcile_cleanly(self):
        for name in ("seatrack_observation_export_v1.json", "dms_document_export_v1.json"):
            export = validate_export(load_export_file(ROOT / "examples" / name))
            reconciled = reconcile_export(export, CATALOG)
            self.assertEqual(reconciled.rejected, (), name)
            self.assertEqual(len(reconciled.records), 1, name)

    def test_station_line_and_equipment_ownership_are_enforced(self):
        scenarios = (
            ("line_id", "LINE-01", "does not belong"),
            ("equipment_id", "EQ-ST-005", "not assigned"),
        )
        for field, value, expected in scenarios:
            with self.subTest(field=field):
                payload = observation_export()
                payload["records"][0]["data"][field] = value
                reconciled = reconcile_export(validate_export(payload), CATALOG)
                self.assertEqual(len(reconciled.records), 0)
                self.assertIn(expected, reconciled.rejected[0]["reason"])

    def test_product_applicability_is_enforced_for_material_and_software(self):
        scenarios = (
            ("material_lot_id", "HSA-L2405"),
            ("firmware_version_id", "SW-FW-2.1.3"),
            ("test_program_version_id", "SW-TP-3.8"),
        )
        for field, value in scenarios:
            with self.subTest(field=field):
                payload = observation_export()
                payload["records"][0]["data"].update(
                    {
                        "product_id": "PRD-HY2001",
                        "material_lot_id": None,
                        "firmware_version_id": None,
                        "test_program_version_id": "SW-TP-5.1",
                        field: value,
                    }
                )
                reconciled = reconcile_export(validate_export(payload), CATALOG)
                self.assertEqual(len(reconciled.records), 0)
                self.assertIn("not approved for product_id", reconciled.rejected[0]["reason"])

    def test_document_station_scope_cannot_contradict_line_scope(self):
        payload = document_export()
        payload["records"][0]["data"]["line_ids"] = ["LINE-01"]
        reconciled = reconcile_export(validate_export(payload), CATALOG)
        self.assertEqual(len(reconciled.records), 0)
        self.assertIn("outside the document line_ids scope", reconciled.rejected[0]["reason"])


class SourceJobLockTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="source-operations-test-")
        self.database = Path(self.temp_dir.name) / "runtime.sqlite3"
        self.storage = RuntimeStorage(self.database)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_active_lease_prevents_a_second_job(self):
        self.storage.acquire_source_job_lock(
            "source-sync",
            "owner-1",
            acquired_at="2026-07-30T00:00:00+00:00",
            now_epoch=100,
            ttl_seconds=60,
        )
        with self.assertRaises(SourceJobLockedError):
            self.storage.acquire_source_job_lock(
                "source-sync",
                "owner-2",
                acquired_at="2026-07-30T00:00:30+00:00",
                now_epoch=130,
                ttl_seconds=60,
            )
        self.assertFalse(self.storage.release_source_job_lock("source-sync", "owner-2"))
        self.assertTrue(self.storage.release_source_job_lock("source-sync", "owner-1"))

    def test_expired_lease_can_be_reclaimed(self):
        self.storage.acquire_source_job_lock(
            "source-sync",
            "owner-1",
            acquired_at="2026-07-30T00:00:00+00:00",
            now_epoch=100,
            ttl_seconds=60,
        )
        lease = self.storage.acquire_source_job_lock(
            "source-sync",
            "owner-2",
            acquired_at="2026-07-30T00:01:00+00:00",
            now_epoch=160,
            ttl_seconds=60,
        )
        self.assertEqual(lease["owner_id"], "owner-2")

    def test_strict_job_rejects_bad_master_data_without_writing_records(self):
        payload = observation_export()
        payload["records"][0]["data"]["line_id"] = "LINE-01"
        export_path = Path(self.temp_dir.name) / "bad-export.json"
        export_path.write_text(json.dumps(payload), encoding="utf-8")
        result = run_source_sync_job(
            export_paths=[export_path],
            database_path=self.database,
            master_data_path=ROOT / "data" / "master_data.json",
        )
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["results"][0]["status"], "REJECTED")
        self.assertEqual(self.storage.list_active_source_records(), [])
        self.assertFalse(result["source_health"]["job_lock"]["held"])

    def test_later_export_for_failed_source_is_skipped(self):
        bad_payload = observation_export()
        bad_payload["records"][0]["data"]["line_id"] = "LINE-01"
        bad_path = Path(self.temp_dir.name) / "bad-first.json"
        good_path = Path(self.temp_dir.name) / "good-second.json"
        bad_path.write_text(json.dumps(bad_payload), encoding="utf-8")
        good_path.write_text(json.dumps(observation_export()), encoding="utf-8")

        result = run_source_sync_job(
            export_paths=[bad_path, good_path],
            database_path=self.database,
            master_data_path=ROOT / "data" / "master_data.json",
        )

        self.assertEqual([item["status"] for item in result["results"]], ["REJECTED", "SKIPPED"])
        self.assertEqual(self.storage.list_active_source_records(), [])

    def test_clean_multi_source_job_completes_and_is_healthy(self):
        result = run_source_sync_job(
            export_paths=[
                ROOT / "examples" / "seatrack_observation_export_v1.json",
                ROOT / "examples" / "dms_document_export_v1.json",
            ],
            database_path=self.database,
            master_data_path=ROOT / "data" / "master_data.json",
        )
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual([item["status"] for item in result["results"]], ["COMPLETED", "COMPLETED"])
        self.assertEqual(len(self.storage.list_active_source_records()), 2)
        self.assertEqual(result["source_health"]["status"], "HEALTHY")


class SourceHealthTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="source-health-test-")
        self.storage = RuntimeStorage(Path(self.temp_dir.name) / "runtime.sqlite3")

    def tearDown(self):
        self.temp_dir.cleanup()

    def sync(self, payload, created_at="2026-07-30T01:10:00+00:00"):
        export = reconcile_export(validate_export(payload), CATALOG)
        return self.storage.sync_source_records(export.as_dict(), created_at=created_at)

    def test_never_synced_sources_are_critical(self):
        health = self.storage.source_health(now=datetime(2026, 7, 30, 1, 0, tzinfo=timezone.utc))
        self.assertEqual(health["status"], "CRITICAL")
        self.assertEqual(
            {alert["source_system"] for alert in health["alerts"] if alert["code"] == "NEVER_SYNCED"},
            {"SEATRACK_EXPORT", "APPROVED_DMS_EXPORT"},
        )

    def test_fresh_clean_sources_are_healthy_and_redacted(self):
        self.sync(observation_export())
        self.sync(document_export())
        health = self.storage.source_health(now=datetime(2026, 7, 30, 1, 20, tzinfo=timezone.utc))
        self.assertEqual(health["status"], "HEALTHY")
        self.assertEqual(health["alerts"], [])
        self.assertNotIn("errors", health["sources"][0]["latest_run"])

    def test_stale_and_questionable_sources_raise_distinct_alerts(self):
        observation = observation_export()
        observation["records"][0]["data"]["quality_status"] = "QUESTIONABLE"
        self.sync(observation, created_at="2026-07-29T00:00:00+00:00")
        self.sync(document_export(), created_at="2026-07-30T01:00:00+00:00")
        health = self.storage.source_health(now=datetime(2026, 7, 30, 2, 0, tzinfo=timezone.utc))
        codes = {alert["code"] for alert in health["alerts"]}
        self.assertIn("STALE_SOURCE", codes)
        self.assertIn("QUESTIONABLE_RECORDS", codes)


class SourceHealthConfigurationTests(unittest.TestCase):
    def test_threshold_configuration_is_explicit_and_bounded(self):
        configured = parse_source_stale_thresholds(
            {
                "RAG_SOURCE_STALE_SECONDS": json.dumps(
                    {"SEATRACK_EXPORT": 3_600, "APPROVED_DMS_EXPORT": 43_200}
                )
            }
        )
        self.assertEqual(configured["SEATRACK_EXPORT"], 3_600)
        with self.assertRaises(ValueError):
            parse_source_stale_thresholds(
                {"RAG_SOURCE_STALE_SECONDS": '{"SEATRACK_EXPORT": 3600}'}
            )

    def test_boolean_threshold_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_source_stale_thresholds(
                {
                    "RAG_SOURCE_STALE_SECONDS": json.dumps(
                        {"SEATRACK_EXPORT": True, "APPROVED_DMS_EXPORT": 43_200}
                    )
                }
            )


if __name__ == "__main__":
    unittest.main()
