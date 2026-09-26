"""Quality — 质量反馈闭环。

Phase 9.4:
    - QualityRetryPolicy: 自动重试策略

用法：
    from pdf2zh.quality import QualityRetryPolicy

    policy = QualityRetryPolicy()
    decision = policy.decide(quality_score=0.4, attempt=1)
    if decision.retry:
        translate(text, mode=decision.mode)
"""

from pdf2zh.quality.retry_policy import QualityRetryPolicy, RetryDecision

__all__ = [
    "QualityRetryPolicy",
    "RetryDecision",
]
