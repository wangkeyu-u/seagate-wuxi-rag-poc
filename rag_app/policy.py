"""Deterministic guardrail for requests that imply production-control action.

The copilot is read-only: it may help an engineer gather evidence, but it must
not stop equipment, change parameters, skip tests, release or scrap material.
This lexical layer is a tested baseline, not a substitute for enterprise policy
and adversarial semantic evaluation.
"""

from __future__ import annotations

import re
import unicodedata


_HIGH_RISK_INTENT_PATTERNS = (
    # Chinese production actions. Text is normalized before matching so spaces,
    # punctuation, and full-width variants cannot split a protected phrase.
    r"(?:跳过|绕过|跳|免|略过|不做)(?:测试|检验|检查)",
    r"(?:测试|检验|检查)(?:跳过|绕过|免做|略过)",
    r"(?:修改|调整|更改|改成|改为|设为|设置)(?:测试|工艺)?(?:参数|阈值|数值)",
    r"(?:测试|工艺)?(?:参数|阈值|数值)(?:修改|调整|更改|改成|改为|设为|设置)",
    r"(?:关闭|关掉|绕过|禁用|停用)(?:安全)?联锁",
    r"(?:安全)?联锁(?:关闭|关掉|绕过|禁用|停用)",
    r"(?:直接|自动)?放行(?:这|该|本)?(?:一)?(?:批|批次|产品|盘|物料)?",
    r"(?:批|批次|产品|盘|物料).{0,4}(?:放行|判定合格|判成合格|批准)",
    r"(?:判定合格|判成合格|批准)(?:这|该|本)?(?:一)?(?:批|批次|产品|盘|物料)?",
    r"(?:请|帮我|替我|立即|马上|现在|直接|自动)(?:把|将)?(?:设备|产线|这批|该批|本批)?(?:停机|停线|启动|报废)",
    r"(?:停机|停线|启动|报废)(?:这|该|本)?(?:一)?(?:批|批次|产品|设备|产线)",
    r"(?:继续生产|继续运行|送往下一站|直接进入下一工序)",
    # English equivalents. Whitespace and punctuation have already been removed.
    r"(?:skip|bypass|omit)(?:the)?(?:test|inspection|check)",
    r"(?:disable|override|bypass)(?:the)?(?:safety)?interlock",
    r"(?:release|approve)(?:this|the)?(?:batch|lot|product)",
    r"(?:scrap)(?:this|the)?(?:batch|lot|product)(?:automatically)?",
    r"(?:set|change|adjust|modify)(?:the)?(?:test|process)?(?:parameter|threshold|limit)",
    r"(?:continueproduction|keeptheline(?:running)?|keepproductionrunning)",
)


def normalize_policy_text(value: str) -> str:
    """Return a comparison form that is stable across ordinary obfuscation."""

    normalized = unicodedata.normalize("NFKC", value or "").lower()
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", normalized)


def is_high_risk_request(query: str) -> bool:
    """Fail closed for requests that ask the copilot to take production actions."""

    compact = normalize_policy_text(query)
    return any(re.search(pattern, compact, re.IGNORECASE) for pattern in _HIGH_RISK_INTENT_PATTERNS)
