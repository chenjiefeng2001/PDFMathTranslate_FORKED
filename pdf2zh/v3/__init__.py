"""pdf2zh V3 Graph-Driven Architecture — Modules 1~10.

Modules:
  - Module 1: Parser Layer            (pdf2zh.v3.parser)
  - Module 2: Normalizer Layer        (pdf2zh.v3.normalizer)
  - Module 3: Document Graph Builder  (pdf2zh.v3.graph)
  - Module 4: Semantic Analyzer       (pdf2zh.v3.analyzer)
  - Module 5: Translation Planner     (pdf2zh.v3.planner)
  - Module 6: Graph Runtime           (pdf2zh.v3.runtime)
  - Module 7: Document Memory         (pdf2zh.v3.memory)
  - Module 8: Visual Tree             (pdf2zh.v3.visual_tree)
  - Module 9: Quality Evaluator       (pdf2zh.v3.evaluator)
  - Module 10: Execution Runtime      (pdf2zh.v3.scheduler)
  - Module 12: Translation Runtime     (pdf2zh.v3.translator)
  - Module 13: Layout Engine           (pdf2zh.v3.layout)
  - Module 14: Renderer                (pdf2zh.v3.renderer)
  - Module 15: Layout Optimizer        (pdf2zh.v3.optimizer)

Usage:
    from pdf2zh.v3 import (
        Parsing, Normalization, Graph, Analysis,
        TranslationPlanning, QualityEvaluation,
        GraphRuntime, DocumentMemory, VisualTree,
        ServiceRegistry, Scheduler,
    )
"""

from pdf2zh.v3.parser import RawBlock, RawBlockType, RawSpan, PDFParser
from pdf2zh.v3.normalizer import NormalizedBlock, Normalizer, NormalizerConfig
from pdf2zh.v3.graph import (
    DocumentNode, NodeType, Edge, EdgeType,
    DocumentGraph, DocumentGraphBuilder, GraphBuildConfig,
)
from pdf2zh.v3.analyzer import SemanticAnalyzer, AnalyzerConfig
from pdf2zh.v3.planner import (
    TranslationPlanner, TranslationPlan, TranslationChunk,
    ContextWindow, PromptManager, ContextBuilder,
    GlossaryEntry, GlossaryManager,
    ChunkStrategy, ChunkSplitter, PlannerConfig,
)
from pdf2zh.v3.evaluator import (
    EvaluationResult, EvaluatorConfig, QualityEvaluator,
)
from pdf2zh.v3.runtime import (
    GraphRuntime, GraphTransaction, GraphVersion,
    GraphSnapshot, GraphObserver, ChangeRecord,
    TransactionStatus,
)
from pdf2zh.v3.memory import (
    DocumentMemory, DocumentMemorySnapshot,
    EntityEntry, GlossaryEntry as MemoryGlossaryEntry,
    AbbreviationEntry,
)
from pdf2zh.v3.visual_tree import (
    VisualTree, VisualNode, VisualNodeType,
    BoundingBox, Page, Paragraph, Line,
    TextRun, GlyphRun, Image, Formula,
)
from pdf2zh.v3.scheduler import (
    Task, TaskStatus, TaskGraph, Executor, Scheduler,
)
from pdf2zh.v3.service import (
    ServiceRegistry, ServiceInterface,
    ParserService, AnalyzerService, PlannerService,
    TranslatorService, LayoutService, RendererService,
    QAService, MemoryService,
)

from pdf2zh.v3.translator import (
    ModelRoute, ModelRouter, PromptComposer,
    CacheEntry, TranslationCache, TranslationSession, Translator,
    LLMResponse, LLMProvider, MockLLMProvider, OpenAIProvider,
    PostProcessResult, PostProcessor, TranslationStats,
)
from pdf2zh.v3.layout import (
    ConstraintType, LayoutConstraint, Measure, Flow,
    ConstraintSolver, LayoutEngine,
    GlyphMetric, InlineLayout, ColumnRegion, ColumnLayout,
    CollisionRecord, CollisionEngine,
)
from pdf2zh.v3.renderer import (
    RenderContext, Renderer, PDFRenderer, HTMLRenderer,
    MarkdownRenderer, SVGRenderer, DOCXRenderer, RendererFactory,
)
from pdf2zh.v3.optimizer import (
    LayoutElement, OptimizationResult, LayoutOptimizer,
)
from pdf2zh.v3.runtime import RuntimeFacade
from pdf2zh.v3.evaluator import (
    Issue, IssueSeverity, IssueGraph, RepairScheduler,
    DiagnosticRecord, DiagnosticReport, EvaluationIssueMapper,
)


