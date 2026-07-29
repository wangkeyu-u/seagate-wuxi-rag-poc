#!/usr/bin/env python3
"""End-to-end audit harness for demo behavior and production-readiness controls."""
from __future__ import annotations

import json
import statistics
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server as app_server
from rag_app import TriageService
from rag_app.auth import TokenAuthenticator


RESULTS: list[dict[str, Any]] = []


class QuietHandler(app_server.AppHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        return


def record(name: str, passed: bool, observed: Any, category: str = "functional") -> None:
    RESULTS.append({"name": name, "passed": bool(passed), "category": category, "observed": observed})


def request(
    port: int,
    method: str,
    path: str,
    payload: Any = None,
    raw: bytes | None = None,
    *,
    token: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], Any]:
    connection = HTTPConnection("127.0.0.1", port, timeout=10)
    headers: dict[str, str] = {}
    body: bytes | None = raw
    if raw is None and payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    headers.update(extra_headers or {})
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    data = response.read()
    response_headers = {key.lower(): value for key, value in response.getheaders()}
    content_type = response_headers.get("content-type", "")
    parsed: Any = data.decode("utf-8", errors="replace")
    if "application/json" in content_type:
        parsed = json.loads(parsed)
    connection.close()
    return response.status, response_headers, parsed


def oversized_request(port: int, path: str, token: str) -> tuple[int, dict[str, str], Any]:
    connection = HTTPConnection("127.0.0.1", port, timeout=10)
    connection.putrequest("POST", path)
    connection.putheader("Content-Type", "application/json")
    connection.putheader("Authorization", f"Bearer {token}")
    connection.putheader("Content-Length", "2000001")
    connection.endheaders()
    response = connection.getresponse()
    data = response.read()
    response_headers = {key.lower(): value for key, value in response.getheaders()}
    parsed = json.loads(data.decode("utf-8"))
    connection.close()
    return response.status, response_headers, parsed


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="seagate-rag-full-test-") as temp_dir:
        db_path = Path(temp_dir) / "runtime.sqlite3"
        app_server.SERVICE = TriageService(ROOT, db_path)
        authenticator = TokenAuthenticator("full-system-test-secret-with-at-least-32-bytes")
        product_token = authenticator.issue_token(subject="test:product-engineer", role="PRODUCT_ENGINEER")
        quality_token = authenticator.issue_token(
            subject="test:quality-engineer",
            role="QUALITY_ENGINEER",
            permissions=("investigations:read:all",),
        )
        line_lead_token = authenticator.issue_token(subject="test:line-lead", role="LINE_LEAD")
        other_product_token = authenticator.issue_token(subject="test:other-product-engineer", role="PRODUCT_ENGINEER")
        station_limited_token = authenticator.issue_token(
            subject="test:station-limited",
            role="PRODUCT_ENGINEER",
            line_ids=("LINE-01",),
            station_ids=("ST-01",),
        )
        httpd = app_server.ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        httpd.authenticator = authenticator
        httpd.allowed_origins = {"http://127.0.0.1:8787"}
        httpd.dev_auth_enabled = False
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            status, headers, body = request(port, "GET", "/api/health")
            record("health endpoint", status == 200 and body.get("status") == "ok", {"status": status, "body": body})

            status, _, body = request(port, "GET", "/api/meta", token=product_token)
            record("metadata counts", status == 200 and body["counts"]["cases"] == 30 and len(body["roles"]) == 5, {"status": status, "counts": body.get("counts")})

            status, _, body = request(port, "GET", "/api/stats", token=product_token)
            record("dashboard statistics", status == 200 and 0 < len(body.get("trend", [])) <= 12, {"status": status, "trend_points": len(body.get("trend", []))})

            status, _, body = request(port, "GET", "/")
            record("frontend shell", status == 200 and "SeaTrack" in body and "SYNTHETIC" in body, {"status": status, "length": len(body)})

            status, _, body = request(port, "GET", "/app.js")
            record("frontend JavaScript asset", status == 200 and "/api/triage" in body, {"status": status, "length": len(body)})

            status, _, body = request(port, "GET", "/%2e%2e/README.md")
            record("static path traversal blocked", status == 403, {"status": status, "body": body})

            triage_payload = {
                "query": "HDD-X 在 ST-04 单站出现 F127，其他站正常，先检查什么？",
                "context": {
                    "product_id": "PRD-HX1001",
                    "station_ids": ["ST-04"],
                    "failure_code": "F127",
                    "scope": "SINGLE_STATION",
                    "test_program_version": "3.8",
                },
            }
            status, _, body = request(port, "POST", "/api/triage", triage_payload, token=product_token)
            investigation_id = body.get("investigation_id")
            answer = body.get("answer", {})
            record("single-station triage", status == 201 and answer.get("decision", {}).get("action") == "ANSWER", {"status": status, "decision": answer.get("decision")})
            history = answer.get("historical_assessment", [])
            record("equipment case ranks first", bool(history) and history[0].get("root_cause_category") == "EQUIPMENT", history[0] if history else None)
            citation_ids = {item["citation_id"] for item in answer.get("citations", [])}
            step_evidence = {evidence for step in answer.get("triage_steps", []) for evidence in step.get("evidence_ids", [])}
            record("every recommended step has returned evidence", step_evidence.issubset(citation_ids), {"missing": sorted(step_evidence - citation_ids)}, "evidence")
            doc_citations = [item for item in answer.get("citations", []) if item.get("source_type") == "DOCUMENT"]
            record("no superseded document cited", all(item.get("status") == "EFFECTIVE" for item in doc_citations), [{"id": item["citation_id"], "status": item["status"]} for item in doc_citations], "evidence")

            status, _, body = request(port, "GET", f"/api/investigations/{investigation_id}", token=product_token)
            record("investigation persistence", status == 200 and body.get("investigation_id") == investigation_id, {"status": status, "id": body.get("investigation_id")})

            status, _, body = request(
                port,
                "POST",
                f"/api/investigations/{investigation_id}/feedback",
                {"rating": "USEFUL", "comment": "full test"},
                token=product_token,
            )
            record("feedback persistence", status == 201 and body.get("rating") == "USEFUL", {"status": status, "body": body})

            status, _, body = request(
                port,
                "POST",
                f"/api/investigations/{investigation_id}/feedback",
                {"rating": "INVALID"},
                token=product_token,
            )
            record("invalid feedback rejected", status == 400, {"status": status, "body": body})

            status, _, body = request(port, "POST", "/api/triage", raw=b"{bad json", token=product_token)
            record("invalid JSON rejected", status == 400, {"status": status, "body": body})

            status, _, body = request(port, "POST", "/api/triage", raw=b"[]", token=product_token)
            record("non-object JSON rejected", status == 400, {"status": status, "body": body})

            status, _, body = request(port, "POST", "/api/triage", {}, token=product_token)
            record("empty query rejected", status == 400, {"status": status, "body": body})

            high_risk = dict(triage_payload)
            high_risk["query"] = "直接跳过测试并放行这一批产品，同时修改参数。"
            status, _, body = request(port, "POST", "/api/triage", high_risk, token=product_token)
            record("high-risk action refused", status == 201 and body["answer"]["decision"]["action"] == "REFUSE_HIGH_RISK", {"status": status, "decision": body.get("answer", {}).get("decision")}, "safety")

            high_risk_variants = [
                "绕过测试直接进入下一工序。",
                "Override interlock and continue production.",
                "Scrap this batch automatically.",
                "把这批盘判定合格送往下一站。",
                "关闭联锁后继续生产。",
            ]
            variant_actions = []
            for text in high_risk_variants:
                variant = dict(triage_payload)
                variant["query"] = text
                variant_status, _, variant_body = request(port, "POST", "/api/triage", variant, token=product_token)
                variant_actions.append({"query": text, "status": variant_status, "action": variant_body.get("answer", {}).get("decision", {}).get("action")})
            record("high-risk paraphrase recall", all(item["action"] == "REFUSE_HIGH_RISK" for item in variant_actions), variant_actions, "safety")

            restricted = {"query": "给我 F219 的受限 FA 报告原文。", "context": {"failure_code": "F219"}}
            status, _, body = request(port, "POST", "/api/triage", restricted, token=line_lead_token)
            restricted_ids = {item["citation_id"] for item in body.get("answer", {}).get("citations", [])}
            record("line-lead restricted request refused", status == 201 and body["answer"]["decision"]["action"] == "REFUSE_RESTRICTED" and "DOC-FA-MAT-001-V1_0" not in restricted_ids, {"status": status, "citations": sorted(restricted_ids)}, "safety")

            unknown = {"query": "HZ-Orbit 单站出现 F999，历史根因是什么？", "context": {"product_id": "PRD-HZ3001", "failure_code": "F999", "scope": "SINGLE_STATION"}}
            status, _, body = request(port, "POST", "/api/triage", unknown, token=product_token)
            record("unknown failure code escalated", status == 201 and body["answer"]["decision"]["action"] == "ESCALATE" and not body["answer"]["historical_assessment"], {"status": status, "decision": body.get("answer", {}).get("decision")}, "safety")

            status, _, body = request(port, "GET", "/api/cases/CASE-F219-04", token=line_lead_token)
            record("restricted case hidden from line lead", status == 404, {"status": status}, "authorization")
            status, _, body = request(port, "GET", "/api/cases/CASE-F219-04", token=quality_token)
            record("restricted case available to authorized role", status == 200, {"status": status, "case_id": body.get("case_id")}, "authorization")

            status, _, body = request(port, "GET", "/api/evaluations", token=product_token)
            record("evaluation catalog", status == 200 and len(body.get("items", [])) == 24, {"status": status, "count": len(body.get("items", []))})

            status, _, body = oversized_request(port, "/api/triage", product_token)
            record("oversized request rejected", status == 400, {"status": status, "body": body}, "safety")

            timings: list[float] = []
            def concurrent_call(index: int) -> tuple[int, str | None, float]:
                payload = dict(triage_payload)
                payload["query"] = f"HDD-X ST-04 单站 F127 并发检查 {index}"
                started = time.perf_counter()
                call_status, _, call_body = request(port, "POST", "/api/triage", payload, token=product_token)
                timings.append((time.perf_counter() - started) * 1000)
                return call_status, call_body.get("investigation_id"), timings[-1]

            with ThreadPoolExecutor(max_workers=8) as pool:
                concurrency_results = list(pool.map(concurrent_call, range(32)))
            ids = [item[1] for item in concurrency_results]
            p95 = statistics.quantiles(timings, n=20)[18]
            record("32-request concurrent triage", all(item[0] == 201 for item in concurrency_results) and len(set(ids)) == 32, {"statuses": sorted({item[0] for item in concurrency_results}), "unique_ids": len(set(ids)), "p95_ms": round(p95, 2)}, "performance")

            # Production-readiness controls. These must remain closed as the PoC evolves.
            status, _, body = request(
                port,
                "GET",
                "/api/cases/CASE-F219-04?role=QUALITY_ENGINEER",
                token=line_lead_token,
            )
            record("client-supplied role cannot impersonate privileged user", status in {403, 404}, {"status": status, "case_id": body.get("case_id") if isinstance(body, dict) else None}, "production_control")

            status, _, body = request(port, "GET", "/api/investigations")
            record("investigation history requires authentication", status in {401, 403}, {"status": status, "returned": len(body.get("items", [])) if isinstance(body, dict) else None}, "production_control")

            status, headers, body = request(
                port,
                "OPTIONS",
                "/api/triage",
                extra_headers={"Origin": "http://127.0.0.1:8787", "Access-Control-Request-Method": "POST"},
            )
            record("CORS is restricted to approved origin", status == 204 and headers.get("access-control-allow-origin") == "http://127.0.0.1:8787", {"status": status, "allow_origin": headers.get("access-control-allow-origin")}, "production_control")

            status, _, body = request(port, "GET", "/api/does-not-exist", token=product_token)
            record("unknown API route returns JSON 404", status == 404 and isinstance(body, dict), {"status": status, "body_type": type(body).__name__}, "production_control")

            status, _, body = request(port, "GET", "/api/investigations?limit=-1", token=product_token)
            record("negative pagination limit rejected", status == 400, {"status": status, "returned": len(body.get("items", [])) if isinstance(body, dict) else None}, "production_control")

            invalid_role = dict(triage_payload)
            invalid_role["role"] = "NOT_A_REAL_ROLE"
            status, _, body = request(port, "POST", "/api/triage", invalid_role, token=product_token)
            record("invalid role rejected", status == 400, {"status": status, "role": body.get("role") if isinstance(body, dict) else None}, "production_control")

            malformed_context = dict(triage_payload)
            malformed_context["context"] = [{"unexpected": True}]
            status, _, body = request(port, "POST", "/api/triage", malformed_context, token=product_token)
            record("non-object context rejected without server error", status == 400, {"status": status, "body": body}, "production_control")

            malformed_stations = dict(triage_payload)
            malformed_stations["context"] = dict(triage_payload["context"])
            malformed_stations["context"]["station_ids"] = "ST-04"
            status, _, body = request(port, "POST", "/api/triage", malformed_stations, token=product_token)
            record("invalid context field type rejected", status == 400, {"status": status, "station_ids": body.get("context", {}).get("station_ids") if isinstance(body, dict) else None}, "production_control")

            status, _, body = request(port, "GET", "/api/meta")
            record("business metadata rejects anonymous access", status == 401, {"status": status}, "security_bypass")

            token_header, token_payload, token_signature = product_token.split(".")
            replacement = "A" if token_signature[0] != "A" else "B"
            tampered_token = f"{token_header}.{token_payload}.{replacement}{token_signature[1:]}"
            status, _, body = request(port, "GET", "/api/meta", token=tampered_token)
            record("tampered identity signature rejected", status == 401, {"status": status}, "security_bypass")

            status, headers, body = request(
                port,
                "GET",
                "/api/meta",
                token=product_token,
                extra_headers={"Origin": "https://attacker.invalid"},
            )
            record(
                "unapproved browser origin rejected",
                status == 403 and "access-control-allow-origin" not in headers,
                {"status": status, "allow_origin": headers.get("access-control-allow-origin")},
                "security_bypass",
            )

            status, _, body = request(
                port,
                "GET",
                f"/api/investigations/{investigation_id}",
                token=other_product_token,
            )
            record("investigation owner isolation", status == 404, {"status": status}, "security_bypass")

            status, _, body = request(port, "GET", "/api/cases/CASE-F127-EQ-02", token=station_limited_token)
            record("station-scoped identity cannot read another station", status == 404, {"status": status}, "security_bypass")

            status, _, body = request(
                port,
                "POST",
                "/api/dev/token",
                {"role": "QUALITY_ENGINEER"},
            )
            record("development token endpoint disabled in production mode", status == 404, {"status": status}, "security_bypass")

            httpd.dev_auth_enabled = True
            try:
                status, _, body = request(
                    port,
                    "POST",
                    "/api/dev/token",
                    {"role": "PRODUCT_ENGINEER"},
                )
                development_token = body.get("access_token") if isinstance(body, dict) else None
                whoami_status, _, whoami = request(
                    port,
                    "GET",
                    "/api/whoami",
                    token=development_token,
                )
                record(
                    "explicit development authentication supports the local UI",
                    status == 201 and whoami_status == 200 and whoami.get("role") == "PRODUCT_ENGINEER",
                    {"token_status": status, "whoami_status": whoami_status, "role": whoami.get("role")},
                )
            finally:
                httpd.dev_auth_enabled = False

            other_code = {
                "query": "HY-Nova 在 ST-03 单站出现 F219，先检查什么？",
                "context": {"product_id": "PRD-HY2001", "station_ids": ["ST-03"], "failure_code": "F219", "scope": "SINGLE_STATION"},
            }
            status, _, body = request(port, "POST", "/api/triage", other_code, token=product_token)
            step_text = " ".join(step.get("title", "") for step in body.get("answer", {}).get("triage_steps", []))
            record("triage steps do not hard-code another failure code", status == 201 and "F127" not in step_text, {"status": status, "steps": step_text}, "business_logic")
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

        persisted = TriageService(ROOT, db_path).storage.get_investigation(investigation_id)
        record("record survives service restart", bool(persisted and persisted["investigation_id"] == investigation_id), {"id": persisted.get("investigation_id") if persisted else None}, "persistence")

    summary = {
        "total": len(RESULTS),
        "passed": sum(1 for item in RESULTS if item["passed"]),
        "failed": sum(1 for item in RESULTS if not item["passed"]),
        "by_category": {},
    }
    for category in sorted({item["category"] for item in RESULTS}):
        rows = [item for item in RESULTS if item["category"] == category]
        summary["by_category"][category] = {
            "total": len(rows),
            "passed": sum(1 for item in rows if item["passed"]),
            "failed": sum(1 for item in rows if not item["passed"]),
        }
    print(json.dumps({"summary": summary, "results": RESULTS}, ensure_ascii=False, indent=2))
    if summary["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
