"""QualityGate — 质量门控。

翻译后检查质量，决定通过/重试/拒绝。

用法：
    gate = QualityGate()
    decision = gate.check(translation_result)
    # GateDecision(action="pass", score=0.92)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class GateConfig:
    """门控配置。"""

    pass_threshold: float = 0.85
    retry_threshold: float = 0.60
    max_retries: int = 2
    strict_mode: bool = False

    # 严格模式阈值
    strict_pass_threshold: float = 0.90
    critical_error_threshold: int = 0  # 不允许任何 critical error


@dataclass
class GateResult:
    """门控结果。"""

    action: str = "pass"  # pass, retry, reject
    score: float = 0.0
    reason: str = ""
    retry_count: int = 0
    errors: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def should_retry(self) -> bool:
        return self.action == "retry"

    @property
    def should_reject(self) -> bool:
        return self.action == "reject"

    def summary(self) -> str:
        return (
            f"GateResult: action={self.action}, "
            f"score={self.score:.2f}, "
            f"reason={self.reason}"
        )


class QualityGate:
    """质量门控。"""

    def __init__(self, config: Optional[GateConfig] = None) -> None:
        self._config = config or GateConfig()
        self._retry_counts: Dict[str, int] = {}

    def check(
        self,
        score: float,
        errors: Optional[List[str]] = None,
        block_id: str = "",
        retry_count: int = 0,
    ) -> GateResult:
        """检查质量。"""
        errors = errors or []
        result = GateResult(
            score=score,
            errors=errors,
            retry_count=retry_count,
        )

        # 检查 critical errors
        critical_errors = [
            e
            for e in errors
            if e in ("number_changed", "formula_changed", "meaning_loss")
        ]

        # 严格模式
        if self._config.strict_mode:
            if critical_errors:
                result.action = "reject"
                result.reason = f"critical_error: {critical_errors[0]}"
                return result
            if score < self._config.strict_pass_threshold:
                result.action = "retry"
                result.reason = f"below_strict_threshold: {score:.2f}"
                return result

        # 标准模式
        if score >= self._config.pass_threshold:
            result.action = "pass"
            result.reason = "quality_acceptable"
        elif retry_count >= self._config.max_retries:
            result.action = "reject"
            result.reason = "max_retries_exceeded"
        elif score >= self._config.retry_threshold:
            result.action = "retry"
            result.reason = f"below_threshold: {score:.2f}"
        else:
            result.action = "reject"
            result.reason = f"quality_too_low: {score:.2f}"

        return result

    def check_translation(
        self,
        source: str,
        translated: str,
        reference: Optional[str] = None,
        block_id: str = "",
        retry_count: int = 0,
    ) -> GateResult:
        """检查翻译结果。"""
        from pdf2zh.validation.llm_judge import LLMJudge

        judge = LLMJudge()
        score_result = judge.evaluate(source, translated, reference)

        errors = []
        if score_result.meaning < 0.7:
            errors.append("meaning_loss")
        if score_result.terminology < 0.8:
            errors.append("terminology_error")
        if score_result.numerical < 0.9:
            errors.append("number_changed")

        return self.check(
            score=score_result.weighted,
            errors=errors,
            block_id=block_id,
            retry_count=retry_count,
        )

    def get_statistics(self) -> Dict[str, Any]:
        """获取统计。"""
        return {
            "config": {
                "pass_threshold": self._config.pass_threshold,
                "retry_threshold": self._config.retry_threshold,
                "max_retries": self._config.max_retries,
                "strict_mode": self._config.strict_mode,
            },
        }
