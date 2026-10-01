"""Translation pipeline — 翻译优化。

Phase 5-10:
    - BlockCache: block 级翻译缓存
    - RepeatedRegionDetector: 重复区域检测
    - TranslationBatchScheduler: token budget 批处理
    - TranslationMemory: 术语一致性
    - Glossary: 领域术语保护
    - TranslationUnitBuilder: block 合并
    - SemanticPolicy: 语义策略引擎
    - LayoutRequest: layout-aware translation
    - TOCTranslator: 目录专用翻译
    - QualityScorer: 翻译质量评分
    - AdaptivePolicy: 自适应策略
    - FeedbackStore: 结果反馈
    - TranslationRouter: 模型路由
    - ContextCache: 上下文感知缓存

数据流：
    PageSnapshot
        ↓
    Block IR
        ↓
    ┌───────────────────┐
    │ Semantic Policy   │ ← 策略引擎
    │ (resolve mode)    │
    └───────────────────┘
        ↓
    ┌───────────────────┐
    │ Translation       │ ← batch + glossary + quality
    │ Pipeline          │
    └───────────────────┘
        ↓
    Translation Response (with quality score)
        ↓
    ┌───────────────────┐
    │ Feedback Loop     │ ← 自适应优化
    │ (learn strategy)  │
    └───────────────────┘
"""

from pdf2zh.translation.block_cache import BlockCache, BlockCacheEntry, BlockCacheKey
from pdf2zh.translation.region_detector import (
    RepeatedRegion,
    RepeatedRegionDetector,
    RegionFingerprint,
)
from pdf2zh.translation.scheduler import (
    TranslationBatchScheduler,
    TranslationBatch,
    TranslationUnit,
)
from pdf2zh.translation.memory import TranslationMemory, TermEntry
from pdf2zh.translation.glossary import Glossary, GlossaryTerm
from pdf2zh.translation.unit_builder import TranslationUnitBuilder, MergeConfig
from pdf2zh.translation.metrics import TranslationMetrics
from pdf2zh.translation.policy import (
    SemanticPolicy,
    TranslationMode,
    BLOCK_TYPE_POLICIES,
)
from pdf2zh.translation.layout_request import TranslationRequest, TranslationResponse
from pdf2zh.translation.toc_translator import TOCTranslator, TOCEntry
from pdf2zh.translation.quality import (
    TranslationQualityScorer,
    QualityScore,
    QualityDimensions,
)

__all__ = [
    # Cache
    "BlockCache",
    "BlockCacheEntry",
    "BlockCacheKey",
    # Region detection
    "RepeatedRegionDetector",
    "RepeatedRegion",
    "RegionFingerprint",
    # Batch scheduling
    "TranslationBatchScheduler",
    "TranslationBatch",
    "TranslationUnit",
    # Memory + Glossary
    "TranslationMemory",
    "TermEntry",
    "Glossary",
    "GlossaryTerm",
    # Unit builder
    "TranslationUnitBuilder",
    "MergeConfig",
    # Metrics
    "TranslationMetrics",
    # Phase 8: Quality
    "SemanticPolicy",
    "TranslationMode",
    "BLOCK_TYPE_POLICIES",
    "TranslationRequest",
    "TranslationResponse",
    "TOCTranslator",
    "TOCEntry",
    "TranslationQualityScorer",
    "QualityScore",
    "QualityDimensions",
]
