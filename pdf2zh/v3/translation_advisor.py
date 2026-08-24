"""Module: TranslationAdvisor — 阶段六/八主链路接管的决策机制。

V4 侧的 Translation Layer（置信度路由）与 LLM Refiner 已就位但未接入
主链路。本模块提供**主链路可消费**的顾问层（引擎仍是规则，不调网络）：

    MainlineTranslationRouter（阶段六）
        ├── 空白/公式标记      → keep
        ├── 纯数字/短编号      → keep
        ├── 品牌/技术名词      → keep（复用 image_engine router 词典）
        ├── 代码特征           → keep
        └── 其余              → translate（置信度 = 文本信号加权）

    LLMRefiner（阶段八）
        └── 低置信度 translate 节点可选交给 LLMProvider 复核（无 Provider
            时零开销跳过 —— MockLLMProvider 供测试/离线演示）

输出 ``TranslationAdvisorReport``（逐段 route/confidence/reason）。
与 v3 全部引擎同风格：纯逻辑、无 I/O、失败即跳过。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

from pdf2zh.v3.image_engine import is_probably_brand_or_technical
from pdf2zh.v3.processors import (
    NodeProcessor,
    NodeStage,
    get_semantic,
    set_policy,
)
from pdf2zh.v3.graph import DocumentGraph, DocumentNode

KEEP_ROUTE = "keep"
TRANSLATE_ROUTE = "translate"

_FORMULA_MARKER_RE = re.compile(r"\{\s*v\d+\s*\}")
_CODE_HINT_RE = re.compile(r"[{}_\\]+|:\w+\s*=")
_NUMBER_ONLY_RE = re.compile(r"^[\d%.\-\s,:;()]+$")


@dataclass
class RouteVerdict:
    """单个段落的翻译路由。"""

    text: str = ""
    route: str = TRANSLATE_ROUTE
    confidence: float = 0.0
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "text": self.text[:80],
            "route": self.route,
            "confidence": round(self.confidence, 4),
            "reason": self.reason,
        }


class MainlineTranslationRouter:
    """主链路置信度路由：决定一个段落是否值得送翻译器。

    置信度 = 文本长度/结构信号加权（与 image_engine 的 per-region
    translation_score 同风格，但面向整段文本）。
    """

    def decide(self, text: str) -> RouteVerdict:
        t = (text or "").strip()
        if not t:
            return RouteVerdict(text, KEEP_ROUTE, 0.0, "empty")
        if _FORMULA_MARKER_RE.search(t):
            return RouteVerdict(text, KEEP_ROUTE, 0.1, "formula_marker")
        if _NUMBER_ONLY_RE.match(t):
            return RouteVerdict(text, KEEP_ROUTE, 0.2, "number_only")
        if is_probably_brand_or_technical(t):
            return RouteVerdict(text, KEEP_ROUTE, 0.3, "brand|technical")
        if _CODE_HINT_RE.search(t) and len(t.split()) <= 8:
            return RouteVerdict(text, KEEP_ROUTE, 0.3, "code_like")
        # 正常文本：置信度随长度与结构信号上升
        words = len(t.split())
        confidence = 0.5
        if 2 <= words <= 12:
            confidence += 0.2
        if 12 < words <= 60:
            confidence += 0.3
        if 60 < words:
            confidence += 0.35
        has_alpha = any(c.isalpha() for c in t)
        if not has_alpha:
            confidence = min(confidence, 0.45)
        reason = "translate:mainline"
        return RouteVerdict(text, TRANSLATE_ROUTE, min(confidence, 1.0), reason)


class LLMRefiner:
    """阶段八：把低置信度的 translate 节点交给 LLM 复核。

    只做**决策复核**（保持/降级 keep），不负责翻译本身；provider 为
    None 时一切跳过（零开销）。provider 接口即 v3.translator.LLMProvider。
    """

    def __init__(
        self, provider=None, min_confidence: float = 0.6, model: str = "gpt-4o-mini"
    ) -> None:
        self.provider = provider
        self.min_confidence = min_confidence
        self.model = model

    def refine(self, verdict: RouteVerdict) -> RouteVerdict:
        if verdict.route != TRANSLATE_ROUTE:
            return verdict
        if verdict.confidence >= self.min_confidence:
            return verdict
        if self.provider is None:
            verdict.reason = verdict.reason + " (refiner:no_provider)"
            return verdict
        try:
            messages = [
                {
                    "role": "system",
                    "content": "Decide if this text should be translated or kept "
                    "as-is (brand/technical/code/number). Reply KEEP or TRANSLATE.",
                },
                {"role": "user", "content": f"Text: {verdict.text[:200]}"},
            ]
            resp = self.provider.complete(messages, model=self.model, temperature=0.0)
            answer = (resp.text or "").strip().upper()
            if answer.startswith("KEEP"):
                return RouteVerdict(
                    verdict.text,
                    KEEP_ROUTE,
                    max(verdict.confidence, 0.7),
                    "refiner:llm_keep",
                )
            verdict.reason = verdict.reason + " (refiner:llm_translate)"
            return verdict
        except Exception:  # noqa: BLE001 — Refiner 失败即跳过
            verdict.reason = verdict.reason + " (refiner:error)"
            return verdict


@dataclass
class TranslationAdvisorReport:
    """逐段路由统计。"""

    total: int = 0
    translate: int = 0
    keep: int = 0
    refined: int = 0
    verdicts: List[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "total": self.total,
            "translate": self.translate,
            "keep": self.keep,
            "refined": self.refined,
            "verdicts": self.verdicts,
        }

    def summary(self) -> str:
        return (
            f"TranslationAdvisor total={self.total} "
            f"translate={self.translate} keep={self.keep} "
            f"refined={self.refined}"
        )


class TranslationAdvisor:
    """路由 + Refiner 的组合入口。"""

    def __init__(
        self,
        router: Optional[MainlineTranslationRouter] = None,
        refiner: Optional[LLMRefiner] = None,
    ) -> None:
        self.router = router or MainlineTranslationRouter()
        self.refiner = refiner

    def advise(self, texts: List[str]) -> TranslationAdvisorReport:
        report = TranslationAdvisorReport(total=len(texts))
        for t in texts:
            verdict = self.router.decide(t)
            if self.refiner is not None:
                before = verdict.route
                verdict = self.refiner.refine(verdict)
                if before != verdict.route:
                    report.refined += 1
            if verdict.route == TRANSLATE_ROUTE:
                report.translate += 1
            else:
                report.keep += 1
            report.verdicts.append(verdict.to_dict())
        return report


class TranslationAdvisorProcessor(NodeProcessor):
    """把路由/精化写成 Node 策略（V9.0 单一 IR 上的阶段六/八接线）。"""

    name = "translation_advisor"
    stages = (NodeStage.TRANSLATION,)
    target_types = None

    def __init__(
        self,
        router: Optional[MainlineTranslationRouter] = None,
        refiner: Optional[LLMRefiner] = None,
    ) -> None:
        self.router = router or MainlineTranslationRouter()
        self.refiner = refiner

    def process(self, node: DocumentNode, graph: DocumentGraph) -> None:
        if node.node_type is None:
            return
        from pdf2zh.v3.processors import POLICY_KEY

        if POLICY_KEY in node.metadata:
            return  # 已有专门策略（TOC/公式/图片…）不覆盖
        verdict = self.router.decide(node.text)
        if self.refiner is not None:
            verdict = self.refiner.refine(verdict)
        route = "translate" if verdict.route == TRANSLATE_ROUTE else "preserve"
        set_policy(node, route, verdict.reason)
        get_semantic(node)["translation"] = verdict.to_dict()


__all__ = [
    "KEEP_ROUTE",
    "TRANSLATE_ROUTE",
    "RouteVerdict",
    "MainlineTranslationRouter",
    "LLMRefiner",
    "TranslationAdvisorReport",
    "TranslationAdvisor",
    "TranslationAdvisorProcessor",
]
