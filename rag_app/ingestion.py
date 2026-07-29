"""Exact-schema validation for offline SeaTrack-style and approved-DMS exports.

This module is intentionally transport-agnostic: it validates bounded bytes and
normalizes records, while signature verification, master-data reconciliation,
and ledger mutation live in separate layers. That separation keeps untrusted
content out of the repository until every gate has passed.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from .auth import ALLOWED_ROLES


SCHEMA_VERSION = "seatrack-export/v1"
DEFAULT_ALLOWED_SOURCES = frozenset({"SEATRACK_EXPORT", "APPROVED_DMS_EXPORT"})
MAX_RECORDS = 5_000
MAX_EXPORT_BYTES = 10 * 1024 * 1024
MAX_DOCUMENT_CONTENT_CHARS = 200_000
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
PROMPT_INJECTION_PATTERNS = (
    re.compile(r"ignore\s+(?:all\s+)?(?:previous|prior|system)\s+instructions?", re.IGNORECASE),
    re.compile(r"(?:reveal|print|return)\s+(?:the\s+)?system\s+prompt", re.IGNORECASE),
    re.compile(r"忽略(?:以上|此前|之前|系统)(?:所有)?(?:指令|提示|规则)"),
    re.compile(r"(?:输出|泄露|显示)(?:系统提示|系统指令)"),
)


class ExportValidationError(ValueError):
    """Raised when an export envelope cannot be processed safely."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise ValueError(f"duplicate JSON key: {key}")
        output[key] = value
    return output


def read_regular_file_bytes(path: Path, *, maximum_bytes: int, label: str) -> bytes:
    """Read a bounded regular file once without following its final symlink."""

    if not isinstance(maximum_bytes, int) or maximum_bytes < 1:
        raise ValueError("maximum_bytes must be positive")
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"{label} path must be a regular, non-symlink file")
        if metadata.st_size > maximum_bytes:
            raise ValueError(f"{label} file exceeds {maximum_bytes} bytes")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            payload = stream.read(maximum_bytes + 1)
        if len(payload) > maximum_bytes:
            raise ValueError(f"{label} file exceeds {maximum_bytes} bytes")
        return payload
    finally:
        if descriptor is not None:
            os.close(descriptor)


def parse_export_bytes(payload: bytes) -> Any:
    try:
        return json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("export must be valid UTF-8 JSON") from exc


def load_export_file(path: Path) -> Any:
    """Read a bounded UTF-8 JSON export without following a final symlink."""

    return parse_export_bytes(
        read_regular_file_bytes(path, maximum_bytes=MAX_EXPORT_BYTES, label="export")
    )


@dataclass(frozen=True)
class ValidatedRecord:
    entity_type: str
    source_record_id: str
    source_updated_at: str
    operation: str
    content_hash: str
    data: dict[str, Any] | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity_type": self.entity_type,
            "source_record_id": self.source_record_id,
            "source_updated_at": self.source_updated_at,
            "operation": self.operation,
            "content_hash": self.content_hash,
            "data": self.data,
        }


@dataclass(frozen=True)
class ValidatedExport:
    schema_version: str
    source_system: str
    exported_at: str
    cursor: str
    records: tuple[ValidatedRecord, ...]
    rejected: tuple[dict[str, Any], ...]
    total_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source_system": self.source_system,
            "exported_at": self.exported_at,
            "cursor": self.cursor,
            "records": [record.as_dict() for record in self.records],
            "rejected": list(self.rejected),
            "total_count": self.total_count,
        }


def _exact_fields(value: dict[str, Any], required: set[str], optional: set[str], label: str) -> None:
    missing = required - value.keys()
    unknown = value.keys() - required - optional
    if missing:
        raise ExportValidationError(f"{label} missing fields: {', '.join(sorted(missing))}")
    if unknown:
        raise ExportValidationError(f"{label} contains unknown fields: {', '.join(sorted(unknown))}")


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ExportValidationError(f"{field} must be a safe identifier")
    return value


def _text(value: Any, field: str, *, maximum: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not allow_empty and not value.strip()):
        raise ExportValidationError(f"{field} must be a non-empty string of at most {maximum} characters")
    if "\x00" in value:
        raise ExportValidationError(f"{field} contains a null character")
    return value.strip() if not allow_empty else value


