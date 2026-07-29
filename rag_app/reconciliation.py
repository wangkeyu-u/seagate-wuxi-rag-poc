"""Reconcile validated exports with the approved manufacturing master data.

Schema-valid IDs are not automatically legitimate relationships. This module
checks product, line, station, equipment, material and software ownership before
records reach the source ledger or retrieval index.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .ingestion import ValidatedExport, ValidatedRecord


class MasterDataError(ValueError):
    """Raised when the trusted local master-data catalog is malformed."""


def _index(items: Any, key: str, label: str) -> dict[str, dict[str, Any]]:
    if not isinstance(items, list):
        raise MasterDataError(f"master data {label} must be an array")
    output: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get(key), str) or not item[key]:
            raise MasterDataError(f"master data {label} contains an invalid {key}")
        if item[key] in output:
            raise MasterDataError(f"master data {label} contains duplicate {key}")
        output[item[key]] = item
    return output


@dataclass(frozen=True)
class MasterDataCatalog:
    products: Mapping[str, dict[str, Any]]
    product_families: frozenset[str]
    lines: Mapping[str, dict[str, Any]]
    stations: Mapping[str, dict[str, Any]]
    equipment: Mapping[str, dict[str, Any]]
    failure_codes: Mapping[str, dict[str, Any]]
    material_lots: Mapping[str, dict[str, Any]]
    software_versions: Mapping[str, dict[str, Any]]
    teams: Mapping[str, dict[str, Any]]

    @classmethod
    def from_mapping(cls, master: Any) -> "MasterDataCatalog":
        if not isinstance(master, dict):
            raise MasterDataError("master data must be a JSON object")
        products = _index(master.get("products"), "product_id", "products")
        product_families = frozenset(
            item["product_family"]
            for item in products.values()
            if isinstance(item.get("product_family"), str) and item["product_family"]
        )
        return cls(
            products=products,
            product_families=product_families,
            lines=_index(master.get("lines"), "line_id", "lines"),
            stations=_index(master.get("stations"), "station_id", "stations"),
            equipment=_index(master.get("equipment"), "equipment_id", "equipment"),
            failure_codes=_index(master.get("failure_codes"), "failure_code", "failure_codes"),
            material_lots=_index(master.get("material_lots"), "material_lot_id", "material_lots"),
            software_versions=_index(
                master.get("software_versions"), "software_version_id", "software_versions"
            ),
            teams=_index(master.get("teams"), "team_id", "teams"),
        )

    @classmethod
    def from_file(cls, path: Path) -> "MasterDataCatalog":
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MasterDataError("master data must be readable UTF-8 JSON") from exc
        return cls.from_mapping(payload)


def _require_known(value: str, catalog: Mapping[str, Any], field: str) -> None:
    if value not in catalog:
        raise ValueError(f"{field} is not present in approved master data")


def _require_applicable(item: Mapping[str, Any], product_id: str, field: str) -> None:
    applicable_products = item.get("applicable_products")
    if not isinstance(applicable_products, list) or product_id not in applicable_products:
        raise ValueError(f"{field} is not approved for product_id")


def _reconcile_observation(data: Mapping[str, Any], catalog: MasterDataCatalog) -> None:
    product_id = data["product_id"]
    line_id = data["line_id"]
    station_id = data["station_id"]
    equipment_id = data["equipment_id"]
    _require_known(product_id, catalog.products, "product_id")
    _require_known(line_id, catalog.lines, "line_id")
    _require_known(station_id, catalog.stations, "station_id")
    _require_known(equipment_id, catalog.equipment, "equipment_id")
    _require_known(data["failure_code"], catalog.failure_codes, "failure_code")

    station = catalog.stations[station_id]
    if station.get("line_id") != line_id:
        raise ValueError("station_id does not belong to line_id in approved master data")
    if station.get("equipment_id") != equipment_id:
        raise ValueError("equipment_id is not assigned to station_id in approved master data")

    material_lot_id = data.get("material_lot_id")
    if material_lot_id:
        _require_known(material_lot_id, catalog.material_lots, "material_lot_id")
        _require_applicable(catalog.material_lots[material_lot_id], product_id, "material_lot_id")

    for field, expected_type in (
        ("firmware_version_id", "FIRMWARE"),
        ("test_program_version_id", "TEST_PROGRAM"),
    ):
        version_id = data.get(field)
        if not version_id:
            continue
        _require_known(version_id, catalog.software_versions, field)
        version = catalog.software_versions[version_id]
        if version.get("software_type") != expected_type:
            raise ValueError(f"{field} has the wrong software_type in approved master data")
        _require_applicable(version, product_id, field)


def _reconcile_document(data: Mapping[str, Any], catalog: MasterDataCatalog) -> None:
    _require_known(data["owner_team_id"], catalog.teams, "owner_team_id")
    for failure_code in data["applicable_failure_codes"]:
        _require_known(failure_code, catalog.failure_codes, "applicable_failure_codes")
    approved_products = set(catalog.products) | set(catalog.product_families)
    if any(product not in approved_products for product in data["applicable_products"]):
        raise ValueError("applicable_products contains an unknown product or product family")
    for line_id in data["line_ids"]:
        _require_known(line_id, catalog.lines, "line_ids")
    for station_id in data["station_ids"]:
        _require_known(station_id, catalog.stations, "station_ids")
    if data["line_ids"]:
        allowed_lines = set(data["line_ids"])
        if any(catalog.stations[station_id].get("line_id") not in allowed_lines for station_id in data["station_ids"]):
            raise ValueError("station_ids contains a station outside the document line_ids scope")


def reconcile_export(export: ValidatedExport, catalog: MasterDataCatalog) -> ValidatedExport:
    """Reject records whose normalized references contradict approved master data."""

    accepted: list[ValidatedRecord] = []
    rejected = list(export.rejected)
    for record in export.records:
        try:
            if record.operation == "UPSERT":
                if record.data is None:
                    raise ValueError("UPSERT record is missing normalized data")
                if record.entity_type == "TEST_OBSERVATION":
                    _reconcile_observation(record.data, catalog)
                elif record.entity_type == "DOCUMENT_VERSION":
                    _reconcile_document(record.data, catalog)
                else:  # pragma: no cover - validation prevents this branch
                    raise ValueError("unsupported entity_type")
            accepted.append(record)
        except (KeyError, TypeError, ValueError) as exc:
            rejected.append(
                {
                    "entity_type": record.entity_type,
                    "source_record_id": record.source_record_id,
                    "reason": f"master-data reconciliation failed: {exc}",
                }
            )
    return ValidatedExport(
        schema_version=export.schema_version,
        source_system=export.source_system,
        exported_at=export.exported_at,
        cursor=export.cursor,
        records=tuple(accepted),
        rejected=tuple(rejected),
        total_count=export.total_count,
    )
