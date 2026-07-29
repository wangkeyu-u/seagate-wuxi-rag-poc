"""Orchestrate one bounded, scheduler-friendly source synchronization run.

This layer coordinates trust, validation, reconciliation and ledger stages. It
does not poll, move source files or retry forever; an enterprise scheduler owns
those concerns and can act on the returned status, health and quarantine IDs.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .ingestion import (
    DEFAULT_ALLOWED_SOURCES,
    MAX_EXPORT_BYTES,
    parse_export_bytes,
    read_regular_file_bytes,
    validate_export,
)
from .reconciliation import MasterDataCatalog, reconcile_export
from .source_manifest import ManifestVerificationError, verify_source_bundle
from .storage import RuntimeStorage


SOURCE_SYNC_LOCK_NAME = "source-sync"
MAX_JOB_EXPORTS = 32


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _artifact_filename(path: Path) -> str | None:
    name = path.name
    return name if name and len(name) <= 256 and "\x00" not in name else None


def _quarantine_summary(reason_code: str) -> str:
    return {
        "MANIFEST_VERIFICATION_FAILED": "signed delivery manifest or artifact binding could not be verified",
        "SOURCE_BINDING_MISMATCH": "signed manifest source does not match the validated export source",
        "MANIFEST_REPLAY_CONFLICT": "signed manifest ID conflicts with an earlier verified delivery",
        "EXPORT_VALIDATION_FAILED": "export envelope or record schema validation failed",
        "MASTER_DATA_RECONCILIATION_FAILED": "one or more records failed approved master-data reconciliation",
        "SOURCE_SYNC_PARTIAL": "source ledger synchronization rejected one or more records",
    }.get(reason_code, "source delivery could not be processed safely")


@dataclass
class _DeliveryState:
    """Mutable processing state for one immutable landing-zone delivery.

    Keeping the stage and trusted identifiers together prevents exception paths
    from writing misleading quarantine metadata (for example, treating a value
    from an unverified manifest as trusted provenance).
    """

    path: Path
    manifest_path: Path | None
    source_system: str | None = None
    manifest_id: str | None = None
    artifact_sha256: str | None = None
    signing_key_id: str | None = None
    manifest_replay: bool | None = None
    stage: str = "SCHEMA"
    reason_code: str = "EXPORT_VALIDATION_FAILED"


def _quarantine_delivery(
    storage: RuntimeStorage,
    state: _DeliveryState,
    *,
    job_id: str,
    severity: str,
    rejected_count: int = 0,
) -> dict[str, object]:
    """Persist only sanitized metadata; never copy source content into SQLite."""

    return storage.create_source_quarantine(
        job_id=job_id,
        created_at=_now().isoformat(timespec="seconds"),
        source_system=state.source_system,
        manifest_id=state.manifest_id,
        artifact_filename=_artifact_filename(state.path),
        artifact_sha256=state.artifact_sha256,
        stage=state.stage,
        reason_code=state.reason_code,
        reason_summary=_quarantine_summary(state.reason_code),
        severity=severity,
        rejected_count=rejected_count,
    )


def _load_delivery_payload(
    state: _DeliveryState,
    *,
    trust_jwks_path: Path | None,
) -> object:
    """Return the exact bytes authenticated by the manifest, or local-compat data."""

    if state.manifest_path is not None:
        state.stage = "MANIFEST"
        state.reason_code = "MANIFEST_VERIFICATION_FAILED"
        bundle = verify_source_bundle(state.path, state.manifest_path, trust_jwks_path)
        state.manifest_id = bundle.manifest_id
        state.artifact_sha256 = bundle.artifact_sha256
        state.signing_key_id = bundle.signing_key_id
        state.source_system = bundle.source_system
        return bundle.export_payload

    # Unsigned mode exists for the repository's local examples. Controlled
    # deployments are expected to require a manifest in the CLI configuration.
    artifact_bytes = read_regular_file_bytes(
        state.path,
        maximum_bytes=MAX_EXPORT_BYTES,
        label="export",
    )
    state.artifact_sha256 = hashlib.sha256(artifact_bytes).hexdigest()
    return parse_export_bytes(artifact_bytes)


def _process_delivery(
    state: _DeliveryState,
    *,
    storage: RuntimeStorage,
    catalog: MasterDataCatalog,
    allowed_sources: frozenset[str],
    allow_partial: bool,
    trust_jwks_path: Path | None,
    job_id: str,
    failed_sources: set[str],
) -> dict[str, object]:
    """Move one delivery through trust, schema, master-data and ledger gates."""

    try:
        raw_export = _load_delivery_payload(state, trust_jwks_path=trust_jwks_path)
        state.stage = "SCHEMA"
        state.reason_code = "EXPORT_VALIDATION_FAILED"
        validated = validate_export(raw_export, allowed_sources=allowed_sources)
        if state.source_system is not None and state.source_system != validated.source_system:
            state.stage = "MANIFEST"
            state.reason_code = "SOURCE_BINDING_MISMATCH"
            raise ManifestVerificationError(
                "signed manifest source_system does not match the validated export"
            )
        if state.manifest_id is not None and state.signing_key_id is not None:
            state.stage = "MANIFEST"
            state.reason_code = "MANIFEST_REPLAY_CONFLICT"
            registration = storage.record_verified_source_manifest(
                manifest_id=state.manifest_id,
                source_system=validated.source_system,
                artifact_sha256=state.artifact_sha256,
                signing_key_id=state.signing_key_id,
                observed_at=_now().isoformat(timespec="seconds"),
                job_id=job_id,
            )
            state.manifest_replay = bool(registration["replay"])

        export = validated
        state.source_system = export.source_system
        if state.source_system in failed_sources:
            return {
                "path": str(state.path),
                "source_system": state.source_system,
                "status": "SKIPPED",
                "error": "an earlier export for this source did not complete cleanly",
                "manifest_id": state.manifest_id,
                "manifest_replay": state.manifest_replay,
            }

        schema_rejected_count = len(export.rejected)
        state.stage = "RECONCILIATION"
        state.reason_code = "MASTER_DATA_RECONCILIATION_FAILED"
        export = reconcile_export(export, catalog)
        if export.rejected and not allow_partial:
            if schema_rejected_count:
                state.stage = "SCHEMA"
                state.reason_code = "EXPORT_VALIDATION_FAILED"
            quarantine = _quarantine_delivery(
                storage,
                state,
                job_id=job_id,
                severity="WARNING",
                rejected_count=len(export.rejected),
            )
            return {
                "path": str(state.path),
                "source_system": state.source_system,
                "status": "REJECTED",
                "manifest_id": state.manifest_id,
                "manifest_verified": state.manifest_path is not None,
                "manifest_replay": state.manifest_replay,
                "quarantine_id": quarantine["quarantine_id"],
                "total_count": export.total_count,
                "accepted_count": len(export.records),
                "rejected_count": len(export.rejected),
                "errors": list(export.rejected),
            }

        state.stage = "LEDGER"
        state.reason_code = "SOURCE_SYNC_PARTIAL"
        result = storage.sync_source_records(
            export.as_dict(),
            created_at=_now().isoformat(timespec="seconds"),
        )
        item: dict[str, object] = {
            "path": str(state.path),
            "manifest_id": state.manifest_id,
            "manifest_verified": state.manifest_path is not None,
            "manifest_replay": state.manifest_replay,
            **result,
        }
        if result["status"] != "COMPLETED":
            quarantine = _quarantine_delivery(
                storage,
                state,
                job_id=job_id,
                severity="WARNING",
                rejected_count=int(result["rejected_count"]),
            )
            item["quarantine_id"] = quarantine["quarantine_id"]
        return item
    # Validation, reconciliation and manifest errors deliberately share
    # ValueError as the job's fail-closed data boundary.
    except (OSError, ValueError) as exc:
        severity = "CRITICAL" if state.stage == "MANIFEST" else "WARNING"
        quarantine = _quarantine_delivery(
            storage,
            state,
            job_id=job_id,
            severity=severity,
        )
        return {
            "path": str(state.path),
            "source_system": state.source_system,
            "status": "REJECTED",
            "manifest_id": state.manifest_id,
            "manifest_verified": False if state.manifest_path is not None else None,
            "manifest_replay": state.manifest_replay,
            "quarantine_id": quarantine["quarantine_id"],
            "error": str(exc),
        }


def run_source_sync_job(
    *,
    export_paths: Iterable[Path],
    database_path: Path,
    master_data_path: Path,
    allowed_sources: Iterable[str] = DEFAULT_ALLOWED_SOURCES,
    allow_partial: bool = False,
    lock_ttl_seconds: int = 900,
    manifest_paths: Iterable[Path] | None = None,
    trust_jwks_path: Path | None = None,
    require_signed_manifests: bool = False,
) -> dict[str, object]:
    """Run one scheduler-friendly, mutually exclusive source synchronization job."""

    paths = list(export_paths)
    if not paths or len(paths) > MAX_JOB_EXPORTS:
        raise ValueError(f"source sync job requires between 1 and {MAX_JOB_EXPORTS} exports")
    normalized_paths = [str(path.resolve(strict=False)) for path in paths]
    if len(set(normalized_paths)) != len(normalized_paths):
        raise ValueError("source sync job export paths must be unique")
    manifests = list(manifest_paths or [])
    if manifests and len(manifests) != len(paths):
        raise ValueError("source sync job requires exactly one manifest per export")
    if manifests and trust_jwks_path is None:
        raise ValueError("signed source manifests require a pinned trust JWKS path")
    if trust_jwks_path is not None and not manifests:
        raise ValueError("a manifest must be supplied for every export when a trust JWKS is configured")
    if require_signed_manifests and (not manifests or trust_jwks_path is None):
        raise ValueError("signed source manifests are required for this job")
    manifest_by_index: list[Path | None] = manifests if manifests else [None] * len(paths)
    allowed = frozenset(allowed_sources)
    if not allowed or not allowed.issubset(DEFAULT_ALLOWED_SOURCES):
        raise ValueError("source sync job allowed_sources must be approved source systems")

    catalog = MasterDataCatalog.from_file(master_data_path)
    storage = RuntimeStorage(database_path)
    job_id = f"JOB-{uuid.uuid4().hex.upper()}"
    started = _now()
    storage.acquire_source_job_lock(
        SOURCE_SYNC_LOCK_NAME,
        job_id,
        acquired_at=started.isoformat(timespec="seconds"),
        now_epoch=int(started.timestamp()),
        ttl_seconds=lock_ttl_seconds,
    )
    results: list[dict[str, object]] = []
    failed_sources: set[str] = set()
    restart_required = False
    try:
        for index, path in enumerate(paths):
            state = _DeliveryState(path=path, manifest_path=manifest_by_index[index])
            item = _process_delivery(
                state,
                storage=storage,
                catalog=catalog,
                allowed_sources=allowed,
                allow_partial=allow_partial,
                trust_jwks_path=trust_jwks_path,
                job_id=job_id,
                failed_sources=failed_sources,
            )
            results.append(item)
            restart_required = restart_required or bool(item.get("restart_required"))
            if item["status"] in {"PARTIAL", "REJECTED"} and state.source_system:
                failed_sources.add(state.source_system)
    finally:
        storage.release_source_job_lock(SOURCE_SYNC_LOCK_NAME, job_id)

    completed = _now()
    clean = all(item["status"] == "COMPLETED" for item in results)
    return {
        "job_id": job_id,
        "status": "COMPLETED" if clean else "FAILED",
        "started_at": started.isoformat(timespec="seconds"),
        "completed_at": completed.isoformat(timespec="seconds"),
        "export_count": len(paths),
        "signed_manifest_mode": bool(manifests),
        "results": results,
        "restart_required": restart_required,
        "source_health": storage.source_health(now=completed),
    }
