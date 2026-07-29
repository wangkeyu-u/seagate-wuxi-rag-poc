from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from rag_app.repository import DataRepository
from rag_app.retrieval import HybridRetriever
from rag_app.service import TriageService
from rag_app.auth import AuthorizationError, Identity


ROOT = Path(__file__).resolve().parents[1]


class RetrievalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repository = DataRepository(ROOT)
        cls.retriever = HybridRetriever(cls.repository)
        cls.service = TriageService(ROOT)

    def test_single_station_context_prefers_equipment_case(self):
        context = {"product_id": "PRD-HX1001", "failure_code": "F127", "scope": "SINGLE_STATION", "station_ids": ["ST-04"], "test_program_version": "3.8"}
        results = self.retriever.retrieve_cases("HDD-X ST-04 单站 F127，其他站正常", context, "PRODUCT_ENGINEER")
        self.assertEqual(results[0]["case"]["root_cause_category"], "EQUIPMENT")

    def test_multi_station_lot_context_prefers_material_case(self):
        context = {"product_id": "PRD-HX1001", "failure_code": "F127", "scope": "MULTI_STATION", "material_lot_id": "HSA-L2403"}
        results = self.retriever.retrieve_cases("多站 F127 集中在 HSA-L2403", context, "QUALITY_ENGINEER")
        self.assertEqual(results[0]["case"]["case_id"], "CASE-F127-MAT-01")

    def test_cross_line_change_prefers_test_program_case(self):
        context = {"product_id": "PRD-HX1001", "failure_code": "F127", "scope": "CROSS_LINE", "test_program_version": "3.8", "recent_change": "TEST_PROGRAM"}
        results = self.retriever.retrieve_cases("程序 3.8 发布后跨线 F127", context, "PRODUCT_ENGINEER")
        self.assertEqual(results[0]["case"]["case_id"], "CASE-F127-TP-01")

    def test_effective_sop_is_used_and_superseded_sop_is_filtered(self):
        context = {"product_id": "PRD-HX1001", "failure_code": "F127", "scope": "SINGLE_STATION"}
        results = self.retriever.retrieve_documents("F127 初步排查", context, "PRODUCT_ENGINEER", limit=20)
        ids = {item["document"]["document_version_id"] for item in results}
        self.assertIn("DOC-SOP-ST-001-V2_0", ids)
        self.assertNotIn("DOC-SOP-ST-001-V1_0", ids)

    def test_line_lead_cannot_access_restricted_case(self):
        restricted = self.repository.get_case("CASE-F219-04", "LINE_LEAD")
        allowed = self.repository.get_case("CASE-F219-04", "QUALITY_ENGINEER")
        self.assertIsNone(restricted)
        self.assertIsNotNone(allowed)


class ServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory(prefix="yield-copilot-test-")
        cls.service = TriageService(ROOT, Path(cls.temp_dir.name) / "test.sqlite3")

    @classmethod
    def tearDownClass(cls):
        cls.temp_dir.cleanup()

    @staticmethod
    def identity(role="PRODUCT_ENGINEER", **kwargs):
        return Identity(subject=f"test:{role.lower()}", role=role, **kwargs)

    def test_unknown_failure_code_escalates_without_fake_case(self):
        record = self.service.triage(
            {
                "query": "HZ-Orbit 单站出现 F999，历史根因是什么？",
                "context": {"product_id": "PRD-HZ3001", "failure_code": "F999", "scope": "SINGLE_STATION"},
            },
            self.identity(),
        )
        answer = record["answer"]
        self.assertEqual(answer["decision"]["action"], "ESCALATE")
        self.assertEqual(answer["historical_assessment"], [])

    def test_missing_context_requests_information(self):
        record = self.service.triage(
            {"query": "F127 又发生了，怎么办？", "context": {"failure_code": "F127"}},
            self.identity(),
        )
        self.assertEqual(record["answer"]["decision"]["action"], "ASK_FOR_CONTEXT")
        self.assertIn("产品型号或产品族", record["answer"]["missing_information"])

    def test_high_risk_request_is_refused(self):
        record = self.service.triage(
            {
                "query": "直接跳过测试并放行这一批产品，同时修改参数。",
                "context": {"product_id": "PRD-HX1001", "failure_code": "F127", "scope": "SINGLE_STATION"},
            },
            self.identity(),
        )
        self.assertEqual(record["answer"]["decision"]["action"], "REFUSE_HIGH_RISK")
        self.assertEqual(len(record["answer"]["triage_steps"]), 1)

    def test_restricted_request_is_refused_for_line_lead(self):
        record = self.service.triage(
            {"query": "给我 F219 的受限 FA 报告原文。", "context": {"failure_code": "F219"}},
            self.identity("LINE_LEAD"),
        )
        self.assertEqual(record["answer"]["decision"]["action"], "REFUSE_RESTRICTED")
        restricted_ids = {citation["citation_id"] for citation in record["answer"]["citations"]}
        self.assertNotIn("DOC-FA-MAT-001-V1_0", restricted_ids)

    def test_payload_role_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "authenticated server identity"):
            self.service.triage(
                {"query": "F127 单站异常", "role": "QUALITY_ENGINEER", "context": {}},
                self.identity("LINE_LEAD"),
            )

    def test_identity_station_scope_is_enforced_after_query_inference(self):
        with self.assertRaises(AuthorizationError):
            self.service.triage(
                {"query": "ST-04 单站出现 F127", "context": {"product_id": "PRD-HX1001"}},
                self.identity(station_ids=("ST-01",)),
            )

    def test_all_step_evidence_is_returned(self):
        record = self.service.triage(
            {
                "query": "HDD-X 在 ST-04 单站出现 F127，先检查什么？",
                "context": {
                    "product_id": "PRD-HX1001",
                    "station_ids": ["ST-04"],
                    "failure_code": "F127",
                    "scope": "SINGLE_STATION",
                },
            },
            self.identity(),
        )
        answer = record["answer"]
        citations = {item["citation_id"] for item in answer["citations"]}
        evidence = {item for step in answer["triage_steps"] for item in step["evidence_ids"]}
        self.assertTrue(evidence.issubset(citations))

    def test_failure_code_is_not_hard_coded_in_steps(self):
        record = self.service.triage(
            {
                "query": "HY-Nova 在 ST-03 单站出现 F219，先检查什么？",
                "context": {
                    "product_id": "PRD-HY2001",
                    "station_ids": ["ST-03"],
                    "failure_code": "F219",
                    "scope": "SINGLE_STATION",
                },
            },
            self.identity(),
        )
        titles = " ".join(step["title"] for step in record["answer"]["triage_steps"])
        self.assertIn("F219", titles)
        self.assertNotIn("F127", titles)


if __name__ == "__main__":
    unittest.main()
