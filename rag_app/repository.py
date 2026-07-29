from __future__ import annotations

import json
from pathlib import Path
from typing import Any


RESTRICTED_ROLES = {"PRODUCT_ENGINEER", "PROCESS_ENGINEER", "QUALITY_ENGINEER", "FA_ENGINEER", "ADMIN"}


class DataRepository:
    def __init__(self, root: Path):
        self.root = root
        self.data_dir = root / "data"
        self.master = self._load("master_data.json")
        self.cases = self._load("cases.json")["cases"]
        self.documents = self._load("documents.json")["documents"]
        self.observations = self._load("observations.json")["observations"]
        self.change_records = self._load("change_records.json")["change_records"]
        self.evaluations = self._load("evaluations.json")["evaluations"]
        self.manifest = self._load("manifest.json")

        self.products_by_id = {item["product_id"]: item for item in self.master["products"]}
        self.stations_by_id = {item["station_id"]: item for item in self.master["stations"]}
        self.failure_codes_by_id = {item["failure_code"]: item for item in self.master["failure_codes"]}
        self.material_lots_by_id = {item["material_lot_id"]: item for item in self.master["material_lots"]}
        self.versions_by_id = {item["software_version_id"]: item for item in self.master["software_versions"]}
        self.cases_by_id = {item["case_id"]: item for item in self.cases}
        self.documents_by_version = {item["document_version_id"]: item for item in self.documents}
        self.documents_by_id: dict[str, list[dict[str, Any]]] = {}
        for item in self.documents:
            self.documents_by_id.setdefault(item["document_id"], []).append(item)
            item["content"] = (root / item["content_path"]).read_text(encoding="utf-8")
        self.changes_by_id = {item["change_record_id"]: item for item in self.change_records}

    def _load(self, name: str) -> Any:
        return json.loads((self.data_dir / name).read_text(encoding="utf-8"))

    @staticmethod
    def can_access(confidentiality: str, role: str) -> bool:
        return confidentiality != "RESTRICTED" or role in RESTRICTED_ROLES

    @staticmethod
    def _within_scope(
        item: dict[str, Any],
        allowed_line_ids: tuple[str, ...] = (),
        allowed_station_ids: tuple[str, ...] = (),
    ) -> bool:
        item_lines = set(item.get("line_ids", []))
        item_stations = set(item.get("station_ids", []))
        if allowed_line_ids and item_lines and item_lines.isdisjoint(allowed_line_ids):
            return False
        if allowed_station_ids and item_stations and item_stations.isdisjoint(allowed_station_ids):
            return False
        return True

    def accessible_cases(
        self,
        role: str,
        allowed_line_ids: tuple[str, ...] = (),
        allowed_station_ids: tuple[str, ...] = (),
    ) -> list[dict[str, Any]]:
        return [
            item
            for item in self.cases
            if self.can_access(item.get("confidentiality", "INTERNAL"), role)
            and self._within_scope(item, allowed_line_ids, allowed_station_ids)
        ]

    def accessible_documents(self, role: str) -> list[dict[str, Any]]:
        return [item for item in self.documents if self.can_access(item.get("confidentiality", "INTERNAL"), role)]

    def get_case(
        self,
        case_id: str,
        role: str,
        allowed_line_ids: tuple[str, ...] = (),
        allowed_station_ids: tuple[str, ...] = (),
    ) -> dict[str, Any] | None:
        item = self.cases_by_id.get(case_id)
        if (
            not item
            or not self.can_access(item.get("confidentiality", "INTERNAL"), role)
            or not self._within_scope(item, allowed_line_ids, allowed_station_ids)
        ):
            return None
        return item

    def get_document(self, version_id: str, role: str) -> dict[str, Any] | None:
        item = self.documents_by_version.get(version_id)
        if not item or not self.can_access(item.get("confidentiality", "INTERNAL"), role):
            return None
        return item

    def current_document_version(self, document_id: str, role: str) -> dict[str, Any] | None:
        versions = self.documents_by_id.get(document_id, [])
        current = [item for item in versions if item["status"] == "EFFECTIVE" and self.can_access(item.get("confidentiality", "INTERNAL"), role)]
        return sorted(current, key=lambda item: item["effective_from"], reverse=True)[0] if current else None

    def meta(self) -> dict[str, Any]:
        return {
            "banner": self.manifest["banner"],
            "counts": self.manifest["counts"],
            "products": self.master["products"],
            "lines": self.master["lines"],
            "stations": self.master["stations"],
            "failure_codes": self.master["failure_codes"],
            "material_lots": self.master["material_lots"],
            "software_versions": self.master["software_versions"],
            "roles": ["PRODUCT_ENGINEER", "PROCESS_ENGINEER", "QUALITY_ENGINEER", "FA_ENGINEER", "LINE_LEAD"],
        }

    def dashboard_stats(self) -> dict[str, Any]:
        recent = self.observations[-48:]
        station_04 = [item for item in recent if item["station_id"] == "ST-04"]
        latest = station_04[-1] if station_04 else None
        peak = max(station_04, key=lambda item: item["failure_rate"]) if station_04 else None
        trend = [
            {
                "time": item["window_start"],
                "failure_rate": item["failure_rate"],
                "baseline": item["baseline_failure_rate"],
            }
            for item in station_04[-12:]
        ]
        return {
            "latest_fpy": latest["first_pass_yield"] if latest else None,
            "peak_failure_rate": peak["failure_rate"] if peak else None,
            "peak_time": peak["window_start"] if peak else None,
            "active_cases": sum(1 for item in self.cases if item["status"] != "RETIRED"),
            "published_cases": sum(1 for item in self.cases if item["status"] == "PUBLISHED"),
            "effective_documents": sum(1 for item in self.documents if item["status"] == "EFFECTIVE"),
            "trend": trend,
        }
