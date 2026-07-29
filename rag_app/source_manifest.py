"""Authenticate offline source deliveries before their records are trusted.

The signed manifest binds the exact export bytes to an approved source, time
window and key. Verification reads each artifact once so a later file change
cannot create a check/use mismatch during the same synchronization attempt.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .auth import AuthenticationError
from .ingestion import MAX_EXPORT_BYTES, parse_export_bytes, read_regular_file_bytes
from .oidc import parse_rs256_jwks, verify_rs256_signature


MANIFEST_SCHEMA_VERSION = "rag-source-manifest/v1"
MAX_MANIFEST_BYTES = 64 * 1024
MAX_TRUST_JWKS_BYTES = 1_000_000
MAX_MANIFEST_LIFETIME_SECONDS = 24 * 60 * 60
SAFE_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
SHA256_HEX = re.compile(r"^[a-f0-9]{64}$")
BASE64URL = re.compile(r"^[A-Za-z0-9_-]+$")


class ManifestVerificationError(ValueError):
    """Raised when a signed source delivery manifest cannot be trusted."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise ManifestVerificationError(f"duplicate JSON member: {key}")
        output[key] = value
    return output


def _read_json(path: Path, *, maximum_bytes: int, label: str) -> dict[str, Any]:
    raw = read_regular_file_bytes(path, maximum_bytes=maximum_bytes, label=label)
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestVerificationError(f"{label} must be valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise ManifestVerificationError(f"{label} must be a JSON object")
    return payload


def _exact_fields(value: dict[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        missing = expected - value.keys()
        unknown = value.keys() - expected
        if missing:
            raise ManifestVerificationError(f"{label} missing fields: {', '.join(sorted(missing))}")
        raise ManifestVerificationError(f"{label} contains unknown fields: {', '.join(sorted(unknown))}")


def _timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ManifestVerificationError(f"{field} must be an ISO-8601 timestamp with timezone")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ManifestVerificationError(f"{field} must be an ISO-8601 timestamp with timezone") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ManifestVerificationError(f"{field} must be an ISO-8601 timestamp with timezone")
    return parsed


def _decode_signature(value: Any) -> bytes:
    if not isinstance(value, str) or not value or len(value) > 2_048 or not BASE64URL.fullmatch(value):
        raise ManifestVerificationError("manifest signature must be bounded base64url")
    try:
        return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise ManifestVerificationError("manifest signature must be bounded base64url") from exc


def manifest_signing_bytes(manifest: dict[str, Any]) -> bytes:
    """Return the documented canonical bytes signed by an approved exporter."""

    signing = manifest.get("signing")
    if not isinstance(signing, dict):
        raise ManifestVerificationError("manifest signing must be a JSON object")
    payload = dict(manifest)
    payload["signing"] = {key: value for key, value in signing.items() if key != "signature"}
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


@dataclass(frozen=True)
class VerifiedSourceBundle:
    manifest_id: str
    source_system: str
    artifact_filename: str
    artifact_sha256: str
    artifact_size: int
    signing_key_id: str
    export_payload: Any


def verify_source_bundle(
    export_path: Path,
    manifest_path: Path,
    trust_jwks_path: Path,
    *,
    now: datetime | None = None,
    clock_skew_seconds: int = 60,
) -> VerifiedSourceBundle:
    """Verify a pinned-key manifest and parse the exact artifact bytes it covers."""

    if not isinstance(clock_skew_seconds, int) or not 0 <= clock_skew_seconds <= 300:
        raise ValueError("manifest clock skew must be between 0 and 300 seconds")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("manifest verification time must include a timezone")

    manifest = _read_json(manifest_path, maximum_bytes=MAX_MANIFEST_BYTES, label="manifest")
    _exact_fields(
        manifest,
        {
            "schema_version",
            "manifest_id",
            "source_system",
            "created_at",
            "expires_at",
            "artifact",
            "signing",
        },
        "manifest",
    )
    if manifest["schema_version"] != MANIFEST_SCHEMA_VERSION:
        raise ManifestVerificationError(f"schema_version must be {MANIFEST_SCHEMA_VERSION}")
    manifest_id = manifest["manifest_id"]
    source_system = manifest["source_system"]
    if not isinstance(manifest_id, str) or not SAFE_IDENTIFIER.fullmatch(manifest_id):
        raise ManifestVerificationError("manifest_id must be a safe identifier")
    if not isinstance(source_system, str) or not SAFE_IDENTIFIER.fullmatch(source_system):
        raise ManifestVerificationError("source_system must be a safe identifier")

    created_at = _timestamp(manifest["created_at"], "created_at")
    expires_at = _timestamp(manifest["expires_at"], "expires_at")
    lifetime = (expires_at - created_at).total_seconds()
    if not 0 < lifetime <= MAX_MANIFEST_LIFETIME_SECONDS:
        raise ManifestVerificationError("manifest validity must be greater than zero and at most 24 hours")
    if current.timestamp() < created_at.timestamp() - clock_skew_seconds:
        raise ManifestVerificationError("manifest is not yet valid")
    if current.timestamp() >= expires_at.timestamp() + clock_skew_seconds:
        raise ManifestVerificationError("manifest has expired")

    artifact = manifest["artifact"]
    if not isinstance(artifact, dict):
        raise ManifestVerificationError("manifest artifact must be a JSON object")
    _exact_fields(artifact, {"filename", "byte_size", "sha256"}, "manifest artifact")
    filename = artifact["filename"]
    byte_size = artifact["byte_size"]
    digest = artifact["sha256"]
    if not isinstance(filename, str) or not SAFE_FILENAME.fullmatch(filename) or Path(filename).name != filename:
        raise ManifestVerificationError("artifact filename must be a safe basename")
    if filename != export_path.name:
        raise ManifestVerificationError("manifest artifact filename does not match the export path")
    if isinstance(byte_size, bool) or not isinstance(byte_size, int) or not 1 <= byte_size <= MAX_EXPORT_BYTES:
        raise ManifestVerificationError("artifact byte_size is invalid")
    if not isinstance(digest, str) or not SHA256_HEX.fullmatch(digest):
        raise ManifestVerificationError("artifact sha256 must be lowercase hexadecimal")

    signing = manifest["signing"]
    if not isinstance(signing, dict):
        raise ManifestVerificationError("manifest signing must be a JSON object")
    _exact_fields(signing, {"alg", "kid", "signature"}, "manifest signing")
    if signing["alg"] != "RS256":
        raise ManifestVerificationError("manifest signing algorithm must be RS256")
    kid = signing["kid"]
    if not isinstance(kid, str) or not SAFE_IDENTIFIER.fullmatch(kid):
        raise ManifestVerificationError("manifest signing kid must be a safe identifier")

    trust_payload = _read_json(
        trust_jwks_path,
        maximum_bytes=MAX_TRUST_JWKS_BYTES,
        label="manifest trust JWKS",
    )
    try:
        keys = parse_rs256_jwks(trust_payload)
    except AuthenticationError as exc:
        raise ManifestVerificationError("manifest trust JWKS contains no acceptable RS256 key set") from exc
    key = keys.get(kid)
    if key is None:
        raise ManifestVerificationError("manifest signing kid is not in the pinned trust JWKS")
    signature = _decode_signature(signing["signature"])
    if not verify_rs256_signature(manifest_signing_bytes(manifest), signature, key):
        raise ManifestVerificationError("manifest signature is invalid")

    artifact_bytes = read_regular_file_bytes(
        export_path,
        maximum_bytes=MAX_EXPORT_BYTES,
        label="export",
    )
    if len(artifact_bytes) != byte_size:
        raise ManifestVerificationError("export byte size does not match the signed manifest")
    actual_digest = hashlib.sha256(artifact_bytes).hexdigest()
    if actual_digest != digest:
        raise ManifestVerificationError("export digest does not match the signed manifest")
    return VerifiedSourceBundle(
        manifest_id=manifest_id,
        source_system=source_system,
        artifact_filename=filename,
        artifact_sha256=digest,
        artifact_size=byte_size,
        signing_key_id=kid,
        export_payload=parse_export_bytes(artifact_bytes),
    )
