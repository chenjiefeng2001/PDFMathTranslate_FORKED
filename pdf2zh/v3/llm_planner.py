"""Module: LLMPlanner — Phase 5.4 LLM Agent 作为 Document Engineer（只做判断）。

LLM 不是 Parser：只接收 {problem, evidence}，输出 {repair, reason} 决策；
provider 缺省/失败时回退规则规划器（零 LLM 依赖）。

    problem="TOC entry corrupted"
    evidence={"font":"CIDFont","unicode_missing":true,...}
        │
        ▼
    LLMRepairPlanner.plan → {"repair":"toc_split","reason":"..."}
"""

from __future__ import annotations

import json
import logging
from typing import Dict, Optional

log = logging.getLogger(__name__)

# issue code → 规则策略（无 LLM 时的默认决策）
RULE_MAP = {
    "unicode_error": "unicode_repair",
    "toc_merged_lines": "toc_split",
    "toc_low_confidence": "toc_split",
    "formula_low_confidence": "math_recovery",
    "empty_block": "empty_block",
    "translation_overflow": "toc_split",
    "font_uncertain": "unicode_repair",
}


class RepairPlanner:
    """决策接口：plan(problem, evidence) -> strategy name。"""

    def plan(self, problem: str, evidence: Dict) -> str:
        raise NotImplementedError


class RuleRepairPlanner(RepairPlanner):
    """规则规划器：issue code → 策略（确定性，无 LLM）。"""

    def plan(self, problem: str, evidence: Dict) -> str:
        return RULE_MAP.get(problem, "")


class LLMRepairPlanner(RepairPlanner):
    """LLM 规划器：provider（v3.translator.LLMProvider 接口）决策。"""

    def __init__(
        self,
        provider=None,
        model: str = "gpt-4o-mini",
        fallback: Optional[RepairPlanner] = None,
    ) -> None:
        self.provider = provider
        self.model = model
        self.fallback = fallback or RuleRepairPlanner()

    def plan(self, problem: str, evidence: Dict) -> str:
        if self.provider is None:
            return self.fallback.plan(problem, evidence)
        prompt = (
            "You are a document engineer. Given a document problem and "
            "evidence, choose ONE repair strategy from: "
            f"{sorted(set(RULE_MAP.values()))}. "
            'Reply with JSON {"repair": "<strategy>", "reason": "<why>"}.'
        )
        try:
            resp = self.provider.complete(
                [
                    {"role": "system", "content": prompt},
                    {
                        "role": "user",
                        "content": f"problem={problem} evidence={json.dumps(evidence, ensure_ascii=False)}",
                    },
                ],
                model=self.model,
                temperature=0.0,
            )
            payload = json.loads((resp.text or "").strip())
            chosen = str(payload.get("repair", "")).strip()
            if chosen in set(RULE_MAP.values()):
                return chosen
        except Exception as e:  # noqa: BLE001 — LLM 失败回退规则
            log.debug("LLM repair planner failed: %s", e)
        return self.fallback.plan(problem, evidence)


__all__ = ["RULE_MAP", "RepairPlanner", "RuleRepairPlanner", "LLMRepairPlanner"]
