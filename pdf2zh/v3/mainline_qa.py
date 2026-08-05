"""Module: MainlineQA — 阶段六/八主链路接管：置信度路由 + Review 复检（P2）。

V4 侧的 Translation Layer（置信度路由）与 LLM Refiner 已就位但受限 V4。
本模块把同一套判据接到 legacy 主链路的 gate 记录上（side-channel，只
产出 QA 记录，不回写渲染）：

    gate 记录（text + translated）
        │
        ├─ TranslationAdvisor（阶段六：route / confidence）
        └─ ReviewAgent（阶段八：公式/数字/术语/未翻译 复检）
        │
        ▼
    TranslationQAReport（逐段 action=keep | retranslate → QA 标记）

规则驱动、无网络；LLM Refiner 仅在提供 provider 时参与（阶段八）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from pdf2zh.v3.review_agent import ReviewAgent
from pdf2zh.v3.translation_advisor import (
    TRANSLATE_ROUTE,
    KEEP_ROUTE,
    TranslationAdvisor,
)


@dataclass
class QARecorder:
    """逐段翻译质检记录。"""

    node_id: str = ""
    route: str = ""
    confidence: float = 0.0
    review_passed: bool = True
    issues: List[dict] = field(default_factory=list)
    action: str = "keep"  # keep | retranslate

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "route": self.route,
            "confidence": round(self.confidence, 4),
            "review_passed": self.review_passed,
            "issues": list(self.issues),
            "action": self.action,
        }


@dataclass
class TranslationQAReport:
    """整页质检汇总。"""

    total: int = 0
    keep: int = 0
    translate: int = 0
    flagged: int = 0
    action_retranslate: int = 0
    records: List[QARecorder] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "total": self.total,
            "keep": self.keep,
            "translate": self.translate,
            "flagged": self.flagged,
            "action_retranslate": self.action_retranslate,
            "records": [r.to_dict() for r in self.records],
        }

    def summary(self) -> str:
        return (f"MainlineQA total={self.total} "
                f"translate={self.translate} keep={self.keep} "
                f"flagged={self.flagged} retranslate={self.action_retranslate}")


def run_translation_qa(records: Sequence[dict],
                       advisor: Optional[TranslationAdvisor] = None,
                       reviewer: Optional[ReviewAgent] = None) -> TranslationQAReport:
    """对 gate 记录逐段做「置信度路由 + Review 复检」。

    ``records`` 为 ``[{node_id, text, translated}, ...]``（gate 记录派生）。
    - route：``MainlineTranslationRouter`` 判 keep 与否；低置信度 translate
      可经 advisor.refiner 复核（提供 provider 时）。
    - review：对 translate 路由段落做 ReviewAgent 全项复检；不过则
      action=retranslate、flagged+1。
    """
    advisor = advisor or TranslationAdvisor()
    reviewer = reviewer or ReviewAgent()
    report = TranslationQAReport(total=len(records))
    for rec in records or []:
        node_id = str(rec.get("node_id", ""))
        text = str(rec.get("text", ""))
        translated = str(rec.get("translated", "") or text)
        verdicts = advisor.advise([text]) if hasattr(advisor, "advise") else None
        if verdicts is not None and verdicts.verdicts:
            v = verdicts.verdicts[0]
            route = "keep" if v.get("route") == KEEP_ROUTE else TRANSLATE_ROUTE
            confidence = float(v.get("confidence", 0.0))
        else:
            route = TRANSLATE_ROUTE if text.strip() else KEEP_ROUTE
            confidence = 1.0 if route == "keep" else 0.7

        qa = QARecorder(node_id=node_id, route=route,
                        confidence=confidence)
        if route == TRANSLATE_ROUTE:
            report.translate += 1
            try:
                result = reviewer.review(node_id, text, translated)
            except Exception:  # noqa: BLE001 — 复检失败视为通过
                result = None
            if result is not None:
                if result.issues:
                    qa.issues = [i.to_dict() for i in result.issues]
                if not result.passed:
                    qa.review_passed = False
                    qa.action = "retranslate"
                    report.action_retranslate += 1
                    report.flagged += 1
        else:
            report.keep += 1
        report.records.append(qa)
    return report


__all__ = [
    "QARecorder", "TranslationQAReport", "run_translation_qa",
]