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
  - V7.0: Operators + Property Graph   (pdf2zh.v3.operators / graph_property)
  - V7.2: State Snapshot (WAL-style)   (pdf2zh.v3.runtime_snapshot)
  - V7.3: Runtime Service              (pdf2zh.v3.runtime_service)
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
# V5 Modules
from pdf2zh.v3.runtime_context import (
    RuntimeConfig, LRUCache, RuntimeContext,
)
from pdf2zh.v3.runtime_kernel import (
    RuntimeKernel, EventBus, Event, EventType, PriorityLevel,
    DeadLetterRecord, NodeStateMachine, NodeLifecycleState,
    DiagnosticCenter, Diagnostic, DiagnosticSeverity,
    MemoryCenter, PluginManager, Plugin, PluginState,
    Capability, CapabilityPlugin,
    TransactionSnapshot, RuntimeTransaction,
    KnowledgeEntry, KnowledgeCenter,
    DiagnosticNode, DiagnosticGraph,
    TelemetrySample, TelemetryCollector,
)
from pdf2zh.v3.storage import (
    StorageTier, StorageStats, MemoryGraph,
    CacheGraph, PersistentGraph, StorageRuntime,
)
from pdf2zh.v3.feature_flags import (
    FeatureFlags, get_feature_flags, set_feature_flags, reset_feature_flags,
)
from pdf2zh.v3.repair import (
    RepairStats, RepairResult, RepairRuntime,
)
from pdf2zh.v3.workflow_engine import (
    WorkflowNodeType, WorkflowNode, WorkflowEngine,
)
from pdf2zh.v3.execution_graph import (
    ExecutionNodeState, ExecutionNode, ExecutionGraph,
)
from pdf2zh.v3.causal_graph import (
    Severity, CausalNode, CausalDiagnosticGraph,
    RepairStatus,
)
from pdf2zh.v3.runtime_supervisor import (
    ResourceUsage, ResourceReport, ResourceManager,
    RecoveryManager, RuntimeSupervisor,
)
from pdf2zh.v3.tracing import (
    TraceSpan, Tracer,
)
# V6 Modules (Constraint Layout, Translation Runtime, Document Intelligence)
from pdf2zh.v3.constraint_graph import (
    ConstraintPriority, ConstraintRelation, ConstraintEdge,
    LayoutNode, ConstraintGraph, ConstraintSolver,
    build_constraint_graph_from_document,
)
from pdf2zh.v3.translation_runtime import (
    ChunkStatus, ConsistencyLevel,
    TranslationChunkResult, TranslationRoute,
    Router, ChunkScheduler, ConsistencyChecker,
    RetryPolicy, TranslationWorkflow, TranslationRuntime,
)
from pdf2zh.v3.document_intelligence import (
    EntityNode, EntityRelation, EntityGraph,
    ConceptNode, ConceptGraph,
    CitationNode, CitationRelation, CitationGraph,
    KnowledgeFuser, DocumentIntelligence,
)
from pdf2zh.v3.visual_tree import DisplayCommand
from pdf2zh.v3.visual_tree_builder import VisualTreeBuilder
# V6.0 Design RFC — 约束布局求解 / 统一渲染适配 / 端到端管线
from pdf2zh.v3.review_agent import (
    ReviewIssue, ReviewResult, ReviewAgent, QualityPipeline,
)
from pdf2zh.v3.relayout_engine import (
    RelayoutConfig, RelayoutResult, ModelSelector,
    RelayoutSolver, OutputAssembler, RelayoutEngine,
)
from pdf2zh.v3.render_adapter import (
    RenderBlock, HTMLFloatRenderer, TextRenderer, RenderAdapter,
)
from pdf2zh.v3.transformation_pipeline import (
    PipelineConfig, PipelineStats, RuleBasedProvider,
    PipelineOutput, TransformationPipeline,
)
# V6.1 Runtime-First — 统一图基础设施（BaseGraph）与文档运行时（DocumentRuntime）
from pdf2zh.v3.base_graph import (
    GraphKind, GraphNode, GraphEdge, GraphProperty,
    GraphTraversal, GraphVisitor, GraphDiff, GraphSnapshot,
    BaseGraph, adapt,
)
from pdf2zh.v3.document_runtime import (
    SessionState, TRANSITIONS, RuntimeCheckpoint,
    DocumentSession, DocumentRuntime,
)
# V7.0-V7.3 Operator-Based Runtime — Document Intelligence Runtime
from pdf2zh.v3.operators import (
    OperatorContext, OperatorGraph, OperatorRegistry,
    ParseOperator, AnalyzeOperator, PlanOperator,
    TranslateOperator, ReviewOperator, LayoutOperator, RenderOperator,
)
from pdf2zh.v3.runtime_snapshot import RuntimeSnapshot, SnapshotDiff
from pdf2zh.v3.graph_property import (
    PropertySchema, PropertyEdge, PropertyQuery, PropertyGraph,
    create_property_graph_from_document,
)
from pdf2zh.v3.runtime_service import (
    ResourceManager, SessionManager, IncrementalPlan,
    IncrementalEngine, ExecutionScheduler, PersistenceLayer,
    RuntimeNotificationBus, RuntimeService,
)
# 阶段七 — Adaptive Typography Engine
from pdf2zh.v3.typography import (
    is_cjk, GlyphMetric, TypographyMetrics, GlyphProbe, AdaptiveTypography,
)
# 阶段十一 — Multi-Agent Pipeline
from pdf2zh.v3.agents import (
    ParserReport, LayoutPlan, TypographyPlan, ReviewOutcome, PipelineReport,
    ParserAgent, LayoutAgent, TypographyAgent, TranslateAgent,
    ReviewerAgent, AgentPipeline,
)
# V8.1 Migration Diff / IR Snapshot Baseline / V8.4 Mainline Gate
from pdf2zh.v3.migration_diff import (
    BlockRecord, normalize_block, dice_similarity, overlap_rate,
    MigrationDiffReport, MigrationDiffHarness, snapshot_ir, SyntheticCorpus,
)
from pdf2zh.v3.mainline_gate import (
    GateBlock, GatedResult, MainlineRelayoutGate,
)
# 阶段二 — Geometry Engine / 阶段三 — Structure Engine
from pdf2zh.v3.geometry import (
    Char, Word, Line, Paragraph, PageGeometry, GeometryConfig,
    GeometryEngine, extract_chars_from_page, extract_chars_from_stream,
    chars_from_ltpage,
)
from pdf2zh.v3.structure import (
    BlockRole, BlockFeatures, ClassifiedBlock,
    compute_features, StructureClassifier, to_document_ir,
)
# V8.6 — Image Translation Engine / Content Preservation Engine
from pdf2zh.v3.image_engine import (
    ImageClass, RenderMode, ImageSource,
    ImageObject, TextRegion, TranslationDecision, RegionDecision,
    ImagePolicy, IMAGE_POLICY,
    ImageFeatures, compute_image_features,
    ImageClassifierBackend, RuleImageClassifier, classify_image,
    detect_text_regions, TranslationDecisionEngine,
    router_should_translate, is_probably_brand_or_technical,
    analyze_image_bytes, analyze_pdf_images,
)
from pdf2zh.v3.content_preservation import (
    PreservationAction, PreservationDecision, DelegateSpec,
    ROLE_DEFAULT, ContentPreservationEngine, classify_node,
)
# V8.7 — TOC Semantic Rendering（目录语义渲染）
from pdf2zh.v3.toc_semantics import (
    TOCKind, TOCEntry, parse_toc_entry,
    TOC_TEMPLATES, toc_structure_prefix,
    TOCTranslationPolicy, compose_toc_title, render_toc_line,
)
# V9.0 — 单一核心 IR + Node Processors（AST-Pass / ECS-System 层）
from pdf2zh.v3.processors import (
    NodeStage, STAGE_KEY, SEMANTIC_KEY, POLICY_KEY,
    ORIGINAL_KEY, TRANSLATED_KEY, RENDER_KEY,
    get_semantic, set_policy,
    NodeProcessor, ProcessorRegistry, default_processor_registry,
    TOCSemanticProcessor, FormulaNodeProcessor, CodeNodeProcessor,
    ImageTranslationProcessor, ContentPolicyProcessor,
    CaptionNodeProcessor, TableNodeProcessor, ReferenceNodeProcessor,
)
from pdf2zh.v3.document_pipeline import (
    DEFAULT_STAGES, PipelineReport, DocumentPipeline,
    run_semantic_pipeline, view_as_ir,
)
# V9.0 P1/P2 — Processor 层主链路接线与渲染/融合/翻译/OCR 顾问
from pdf2zh.v3.render_advisor import (
    RenderAdvisor, RoutingDecision,
    PATH_TRANSLATE_REFIT, PATH_SHIFT_DOWN, PATH_PRESERVE_FLOAT,
    PATH_OVERLAY, PATH_BLOCK,
)
from pdf2zh.v3.geometry_merge import (
    GeometryMergeReport, dice_similarity,
    rows_from_geometry, merge_geometry_and_legacy,
)
from pdf2zh.v3.structure_fusion import (
    FusionReport, StructureFusion,
)
from pdf2zh.v3.translation_advisor import (
    KEEP_ROUTE, TRANSLATE_ROUTE,
    RouteVerdict, MainlineTranslationRouter, LLMRefiner,
    TranslationAdvisorReport, TranslationAdvisor,
    TranslationAdvisorProcessor,
)
from pdf2zh.v3.ocr_engine import (
    OCRBackend, DeterministicOCRBackend,
    ocr_regions_into_object, ocr_into_pixels,
)
from pdf2zh.v3.image_renderer import (
    render_preserve, render_region_replace,
    render_overlay, render_full_repaint, render_image_decision,
)
from pdf2zh.v3.image_calibrate import (
    CalibrationSample, CalibrationReport,
    accuracy, calibrate,
)
from pdf2zh.v3.ir_convergence import (
    DEPRECATED_VIEWS, DEPRECATION_NOTE,
    deprecated_note, converged_snapshot, snapshot_consistency,
)
from pdf2zh.v3.toc_semantics import toc_to_ir_records
from pdf2zh.v3.mainline_wiring import (
    run_mainline_channels, run_processor_channels,
    run_toc_channel, emit_page_ir, run_writeback_gate,
    run_render_takeover, run_translation_qa_channel,
)
from pdf2zh.v3.image_pipeline import (
    ImageRenderSummary, PlateRenderer, SolidPlateRenderer,
    PillowPlateRenderer, decide_with_ocr, translate_image_pixels,
)
from pdf2zh.v3.render_takeover import (
    WritebackBlock, plan_writeback_takeover, apply_render_plan,
)
from pdf2zh.v3.mainline_qa import (
    QARecorder, TranslationQAReport, run_translation_qa,
)
from pdf2zh.v3.geometry_merge import (
    AdoptedParagraph, adopt_geometry_cluster,
)
from pdf2zh.v3.pipeline_dump import (
    has_replacement, glyph_dump, run_dump, line_dump, block_dump,
    toc_confidence, toc_dump, translation_dump, layout_dump,
    dump_page, dump_pdf_pipeline, main as pipeline_dump_main,
)
from pdf2zh.v3.toc_tree import build_toc_tree
from pdf2zh.v3.toc_analyzer import (
    TOCEntry, parse_entry_text, split_merged_block, analyze_toc_blocks,
    split_toc_blocks, rebuild_toc_page, analyze_toc_result,
    render_toc_entry, split_merged_toc_paragraphs, _physical_rows,
)
from pdf2zh.v3.canonical_page import (
    GlyphModel, SpanModel, LineModel, BlockModel, PageModel,
    build_page_model, annotate_toc, annotate_toc_scan,
    annotate_formulas, annotate_style,
)
from pdf2zh.v3.document_model import (
    REL_FOLLOWS, REL_TOC_CHILD_OF, REL_CAPTION_OF,
    block_id, Relation, DocumentModel,
    annotate_roles, annotate_translation, annotate_render,
    build_document_model, translate_document, render_plan_from_model,
    toc_records_from_model, annotate_translation_from_records,
)
from pdf2zh.v3.mainline_wiring import (
    run_document_model,
)
from pdf2zh.v3.doc_passes import (
    DocumentPass, PassDiffEntry, PassResult, PassRunReport, PassManager,
    normalize_text, NormalizePass, SemanticPass, detect_code_block,
    detect_table_block, TranslationPolicyPass, translation_policy_for,
    TypographyPass, default_pass_manager,
)
from pdf2zh.v3.document_inspector import inspect, inspect_all, inspect_toc
from pdf2zh.v3.typography_engine import (
    build_width_map, measure, line_break, justify_advances,
    widow_orphan_flag,
)
from pdf2zh.v3.semantic_graph import (
    REL_BELONGS_TO, REL_MENTIONS,
    section_number, build_sections, detect_mentions,
    resolve_mentions, build_semantic_relations,
)
from pdf2zh.v3.domain_glossary import (
    DEFAULT_GLOSSARIES, detect_domain, DomainGlossary,
)
from pdf2zh.v3.context_translation import (
    document_context_for, context_to_prompt,
    translate_document_context_aware,
)
from pdf2zh.v3.references import resolve_references, renumber_references
from pdf2zh.v3.figure_understanding import (
    STRATEGY_MAP, figure_strategy, annotate_figures,
)
from pdf2zh.v3.incremental import node_hash, IncrementalEngine
from pdf2zh.v3.diagnostics import (
    CODE_SEVERITY, DiagnosticIssue, DiagnosticReport,
    analyze_document, node_confidence, annotate_confidence,
)
from pdf2zh.v3.evidence import (
    DEFAULT_WEIGHTS, fuse_evidence, FusedVerdict, fuse_verdict,
)
from pdf2zh.v3.repair_engine import (
    RepairResult, RepairReport, RepairStrategy,
    TOCSplitRepair, UnicodeRepair, MathRecoveryRepair, EmptyBlockRepair,
    DEFAULT_STRATEGIES, RepairEngine, repair_loop,
)
from pdf2zh.v3.llm_planner import (
    RULE_MAP, RepairPlanner, RuleRepairPlanner, LLMRepairPlanner,
)
from pdf2zh.v3.corpus_regression import (
    expected_from_model, compare_expected,
    RegressionResult, RegressionReport, run_regression,
)
from pdf2zh.v3.resources import FontResource, ImageResource, ResourceManager
from pdf2zh.v3.query import DocumentQuery, query
from pdf2zh.v3.cache import LayerCache, CacheStats, DocumentCache
from pdf2zh.v3.exports import export_markdown, export_html, export_text
from pdf2zh.v3.plugins import (
    DocumentPlugin, PassPlugin, TranslatePlugin, ExportPlugin,
    PluginRegistry,
)
from pdf2zh.v3.build_system import (
    STAGE_ORDER, BuildPlan, DependencyGraph, BuildSystem,
)
from pdf2zh.v3.runtime_doc import NodeRevision, VersionManager, DocumentRuntime
from pdf2zh.v3.mainline_wiring import (
    run_pipeline_dump,
)
# Phase D (v1.18): Document Observability Framework（D0–D9）
from pdf2zh.v3.observability import (
    STAGES, ROLE_COLORS, new_document_id, DocumentID, NodeID,
    TraceContext, SnapshotStore, capture_snapshot, DecisionLog,
    DecisionRecord, DiagnosticEngine, ObsSession, make_session,
)
from pdf2zh.v3.pass_diff import (
    FieldDiff, PassDiffReport, diff_snapshots, diff_json,
    render_diff_report,
)
from pdf2zh.v3.layout_debug import (
    LineMetrics, line_metrics_from_page, line_metrics_from_snapshot,
    render_svg as layout_render_svg, render_html as layout_render_html,
    metrics_json,
)
from pdf2zh.v3.overlay_view import (
    OverlayRecord, overlay_for_page, overlay_from_snapshot,
    render_svg as overlay_render_svg, render_html as overlay_render_html,
    records_json,
)
from pdf2zh.v3.replay import (
    StageInput, StageInputStore, TranslationMemo, ReplayStep,
    ReplayReport, ReplaySystem,
)
from pdf2zh.v3.inspector_view import (
    build_inspector_html, build_inspector_html_from_bundle,
)
from pdf2zh.v3.regression import (
    canonical_json, snapshot_hash, record_for, build_baseline_dir,
    diff_records, diff_baselines, run_snapshot_regression, record_session,
)