def build_document_graph(
    pdf_path: str,
    lang_in: str = "auto",
    dpi: int = 300,
) -> DocumentGraph:
    """One-shot pipeline: parse → normalize → build graph.

    Args:
        pdf_path: Path to PDF file.
        lang_in: Input language code ('auto' for automatic detection).
        dpi: Rendering DPI for layout analysis.

    Returns:
        A DocumentGraph with initial semantic labels.
    """
    # Module 1: Parse
    parser = PDFParser()
    raw_blocks = parser.parse(pdf_path, dpi=dpi)

    # Module 2: Normalize
    normalizer = Normalizer(NormalizerConfig(lang_in=lang_in))
    normalized = normalizer.normalize(raw_blocks)

    # Module 3: Build graph
    builder = DocumentGraphBuilder()
    graph = builder.build(normalized)

    # Module 4: Analyze
    analyzer = SemanticAnalyzer(AnalyzerConfig(lang_in=lang_in))
    graph = analyzer.analyze(graph)

    return graph


__all__ = [
    # Module 1: Parser
    "RawBlock", "RawBlockType", "RawSpan", "PDFParser",
    # Module 2: Normalizer
    "NormalizedBlock", "Normalizer", "NormalizerConfig",
    # Module 3: Graph
    "DocumentNode", "NodeType", "Edge", "EdgeType",
    "DocumentGraph", "DocumentGraphBuilder", "GraphBuildConfig",
    # Module 4: Analyzer
    "SemanticAnalyzer", "AnalyzerConfig",
    # Module 5: Planner
    "TranslationPlanner", "TranslationPlan", "TranslationChunk",
    "ContextWindow", "PromptManager", "ContextBuilder",
    "GlossaryEntry", "GlossaryManager",
    "ChunkStrategy", "ChunkSplitter", "PlannerConfig",
    # Module 6: Graph Runtime
    "GraphRuntime", "GraphTransaction", "GraphVersion",
    "GraphSnapshot", "GraphObserver", "ChangeRecord",
    "TransactionStatus",
    # Module 7: Document Memory
    "DocumentMemory", "DocumentMemorySnapshot",
    "EntityEntry", "MemoryGlossaryEntry", "AbbreviationEntry",
    # Module 8: Visual Tree
    "VisualTree", "VisualNode", "VisualNodeType",
    "BoundingBox", "Page", "Paragraph", "Line",
    "TextRun", "GlyphRun", "Image", "Formula",
    # Module 9: Evaluator
    "EvaluationResult", "EvaluatorConfig", "QualityEvaluator",
    # Module 10: Execution Runtime
    "Task", "TaskStatus", "TaskGraph", "Executor", "Scheduler",
    # Module 11: Service Registry
    "ServiceRegistry", "ServiceInterface",
    "ParserService", "AnalyzerService", "PlannerService",
    "TranslatorService", "LayoutService", "RendererService",
    "QAService", "MemoryService",
    # Module 12: Translator (Phase 2)
    "ModelRoute", "ModelRouter", "PromptComposer",
    "CacheEntry", "TranslationCache", "TranslationSession", "Translator",
    "LLMResponse", "LLMProvider", "MockLLMProvider", "OpenAIProvider",
    "PostProcessResult", "PostProcessor", "TranslationStats",
    # Module 13: Layout Engine (Phase 2)
    "ConstraintType", "LayoutConstraint", "Measure", "Flow",
    "ConstraintSolver", "LayoutEngine",
    "GlyphMetric", "InlineLayout", "ColumnRegion", "ColumnLayout",
    "CollisionRecord", "CollisionEngine",
    # Module 14: Renderer (Phase 2)
    "RenderContext", "Renderer", "PDFRenderer", "HTMLRenderer",
    "MarkdownRenderer", "SVGRenderer", "DOCXRenderer", "RendererFactory",
    # Module 15: Optimizer (Phase 2)
    "LayoutElement", "OptimizationResult", "LayoutOptimizer",
    # Runtime Facade (Phase 2)
    "RuntimeFacade",
    # Issue Graph (Phase 2)
    "Issue", "IssueSeverity", "IssueGraph", "RepairScheduler",
    "DiagnosticRecord", "DiagnosticReport", "EvaluationIssueMapper",
    # Pipeline
    "build_document_graph",
]
