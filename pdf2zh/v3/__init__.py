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
]
