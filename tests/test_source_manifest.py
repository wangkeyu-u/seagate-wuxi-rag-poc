from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rag_app.source_manifest import (
    ManifestVerificationError,
    manifest_signing_bytes,
    verify_source_bundle,
)
from rag_app.source_operations import run_source_sync_job
from rag_app.storage import RuntimeStorage
from tests.test_oidc import public_jwk, rsa_sign


ROOT = Path(__file__).resolve().parents[1]


def b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def signed_manifest(
    artifact_path: Path,
    *,
    source_system: str = "SEATRACK_EXPORT",
    created_at: datetime | None = None,
    expires_at: datetime | None = None,
    kid: str = "rfc-test-key",
) -> dict:
    created = created_at or datetime.now(timezone.utc) - timedelta(minutes=1)
    expires = expires_at or created + timedelta(hours=1)
    artifact = artifact_path.read_bytes()
    manifest = {
        "schema_version": "rag-source-manifest/v1",
        "manifest_id": "MANIFEST-TEST-0001",
        "source_system": source_system,
        "created_at": created.isoformat(timespec="seconds"),
        "expires_at": expires.isoformat(timespec="seconds"),
        "artifact": {
            "filename": artifact_path.name,
            "byte_size": len(artifact),
            "sha256": hashlib.sha256(artifact).hexdigest(),
        },
        "signing": {"alg": "RS256", "kid": kid, "signature": "pending"},
    }
    manifest["signing"]["signature"] = b64url(rsa_sign(manifest_signing_bytes(manifest)))
    return manifest


class SignedSourceManifestTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="signed-source-manifest-test-")
        self.root = Path(self.temp_dir.name)
        self.export_path = self.root / "seatrack-export.json"
        self.export_path.write_bytes(
            (ROOT / "examples" / "seatrack_observation_export_v1.json").read_bytes()
        )
        self.trust_path = self.root / "source-trust.jwks.json"
        self.trust_path.write_text(json.dumps({"keys": [public_jwk()]}), encoding="utf-8")
        self.manifest_path = self.root / "seatrack-export.manifest.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def write_manifest(self, **updates) -> dict:
        manifest = signed_manifest(self.export_path, **updates)
        self.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        return manifest

    def test_valid_manifest_verifies_signature_and_exact_artifact_bytes(self):
        manifest = self.write_manifest()
        verified = verify_source_bundle(
            self.export_path,
            self.manifest_path,
            self.trust_path,
        )
        self.assertEqual(verified.manifest_id, manifest["manifest_id"])
        self.assertEqual(verified.artifact_sha256, manifest["artifact"]["sha256"])
        self.assertEqual(verified.export_payload["source_system"], "SEATRACK_EXPORT")

    def test_artifact_and_signature_tampering_are_rejected(self):
        manifest = self.write_manifest()
        original = self.export_path.read_bytes()
        self.export_path.write_bytes(original + b" ")
        with self.assertRaisesRegex(ManifestVerificationError, "size"):
            verify_source_bundle(self.export_path, self.manifest_path, self.trust_path)
        self.export_path.write_bytes(original)
        signature = manifest["signing"]["signature"]
        manifest["signing"]["signature"] = ("A" if signature[0] != "A" else "B") + signature[1:]
        self.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ManifestVerificationError, "signature"):
            verify_source_bundle(self.export_path, self.manifest_path, self.trust_path)

    def test_expired_or_overlong_manifest_validity_is_rejected(self):
        now = datetime.now(timezone.utc)
        scenarios = (
            (now - timedelta(hours=2), now - timedelta(hours=1), "expired"),
            (now - timedelta(minutes=1), now + timedelta(days=2), "at most 24 hours"),
        )
        for created, expires, message in scenarios:
            with self.subTest(message=message):
                self.write_manifest(created_at=created, expires_at=expires)
                with self.assertRaisesRegex(ManifestVerificationError, message):
                    verify_source_bundle(self.export_path, self.manifest_path, self.trust_path)

    def test_signed_job_rejects_source_binding_mismatch_into_critical_quarantine(self):
        self.write_manifest(source_system="APPROVED_DMS_EXPORT")
        database = self.root / "runtime.sqlite3"
        result = run_source_sync_job(
            export_paths=[self.export_path],
            manifest_paths=[self.manifest_path],
            trust_jwks_path=self.trust_path,
            require_signed_manifests=True,
            database_path=database,
            master_data_path=ROOT / "data" / "master_data.json",
        )
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["results"][0]["status"], "REJECTED")
        quarantine = RuntimeStorage(database).list_source_quarantine()
        self.assertEqual(quarantine[0]["stage"], "MANIFEST")
        self.assertEqual(quarantine[0]["reason_code"], "SOURCE_BINDING_MISMATCH")
        self.assertEqual(quarantine[0]["severity"], "CRITICAL")
        self.assertEqual(result["source_health"]["quarantine"]["critical_open_count"], 1)

    def test_signed_job_completes_with_pinned_key_and_rejects_missing_configuration(self):
        self.write_manifest()
        database = self.root / "runtime.sqlite3"
        result = run_source_sync_job(
            export_paths=[self.export_path],
            manifest_paths=[self.manifest_path],
            trust_jwks_path=self.trust_path,
            require_signed_manifests=True,
            database_path=database,
            master_data_path=ROOT / "data" / "master_data.json",
        )
        self.assertEqual(result["status"], "COMPLETED")
        self.assertTrue(result["results"][0]["manifest_verified"])
        self.assertEqual(result["results"][0]["manifest_id"], "MANIFEST-TEST-0001")
        retry = run_source_sync_job(
            export_paths=[self.export_path],
            manifest_paths=[self.manifest_path],
            trust_jwks_path=self.trust_path,
            require_signed_manifests=True,
            database_path=database,
            master_data_path=ROOT / "data" / "master_data.json",
        )
        self.assertEqual(retry["status"], "COMPLETED")
        self.assertTrue(retry["results"][0]["manifest_replay"])

        self.export_path.write_bytes(self.export_path.read_bytes() + b" ")
        self.write_manifest()
        conflict = run_source_sync_job(
            export_paths=[self.export_path],
            manifest_paths=[self.manifest_path],
            trust_jwks_path=self.trust_path,
            require_signed_manifests=True,
            database_path=database,
            master_data_path=ROOT / "data" / "master_data.json",
        )
        self.assertEqual(conflict["status"], "FAILED")
        self.assertIn("conflicts", conflict["results"][0]["error"])
        self.assertEqual(
            RuntimeStorage(database).list_source_quarantine()[0]["reason_code"],
            "MANIFEST_REPLAY_CONFLICT",
        )
        with self.assertRaisesRegex(ValueError, "required"):
            run_source_sync_job(
                export_paths=[self.export_path],
                require_signed_manifests=True,
                database_path=self.root / "other.sqlite3",
                master_data_path=ROOT / "data" / "master_data.json",
            )


class SourceQuarantineLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="source-quarantine-test-")
        self.storage = RuntimeStorage(Path(self.temp_dir.name) / "runtime.sqlite3")

    def tearDown(self):
        self.temp_dir.cleanup()

    def create_event(self):
        return self.storage.create_source_quarantine(
            job_id="JOB-TEST",
            created_at="2026-07-30T02:00:00+00:00",
            source_system="SEATRACK_EXPORT",
            manifest_id="MANIFEST-TEST",
            artifact_filename="export.json",
            artifact_sha256="a" * 64,
            stage="MANIFEST",
            reason_code="MANIFEST_VERIFICATION_FAILED",
            reason_summary="signed delivery manifest or artifact binding could not be verified",
            severity="CRITICAL",
        )

    def test_quarantine_requires_one_explicit_resolution(self):
        event = self.create_event()
        open_items = self.storage.list_source_quarantine()
        self.assertEqual(open_items[0]["quarantine_id"], event["quarantine_id"])
        resolved = self.storage.resolve_source_quarantine(
            event["quarantine_id"],
            resolved_at="2026-07-30T03:00:00+00:00",
            resolved_by="quality-user",
            resolved_by_role="QUALITY_ENGINEER",
            resolution="REJECT",
            notes="Exporter will regenerate the signed delivery.",
        )
        self.assertEqual(resolved["status"], "RESOLVED_REJECTED")
        self.assertEqual(self.storage.list_source_quarantine(), [])
        with self.assertRaisesRegex(ValueError, "already resolved"):
            self.storage.resolve_source_quarantine(
                event["quarantine_id"],
                resolved_at="2026-07-30T04:00:00+00:00",
                resolved_by="quality-user",
                resolved_by_role="QUALITY_ENGINEER",
                resolution="RETRY",
                notes="duplicate resolution",
            )

    def test_open_quarantine_is_included_in_redacted_health(self):
        self.create_event()
        health = self.storage.source_health(
            now=datetime(2026, 7, 30, 2, 5, tzinfo=timezone.utc)
        )
        self.assertEqual(health["quarantine"]["open_count"], 1)
        self.assertIn("OPEN_QUARANTINE", {alert["code"] for alert in health["alerts"]})


if __name__ == "__main__":
    unittest.main()