__all__ = [
    "RawBlock", "RawBlockType", "RawSpan", "PDFParser",
    "NormalizedBlock", "Normalizer", "NormalizerConfig",
    "DocumentNode", "NodeType", "Edge", "EdgeType",
    "DocumentGraph", "DocumentGraphBuilder", "GraphBuildConfig",
    "SemanticAnalyzer", "AnalyzerConfig",
    "TranslationPlanner", "TranslationPlan", "TranslationChunk",
    "ContextWindow", "PromptManager", "ContextBuilder",
    "GlossaryEntry", "GlossaryManager",
    "ChunkStrategy", "ChunkSplitter", "PlannerConfig",
    "GraphRuntime", "GraphTransaction", "GraphVersion",
    "GraphSnapshot", "GraphObserver", "ChangeRecord",
    "TransactionStatus",
    "DocumentMemory", "DocumentMemorySnapshot", "EntityEntry",
    "VisualTree", "VisualNode", "VisualNodeType",
    "BoundingBox", "Page", "Paragraph", "Line",
    "TextRun", "GlyphRun", "Image", "Formula",
    "Task", "TaskStatus", "TaskGraph", "Executor", "Scheduler",
    "ServiceRegistry", "ServiceInterface",
    "ModelRoute", "ModelRouter", "PromptComposer",
    "CacheEntry", "TranslationCache", "TranslationSession", "Translator",
    "ConstraintType", "LayoutConstraint", "Measure", "Flow",
    "ConstraintSolver", "LayoutEngine",
    "CollisionRecord", "CollisionEngine",
    "RenderContext", "Renderer", "PDFRenderer",
    "HTMLRenderer", "MarkdownRenderer", "SVGRenderer",
    "DOCXRenderer", "RendererFactory",
    "LayoutElement", "OptimizationResult", "LayoutOptimizer",
    "DisplayCommand",
    "VisualTreeBuilder",
    "RuntimeFacade",
    "Issue", "IssueSeverity", "IssueGraph", "RepairScheduler",
    "EvaluationResult", "EvaluatorConfig", "QualityEvaluator",
    "DiagnosticRecord", "DiagnosticReport", "EvaluationIssueMapper",
    "RuntimeConfig", "RuntimeContext",
    "LRUCache",
    "WorkflowNodeType", "WorkflowNode", "WorkflowEngine",
    "ExecutionNodeState", "ExecutionNode", "ExecutionGraph",
    "Severity", "CausalNode", "CausalDiagnosticGraph", "RepairStatus",
    "ResourceUsage", "ResourceReport", "ResourceManager",
    "RecoveryManager", "RuntimeSupervisor",
    "TraceSpan", "Tracer",
    # V5 Runtime Kernel & Infrastructure
    "RuntimeKernel", "EventBus", "Event", "EventType", "PriorityLevel",
    "DeadLetterRecord", "NodeStateMachine", "NodeLifecycleState",
    "DiagnosticCenter", "Diagnostic", "DiagnosticSeverity",
    "MemoryCenter", "PluginManager", "Plugin", "PluginState",
    "Capability", "CapabilityPlugin",
    "TransactionSnapshot", "RuntimeTransaction",
    "KnowledgeEntry", "KnowledgeCenter",
    "DiagnosticNode", "DiagnosticGraph",
    "TelemetrySample", "TelemetryCollector",
    "StorageTier", "StorageStats", "MemoryGraph",
    "CacheGraph", "PersistentGraph", "StorageRuntime",
    "FeatureFlags", "get_feature_flags", "set_feature_flags", "reset_feature_flags",
    "RepairStats", "RepairResult", "RepairRuntime",
    # V6
    "ConstraintPriority", "ConstraintRelation", "ConstraintEdge",
    "LayoutNode", "ConstraintGraph", "ConstraintSolver", "KiwiSolver",
    "build_constraint_graph_from_document",
    "ChunkStatus", "ConsistencyLevel",
    "TranslationChunkResult", "TranslationRoute",
    "Router", "ChunkScheduler", "ConsistencyChecker",
    "RetryPolicy", "TranslationWorkflow", "TranslationRuntime",
    "EntityNode", "EntityRelation", "EntityGraph",
    "ConceptNode", "ConceptGraph",
    "CitationNode", "CitationRelation", "CitationGraph",
    "KnowledgeFuser", "DocumentIntelligence",
    # V6.0 Design RFC — 约束布局求解 / 统一渲染适配 / 端到端管线
    "ReviewIssue", "ReviewResult", "ReviewAgent", "QualityPipeline",
    "RelayoutConfig", "RelayoutResult", "ModelSelector",
    "RelayoutSolver", "OutputAssembler", "RelayoutEngine",
    "RenderBlock", "HTMLFloatRenderer", "TextRenderer", "RenderAdapter",
    "PipelineConfig", "PipelineStats", "RuleBasedProvider",
    "PipelineOutput", "TransformationPipeline",
    # V6.1 Runtime-First
    "GraphKind", "GraphNode", "GraphEdge", "GraphProperty",
    "GraphTraversal", "GraphVisitor", "GraphDiff", "GraphSnapshot",
    "BaseGraph", "adapt",
    "SessionState", "TRANSITIONS", "RuntimeCheckpoint",
    "DocumentSession", "DocumentRuntime",
    # V7.0-V7.3 Operator-Based Runtime — Document Intelligence Runtime
    "OperatorContext", "OperatorGraph", "OperatorRegistry",
    "ParseOperator", "AnalyzeOperator", "PlanOperator",
    "TranslateOperator", "ReviewOperator", "LayoutOperator",
    "RenderOperator",
    "RuntimeSnapshot", "SnapshotDiff",
    "PropertySchema", "PropertyEdge", "PropertyQuery", "PropertyGraph",
    "create_property_graph_from_document",
    "ResourceManager", "SessionManager", "IncrementalPlan",
    "IncrementalEngine", "ExecutionScheduler", "PersistenceLayer",
    "RuntimeNotificationBus", "RuntimeService",
    # 阶段七 Adaptive Typography / 阶段十一 Agents / V8.1+V8.4
    "is_cjk", "TypographyMetrics", "GlyphProbe", "AdaptiveTypography",
    "ParserReport", "LayoutPlan", "TypographyPlan", "ReviewOutcome",
    "PipelineReport", "ParserAgent", "LayoutAgent", "TypographyAgent",
    "TranslateAgent", "ReviewerAgent", "AgentPipeline",
    "BlockRecord", "normalize_block", "dice_similarity", "overlap_rate",
    "MigrationDiffReport", "MigrationDiffHarness", "snapshot_ir",
    "SyntheticCorpus", "GateBlock", "GatedResult", "MainlineRelayoutGate",
    # 阶段二/三 — Geometry + Structure Engine
    "Char", "Word", "Line", "Paragraph", "PageGeometry",
    "GeometryConfig", "GeometryEngine",
    "extract_chars_from_page", "extract_chars_from_stream", "chars_from_ltpage",
    "BlockRole", "BlockFeatures", "ClassifiedBlock",
    "compute_features", "StructureClassifier", "to_document_ir",
    # V8.6 — Image Translation Engine / Content Preservation Engine
    "ImageClass", "RenderMode", "ImageSource",
    "ImageObject", "TextRegion", "TranslationDecision", "RegionDecision",
    "ImagePolicy", "IMAGE_POLICY",
    "ImageFeatures", "compute_image_features",
    "ImageClassifierBackend", "RuleImageClassifier", "classify_image",
    "detect_text_regions", "TranslationDecisionEngine",
    "router_should_translate", "is_probably_brand_or_technical",
    "analyze_image_bytes", "analyze_pdf_images",
    "PreservationAction", "PreservationDecision", "DelegateSpec",
    "ROLE_DEFAULT", "ContentPreservationEngine", "classify_node",
    # V8.7 — TOC Semantic Rendering
    "TOCKind", "TOCEntry", "parse_toc_entry",
    "TOC_TEMPLATES", "toc_structure_prefix",
    "TOCTranslationPolicy", "compose_toc_title", "render_toc_line",
    # V9.0 — 单一核心 IR + Node Processors
    "NodeStage", "STAGE_KEY", "SEMANTIC_KEY", "POLICY_KEY",
    "ORIGINAL_KEY", "TRANSLATED_KEY", "RENDER_KEY",
    "get_semantic", "set_policy",
    "NodeProcessor", "ProcessorRegistry", "default_processor_registry",
    "TOCSemanticProcessor", "FormulaNodeProcessor", "CodeNodeProcessor",
    "ImageTranslationProcessor", "ContentPolicyProcessor",
    "CaptionNodeProcessor", "TableNodeProcessor", "ReferenceNodeProcessor",
    "DEFAULT_STAGES", "PipelineReport", "DocumentPipeline",
    "run_semantic_pipeline", "view_as_ir",
    # V9.0 P1/P2 — Processor 主链路接线与顾问模块
    "RenderAdvisor", "RoutingDecision",
    "PATH_TRANSLATE_REFIT", "PATH_SHIFT_DOWN", "PATH_PRESERVE_FLOAT",
    "PATH_OVERLAY", "PATH_BLOCK",
    "GeometryMergeReport", "dice_similarity",
    "rows_from_geometry", "merge_geometry_and_legacy",
    "FusionReport", "StructureFusion",
    "KEEP_ROUTE", "TRANSLATE_ROUTE",
    "RouteVerdict", "MainlineTranslationRouter", "LLMRefiner",
    "TranslationAdvisorReport", "TranslationAdvisor",
    "TranslationAdvisorProcessor",
    "OCRBackend", "DeterministicOCRBackend",
    "ocr_regions_into_object", "ocr_into_pixels",
    "render_preserve", "render_region_replace",
    "render_overlay", "render_full_repaint", "render_image_decision",
    "CalibrationSample", "CalibrationReport",
    "accuracy", "calibrate",
    "DEPRECATED_VIEWS", "DEPRECATION_NOTE",
    "deprecated_note", "converged_snapshot", "snapshot_consistency",
    "toc_to_ir_records",
    "run_mainline_channels", "run_processor_channels",
    "run_toc_channel", "emit_page_ir", "run_writeback_gate",
    "run_render_takeover", "run_translation_qa_channel",
    "ImageRenderSummary", "PlateRenderer", "SolidPlateRenderer",
    "PillowPlateRenderer", "decide_with_ocr", "translate_image_pixels",
    "WritebackBlock", "plan_writeback_takeover", "apply_render_plan",
    "QARecorder", "TranslationQAReport", "run_translation_qa",
    "AdoptedParagraph", "adopt_geometry_cluster",
    "has_replacement", "glyph_dump", "run_dump", "line_dump", "block_dump",
    "toc_confidence", "toc_dump", "translation_dump", "layout_dump",
    "dump_page", "dump_pdf_pipeline", "pipeline_dump_main",
    "build_toc_tree", "run_pipeline_dump",
    "TOCEntry", "parse_entry_text", "split_merged_block",
    "analyze_toc_blocks", "split_toc_blocks", "rebuild_toc_page",
    "analyze_toc_result", "render_toc_entry",
    "split_merged_toc_paragraphs", "_physical_rows",
    "GlyphModel", "SpanModel", "LineModel", "BlockModel", "PageModel",
    "build_page_model", "annotate_toc", "annotate_toc_scan",
    "annotate_formulas", "annotate_style",
    "REL_FOLLOWS", "REL_TOC_CHILD_OF", "REL_CAPTION_OF",
    "block_id", "Relation", "DocumentModel",
    "annotate_roles", "annotate_translation", "annotate_render",
    "build_document_model", "translate_document", "render_plan_from_model",
    "toc_records_from_model", "annotate_translation_from_records",
    "run_document_model",
    "DocumentPass", "PassDiffEntry", "PassResult", "PassRunReport",
    "PassManager", "normalize_text", "NormalizePass",
    "SemanticPass", "detect_code_block", "detect_table_block",
    "TranslationPolicyPass", "translation_policy_for",
    "TypographyPass", "default_pass_manager",
    "inspect", "inspect_all", "inspect_toc",
    "build_width_map", "measure", "line_break", "justify_advances",
    "widow_orphan_flag",
    "REL_BELONGS_TO", "REL_MENTIONS",
    "section_number", "build_sections", "detect_mentions",
    "resolve_mentions", "build_semantic_relations",
    "DEFAULT_GLOSSARIES", "detect_domain", "DomainGlossary",
    "document_context_for", "context_to_prompt",
    "translate_document_context_aware",
    "resolve_references", "renumber_references",
    "STRATEGY_MAP", "figure_strategy", "annotate_figures",
    "node_hash", "IncrementalEngine",
    "CODE_SEVERITY", "DiagnosticIssue", "DiagnosticReport",
    "analyze_document", "node_confidence", "annotate_confidence",
    "DEFAULT_WEIGHTS", "fuse_evidence", "FusedVerdict", "fuse_verdict",
    "RepairResult", "RepairReport", "RepairStrategy",
    "TOCSplitRepair", "UnicodeRepair", "MathRecoveryRepair",
    "EmptyBlockRepair", "DEFAULT_STRATEGIES", "RepairEngine", "repair_loop",
    "RULE_MAP", "RepairPlanner", "RuleRepairPlanner", "LLMRepairPlanner",
    "expected_from_model", "compare_expected",
    "RegressionResult", "RegressionReport", "run_regression",
    "FontResource", "ImageResource", "ResourceManager",
    "DocumentQuery", "query",
    "LayerCache", "CacheStats", "DocumentCache",
    "export_markdown", "export_html", "export_text",
    "DocumentPlugin", "PassPlugin", "TranslatePlugin", "ExportPlugin",
    "PluginRegistry",
    "STAGE_ORDER", "BuildPlan", "DependencyGraph", "BuildSystem",
    "NodeRevision", "VersionManager", "DocumentRuntime",
    # Phase D (v1.18): Document Observability Framework（D0–D9）
    "STAGES", "ROLE_COLORS", "new_document_id", "DocumentID", "NodeID",
    "TraceContext", "SnapshotStore", "capture_snapshot", "DecisionLog",
    "DecisionRecord", "DiagnosticEngine", "ObsSession", "make_session",
    "FieldDiff", "PassDiffReport", "diff_snapshots", "diff_json",
    "render_diff_report",
    "LineMetrics", "line_metrics_from_page", "line_metrics_from_snapshot",
    "layout_render_svg", "layout_render_html", "metrics_json",
    "OverlayRecord", "overlay_for_page", "overlay_from_snapshot",
    "overlay_render_svg", "overlay_render_html", "records_json",
    "StageInput", "StageInputStore", "TranslationMemo", "ReplayStep",
    "ReplayReport", "ReplaySystem",
    "build_inspector_html", "build_inspector_html_from_bundle",
    "canonical_json", "snapshot_hash", "record_for", "build_baseline_dir",
    "diff_records", "diff_baselines", "run_snapshot_regression",
    "record_session",
]
