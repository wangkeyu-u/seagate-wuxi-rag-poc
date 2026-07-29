"""Strict business-request schema and authenticated manufacturing-scope checks."""

from __future__ import annotations

import re
from typing import Any

from .auth import AuthorizationError, Identity
from .repository import DataRepository


ALLOWED_PAYLOAD_FIELDS = {"query", "context"}
ALLOWED_CONTEXT_FIELDS = {
    "product_id",
    "product_family",
    "failure_code",
    "scope",
    "material_lot_id",
    "test_program_version",
    "firmware_version",
    "recent_change",
    "station_ids",
    "line_ids",
}
ALLOWED_SCOPES = {"SINGLE_STATION", "MULTI_STATION", "CROSS_LINE", "UNKNOWN"}
ALLOWED_CHANGES = {"TEST_PROGRAM", "FIRMWARE", "MATERIAL", "EQUIPMENT", "PROCESS", "UNKNOWN"}
VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
FAILURE_CODE_PATTERN = re.compile(r"^F\d{3}$")


def _validate_optional_string(value: Any, field: str, *, maximum: int = 128) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError(f"context.{field} must be a string")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"context.{field} has invalid length")
    return normalized


def _validate_string_list(value: Any, field: str, *, maximum: int = 20) -> list[str]:
    if value in (None, []):
        return []
    if not isinstance(value, list):
        raise ValueError(f"context.{field} must be an array")
    if len(value) > maximum:
        raise ValueError(f"context.{field} contains too many values")
    output: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or len(item) > 128:
            raise ValueError(f"context.{field} contains an invalid value")
        normalized = item.strip().upper()
        if normalized not in output:
            output.append(normalized)
    return output


def validate_triage_payload(payload: dict[str, Any], repository: DataRepository) -> tuple[str, dict[str, Any]]:
    unknown_payload = set(payload) - ALLOWED_PAYLOAD_FIELDS
    if unknown_payload:
        if "role" in unknown_payload:
            raise ValueError("role must come from the authenticated server identity")
        raise ValueError(f"unsupported request field: {sorted(unknown_payload)[0]}")
    query = payload.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query is required")
    query = query.strip()
    if len(query) > 2000:
        raise ValueError("query is too long")
    supplied = payload.get("context", {})
    if supplied is None:
        supplied = {}
    if not isinstance(supplied, dict):
        raise ValueError("context must be an object")
    unknown_context = set(supplied) - ALLOWED_CONTEXT_FIELDS
    if unknown_context:
        raise ValueError(f"unsupported context field: {sorted(unknown_context)[0]}")

    context: dict[str, Any] = {}
    string_fields = (
        "product_id",
        "product_family",
        "failure_code",
        "scope",
        "material_lot_id",
        "test_program_version",
        "firmware_version",
        "recent_change",
    )
    for field in string_fields:
        value = _validate_optional_string(supplied.get(field), field)
        if value is not None:
            context[field] = value.upper() if field not in {"test_program_version", "firmware_version"} else value
    context["station_ids"] = _validate_string_list(supplied.get("station_ids"), "station_ids")
    context["line_ids"] = _validate_string_list(supplied.get("line_ids"), "line_ids")

    failure_code = context.get("failure_code")
    if failure_code and not FAILURE_CODE_PATTERN.fullmatch(failure_code):
        raise ValueError("context.failure_code must use the F000 format")
    scope = context.get("scope")
    if scope and scope not in ALLOWED_SCOPES:
        raise ValueError("context.scope is invalid")
    change = context.get("recent_change")
    if change and change not in ALLOWED_CHANGES:
        raise ValueError("context.recent_change is invalid")
    for field in ("test_program_version", "firmware_version"):
        value = context.get(field)
        if value and not VERSION_PATTERN.fullmatch(value):
            raise ValueError(f"context.{field} has an invalid format")
    product_id = context.get("product_id")
    if product_id and product_id not in repository.products_by_id:
        raise ValueError("context.product_id is unknown")
    material_lot = context.get("material_lot_id")
    if material_lot and material_lot not in repository.material_lots_by_id:
        raise ValueError("context.material_lot_id is unknown")
    unknown_stations = set(context["station_ids"]) - set(repository.stations_by_id)
    if unknown_stations:
        raise ValueError(f"unknown station: {sorted(unknown_stations)[0]}")
    known_lines = {item["line_id"] for item in repository.master["lines"]}
    unknown_lines = set(context["line_ids"]) - known_lines
    if unknown_lines:
        raise ValueError(f"unknown line: {sorted(unknown_lines)[0]}")
    return query, {key: value for key, value in context.items() if value not in (None, "", [])}


def enforce_identity_scope(context: dict[str, Any], identity: Identity) -> None:
    requested_lines = set(context.get("line_ids", []))
    requested_stations = set(context.get("station_ids", []))
    if identity.line_ids and not requested_lines.issubset(identity.line_ids):
        raise AuthorizationError("requested line is outside the authenticated identity scope")
    if identity.station_ids and not requested_stations.issubset(identity.station_ids):
        raise AuthorizationError("requested station is outside the authenticated identity scope")
