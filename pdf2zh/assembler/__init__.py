"""PDF Assembler — 解耦 PDF 组装层，消除 insert_file / move_page 瓶颈。

设计原则（v2 最终版）：
    1. PageTask / PageResult / TranslationArtifact 是跨进程数据协议
    2. xref / IndirectObject 等概念仅存在于 assembler 内部
    3. 字体需求由 FontRequirement 描述（含 font_identity）
    4. assembler 不关心 mono/dual —— 上层 Translator 构建不同的 TranslationArtifact
    5. BackendCapabilities 声明每个 backend 的能力边界

用法：
    from pdf2zh.assembler import (
        get_assembler, PageTask, PageResult, TranslationArtifact,
    )

    # 主进程构建 PageTask
    task = PageTask(page_index=0, pdf_bytes=..., source_page_hash="...")

    # worker 处理
    result = process_page(task)  # 返回 PageResult

    # 主进程组装
    artifact = TranslationArtifact(pages=[result], mode="dual", ...)
    asm = get_assembler()
    pdf_bytes = asm.assemble(artifact)

环境变量：
    PDF2ZH_BACKEND=mupdf    默认，兼容层（行为不变）
    PDF2ZH_BACKEND=pikepdf  对象级复用实验
"""

from pdf2zh.assembler.base import (
    SCHEMA_VERSION,
    BackendCapabilities,
    BenchmarkResult,
    FontIdentity,
    FontRequirement,
    OutlineEntry,
    PageCacheKey,
    PageGeometry,
    PageMetrics,
    PageResult,
    PageSize,
    PageTask,
    PDFAssembler,
    ReaderCompatibilityResult,
    ResourceRequirement,
    SemanticDiff,
    SemanticPolicy,
    SourceInfo,
    TranslationArtifact,
    compare_page_semantics,
    generate_migration_report,
    summarize_semantic_diffs,
)
from pdf2zh.assembler.artifacts import (
    BlockArtifact,
    BlockType,
    LayoutPolicy,
    TOCEntryMeta,
    make_block,
    make_toc_block,
    policy_for,
)
from pdf2zh.assembler.factory import get_assembler
from pdf2zh.assembler.mupdf_assembler import MuPDFAssembler
from pdf2zh.assembler.backend import (
    PDFBackend,
    PDFBackendCapabilities,
    BackendMetrics,
)
from pdf2zh.assembler.pikepdf_backend import PikepdfBackend
from pdf2zh.assembler.mupdf_backend import MuPDFBackend
from pdf2zh.assembler.selector import BackendSelector
from pdf2zh.assembler.object_store import ObjectStore, ObjectIdentity, ObjectType
from pdf2zh.assembler.content_artifact import (
    ContentArtifact,
    DrawText,
    DrawImage,
    DrawOp,
)
from pdf2zh.parser import PageSnapshot, TextSpan, ImageInfo, FontInfo
from pdf2zh.assembler.page_classifier import (
    PageClassifier,
    ClassificationResult,
    PageComplexity,
)
from pdf2zh.layout import (
    LayoutRequest,
    LayoutResult,
    LayoutBlock,
    LayoutBatchScheduler,
    LayoutCacheKey,
)
from pdf2zh.assembler.resources import (
    FontPool,
    FontUsageScope,
    ResourceOwnership,
    ResourceUsageTracker,
    ResourceResolver,
)

__all__ = [
    # 版本
    "SCHEMA_VERSION",
    # 核心接口
    "PDFAssembler",
    "get_assembler",
    # 数据结构 — 输入
    "PageTask",
    # 数据结构 — 输出
    "PageResult",
    "PageSize",
    "PageGeometry",
    "PageMetrics",
    "ResourceRequirement",
    "FontIdentity",
    "FontRequirement",
    # 语义块
    "BlockArtifact",
    "BlockType",
    "LayoutPolicy",
    "TOCEntryMeta",
    "make_block",
    "make_toc_block",
    "policy_for",
    # 数据结构 — 文档级
    "OutlineEntry",
    "SourceInfo",
    "TranslationArtifact",
    "BackendCapabilities",
    "BenchmarkResult",
    "ReaderCompatibilityResult",
    # 缓存
    "PageCacheKey",
    # 比较工具
    "SemanticPolicy",
    "SemanticDiff",
    "compare_page_semantics",
    "summarize_semantic_diffs",
    "generate_migration_report",
    # 资源管理
    "FontPool",
    "FontUsageScope",
    "ResourceOwnership",
    "ResourceUsageTracker",
    "ResourceResolver",
    # Phase 4: Object management
    "ObjectStore",
    "ObjectIdentity",
    "ObjectType",
    "ContentArtifact",
    "DrawText",
    "DrawImage",
    "DrawOp",
    # Phase Priority 1-2: Pipeline optimization
    "PageSnapshot",
    "TextSpan",
    "ImageInfo",
    "FontInfo",
    "PageClassifier",
    "ClassificationResult",
    "PageComplexity",
    # Phase Priority 3: Layout batch
    "LayoutRequest",
    "LayoutResult",
    "LayoutBlock",
    "LayoutBatchScheduler",
    "LayoutCacheKey",
    # Phase 5: Backend abstraction
    "PDFBackend",
    "PDFBackendCapabilities",
    "BackendMetrics",
    "PikepdfBackend",
    "MuPDFBackend",
    "BackendSelector",
    # 实现
    "MuPDFAssembler",
]
