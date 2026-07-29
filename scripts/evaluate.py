#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rag_app import TriageService  # noqa: E402
from rag_app.auth import Identity  # noqa: E402


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="yield-copilot-eval-") as temp_dir:
        service = TriageService(ROOT, Path(temp_dir) / "evaluation.sqlite3")
        rows = []
        for evaluation in service.repository.evaluations:
            record = service.triage(
                {
                    "query": evaluation["user_query"],
                    "context": evaluation.get("context", {}),
                },
                Identity(subject=f"evaluation:{evaluation['evaluation_id']}", role=evaluation["user_role"]),
            )
            answer = record["answer"]
            actual_action = answer["decision"]["action"]
            top_case = answer["historical_assessment"][0]["case_id"] if answer["historical_assessment"] else None
            citation_ids = {item["citation_id"] for item in answer["citations"]}
            expected_cases = set(evaluation.get("expected_case_ids", []))
            acceptable_cases = set(evaluation.get("acceptable_case_ids", []))
            forbidden_top = set(evaluation.get("forbidden_top_case_ids", []))
            expected_docs = set(evaluation.get("expected_document_version_ids", []))
            forbidden_docs = set(evaluation.get("forbidden_document_version_ids", []))

            checks = {
                "action": actual_action == evaluation["expected_action"],
                "top_case": (top_case in expected_cases | acceptable_cases) if expected_cases | acceptable_cases else top_case is None,
                "forbidden_top": top_case not in forbidden_top,
                "expected_docs": expected_docs.issubset(citation_ids),
                "forbidden_docs": not bool(forbidden_docs.intersection(citation_ids)),
            }
            passed = all(checks.values())
            rows.append(
                {
                    "evaluation_id": evaluation["evaluation_id"],
                    "passed": passed,
                    "expected_action": evaluation["expected_action"],
                    "actual_action": actual_action,
                    "top_case": top_case,
                    "checks": checks,
                }
            )

    passed_count = sum(1 for row in rows if row["passed"])
    report = {
        "summary": {
            "total": len(rows),
            "passed": passed_count,
            "failed": len(rows) - passed_count,
            "pass_rate": round(passed_count / len(rows), 4) if rows else 0,
        },
        "results": rows,
    }
    output = ROOT / "runtime" / "evaluation_report.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    for row in rows:
        if not row["passed"]:
            print(json.dumps(row, ensure_ascii=False))
    if passed_count != len(rows):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
