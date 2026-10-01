"""Layout pipeline — layout 推理调度。

Priority 3.1 + 9:
    - LayoutRequest: 轻量级请求
    - LayoutResult: 统一输出
    - LayoutBatchScheduler: batch 调度
    - LayoutCacheKey: 缓存 key
    - LayoutStage: 抽象 stage
    - RuleLayoutStage: 规则 layout
    - LayoutCascade: cascade 控制器

数据流：
    PageSnapshot
        ↓
    LayoutRequest (轻量化)
        ↓
    LayoutCascade
        ↓
    ┌────────────────────────────────────┐
    │ Level 0: Rule Layout (fast path)   │
    │ Level 1: Column Detection          │
    │ Level 2: ML Model                  │
    │ Level 3: Vision/OCR                │
    └────────────────────────────────────┘
        ↓
    LayoutResult (统一)
        ↓
    LayoutCache (可选)
        ↓
    Block IR
"""

from pdf2zh.layout.cache_key import LayoutCacheKey
from pdf2zh.layout.cascade import LayoutCascade
from pdf2zh.layout.request import LayoutRequest
from pdf2zh.layout.result import LayoutBlock, LayoutResult
from pdf2zh.layout.rule_stage import RuleLayoutStage
from pdf2zh.layout.scheduler import LayoutBatchScheduler
from pdf2zh.layout.stage import LayoutConfidence, LayoutStage

__all__ = [
    "LayoutRequest",
    "LayoutResult",
    "LayoutBlock",
    "LayoutBatchScheduler",
    "LayoutCacheKey",
    # Priority 9: Cascade
    "LayoutStage",
    "LayoutConfidence",
    "RuleLayoutStage",
    "LayoutCascade",
]
