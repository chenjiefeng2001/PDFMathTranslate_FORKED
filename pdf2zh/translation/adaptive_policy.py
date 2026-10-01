"""AdaptiveTranslationPolicy — 自适应翻译策略。

根据历史自动选择最优策略。

用法：
    policy = AdaptiveTranslationPolicy()
    strategy = policy.select(block)
    # TranslationStrategy(mode="technical", temperature=0.2)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class TranslationStrategy:
    """翻译策略。"""

    mode: str = "normal"
    temperature: float = 0.3
    preserve_terms: bool = True
    max_length_ratio: float = 1.5
    shorten_if_overflow: bool = False
    use_glossary: bool = True
    domain: str = "general"
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_prompt_hint(self) -> str:
        """生成 prompt 提示。"""
        hints = []
        if self.mode == "technical":
            hints.append("使用专业术语，保持技术准确性")
        elif self.mode == "concise":
            hints.append("简洁翻译，控制长度")
        elif self.mode == "academic":
            hints.append("学术风格，保持正式")
        elif self.mode == "toc":
            hints.append("目录格式，保持结构")
        elif self.mode == "code":
            hints.append("代码注释，只翻译注释部分")

        if self.shorten_if_overflow:
            hints.append("如果翻译过长，请缩短")

        return "；".join(hints) if hints else ""


# 预定义策略
STRATEGIES = {
    "normal": TranslationStrategy(mode="normal"),
    "technical": TranslationStrategy(
        mode="technical",
        temperature=0.2,
        preserve_terms=True,
    ),
    "concise": TranslationStrategy(
        mode="concise",
        temperature=0.3,
        max_length_ratio=1.2,
        shorten_if_overflow=True,
    ),
    "academic": TranslationStrategy(
        mode="academic",
        temperature=0.2,
        preserve_terms=True,
        domain="academic",
    ),
    "toc": TranslationStrategy(
        mode="toc",
        temperature=0.1,
        max_length_ratio=1.0,
        shorten_if_overflow=True,
    ),
    "code": TranslationStrategy(
        mode="code",
        temperature=0.1,
        preserve_terms=True,
    ),
    "caption": TranslationStrategy(
        mode="caption",
        temperature=0.3,
        max_length_ratio=1.3,
        shorten_if_overflow=True,
    ),
}


class AdaptiveTranslationPolicy:
    """自适应翻译策略。"""

    def __init__(self) -> None:
        self._strategies = dict(STRATEGIES)
        self._history: Dict[str, List[float]] = {}

    def select(self, block: Any) -> TranslationStrategy:
        """选择策略。"""
        block_type = getattr(block, "type", "paragraph")
        text = getattr(block, "text", "")

        # 基于 block type 选择基础策略
        if block_type == "toc":
            base = self._strategies.get("toc", self._strategies["normal"])
        elif block_type == "formula":
            base = TranslationStrategy(mode="skip")
        elif block_type == "code":
            base = self._strategies.get("code", self._strategies["normal"])
        elif block_type == "caption":
            base = self._strategies.get("caption", self._strategies["normal"])
        elif block_type in ("title", "heading"):
            base = self._strategies.get("concise", self._strategies["normal"])
        else:
            # 根据文本特征判断
            base = self._select_by_text(text)

        # 根据历史调整
        base = self._adjust_by_history(base, block_type)

        return base

    def _select_by_text(self, text: str) -> TranslationStrategy:
        """根据文本特征选择策略。"""
        # 检查是否是技术文本
        tech_terms = [
            "algorithm",
            "function",
            "method",
            "system",
            "model",
            "network",
            "data",
            "process",
        ]
        if any(term in text.lower() for term in tech_terms):
            return self._strategies.get("technical", self._strategies["normal"])

        # 检查是否是学术文本
        academic_terms = [
            "research",
            "study",
            "analysis",
            "hypothesis",
            "experiment",
            "result",
            "conclusion",
        ]
        if any(term in text.lower() for term in academic_terms):
            return self._strategies.get("academic", self._strategies["normal"])

        return self._strategies["normal"]

    def _adjust_by_history(
        self,
        strategy: TranslationStrategy,
        block_type: str,
    ) -> TranslationStrategy:
        """根据历史调整策略。"""
        history_key = f"{block_type}/{strategy.mode}"
        scores = self._history.get(history_key, [])

        if len(scores) < 5:
            return strategy

        avg_score = sum(scores[-10:]) / len(scores[-10:])

        # 如果平均分低，切换到更保守的策略
        if avg_score < 0.5:
            if strategy.mode != "concise":
                return self._strategies.get("concise", strategy)
        elif avg_score < 0.7:
            strategy.shorten_if_overflow = True

        return strategy

    def record_outcome(
        self,
        block_type: str,
        strategy_mode: str,
        score: float,
    ) -> None:
        """记录结果。"""
        key = f"{block_type}/{strategy_mode}"
        if key not in self._history:
            self._history[key] = []
        self._history[key].append(score)
        # 只保留最近 100 条
        if len(self._history[key]) > 100:
            self._history[key] = self._history[key][-100:]

    def get_strategy_stats(self) -> Dict[str, Dict]:
        """获取策略统计。"""
        stats = {}
        for key, scores in self._history.items():
            if scores:
                stats[key] = {
                    "count": len(scores),
                    "avg_score": sum(scores) / len(scores),
                    "min_score": min(scores),
                    "max_score": max(scores),
                }
        return stats
