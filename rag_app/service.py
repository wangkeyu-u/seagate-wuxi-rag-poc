"""Application orchestration for evidence triage and human investigation flow.

``TriageService`` assembles authorized evidence, applies refusal/escalation
gates, verifies every recommended step has a returned citation, and persists
the investigation.  An optional model gateway can enrich an approved answer,
but it stays inside those gates and cannot alter the deterministic decision.
"""

from __future__ import annotations

import re
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .auth import Identity
from .generation import AnswerGenerator, validate_generated_analysis
from .repository import DataRepository
from .retrieval import HybridRetriever
from .storage import DuplicateInvestigationError, RuntimeStorage
from .validation import enforce_identity_scope, validate_triage_payload


TZ = timezone(timedelta(hours=8))
MIN_ANSWER_CASE_SCORE = 0.36
MIN_ESCALATION_CASE_SCORE = 0.30
ROLE_LABELS = {
    "PRODUCT_ENGINEER": "产品工程师",
    "PROCESS_ENGINEER": "工艺工程师",
    "QUALITY_ENGINEER": "质量工程师",
    "FA_ENGINEER": "失效分析工程师",
    "LINE_LEAD": "线长",
    "ADMIN": "系统管理员",
}


class TriageService:
    def __init__(
        self,
        root: Path,
        runtime_db_path: Path | None = None,
        answer_generator: AnswerGenerator | None = None,
    ):
        self.root = root
        self.storage = RuntimeStorage(runtime_db_path or root / "runtime" / "rag_mvp.sqlite3")
        self.repository = DataRepository(root, self.storage.list_active_source_records())
        self.retriever = HybridRetriever(self.repository)
        self.answer_generator = answer_generator
        self.generation_mode = (
            answer_generator.mode if answer_generator else "deterministic evidence synthesis"
        )

    def triage(self, payload: dict[str, Any], identity: Identity) -> dict[str, Any]:
        started = time.perf_counter()
        # The order is a security invariant: validate input, infer structured
        # context, enforce identity scope, then retrieve already-filtered data.
        query, supplied_context = validate_triage_payload(payload, self.repository)
        role = identity.role
        context = self.retriever.infer_context(query, supplied_context)
        enforce_identity_scope(context, identity)
        action, missing = self._determine_action(query, context, role)
        case_results = self.retriever.retrieve_cases(
            query,
            context,
            role,
            limit=5,
            allowed_line_ids=identity.line_ids,
            allowed_station_ids=identity.station_ids,
        )
        document_results = self.retriever.retrieve_documents(
            query,
            context,
            role,
            limit=7,
            allowed_line_ids=identity.line_ids,
            allowed_station_ids=identity.station_ids,
        )

        if action == "ASK_FOR_CONTEXT" and not context.get("failure_code"):
            case_results = []
        # Weak evidence becomes an explicit escalation instead of a plausible-
        # sounding root cause. Thresholds are demo baselines pending a golden set.
        if action == "ANSWER" and (
            not case_results or case_results[0]["score"] < MIN_ANSWER_CASE_SCORE
        ):
            action = "ESCALATE"
        if context.get("failure_code") and context.get("failure_code") not in self.repository.failure_codes_by_id and not action.startswith("REFUSE"):
            action = "ESCALATE"
            case_results = []
        elif action == "ESCALATE" and (
            not case_results or case_results[0]["score"] < MIN_ESCALATION_CASE_SCORE
        ):
            case_results = []

        created_at = datetime.now(TZ).isoformat(timespec="seconds")
        for _attempt in range(3):
            investigation_id = (
                f"INV-{datetime.now(TZ).strftime('%Y%m%d')}-"
                f"{uuid.uuid4().hex.upper()}-{secrets.token_hex(8).upper()}"
            )
            answer = self._compose_answer(
                investigation_id=investigation_id,
                query=query,
                context=context,
                role=role,
                action=action,
                missing=missing,
                case_results=case_results,
                document_results=document_results,
                allowed_line_ids=identity.line_ids,
                allowed_station_ids=identity.station_ids,
            )
            generation_status = self._apply_generated_analysis(
                answer=answer,
                query=query,
                context=context,
                action=action,
            )
            answer["metrics"] = {
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                "cases_considered": len(self.repository.accessible_cases(role, identity.line_ids, identity.station_ids)),
                "documents_considered": len(
                    self.repository.accessible_documents(role, identity.line_ids, identity.station_ids)
                ),
                "retrieval_mode": "hybrid: lexical + hashed-vector + structured context",
                "generation_mode": self.generation_mode,
                "generation_status": generation_status,
            }
            record = {
                "investigation_id": investigation_id,
                "created_at": created_at,
                "subject": identity.subject,
                "role": role,
                "query": query,
                "context": context,
                "answer": answer,
            }
            try:
                self.storage.save_investigation(record)
            except DuplicateInvestigationError:
                continue
            return record
        raise RuntimeError("could not allocate a unique investigation id")

    def _apply_generated_analysis(
        self,
        *,
        answer: dict[str, Any],
        query: str,
        context: dict[str, Any],
        action: str,
    ) -> str:
        """Apply optional model analysis without weakening deterministic gates.

        The model is deliberately skipped for refusals, missing context and weak
        evidence.  Any transport, refusal, parsing or citation-validation error
        falls back to the already-composed answer and never blocks persistence.
        """

        if self.answer_generator is None:
            return "DISABLED"
        if action != "ANSWER":
            return "SKIPPED_POLICY"
        citations = answer.get("citations", [])
        if not citations:
            return "SKIPPED_NO_EVIDENCE"
        try:
            generated = self.answer_generator.generate(
                query=query,
                context=context,
                citations=citations,
                historical_assessment=answer.get("historical_assessment", []),
            )
            allowed_ids = {str(item["citation_id"]) for item in citations}
            answer["generated_analysis"] = validate_generated_analysis(generated, allowed_ids)
        except Exception:
            # A model is an optional dependency, not part of the safety or
            # availability boundary.  Do not expose upstream error details.
            answer["warnings"].append(
                "模型分析不可用或未通过证据校验；当前结果已自动使用确定性证据合成。"
            )
            return "FALLBACK"
        answer["warnings"].append(
            "候选假设由模型基于当前授权证据生成；证据 ID 已校验，但语义仍需工程师复核。"
        )
        return "APPLIED"

    def _determine_action(self, query: str, context: dict[str, Any], role: str) -> tuple[str, list[str]]:
        if context.get("high_risk_request"):
            return "REFUSE_HIGH_RISK", []
        if role == "LINE_LEAD" and any(term in query.lower() for term in ["受限", "fa 结论", "fa报告", "原文"]):
            return "REFUSE_RESTRICTED", []
        missing = []
        if not context.get("failure_code"):
            missing.append("Failure Code 或明确失败现象")
        if not context.get("product_id"):
            missing.append("产品型号或产品族")
        if not context.get("scope") and not context.get("station_ids"):
            missing.append("异常范围（单站、多站或跨线）")
        if missing:
            return "ASK_FOR_CONTEXT", missing
        return "ANSWER", []

    def _compose_answer(
        self,
        *,
        investigation_id: str,
        query: str,
        context: dict[str, Any],
        role: str,
        action: str,
        missing: list[str],
        case_results: list[dict[str, Any]],
        document_results: list[dict[str, Any]],
        allowed_line_ids: tuple[str, ...],
        allowed_station_ids: tuple[str, ...],
    ) -> dict[str, Any]:
        facts = self._facts(context)
        historical = self._historical(case_results, action)
        steps = self._triage_steps(context, document_results, action)
        required_evidence = {
            evidence_id
            for step in steps
            for evidence_id in step.get("evidence_ids", [])
        }
        citations = self._build_citations(
            case_results,
            document_results,
            role,
            required_evidence,
            allowed_line_ids=allowed_line_ids,
            allowed_station_ids=allowed_station_ids,
        )
        escalation = self._escalation(action, context)
        confidence = self._confidence(action, case_results, context)
        headline = self._headline(action, context, case_results)
        warnings = [
            "Failure Code 描述失败类型，不等同于最终根因。",
            "所有数据均为虚构演示；系统不执行设备、参数、隔离、放行或报废操作。",
        ]
        if any(result["case"]["status"] != "PUBLISHED" for result in case_results[:3]):
            warnings.append("部分召回案例尚未审核，不能作为确认根因的唯一依据。")
        if action == "REFUSE_RESTRICTED":
            warnings.insert(0, "当前角色无权获取受限 FA 内容；系统未将受限文档放入回答上下文。")
        if action == "REFUSE_HIGH_RISK":
            warnings.insert(0, "该请求涉及高风险生产决策，系统不会执行或代替授权人员批准。")

        return {
            "investigation_id": investigation_id,
            "status": "TRIAGE",
            "decision": {"action": action, "confidence": confidence, "headline": headline},
            "context": context,
            "role": {"code": role, "label": ROLE_LABELS.get(role, role)},
            "known_facts": facts,
            "historical_assessment": historical,
            "triage_steps": steps,
            "missing_information": missing,
            "escalation": escalation,
            "citations": citations,
            "warnings": warnings,
            "summary_markdown": self._summary_markdown(headline, facts, historical, steps, missing, escalation, action),
        }

    def _facts(self, context: dict[str, Any]) -> list[dict[str, str]]:
        facts = []
        mappings = [
            ("product_id", "产品"), ("failure_code", "失败代码"), ("scope", "异常范围"),
            ("material_lot_id", "物料批次"), ("test_program_version", "测试程序"),
            ("firmware_version", "固件版本"), ("recent_change", "近期变更"),
        ]
        for key, label in mappings:
            value = context.get(key)
            if value:
                facts.append({"label": label, "value": str(value), "source": "用户输入或字段识别"})
        if context.get("station_ids"):
            facts.append({"label": "测试站", "value": ", ".join(context["station_ids"]), "source": "用户输入或字段识别"})
        if context.get("line_ids"):
            facts.append({"label": "产线", "value": ", ".join(context["line_ids"]), "source": "用户输入或字段识别"})
        return facts

    def _historical(self, results: list[dict[str, Any]], action: str) -> list[dict[str, Any]]:
        if action in {"REFUSE_HIGH_RISK", "REFUSE_RESTRICTED"}:
            return []
        output = []
        for result in results[:3]:
            case = result["case"]
            output.append(
                {
                    "case_id": case["case_id"],
                    "title": case["title"],
                    "score": result["score"],
                    "score_percent": round(result["score"] * 100),
                    "root_cause_category": case["root_cause_category"],
                    "root_cause": case.get("confirmed_root_cause"),
                    "status": case["status"],
                    "confidence": case["confidence"],
                    "matched_on": result["matched_on"],
                    "differences": result["differences"],
                    "applicable_conditions": case["applicable_conditions"],
                    "non_applicable_conditions": case["non_applicable_conditions"],
                }
            )
        return output

    def _triage_steps(self, context: dict[str, Any], documents: list[dict[str, Any]], action: str) -> list[dict[str, Any]]:
        if action in {"REFUSE_HIGH_RISK", "REFUSE_RESTRICTED"}:
            return [
                {
                    "sequence": 1,
                    "title": "进入正式审批或权限申请流程",
                    "purpose": "确保高风险动作和受限信息由授权角色处理",
                    "owner": "质量工程师或系统管理员",
                    "risk": "HIGH",
                    "evidence_ids": ["DOC-QA-ESC-001-V1_0"],
                    "basis": "APPROVED_PROCESS",
                }
            ]
        scope = context.get("scope")
        failure_code = context.get("failure_code") or "当前失败模式"
        steps = [
            {
                "sequence": 1,
                "title": "确认异常影响范围",
                "purpose": "区分单站、多站、单线或跨线模式",
                "owner": "产品工程师",
                "risk": "LOW",
                "evidence_ids": ["DOC-SOP-ST-001-V2_0"],
                "basis": "APPROVED_SOP",
            }
        ]
        if scope == "SINGLE_STATION":
            steps.extend(
                [
                    {
                        "sequence": 2,
                        "title": f"比较同产品在其他测试站的 {failure_code} 分布",
                        "purpose": "验证异常是否为站点特有",
                        "owner": "产品工程师",
                        "risk": "LOW",
                        "evidence_ids": ["DOC-SOP-ST-001-V2_0"],
                        "basis": "APPROVED_SOP",
                    },
                    {
                        "sequence": 3,
                        "title": "审阅该站点近期维护、连接和校准记录",
                        "purpose": "验证设备方向，同时避免直接把相关性当根因",
                        "owner": "工艺工程师",
                        "risk": "LOW",
                        "evidence_ids": ["DOC-MAINT-CAL-001-V1_0", "DOC-FA-EQP-001-V1_0"],
                        "basis": "APPROVED_GUIDE_AND_HISTORY",
                    },
                ]
            )
        elif scope == "MULTI_STATION":
            steps.extend(
                [
                    {
                        "sequence": 2,
                        "title": "按物料批次比较多站失败分布",
                        "purpose": "判断异常是否集中于同一 HSA 批次",
                        "owner": "工艺工程师",
                        "risk": "LOW",
                        "evidence_ids": ["DOC-HSA-LOT-001-V1_0", "DOC-SOP-ST-001-V2_0"],
                        "basis": "APPROVED_SOP",
                    },
                    {
                        "sequence": 3,
                        "title": "核对不同站点的软件和设备共同条件",
                        "purpose": "排除仅凭批次相关性形成的过早结论",
                        "owner": "产品工程师",
                        "risk": "LOW",
                        "evidence_ids": ["DOC-SOP-ST-001-V2_0"],
                        "basis": "APPROVED_SOP",
                    },
                ]
            )
        elif scope == "CROSS_LINE":
            steps.extend(
                [
                    {
                        "sequence": 2,
                        "title": "对齐异常时间与测试程序发布时间",
                        "purpose": "判断跨线异常是否共享同一程序变更窗口",
                        "owner": "产品工程师",
                        "risk": "LOW",
                        "evidence_ids": ["DOC-CHG-TP38-V1_0", "DOC-SOP-ST-001-V2_0"],
                        "basis": "CHANGE_RECORD_AND_SOP",
                    },
                    {
                        "sequence": 3,
                        "title": "比较不同物料与设备下的同版本表现",
                        "purpose": "验证程序版本是否为共同因素",
                        "owner": "产品工程师",
                        "risk": "LOW",
                        "evidence_ids": ["DOC-FA-TP-001-V1_0"],
                        "basis": "HISTORICAL_CASE",
                    },
                ]
            )
        else:
            steps.append(
                {
                    "sequence": 2,
                    "title": "补充产品、站点和异常范围",
                    "purpose": "在排查前建立足够上下文",
                    "owner": "发起人",
                    "risk": "LOW",
                    "evidence_ids": ["DOC-SOP-ST-001-V2_0"],
                    "basis": "APPROVED_SOP",
                }
            )
        steps.append(
            {
                "sequence": len(steps) + 1,
                "title": "记录检查结果并由工程师决定是否升级",
                "purpose": "形成可追溯闭环；系统不自动确认根因",
                "owner": "产品工程师 / 质量工程师",
                "risk": "LOW",
                "evidence_ids": ["DOC-QA-ESC-001-V1_0"],
                "basis": "APPROVED_PROCESS",
            }
        )
        return steps

    def _build_citations(
        self,
        cases: list[dict[str, Any]],
        docs: list[dict[str, Any]],
        role: str,
        required_evidence: set[str] | None = None,
        *,
        allowed_line_ids: tuple[str, ...] = (),
        allowed_station_ids: tuple[str, ...] = (),
    ) -> list[dict[str, Any]]:
        citations: list[dict[str, Any]] = []
        seen: set[str] = set()
        enriched_docs = list(docs)
        for version_id in sorted(required_evidence or set()):
            if any(result["document"]["document_version_id"] == version_id for result in enriched_docs):
                continue
            document = self.repository.get_document(
                version_id,
                role,
                allowed_line_ids,
                allowed_station_ids,
            )
            if document and self.repository.is_document_effective(document):
                enriched_docs.append({"document": document, "score": 1.0, "matched_on": ["推荐步骤依据"]})
        for result in enriched_docs:
            doc = result["document"]
            version_id = doc["document_version_id"]
            if version_id in seen:
                continue
            seen.add(version_id)
            citations.append(
                {
                    "citation_id": version_id,
                    "source_type": "DOCUMENT",
                    "title": doc["title"],
                    "version": doc["version"],
                    "status": doc["status"],
                    "document_type": doc["document_type"],
                    "locator": "document",
                    "excerpt": self._clean_excerpt(doc["summary"]),
                    "score": result["score"],
                    "uri": f"/api/documents/{version_id}",
                }
            )
        for result in cases[:3]:
            case = result["case"]
            if case["case_id"] in seen:
                continue
            seen.add(case["case_id"])
            citations.append(
                {
                    "citation_id": case["case_id"],
                    "source_type": "CASE",
                    "title": case["title"],
                    "version": "published" if case["status"] == "PUBLISHED" else "draft",
                    "status": case["status"],
                    "document_type": "HISTORICAL_CASE",
                    "locator": "root-cause-and-applicability",
                    "excerpt": self._clean_excerpt(case["summary"]),
                    "score": result["score"],
                    "uri": f"/api/cases/{case['case_id']}",
                }
            )
        return citations

    @staticmethod
    def _clean_excerpt(text: str, limit: int = 220) -> str:
        clean = re.sub(r"\s+", " ", text).strip()
        return clean if len(clean) <= limit else clean[: limit - 1] + "…"

    @staticmethod
    def _confidence(action: str, cases: list[dict[str, Any]], context: dict[str, Any]) -> str:
        if action in {"REFUSE_HIGH_RISK", "REFUSE_RESTRICTED"}:
            return "CONTROLLED"
        if action in {"ASK_FOR_CONTEXT", "ESCALATE"}:
            return "LOW"
        if cases and cases[0]["score"] >= 0.72 and context.get("scope"):
            return "HIGH"
        return "MEDIUM"

    @staticmethod
    def _headline(action: str, context: dict[str, Any], cases: list[dict[str, Any]]) -> str:
        if action == "REFUSE_HIGH_RISK":
            return "该请求需要正式授权，知识助手不会执行生产决策"
        if action == "REFUSE_RESTRICTED":
            return "当前角色无权访问受限失效分析资料"
        if action == "ASK_FOR_CONTEXT":
            return "已有失败代码，但上下文不足以判断最相关的历史模式"
        if action == "ESCALATE":
            return "未找到足够可靠的历史答案，建议升级人工调查"
        top = cases[0] if cases else None
        category_labels = {"EQUIPMENT": "站点/设备方向", "MATERIAL": "物料批次方向", "TEST_PROGRAM": "测试程序方向"}
        if top:
            label = category_labels.get(top["case"]["root_cause_category"], "历史案例方向")
            return f"当前上下文更接近{label}，但仍需按证据完成验证"
        return "已形成首轮排查建议"

    @staticmethod
    def _escalation(action: str, context: dict[str, Any]) -> dict[str, Any]:
        if action == "REFUSE_RESTRICTED":
            return {"required": True, "team": "系统管理员或质量工程", "reason": "需要正式权限申请"}
        if action == "REFUSE_HIGH_RISK":
            return {"required": True, "team": "质量工程", "reason": "涉及放行、跳测、参数或其他高风险生产决策"}
        if action == "ESCALATE":
            return {"required": True, "team": "产品工程与失效分析", "reason": "缺少可靠历史证据或出现新失败模式"}
        if action == "ASK_FOR_CONTEXT":
            return {"required": False, "team": "发起人", "reason": "先补充关键上下文"}
        if context.get("scope") in {"MULTI_STATION", "CROSS_LINE"}:
            return {"required": True, "team": "产品工程与质量工程", "reason": "异常范围跨站点或跨产线"}
        return {"required": False, "team": "产品工程", "reason": "先完成低风险首轮检查"}

    @staticmethod
    def _summary_markdown(
        headline: str,
        facts: list[dict[str, str]],
        historical: list[dict[str, Any]],
        steps: list[dict[str, Any]],
        missing: list[str],
        escalation: dict[str, Any],
        action: str,
    ) -> str:
        lines = [f"## 判断\n\n{headline}", "\n## 已知事实"]
        lines.extend(f"- {item['label']}：{item['value']}" for item in facts)
        if missing:
            lines.append("\n## 缺失信息")
            lines.extend(f"- {item}" for item in missing)
        if historical:
            lines.append("\n## 历史相似案例")
            for item in historical:
                lines.append(f"- {item['case_id']}：{item['title']}（相似度 {item['score_percent']}%）")
        lines.append("\n## 首轮排查")
        lines.extend(f"{item['sequence']}. {item['title']} — {item['purpose']}" for item in steps)
        lines.append(f"\n## 升级建议\n\n{'需要' if escalation['required'] else '暂不需要'}升级至{escalation['team']}：{escalation['reason']}。")
        if action != "ANSWER":
            lines.append("\n> 系统未形成确定性根因结论。")
        return "\n".join(lines)
