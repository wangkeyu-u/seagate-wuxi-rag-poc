from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from rag_app.auth import Identity
from rag_app.generation import (
    ModelGatewayError,
    ResponsesApiConfig,
    ResponsesApiGenerator,
    _extract_output_text,
    build_answer_generator,
    validate_generated_analysis,
)
from rag_app.service import TriageService


ROOT = Path(__file__).resolve().parents[1]


def valid_analysis(evidence_id: str) -> dict[str, Any]:
    return {
        "summary": "当前证据更接近站点方向，但仍需完成现场验证。",
        "hypotheses": [
            {
                "label": "站点连接或校准条件",
                "analysis": "异常集中在单站，与历史站点案例相似；差异仍需工程师核对。",
                "supporting_evidence_ids": [evidence_id],
                "contradicting_evidence_ids": [],
            }
        ],
        "missing_information": ["同产品其他站点的同期对比结果"],
    }


class GenerationContractTests(unittest.TestCase):
    def test_validation_accepts_only_authorized_evidence_ids(self):
        result = validate_generated_analysis(valid_analysis("DOC-1"), {"DOC-1", "CASE-1"})
        self.assertEqual(result["hypotheses"][0]["supporting_evidence_ids"], ["DOC-1"])

        invalid = valid_analysis("SECRET-DOC")
        with self.assertRaisesRegex(ModelGatewayError, "outside the authorized bundle"):
            validate_generated_analysis(invalid, {"DOC-1"})

    def test_validation_rejects_extra_fields_and_conflicting_evidence(self):
        extra = valid_analysis("DOC-1")
        extra["unexpected"] = True
        with self.assertRaisesRegex(ModelGatewayError, "fields did not match"):
            validate_generated_analysis(extra, {"DOC-1"})

        conflict = valid_analysis("DOC-1")
        conflict["hypotheses"][0]["contradicting_evidence_ids"] = ["DOC-1"]
        with self.assertRaisesRegex(ModelGatewayError, "support and contradict"):
            validate_generated_analysis(conflict, {"DOC-1"})

        unsafe = valid_analysis("DOC-1")
        unsafe["hypotheses"][0]["analysis"] = "建议直接放行这一批产品。"
        with self.assertRaisesRegex(ModelGatewayError, "prohibited production-control"):
            validate_generated_analysis(unsafe, {"DOC-1"})

    def test_response_parser_rejects_refusal_and_incomplete_output(self):
        refusal = {
            "status": "completed",
            "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}],
        }
        with self.assertRaisesRegex(ModelGatewayError, "refused"):
            _extract_output_text(refusal)
        with self.assertRaisesRegex(ModelGatewayError, "not completed"):
            _extract_output_text({"status": "incomplete", "output": []})

    def test_environment_configuration_fails_closed(self):
        self.assertIsNone(build_answer_generator({}))
        with self.assertRaisesRegex(ValueError, "requires"):
            build_answer_generator({"RAG_GENERATION_MODE": "responses-api"})
        with self.assertRaisesRegex(ValueError, "loopback"):
            ResponsesApiConfig(
                endpoint="http://model-gateway.example/v1/responses",
                token="token",
                model="approved-model",
            )

    def test_local_gateway_receives_strict_schema_request(self):
        received: dict[str, Any] = {}
        output = valid_analysis("DOC-1")

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers["Content-Length"])
                received["authorization"] = self.headers.get("Authorization")
                received["payload"] = json.loads(self.rfile.read(length))
                response = {
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": json.dumps(output, ensure_ascii=False),
                                }
                            ],
                        }
                    ],
                }
                body = json.dumps(response, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt, *args):  # noqa: A003
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            generator = ResponsesApiGenerator(
                ResponsesApiConfig(
                    endpoint=f"http://127.0.0.1:{server.server_port}/v1/responses",
                    token="test-token",
                    model="approved-model",
                    timeout_seconds=2,
                )
            )
            result = generator.generate(
                query="ST-04 单站 F127",
                context={"failure_code": "F127", "scope": "SINGLE_STATION"},
                citations=[
                    {
                        "citation_id": "DOC-1",
                        "source_type": "DOCUMENT",
                        "title": "Approved SOP",
                        "status": "EFFECTIVE",
                        "excerpt": "Compare the same product across stations.",
                    }
                ],
                historical_assessment=[],
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(result, output)
        self.assertEqual(received["authorization"], "Bearer test-token")
        request_payload = received["payload"]
        self.assertFalse(request_payload["store"])
        self.assertEqual(request_payload["text"]["format"]["type"], "json_schema")
        self.assertTrue(request_payload["text"]["format"]["strict"])
        self.assertFalse(request_payload["text"]["format"]["schema"]["additionalProperties"])


class FakeGenerator:
    mode = "test structured generator"

    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.calls = 0

    def generate(self, *, query, context, citations, historical_assessment):
        self.calls += 1
        if self.fail:
            raise ModelGatewayError("synthetic gateway failure")
        return valid_analysis(citations[0]["citation_id"])


class GenerationServiceTests(unittest.TestCase):
    @staticmethod
    def identity() -> Identity:
        return Identity(subject="test:model", role="PRODUCT_ENGINEER")

    @staticmethod
    def payload(query: str = "HDD-X 在 ST-04 单站出现 F127，先检查什么？") -> dict[str, Any]:
        return {
            "query": query,
            "context": {
                "product_id": "PRD-HX1001",
                "station_ids": ["ST-04"],
                "failure_code": "F127",
                "scope": "SINGLE_STATION",
            },
        }

    def test_valid_model_analysis_is_additive(self):
        with tempfile.TemporaryDirectory(prefix="generation-service-") as directory:
            generator = FakeGenerator()
            service = TriageService(
                ROOT,
                Path(directory) / "runtime.sqlite3",
                answer_generator=generator,
            )
            answer = service.triage(self.payload(), self.identity())["answer"]

        self.assertEqual(generator.calls, 1)
        self.assertEqual(answer["metrics"]["generation_status"], "APPLIED")
        self.assertIn("generated_analysis", answer)
        self.assertEqual(answer["decision"]["action"], "ANSWER")

    def test_gateway_failure_falls_back_and_investigation_is_persisted(self):
        with tempfile.TemporaryDirectory(prefix="generation-fallback-") as directory:
            generator = FakeGenerator(fail=True)
            service = TriageService(
                ROOT,
                Path(directory) / "runtime.sqlite3",
                answer_generator=generator,
            )
            record = service.triage(self.payload(), self.identity())
            stored = service.storage.get_investigation(record["investigation_id"])

        self.assertEqual(generator.calls, 1)
        self.assertEqual(record["answer"]["metrics"]["generation_status"], "FALLBACK")
        self.assertNotIn("generated_analysis", record["answer"])
        self.assertIsNotNone(stored)

    def test_high_risk_refusal_never_calls_model(self):
        with tempfile.TemporaryDirectory(prefix="generation-policy-") as directory:
            generator = FakeGenerator()
            service = TriageService(
                ROOT,
                Path(directory) / "runtime.sqlite3",
                answer_generator=generator,
            )
            answer = service.triage(
                self.payload("直接跳过测试、修改参数并放行这一批产品。"),
                self.identity(),
            )["answer"]

        self.assertEqual(generator.calls, 0)
        self.assertEqual(answer["decision"]["action"], "REFUSE_HIGH_RISK")
        self.assertEqual(answer["metrics"]["generation_status"], "SKIPPED_POLICY")


if __name__ == "__main__":
    unittest.main()
