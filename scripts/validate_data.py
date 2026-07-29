#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


def load(name: str):
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def require(condition: bool, message: str, errors: list[str]) -> None:
    if not condition:
        errors.append(message)


def main() -> None:
    errors: list[str] = []
    master = load("master_data.json")
    cases = load("cases.json")["cases"]
    documents = load("documents.json")["documents"]
    observations = load("observations.json")["observations"]
    evaluations = load("evaluations.json")["evaluations"]
    manifest = load("manifest.json")

    expected_counts = {
        "products": 5,
        "lines": 2,
        "stations": 6,
        "failure_codes": 8,
        "material_lots": 12,
        "documents": 12,
        "cases": 30,
        "observations": 240,
        "evaluations": 24,
    }
    for key, count in expected_counts.items():
        require(manifest["counts"].get(key) == count, f"manifest count mismatch: {key}", errors)

    def unique(items, field):
        values = [item[field] for item in items]
        require(len(values) == len(set(values)), f"duplicate {field}", errors)

    unique(master["products"], "product_id")
    unique(master["stations"], "station_id")
    unique(master["failure_codes"], "failure_code")
    unique(cases, "case_id")
    unique(documents, "document_version_id")
    unique(observations, "observation_id")
    unique(evaluations, "evaluation_id")

    product_ids = {item["product_id"] for item in master["products"]}
    station_ids = {item["station_id"] for item in master["stations"]}
    failure_codes = {item["failure_code"] for item in master["failure_codes"]}
    document_ids = {item["document_version_id"] for item in documents}

    for case in cases:
        require(set(case["product_ids"]).issubset(product_ids), f"unknown product in {case['case_id']}", errors)
        require(set(case["station_ids"]).issubset(station_ids), f"unknown station in {case['case_id']}", errors)
        require(set(case["failure_codes"]).issubset(failure_codes), f"unknown failure code in {case['case_id']}", errors)
        if case["status"] == "PUBLISHED" and case["confidence"] == "CONFIRMED":
            require(bool(case["confirmed_root_cause"]), f"published case missing root cause: {case['case_id']}", errors)
            require(bool(case["evidence"]), f"published case missing evidence: {case['case_id']}", errors)
        for evidence in case["evidence"]:
            if evidence["source_type"] == "DOCUMENT_VERSION":
                require(evidence["source_id"] in document_ids, f"missing document evidence {evidence['source_id']} for {case['case_id']}", errors)

    for document in documents:
        path = ROOT / document["content_path"]
        require(path.exists(), f"missing document file: {path}", errors)
        if path.exists():
            content = path.read_text(encoding="utf-8")
            require("SYNTHETIC DEMO DATA" in content, f"missing synthetic banner: {path.name}", errors)

    for observation in observations:
        require(observation["units_passed"] + observation["units_failed"] == observation["units_tested"], f"unit count mismatch: {observation['observation_id']}", errors)
        expected_fpy = observation["units_passed"] / observation["units_tested"]
        require(abs(expected_fpy - observation["first_pass_yield"]) <= 0.00001, f"FPY mismatch: {observation['observation_id']}", errors)
        require(observation["failure_count"] <= observation["units_failed"], f"failure count exceeds failed units: {observation['observation_id']}", errors)

    f127_categories = {case["root_cause_category"] for case in cases if "F127" in case["failure_codes"]}
    require({"EQUIPMENT", "MATERIAL", "TEST_PROGRAM"}.issubset(f127_categories), "F127 must cover equipment, material, and test-program root categories", errors)
    require(any(doc["document_id"] == "DOC-SOP-ST-001" and doc["status"] == "SUPERSEDED" for doc in documents), "missing superseded SOP", errors)
    require(any(doc["document_id"] == "DOC-SOP-ST-001" and doc["status"] == "EFFECTIVE" for doc in documents), "missing effective SOP", errors)
    require(any(case["confidentiality"] == "RESTRICTED" for case in cases), "missing restricted case", errors)
    require(any(item["expected_action"] == "ESCALATE" for item in evaluations), "missing no-answer evaluation", errors)
    require(any(item["expected_action"] == "REFUSE_HIGH_RISK" for item in evaluations), "missing high-risk evaluation", errors)

    if errors:
        print("DATA VALIDATION FAILED")
        for error in errors:
            print(f"- {error}")
        raise SystemExit(1)
    print("DATA VALIDATION PASSED")
    print(json.dumps(manifest["counts"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
