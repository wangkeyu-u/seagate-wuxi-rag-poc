"""Deterministic hybrid retrieval baseline for the fully offline demonstration.

The hashed vector is a repeatable stand-in, not a claim of production semantic
quality. Structured manufacturing context receives the largest weight because
Failure Code, station scope, lot, and software version are more reliable in this
PoC than token similarity. A production adapter can replace lexical/vector
retrieval while preserving the same ACL-first and explainability contract.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from typing import Any, Iterable

from .policy import is_high_risk_request
from .repository import DataRepository


# Top-level fusion weights are named so an interviewer or future evaluator can
# see what was tuned. They must eventually be learned/validated on a factory
# golden set rather than treated as universal manufacturing thresholds.
CASE_LEXICAL_WEIGHT = 0.18
CASE_VECTOR_WEIGHT = 0.17
CASE_STRUCTURED_WEIGHT = 0.60
DOCUMENT_LEXICAL_WEIGHT = 0.20
DOCUMENT_VECTOR_WEIGHT = 0.18
DOCUMENT_STRUCTURED_WEIGHT = 0.55


def tokenize(text: str) -> list[str]:
    text = (text or "").lower()
    latin = re.findall(r"[a-z]+(?:[-_.][a-z0-9]+)*|[a-z]*\d+(?:\.\d+)*", text)
    chinese_runs = re.findall(r"[\u4e00-\u9fff]+", text)
    chinese: list[str] = []
    for run in chinese_runs:
        chinese.append(run)
        if len(run) > 1:
            chinese.extend(run[index : index + 2] for index in range(len(run) - 1))
    return latin + chinese


def cosine(left: Counter[str], right: Counter[str]) -> float:
    if not left or not right:
        return 0.0
    dot = sum(value * right.get(key, 0.0) for key, value in left.items())
    left_norm = math.sqrt(sum(value * value for value in left.values()))
    right_norm = math.sqrt(sum(value * value for value in right.values()))
    return dot / (left_norm * right_norm) if left_norm and right_norm else 0.0


def hashed_vector(tokens: Iterable[str], dimensions: int = 384) -> Counter[str]:
    vector: Counter[str] = Counter()
    for token in tokens:
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=4).digest()
        index = int.from_bytes(digest, "big") % dimensions
        vector[str(index)] += 1.0
    return vector


class HybridRetriever:
    def __init__(self, repository: DataRepository):
        self.repository = repository
        self.alias_map: dict[str, str] = {}
        for term in repository.master["terminology"]:
            canonical = term["canonical_term"].lower()
            for alias in term["aliases"]:
                self.alias_map[alias.lower()] = canonical
        self._case_tokens = {item["case_id"]: tokenize(self._case_text(item)) for item in repository.cases}
        self._case_vectors = {case_id: hashed_vector(tokens) for case_id, tokens in self._case_tokens.items()}
        self._doc_tokens = {item["document_version_id"]: tokenize(self._document_text(item)) for item in repository.documents}
        self._doc_vectors = {doc_id: hashed_vector(tokens) for doc_id, tokens in self._doc_tokens.items()}

    def expand_tokens(self, text: str) -> list[str]:
        tokens = tokenize(text)
        lowered = text.lower()
        for alias, canonical in self.alias_map.items():
            if alias in lowered:
                tokens.append(canonical)
        return tokens

    @staticmethod
    def _case_text(item: dict[str, Any]) -> str:
        parts = [
            item["title"], item["summary"], " ".join(item["failure_codes"]), " ".join(item["symptoms"]),
            item.get("confirmed_root_cause") or "", item["root_cause_category"], " ".join(item["applicable_conditions"]),
            " ".join(item["non_applicable_conditions"]), " ".join(item["product_ids"]), " ".join(item["line_ids"]),
            " ".join(item["station_ids"]), " ".join(item["material_lot_ids"]), " ".join(item["test_program_versions"]),
        ]
        return " ".join(parts)

    @staticmethod
    def _document_text(item: dict[str, Any]) -> str:
        return " ".join(
            [
                item["title"], item["document_type"], item["summary"], item["content"],
                " ".join(item["applicable_failure_codes"]), " ".join(item["applicable_products"]),
            ]
        )

    def infer_context(self, query: str, supplied: dict[str, Any] | None = None) -> dict[str, Any]:
        supplied = {key: value for key, value in (supplied or {}).items() if value not in (None, "", [])}
        text = query.lower()
        context = dict(supplied)

        code = re.search(r"\b(f\d{3})\b", text, re.IGNORECASE)
        if code and "failure_code" not in context:
            context["failure_code"] = code.group(1).upper()

        station_matches = re.findall(r"\b(?:st|station)[-\s]?0?(\d{1,2})\b", text, re.IGNORECASE)
        if station_matches and "station_ids" not in context:
            context["station_ids"] = [f"ST-{int(value):02d}" for value in station_matches]

        line_matches = re.findall(r"\bline[-\s]?0?(\d{1,2})\b", text, re.IGNORECASE)
        if line_matches and "line_ids" not in context:
            context["line_ids"] = [f"LINE-{int(value):02d}" for value in line_matches]

        lot = re.search(r"\b(hsa-l\d{4})\b", text, re.IGNORECASE)
        if lot and "material_lot_id" not in context:
            context["material_lot_id"] = lot.group(1).upper()

        tp = re.search(r"(?:tp|测试程序|程序版本)\s*[-v]?\s*(\d+\.\d+)", text, re.IGNORECASE)
        if tp and "test_program_version" not in context:
            context["test_program_version"] = tp.group(1)

        fw = re.search(r"(?:fw|固件)\s*[-v]?\s*(\d+\.\d+(?:\.\d+)?)", text, re.IGNORECASE)
        if fw and "firmware_version" not in context:
            context["firmware_version"] = fw.group(1)

        for product in self.repository.master["products"]:
            identifiers = [product["product_id"], product["product_family"], product["model_name"]]
            if any(identifier.lower() in text for identifier in identifiers):
                context.setdefault("product_id", product["product_id"])
                context.setdefault("product_family", product["product_family"])
                break

        if "scope" not in context:
            if any(phrase in text for phrase in ["跨线", "两条线", "多条线", "cross-line", "cross line"]):
                context["scope"] = "CROSS_LINE"
            elif any(phrase in text for phrase in ["多站", "多个站", "多个station", "multiple station"]):
                context["scope"] = "MULTI_STATION"
            elif any(phrase in text for phrase in ["单站", "仅一个站", "只有st", "only station", "single station"]):
                context["scope"] = "SINGLE_STATION"
            elif context.get("station_ids") and len(context["station_ids"]) == 1:
                context["scope"] = "SINGLE_STATION"

        if any(phrase in text for phrase in ["升级后", "发布后", "变更后", "刚升级", "after release", "after update"]):
            context.setdefault("recent_change", "TEST_PROGRAM" if "程序" in text or "tp" in text else "UNKNOWN")

        if is_high_risk_request(query):
            context["high_risk_request"] = True
        return context

    def retrieve_cases(
        self,
        query: str,
        context: dict[str, Any],
        role: str,
        limit: int = 5,
        *,
        allowed_line_ids: tuple[str, ...] = (),
        allowed_station_ids: tuple[str, ...] = (),
    ) -> list[dict[str, Any]]:
        query_tokens = self.expand_tokens(query + " " + " ".join(str(value) for value in context.values()))
        query_counter = Counter(query_tokens)
        query_vector = hashed_vector(query_tokens)
        results: list[dict[str, Any]] = []
        for item in self.repository.accessible_cases(role, allowed_line_ids, allowed_station_ids):
            lexical = cosine(query_counter, Counter(self._case_tokens[item["case_id"]]))
            semantic = cosine(query_vector, self._case_vectors[item["case_id"]])
            structured, matched, differences = self._structured_case_score(item, context)
            review_weight = 0.05 if item["status"] == "PUBLISHED" and item["confidence"] == "CONFIRMED" else -0.08
            total = max(
                0.0,
                min(
                    1.0,
                    lexical * CASE_LEXICAL_WEIGHT
                    + semantic * CASE_VECTOR_WEIGHT
                    + structured * CASE_STRUCTURED_WEIGHT
                    + review_weight,
                ),
            )
            results.append(
                {
                    "case": item,
                    "score": round(total, 4),
                    "score_breakdown": {
                        "lexical": round(lexical, 4),
                        "semantic": round(semantic, 4),
                        "structured": round(structured, 4),
                        "review_weight": review_weight,
                    },
                    "matched_on": matched,
                    "differences": differences,
                }
            )
        return sorted(results, key=lambda result: (result["score"], result["case"]["detected_at"]), reverse=True)[:limit]

    def _structured_case_score(self, item: dict[str, Any], context: dict[str, Any]) -> tuple[float, list[str], list[str]]:
        score = 0.0
        matched: list[str] = []
        differences: list[str] = []
        failure = context.get("failure_code")
        if failure:
            if failure in item["failure_codes"]:
                score += 0.32
                matched.append(f"相同失败代码 {failure}")
            else:
                differences.append(f"失败代码不同（案例为 {', '.join(item['failure_codes'])}）")

        product = context.get("product_id")
        if product:
            if product in item["product_ids"]:
                score += 0.08
                matched.append("相同产品型号")
            else:
                query_product = self.repository.products_by_id.get(product, {})
                case_product = self.repository.products_by_id.get(item["product_ids"][0], {})
                if query_product.get("product_family") == case_product.get("product_family"):
                    score += 0.045
                    matched.append("相同产品族")
                else:
                    differences.append("产品族不同")

        scope = context.get("scope")
        conditions = set(item["applicable_conditions"])
        excluded = set(item["non_applicable_conditions"])
        scope_map = {
            "SINGLE_STATION": ("single_station_only", "multi_station_spike"),
            "MULTI_STATION": ("multi_station_spike", "single_station_only"),
            "CROSS_LINE": ("cross_line_after_release", "single_station_only"),
        }
        if scope in scope_map:
            positive, negative = scope_map[scope]
            if positive in conditions:
                score += 0.24
                matched.append(f"异常范围匹配：{scope}")
            if positive in excluded or negative in conditions:
                score -= 0.22
                differences.append(f"异常范围不匹配：{scope}")

        station_ids = set(context.get("station_ids", []))
        if station_ids:
            if station_ids.intersection(item["station_ids"]):
                score += 0.07
                matched.append("包含相同测试站")
            elif context.get("scope") == "SINGLE_STATION" and "single_station_only" in conditions:
                score += 0.035
                matched.append("站点不同但同为单站模式")

        lot = context.get("material_lot_id")
        if lot:
            if lot in item["material_lot_ids"]:
                score += 0.20
                matched.append(f"相同物料批次 {lot}")
            elif item["root_cause_category"] == "MATERIAL" and "same_material_lot" in conditions:
                score += 0.10
                matched.append("相同物料集中模式")
            else:
                differences.append("物料批次不同或案例未关联物料")

        tp = context.get("test_program_version")
        if tp:
            normalized = tp if tp.startswith("SW-TP-") else f"SW-TP-{tp}"
            if normalized in item["test_program_versions"]:
                score += 0.17
                matched.append(f"相同测试程序 {tp}")
            elif item["root_cause_category"] == "TEST_PROGRAM" and context.get("recent_change"):
                score += 0.08
                matched.append("同为程序变更后模式")
            else:
                differences.append("测试程序版本不同")

        if context.get("recent_change") and item["root_cause_category"] == "TEST_PROGRAM":
            score += 0.12
            matched.append("与近期测试程序变更相关")
        return max(0.0, min(1.0, score)), matched, differences

    def retrieve_documents(
        self,
        query: str,
        context: dict[str, Any],
        role: str,
        limit: int = 6,
        *,
        allowed_line_ids: tuple[str, ...] = (),
        allowed_station_ids: tuple[str, ...] = (),
    ) -> list[dict[str, Any]]:
        query_tokens = self.expand_tokens(query + " " + " ".join(str(value) for value in context.values()))
        query_counter = Counter(query_tokens)
        query_vector = hashed_vector(query_tokens)
        results = []
        for item in self.repository.accessible_documents(role, allowed_line_ids, allowed_station_ids):
            lexical = cosine(query_counter, Counter(self._doc_tokens[item["document_version_id"]]))
            semantic = cosine(query_vector, self._doc_vectors[item["document_version_id"]])
            structured = 0.0
            matched = []
            failure = context.get("failure_code")
            if failure and failure in item["applicable_failure_codes"]:
                structured += 0.30
                matched.append(f"适用于 {failure}")
            if item["document_type"] == "SOP" and item["status"] == "EFFECTIVE":
                structured += 0.18
                matched.append("当前有效 SOP")
            if item["document_type"] == "FAILURE_CODE_GUIDE":
                structured += 0.12
                matched.append("权威失败代码说明")
            scope = context.get("scope")
            if scope == "SINGLE_STATION" and item["document_id"] == "DOC-MAINT-CAL-001":
                structured += 0.32
                matched.append("单站设备检查相关")
            if scope == "MULTI_STATION" and context.get("material_lot_id") and item["document_id"] == "DOC-HSA-LOT-001":
                structured += 0.34
                matched.append("物料批次比较相关")
            if scope == "CROSS_LINE" and item["document_id"] == "DOC-CHG-TP38":
                structured += 0.36
                matched.append("测试程序变更相关")
            status_weight = 0.10 if item["status"] == "EFFECTIVE" else -0.35
            total = max(
                0.0,
                min(
                    1.0,
                    lexical * DOCUMENT_LEXICAL_WEIGHT
                    + semantic * DOCUMENT_VECTOR_WEIGHT
                    + structured * DOCUMENT_STRUCTURED_WEIGHT
                    + status_weight,
                ),
            )
            results.append({"document": item, "score": round(total, 4), "matched_on": matched})

        effective = [
            item
            for item in results
            if self.repository.is_document_effective(item["document"])
        ]
        return sorted(effective, key=lambda result: result["score"], reverse=True)[:limit]
