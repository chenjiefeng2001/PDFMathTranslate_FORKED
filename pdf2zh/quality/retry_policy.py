"""QualityRetryPolicy — 质量重试策略。

当质量评分低时，自动调整策略重试。

用法：
    policy = QualityRetryPolicy()
    decision = policy.decide(quality_score, attempt=1)
    # RetryDecision(retry=True, mode="short_translation")
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class RetryDecision:
    """重试决策。"""

    retry: bool = False
    mode: str = "normal"
    reason: str = ""
    max_retries: int = 2
    backoff_ms: float = 100.0


class QualityRetryPolicy:
    """质量重试策略。"""

    def __init__(
        self,
        quality_threshold: float = 0.6,
        max_retries: int = 2,
    ) -> None:
        self._quality_threshold = quality_threshold
        self._max_retries = max_retries

    def decide(
        self,
        quality_score: float,
        attempt: int = 0,
        block_type: str = "paragraph",
    ) -> RetryDecision:
        """决定是否重试。"""
        if attempt >= self._max_retries:
            return RetryDecision(
                retry=False,
                reason="max_retries_exceeded",
            )

        if quality_score >= self._quality_threshold:
            return RetryDecision(
                retry=False,
                reason="quality_acceptable",
            )

        # 根据质量分数选择策略
        if quality_score < 0.3:
            # 非常差：短翻译 + 术语保护
            return RetryDecision(
                retry=True,
                mode="conservative",
                reason="quality_very_low",
                backoff_ms=200,
            )
        elif quality_score < 0.5:
            # 差：短翻译
            return RetryDecision(
                retry=True,
                mode="short_translation",
                reason="quality_low",
                backoff_ms=150,
            )
        else:
            # 一般：布局感知
            return RetryDecision(
                retry=True,
                mode="layout_aware",
                reason="quality_below_threshold",
                backoff_ms=100,
            )

    def adjust_for_block_type(
        self,
        decision: RetryDecision,
        block_type: str,
    ) -> RetryDecision:
        """根据 block 类型调整策略。"""
        if block_type == "toc":
            return RetryDecision(
                retry=decision.retry,
                mode="structure_preserve",
                reason="toc_specific",
                backoff_ms=decision.backoff_ms,
            )
        elif block_type == "title":
            return RetryDecision(
                retry=decision.retry,
                mode="style_preserve",
                reason="title_specific",
                backoff_ms=decision.backoff_ms,
            )
        elif block_type == "caption":
            return RetryDecision(
                retry=decision.retry,
                mode="short_translation",
                reason="caption_specific",
                backoff_ms=decision.backoff_ms,
            )
        return decision