def _timestamp(value: Any, field: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or len(value) > 64:
        raise ExportValidationError(f"{field} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ExportValidationError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ExportValidationError(f"{field} must include a timezone")
    return parsed.isoformat(timespec="seconds")


def _integer(value: Any, field: str, *, minimum: int = 0, maximum: int = 1_000_000_000) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ExportValidationError(f"{field} must be an integer between {minimum} and {maximum}")
    return value


def _rate(value: Any, field: str, *, nullable: bool = False) -> float | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ExportValidationError(f"{field} must be a number between 0 and 1")
    result = float(value)
    if not math.isfinite(result) or not 0 <= result <= 1:
        raise ExportValidationError(f"{field} must be a finite number between 0 and 1")
    return result


def _identifier_list(value: Any, field: str, *, maximum: int = 64) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ExportValidationError(f"{field} must be an array with at most {maximum} items")
    output: list[str] = []
    for index, item in enumerate(value):
        normalized = _identifier(item, f"{field}[{index}]")
        if normalized not in output:
            output.append(normalized)
    return output


def _normalize_observation(data: dict[str, Any], source_system: str, source_record_id: str) -> dict[str, Any]:
    required = {
        "observation_id",
        "window_start",
        "window_end",
        "product_id",
        "line_id",
        "station_id",
        "equipment_id",
        "failure_code",
        "material_lot_id",
        "firmware_version_id",
        "test_program_version_id",
        "units_tested",
        "units_passed",
        "units_failed",
        "first_pass_yield",
        "failure_count",
        "failure_rate",
        "baseline_failure_rate",
        "quality_status",
    }
    _exact_fields(data, required, set(), "TEST_OBSERVATION data")
    observation_id = _identifier(data["observation_id"], "observation_id")
    if observation_id != source_record_id:
        raise ExportValidationError("source_record_id must equal observation_id")
    window_start = _timestamp(data["window_start"], "window_start")
    window_end = _timestamp(data["window_end"], "window_end")
    if datetime.fromisoformat(window_end) <= datetime.fromisoformat(window_start):
        raise ExportValidationError("window_end must be later than window_start")
    units_tested = _integer(data["units_tested"], "units_tested", minimum=1)
    units_passed = _integer(data["units_passed"], "units_passed")
    units_failed = _integer(data["units_failed"], "units_failed")
    failure_count = _integer(data["failure_count"], "failure_count")
    first_pass_yield = _rate(data["first_pass_yield"], "first_pass_yield")
    failure_rate = _rate(data["failure_rate"], "failure_rate")
    baseline_failure_rate = _rate(data["baseline_failure_rate"], "baseline_failure_rate", nullable=True)
    if units_passed + units_failed != units_tested:
        raise ExportValidationError("units_passed + units_failed must equal units_tested")
    if failure_count > units_failed:
        raise ExportValidationError("failure_count must not exceed units_failed")
    if abs(first_pass_yield - units_passed / units_tested) > 0.00001:
        raise ExportValidationError("first_pass_yield does not match unit counts")
    if abs(failure_rate - failure_count / units_tested) > 0.00001:
        raise ExportValidationError("failure_rate does not match failure_count / units_tested")
    quality_status = data["quality_status"]
    if quality_status not in {"RAW", "VALIDATED", "QUESTIONABLE"}:
        raise ExportValidationError("quality_status must be RAW, VALIDATED, or QUESTIONABLE")

    def optional_identifier(value: Any, field: str) -> str | None:
        return None if value is None else _identifier(value, field)

    return {
        "observation_id": observation_id,
        "window_start": window_start,
        "window_end": window_end,
        "product_id": _identifier(data["product_id"], "product_id"),
        "line_id": _identifier(data["line_id"], "line_id"),
        "station_id": _identifier(data["station_id"], "station_id"),
        "equipment_id": _identifier(data["equipment_id"], "equipment_id"),
        "failure_code": _identifier(data["failure_code"], "failure_code"),
        "material_lot_id": optional_identifier(data["material_lot_id"], "material_lot_id"),
        "firmware_version_id": optional_identifier(data["firmware_version_id"], "firmware_version_id"),
        "test_program_version_id": _identifier(data["test_program_version_id"], "test_program_version_id"),
        "units_tested": units_tested,
        "units_passed": units_passed,
        "units_failed": units_failed,
        "first_pass_yield": first_pass_yield,
        "failure_count": failure_count,
        "failure_rate": failure_rate,
        "baseline_failure_rate": baseline_failure_rate,
        "source_system": source_system,
        "quality_status": quality_status,
    }


def _normalize_document(data: dict[str, Any], source_system: str, source_record_id: str) -> dict[str, Any]:
    required = {
        "document_id",
        "document_version_id",
        "document_type",
        "title",
        "version",
        "status",
        "language",
        "effective_from",
        "effective_to",
        "owner_team_id",
        "approved_by",
        "confidentiality",
        "canonical_uri",
        "applicable_failure_codes",
        "applicable_products",
        "supersedes_version_id",
        "summary",
        "content",
        "allowed_roles",
        "line_ids",
        "station_ids",
    }
    _exact_fields(data, required, set(), "DOCUMENT_VERSION data")
    version_id = _identifier(data["document_version_id"], "document_version_id")
    if version_id != source_record_id:
        raise ExportValidationError("source_record_id must equal document_version_id")
    status = data["status"]
    if status not in {"DRAFT", "EFFECTIVE", "SUPERSEDED", "WITHDRAWN"}:
        raise ExportValidationError("invalid document status")
    approved_by = None if data["approved_by"] is None else _identifier(data["approved_by"], "approved_by")
    if status == "EFFECTIVE" and not approved_by:
        raise ExportValidationError("effective documents require approved_by")
    confidentiality = data["confidentiality"]
    if confidentiality not in {"PUBLIC", "INTERNAL", "RESTRICTED"}:
        raise ExportValidationError("invalid document confidentiality")
    allowed_roles = _identifier_list(data["allowed_roles"], "allowed_roles", maximum=16)
    if any(role not in ALLOWED_ROLES for role in allowed_roles):
        raise ExportValidationError("allowed_roles contains an unsupported role")
    effective_from = _timestamp(data["effective_from"], "effective_from")
    effective_to = _timestamp(data["effective_to"], "effective_to", nullable=True)
    if effective_to and datetime.fromisoformat(effective_to) <= datetime.fromisoformat(effective_from):
        raise ExportValidationError("effective_to must be later than effective_from")
    canonical_uri = _text(data["canonical_uri"], "canonical_uri", maximum=2_048)
    parsed_uri = urlparse(canonical_uri)
    if parsed_uri.scheme.lower() not in {"https", "approved-dms", "seatrack"}:
        raise ExportValidationError("canonical_uri must use https, approved-dms, or seatrack")
    if not parsed_uri.netloc:
        raise ExportValidationError("canonical_uri must include an authority")
    content = _text(data["content"], "content", maximum=MAX_DOCUMENT_CONTENT_CHARS)
    summary = _text(data["summary"], "summary", maximum=4_000)
    title = _text(data["title"], "title", maximum=300)
    if any(pattern.search("\n".join((title, summary, content))) for pattern in PROMPT_INJECTION_PATTERNS):
        raise ExportValidationError("document content contains an instruction-like prompt injection pattern")
    supersedes = data["supersedes_version_id"]
    if supersedes is not None:
        supersedes = _identifier(supersedes, "supersedes_version_id")
        if supersedes == version_id:
            raise ExportValidationError("a document version cannot supersede itself")
    language = _identifier(data["language"], "language")
    return {
        "document_id": _identifier(data["document_id"], "document_id"),
        "document_version_id": version_id,
        "document_type": _identifier(data["document_type"], "document_type"),
        "title": title,
        "version": _text(data["version"], "version", maximum=64),
        "status": status,
        "language": language,
        "effective_from": effective_from,
        "effective_to": effective_to,
        "owner_team_id": _identifier(data["owner_team_id"], "owner_team_id"),
        "approved_by": approved_by,
        "confidentiality": confidentiality,
        "source_system": source_system,
        "canonical_uri": canonical_uri,
        "applicable_failure_codes": _identifier_list(
            data["applicable_failure_codes"], "applicable_failure_codes"
        ),
        "applicable_products": _identifier_list(data["applicable_products"], "applicable_products"),
        "supersedes_version_id": supersedes,
        "summary": summary,
        "content": content,
        "allowed_roles": allowed_roles,
        "line_ids": _identifier_list(data["line_ids"], "line_ids"),
        "station_ids": _identifier_list(data["station_ids"], "station_ids"),
    }


def _content_hash(entity_type: str, operation: str, data: dict[str, Any] | None) -> str:
    canonical = json.dumps(
        {"entity_type": entity_type, "operation": operation, "data": data},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def validate_export(
    payload: Any,
    *,
    allowed_sources: Iterable[str] = DEFAULT_ALLOWED_SOURCES,
) -> ValidatedExport:
    if not isinstance(payload, dict):
        raise ExportValidationError("export must be a JSON object")
    _exact_fields(
        payload,
        {"schema_version", "source_system", "exported_at", "cursor", "records"},
        set(),
        "export",
    )
    if payload["schema_version"] != SCHEMA_VERSION:
        raise ExportValidationError(f"schema_version must be {SCHEMA_VERSION}")
    source_system = _identifier(payload["source_system"], "source_system")
    normalized_sources = frozenset(allowed_sources)
    if source_system not in normalized_sources:
        raise ExportValidationError("source_system is not allowlisted")
    exported_at = _timestamp(payload["exported_at"], "exported_at")
    cursor = _text(payload["cursor"], "cursor", maximum=512)
    records = payload["records"]
    if not isinstance(records, list) or len(records) > MAX_RECORDS:
        raise ExportValidationError(f"records must be an array with at most {MAX_RECORDS} items")

    accepted: list[ValidatedRecord] = []
    rejected: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str]] = set()
    seen_observation_windows: set[tuple[str, str, str, str, str]] = set()
    for index, raw_record in enumerate(records):
        source_record_id: str | None = None
        entity_type: str | None = None
        try:
            if not isinstance(raw_record, dict):
                raise ExportValidationError("record must be a JSON object")
            _exact_fields(
                raw_record,
                {"entity_type", "source_record_id", "source_updated_at", "operation"},
                {"data"},
                f"record[{index}]",
            )
            entity_type = raw_record["entity_type"]
            if entity_type not in {"TEST_OBSERVATION", "DOCUMENT_VERSION"}:
                raise ExportValidationError("unsupported entity_type")
            expected_source = {
                "TEST_OBSERVATION": "SEATRACK_EXPORT",
                "DOCUMENT_VERSION": "APPROVED_DMS_EXPORT",
            }[entity_type]
            if source_system != expected_source:
                raise ExportValidationError(f"{entity_type} records require source_system {expected_source}")
            source_record_id = _identifier(raw_record["source_record_id"], "source_record_id")
            key = (entity_type, source_record_id)
            if key in seen_keys:
                raise ExportValidationError("duplicate entity_type/source_record_id in export")
            seen_keys.add(key)
            source_updated_at = _timestamp(raw_record["source_updated_at"], "source_updated_at")
            operation = raw_record["operation"]
            if operation not in {"UPSERT", "DELETE"}:
                raise ExportValidationError("operation must be UPSERT or DELETE")
            raw_data = raw_record.get("data")
            if operation == "DELETE":
                if raw_data is not None:
                    raise ExportValidationError("DELETE records must omit data or set it to null")
                normalized = None
            else:
                if not isinstance(raw_data, dict):
                    raise ExportValidationError("UPSERT records require a data object")
                if entity_type == "TEST_OBSERVATION":
                    normalized = _normalize_observation(raw_data, source_system, source_record_id)
                    observation_window = (
                        normalized["window_start"],
                        normalized["window_end"],
                        normalized["product_id"],
                        normalized["line_id"],
                        normalized["station_id"],
                    )
                    if observation_window in seen_observation_windows:
                        raise ExportValidationError(
                            "duplicate observation window/product/line/station in export"
                        )
                    seen_observation_windows.add(observation_window)
                else:
                    normalized = _normalize_document(raw_data, source_system, source_record_id)
            accepted.append(
                ValidatedRecord(
                    entity_type=entity_type,
                    source_record_id=source_record_id,
                    source_updated_at=source_updated_at,
                    operation=operation,
                    content_hash=_content_hash(entity_type, operation, normalized),
                    data=normalized,
                )
            )
        except ExportValidationError as exc:
            rejected.append(
                {
                    "index": index,
                    "entity_type": entity_type,
                    "source_record_id": source_record_id,
                    "reason": str(exc),
                }
            )
    return ValidatedExport(
        schema_version=SCHEMA_VERSION,
        source_system=source_system,
        exported_at=exported_at,
        cursor=cursor,
        records=tuple(accepted),
        rejected=tuple(rejected),
        total_count=len(records),
    )
