"""Unified RuntimeService — Multi-End Service Layer for V4/V5 Kernel.

Purpose:
  Provides a single, thread-safe service that wraps RuntimeFacade for
  Gradio UI, Flask REST API, and MCP Server.

Lifecycle:
    service = RuntimeService()
    task_id = service.submit_task(TranslationRequest(...))
    for event in service.subscribe_events(task_id):
        print(event.stage, event.progress)
    state = service.get_task_state(task_id)
    service.cancel_task(task_id)
"""

from __future__ import annotations

import dataclasses
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Iterator, List, Optional

logger = logging.getLogger(__name__)


# 自愈策略映射：诊断 issue code -> 修复动作（与 v3/repair_engine 策略一致）。
_HEAL_ACTIONS = {
    "unicode_error": "Unicode 修复 (OCR 计划)",
    "toc_merged_lines": "TOC 拆分重切 (TOCSplitRepair)",
    "toc_low_confidence": "TOC 拆分重切 (TOCSplitRepair)",
    "formula_low_confidence": "公式重建 (MathRecoveryRepair)",
    "translation_overflow": "溢出重排 (EmptyBlockRepair)",
    "font_uncertain": "字体归一化",
    "empty_block": "空块清理",
}

# ── 引擎翻译模式预设 ─────────────────────────────────────────────────────────
#
# GUI「引擎模式」下拉的 v0..v4 五种模式 → 具体管线。每个模式是 ServiceConfig
# 字段的覆盖集合；"auto" 表示保持调用方配置（语义上等价于默认模式）。
# 落地管线只有两条（都是已实现路径）：
#   - legacy：``translate_stream`` 经典管线（v0/v1/v2）
#   - V4：``_execute_v4``（RuntimeFacade 全流程，v3/v4）
MODE_PRESETS: Dict[str, Dict[str, Any]] = {
    # v0 基础：纯 legacy 经典路径，关闭全部现代 side-channel（最快、干预最少）
    "v0": {
        "use_v4_engine": False,
        "use_v4_translator": False,
        "use_v4_layout": False,
        "use_v4_repair": False,
        "use_v4_fix_validate_loop": False,
        "run_evaluation": False,
        "emit_ir": False,
        "use_v4_gate": False,
        "relink_links": False,
        "image_engine": False,
        "content_preservation": False,
        "processor_channels": False,
    },
    # v1 标准：legacy + 全部现代 side-channel + 文档模型（当前生产默认行为）
    "v1": {
        "use_v4_engine": False,
        "use_v4_translator": False,
        "use_v4_layout": False,
        "use_v4_repair": False,
        "use_v4_fix_validate_loop": False,
        "use_v4_gate": False,
        "run_evaluation": False,
        "emit_ir": True,
        "relink_links": True,
        "image_engine": True,
        "content_preservation": True,
        "processor_channels": True,
    },
    # v2 高质量：legacy + 文档级评测 + 写回门控 + QA 侧通道
    "v2": {
        "use_v4_engine": False,
        "use_v4_translator": False,
        "use_v4_layout": False,
        "use_v4_repair": False,
        "use_v4_fix_validate_loop": False,
        "use_v4_gate": True,
        "run_evaluation": True,
        "emit_ir": True,
        "relink_links": True,
        "image_engine": True,
        "content_preservation": True,
        "processor_channels": True,
    },
    # v3 精准：V4 引擎 + 评测（自带 QualityEvaluator）+ 写回门控
    "v3": {
        "use_v4_engine": True,
        "use_v4_translator": True,
        "use_v4_layout": False,
        "use_v4_repair": False,
        "use_v4_fix_validate_loop": False,
        "use_v4_gate": True,
        "run_evaluation": False,  # V4 内部自带评测，不需文档级二次评测
        "relink_links": True,
        "image_engine": False,
        "content_preservation": True,
        "processor_channels": True,
    },
    # v4 布局优先：V4 引擎 + 布局/修复 + Fix-Validate 自愈循环
    "v4": {
        "use_v4_engine": True,
        "use_v4_translator": True,
        "use_v4_layout": True,
        "use_v4_repair": True,
        "use_v4_fix_validate_loop": True,
        "max_repair_passes": 2,
        "use_v4_gate": True,
        "run_evaluation": False,
        "relink_links": True,
        "image_engine": False,
        "content_preservation": True,
        "processor_channels": True,
    },
}

#: legacy 模式注入 translate_stream 的额外模态 kwargs（V4 模式不走此表）。
MODE_LEGACY_KWARGS: Dict[str, Dict[str, Any]] = {
    "v0": {"document_model": False, "toc_split": True,
           "render_takeover": False, "translation_qa": False,
           "geometry_cluster": False, "observability": False,
           "pipeline_dump": False},
    "v1": {"document_model": True, "toc_split": True},
    "v2": {"document_model": True, "toc_split": True,
           "render_takeover": True, "translation_qa": True,
           "geometry_cluster": True},
}


def resolve_mode_config(
    mode_choice: Optional[str], base: ServiceConfig,
) -> ServiceConfig:
    """Resolve an engine-mode preset onto a ``ServiceConfig``.

    Unknown/empty modes and ``auto`` return the base config unchanged
    (auto-selection keeps the caller-provided defaults). Unknown preset
    keys are filtered so a future flag never breaks resolution.
    """
    preset = MODE_PRESETS.get((mode_choice or "auto").strip() or "auto")
    if not preset:
        return base
    overrides = {k: v for k, v in preset.items() if hasattr(base, k)}
    return dataclasses.replace(base, **overrides)


def legacy_mode_kwargs(mode_choice: Optional[str]) -> Dict[str, Any]:
    """Extra ``translate_stream`` kwargs for legacy engine modes."""
    return dict(MODE_LEGACY_KWARGS.get((mode_choice or "auto").strip() or "auto", {}) or {})


# ── Data Models ──────────────────────────────────────────────────────────────


class TaskStage(str, Enum):
    PENDING = "pending"
    PARSING = "parsing"
    NORMALIZING = "normalizing"
    ANALYZING = "analyzing"
    PLANNING = "planning"
    TRANSLATING = "translating"
    LAYOUTING = "layouting"
    RENDERING = "rendering"
    EVALUATING = "evaluating"
    REPAIRING = "repairing"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass
class TranslationRequest:
    """Strong-typed request replacing the 21-parameter tuple pattern.

    ``files`` (batch mode) takes precedence over the legacy single-file
    ``source_path``. Both may be provided for compatibility; ``resolved_files()``
    returns the effective list of files to process.
    """

    source_path: str = ""
    files: List[str] = field(default_factory=list)
    target_lang: str = "zh-CN"
    source_lang: str = "auto"
    engine: str = "google"
    output_formats: List[str] = field(default_factory=lambda: ["pdf"])
    enable_repair: bool = True
    page_range: Optional[str] = None
    vfont: str = ""
    vchar: str = ""
    threads: int = 4
    skip_subset_fonts: bool = False
    ignore_cache: bool = False
    extra_config: Dict[str, Any] = field(default_factory=dict)

    def resolved_files(self) -> List[str]:
        """Return the effective list of files to translate (batch or single)."""
        files = [f for f in (self.files or []) if f and f.strip()]
        if not files and self.source_path and self.source_path.strip():
            files = [self.source_path]
        return files

    def to_dict(self) -> Dict[str, Any]:
        return {
            "lang_in": self.source_lang,
            "lang_out": self.target_lang,
            "service": self.engine,
            "vfont": self.vfont,
            "vchar": self.vchar,
            "thread": self.threads,
            "pages": self.page_range,
            "skip_subset_fonts": self.skip_subset_fonts,
            "ignore_cache": self.ignore_cache,
            **self.extra_config,
        }


@dataclass
class TaskProgressEvent:
    """Real-time progress event from RuntimeKernel EventBus."""

    task_id: str
    stage: str
    progress: float
    current_node_count: int = 0
    diagnostics_count: int = 0
    message: str = ""
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "stage": self.stage,
            "progress": self.progress,
            "current_node_count": self.current_node_count,
            "diagnostics_count": self.diagnostics_count,
            "message": self.message,
            "timestamp": self.timestamp,
        }


@dataclass
class _BatchContext:
    """Per-task aggregation state for multi-file (batch) translation.

    Tracks how many files completed / failed so per-file progress events can
    be folded into one smooth overall progress for the whole task.
    """

    total_files: int
    completed_files: int = 0
    failed_files: int = 0
    current_file: str = ""


@dataclass
class TaskState:
    """Type-safe task state replacing bare dict (20+ fields)."""

    task_id: str
    status: str = TaskStage.PENDING.value
    progress: float = 0.0
    message: str = ""
    stage: str = ""
    mode_choice: str = ""
    """用户选择的引擎模式（auto/v0/v1/v2/v3/v4），用于可观测与模式解析。"""
    file_progress: float = 0.0
    total_progress: float = 0.0
    current_file_name: str = ""
    file_list: List[str] = field(default_factory=list)
    total_files: int = 0
    completed_files: int = 0
    failed_files: int = 0
    file_failures: List[Dict[str, Any]] = field(default_factory=list)
    result_files: List[Dict[str, str]] = field(default_factory=list)
    selected_file: Optional[str] = None
    result_zip: Optional[str] = None
    preview_path: Optional[str] = None
    diagnostic_summary: Optional[str] = None
    quality_scores: Optional[Dict[str, float]] = None
    node_overview: Optional[Dict[str, int]] = None
    """文档智能分析概况（pages/paragraphs/headings/figures/formulas 节点计数）。"""
    diagnostic_report: Optional[Dict[str, Any]] = None
    """结构化诊断报告：legacy 为 errors/warnings/admissible/issues，
    V4 为 evaluator 的 records/pass_rate 记录。"""
    heal_status: Optional[Dict[str, Any]] = None
    """自愈行程摘要（ran/iterations/before_errors/after_errors/improved）。"""
    repair_records: Optional[List[Dict[str, Any]]] = None
    """自愈处置记录：每个 issue -> {code, node_id, page, severity, message, action, status}。"""
    confidence_stats: Optional[Dict[str, float]] = None
    """文档置信度统计（annotated/avg/min/max）。"""
    ir_snapshots: Optional[Dict[str, Any]] = None
    """V8.3: 主链路产出的 DocumentIR 快照（pageid -> snapshot dict）。"""
    gate_verdicts: Optional[Dict[str, Any]] = None
    """V8.4: 写回门控裁决（pageid -> GatedResult.to_dict()）。"""
    processor_reports: Optional[Dict[str, Any]] = None
    """V9.0: Processor 语义通道报告（pageid -> PipelineReport.to_dict()）。"""
    toc_ir_records: Optional[Dict[str, Any]] = None
    """V9.0: 目录条目 IR 结构化记录（pageid -> toc_to_ir_records 输出）。"""
    error_message: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "progress": self.progress,
            "message": self.message,
            "stage": self.stage,
            "mode_choice": self.mode_choice,
            "file_progress": self.file_progress,
            "total_progress": self.total_progress,
            "current_file_name": self.current_file_name,
            "file_list": self.file_list,
            "total_files": self.total_files,
            "completed_files": self.completed_files,
            "failed_files": self.failed_files,
            "file_failures": self.file_failures,
            "result_files": self.result_files,
            "selected_file": self.selected_file,
            "result_zip": self.result_zip,
            "preview_path": self.preview_path,
            "diagnostic_summary": self.diagnostic_summary,
            "quality_scores": self.quality_scores,
            "node_overview": self.node_overview,
            "diagnostic_report": self.diagnostic_report,
            "heal_status": self.heal_status,
            "repair_records": self.repair_records,
            "confidence_stats": self.confidence_stats,
            "ir_snapshots": self.ir_snapshots,
            "gate_verdicts": self.gate_verdicts,
            "processor_reports": self.processor_reports,
            "toc_ir_records": self.toc_ir_records,
            "error_message": self.error_message,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class ServiceConfig:
    max_concurrency: int = 4
    use_v4_engine: bool = False
    use_v4_translator: bool = False
    use_v4_layout: bool = False
    use_v4_repair: bool = False
    use_v4_fix_validate_loop: bool = False
    """V4：Fix-Validate 修复自愈循环（pipeline 内最多 max_repair_passes 轮）。"""
    max_repair_passes: int = 2
    run_evaluation: bool = False
    """翻译完成后对输出 mono PDF 运行文档级评测（几何/结构/翻译/渲染）。"""
    # V8.3: legacy 主链路直接产出 DocumentIR（side-channel，不影响渲染）
    emit_ir: bool = False
    # V8.4: legacy 内容流写回前的主链路重排版门控
    use_v4_gate: bool = False
    # V8.5: 译文页面超链接 /Rect 重定位（默认开，side-channel 数据缺失时自动跳过）
    relink_links: bool = True
    # V8.6: 图片翻译决策层（独立 side-channel，不影响 legacy 主链路渲染在前台默认关闭）
    image_engine: bool = False
    content_preservation: bool = False
    # V8.6: 是否把图片决策回传 task state / v3_output（side-channel）
    emit_preservation: bool = True
    # V9.0: Processor 层（RAW/SEMANTIC 语义通道 + TOC 结构化记录）挂主链路
    # v1.6 双轨对比（开/关输出恒等）后默认翻转开
    processor_channels: bool = True
    # P1: 评测 <90 分自动留存的报告目录（report_dir/<basename>/）
    evaluation_report_dir: str = ""
    output_dir: str = ""


# ── Thread-Safe Task Store ───────────────────────────────────────────────────


class _TaskStore:
    """Thread-safe in-memory task store."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tasks: Dict[str, TaskState] = {}
        self._events: Dict[str, List[TaskProgressEvent]] = {}
        self._cancel_events: Dict[str, threading.Event] = {}

    def create_task(self, task_id: str) -> TaskState:
        state = TaskState(task_id=task_id)
        with self._lock:
            self._tasks[task_id] = state
            self._events[task_id] = []
            self._cancel_events[task_id] = threading.Event()
        return state

    def get_task(self, task_id: str) -> Optional[TaskState]:
        with self._lock:
            return self._tasks.get(task_id)

    def list_task_ids(self) -> List[str]:
        """Task ids in creation order (oldest first)."""
        with self._lock:
            return list(self._tasks.keys())

    def update_task(self, task_id: str, **kwargs: Any) -> Optional[TaskState]:
        with self._lock:
            state = self._tasks.get(task_id)
            if state is None:
                return None
            for key, value in kwargs.items():
                if hasattr(state, key):
                    setattr(state, key, value)
            state.updated_at = time.time()
            return state

    def add_event(self, task_id: str, event: TaskProgressEvent) -> None:
        with self._lock:
            if task_id in self._events:
                self._events[task_id].append(event)

    def get_events(self, task_id: str, since: int = 0) -> List[TaskProgressEvent]:
        with self._lock:
            events = self._events.get(task_id, [])
            return events[since:]

    def is_cancelled(self, task_id: str) -> bool:
        with self._lock:
            ev = self._cancel_events.get(task_id)
            return ev is not None and ev.is_set()

    def cancel_task(self, task_id: str) -> None:
        with self._lock:
            ev = self._cancel_events.get(task_id)
            if ev:
                ev.set()
            state = self._tasks.get(task_id)
            if state and state.status not in (
                TaskStage.COMPLETED.value,
                TaskStage.CANCELLED.value,
                TaskStage.FAILED.value,
            ):
                state.status = TaskStage.CANCELLED.value

    def remove_task(self, task_id: str) -> None:
        with self._lock:
            self._tasks.pop(task_id, None)
            self._events.pop(task_id, None)
            self._cancel_events.pop(task_id, None)

    def pause_task(self, task_id: str) -> None:
        with self._lock:
            state = self._tasks.get(task_id)
            if state:
                state.message = "Paused"

    def resume_task(self, task_id: str) -> None:
        with self._lock:
            state = self._tasks.get(task_id)
            if state:
                state.message = "Resumed"

    def skip_task(self, task_id: str) -> None:
        with self._lock:
            ev = self._cancel_events.get(task_id)
            if ev:
                ev.set()
            state = self._tasks.get(task_id)
            if state:
                state.message = "Skipped"


# ── RuntimeService ───────────────────────────────────────────────────────────


class RuntimeService:
    """Unified multi-end service layer over RuntimeFacade.

    Thread-safe. Shared across Gradio UI, Flask REST API, and MCP Server.
    """

    def __init__(self, config: Optional[ServiceConfig] = None) -> None:
        self.config = config or ServiceConfig()
        self._store = _TaskStore()
        self._lock = threading.Lock()
        self._active_count = 0
        #: Per-task batch aggregation state (only for multi-file tasks).
        self._batch_ctx: Dict[str, _BatchContext] = {}
        self._batch_ctx_lock = threading.Lock()
        #: External callbacks invoked on every emitted ``TaskProgressEvent``
        #: (Observer pattern -- the service stays fully decoupled from the
        #: GUI: listeners receive low-level records only).
        self._event_listeners: List[Callable[[TaskProgressEvent], None]] = []
        self._listeners_lock = threading.Lock()
        #: Per-task last emitted progress (throttle for smooth parallel reports).
        self._last_progress: Dict[str, float] = {}
        self._progress_lock = threading.Lock()

    # ── Event listener API (Worker -> EventBus bridge) ───────────────────────

    def add_event_listener(
        self, listener: Callable[[TaskProgressEvent], None]
    ) -> None:
        """Register a callback invoked on every emitted ``TaskProgressEvent``."""
        with self._listeners_lock:
            if listener not in self._event_listeners:
                self._event_listeners.append(listener)

    def remove_event_listener(
        self, listener: Callable[[TaskProgressEvent], None]
    ) -> None:
        """Unregister a previously added callback."""
        with self._listeners_lock:
            if listener in self._event_listeners:
                self._event_listeners.remove(listener)

    def clear_event_listeners(self) -> None:
        """Remove all registered event listeners."""
        with self._listeners_lock:
            self._event_listeners.clear()


    def submit_task(self, request: TranslationRequest) -> str:
        """Submit a translation task; returns task_id.

        Supports both single-file requests (legacy ``source_path``) and
        multi-file batch requests (``files``). Batch tasks are executed
        sequentially with aggregate progress reporting.
        """
        task_id = f"task_{uuid.uuid4().hex[:12]}"
        self._store.create_task(task_id)
        self._store.update_task(
            task_id,
            mode_choice=(request.extra_config or {}).get("mode_choice") or "auto",
        )
        with self._progress_lock:
            self._last_progress[task_id] = -1.0
        files = request.resolved_files()
        if not files:
            self._store.update_task(
                task_id, status=TaskStage.FAILED.value,
                error_message="No source files provided",
                message="Error: No source files provided",
            )
            return task_id
        filenames = [os.path.basename(f) for f in files]
        self._store.update_task(
            task_id,
            status=TaskStage.PENDING.value,
            current_file_name=filenames[0],
            file_list=filenames,
            total_files=len(files),
        )
        if len(files) > 1:
            with self._batch_ctx_lock:
                self._batch_ctx[task_id] = _BatchContext(total_files=len(files))
        thread = threading.Thread(
            target=self._execute_task, args=(task_id, request), daemon=True,
        )
        thread.start()
        return task_id

    def submit_batch(self, request: TranslationRequest) -> str:
        """Submit a multi-file batch translation; returns task_id.

        Semantically equivalent to ``submit_task`` (which already detects
        batch requests via ``request.files``); kept as an explicit API for
        batch-oriented callers.
        """
        return self.submit_task(request)

    def get_task_state(self, task_id: str) -> Optional[TaskState]:
        return self._store.get_task(task_id)

    def list_task_ids(self) -> List[str]:
        """Task ids in creation order (oldest first) -- single source of truth
        for GUI task recovery (refresh / new session after a submit)."""
        return self._store.list_task_ids()

    def update_task_state(self, task_id: str, **kwargs: Any) -> Optional[TaskState]:
        """Update arbitrary task-state fields (e.g. the GUI output selection)."""
        return self._store.update_task(task_id, **kwargs)

    def cancel_task(self, task_id: str) -> bool:
        state = self._store.get_task(task_id)
        if state is None:
            return False
        self._store.cancel_task(task_id)
        self._store.update_task(
            task_id, status=TaskStage.CANCELLED.value, message="Cancelled by user",
        )
        return True

    def pause_task(self, task_id: str) -> bool:
        """Pause a running task."""
        state = self._store.get_task(task_id)
        if state is None:
            return False
        if state.status in (TaskStage.COMPLETED.value, TaskStage.CANCELLED.value, TaskStage.FAILED.value):
            return False
        self._store.pause_task(task_id)
        self._store.update_task(task_id, message="Paused by user")
        return True

    def resume_task(self, task_id: str) -> bool:
        """Resume a paused task."""
        state = self._store.get_task(task_id)
        if state is None:
            return False
        self._store.resume_task(task_id)
        self._store.update_task(task_id, message="Resumed")
        return True

    def skip_task(self, task_id: str) -> bool:
        """Skip the current file in a task."""
        state = self._store.get_task(task_id)
        if state is None:
            return False
        if state.status in (TaskStage.COMPLETED.value, TaskStage.CANCELLED.value, TaskStage.FAILED.value):
            return False
        self._store.skip_task(task_id)
        self._store.update_task(task_id, message="Skipping current file...")
        return True

    def subscribe_events(
        self, task_id: str, poll_interval: float = 0.5
    ) -> Iterator[TaskProgressEvent]:
        """Generator yielding new events as they arrive (for Gradio streaming)."""
        last_index = 0
        while True:
            state = self._store.get_task(task_id)
            if state is None:
                break
            events = self._store.get_events(task_id, since=last_index)
            for event in events:
                yield event
            last_index += len(events)
            if state.status in (
                TaskStage.COMPLETED.value, TaskStage.CANCELLED.value, TaskStage.FAILED.value,
            ):
                break
            time.sleep(poll_interval)

    def _execute_task(self, task_id: str, request: TranslationRequest) -> None:
        """Internal: run translation in background thread."""
        try:
            self._emit_event(task_id, TaskStage.PARSING.value, 5.0, "Starting...")
            if self._store.is_cancelled(task_id):
                return
            mode = (request.extra_config or {}).get("mode_choice") or "auto"
            self._store.update_task(task_id, mode_choice=mode)
            task_config = resolve_mode_config(mode, self.config)
            self._sync_feature_flags(task_id, task_config)
            files = request.resolved_files()
            if len(files) > 1:
                self._execute_batch(task_id, request, files, task_config)
            elif task_config.use_v4_engine:
                self._execute_v4(task_id, request, task_config)
            else:
                self._execute_legacy(task_id, request, task_config)
        except Exception as exc:
            logger.error("Task %s failed: %s", task_id, exc, exc_info=True)
            self._store.update_task(
                task_id, status=TaskStage.FAILED.value,
                error_message=str(exc), message=f"Error: {exc}",
            )
            self._emit_event(task_id, TaskStage.FAILED.value, 100.0, f"Failed: {exc}")

    def _execute_batch(
        self, task_id: str, request: TranslationRequest, files: List[str],
        config: ServiceConfig,
    ) -> None:
        """Execute a multi-file batch sequentially, aggregating overall progress.

        Per-file results are accumulated into ``result_files`` and failures are
        recorded per-file (task continues with the next file). The task only
        reaches a terminal stage once every file has been processed.
        """
        ctx = self._batch_ctx.get(task_id)
        if ctx is None:
            ctx = _BatchContext(total_files=len(files))
            with self._batch_ctx_lock:
                self._batch_ctx[task_id] = ctx
        total = ctx.total_files
        for path in files:
            if self._store.is_cancelled(task_id):
                return
            ctx.current_file = os.path.basename(path)
            # Reset to running state (a previous file may have ended in FAILED).
            self._store.update_task(
                task_id,
                status=TaskStage.PARSING.value,
                current_file_name=ctx.current_file,
                file_progress=0.0,
                total_progress=self._agg(ctx, 0.0),
                progress=self._agg(ctx, 0.0),
                message=f"Processing {ctx.current_file}",
            )
            sub_request = dataclasses.replace(request, source_path=path, files=[])
            try:
                if config.use_v4_engine:
                    self._execute_v4(task_id, sub_request, config)
                else:
                    self._execute_legacy(task_id, sub_request, config)
            except Exception as exc:
                logger.error(
                    "[task=%s] file %s failed: %s", task_id, path, exc, exc_info=True,
                )
                self._fail_file(task_id, exc, total_files=total)
            # Defensive: if the per-file executor left the task in FAILED
            # (some legacy error paths update status directly), record it as a
            # file failure but keep the batch going.
            state = self._store.get_task(task_id)
            if (
                state is not None
                and state.status == TaskStage.FAILED.value
                and not self._file_failure_recorded(task_id, ctx)
            ):
                self._fail_file(task_id, state.error_message or "File failed",
                                total_files=total)
        if self._store.is_cancelled(task_id):
            return
        self._finish_batch(task_id, ctx)

    def _finish_batch(self, task_id: str, ctx: _BatchContext) -> None:
        """Terminal wrap-up for a batch task after every file was processed."""
        total = ctx.total_files
        if ctx.failed_files >= total:
            self._store.update_task(
                task_id, status=TaskStage.FAILED.value, progress=100.0,
                total_progress=100.0, message="All files failed",
                error_message="All files failed",
            )
            self._emit_event(task_id, TaskStage.FAILED.value, 100.0, "All files failed")
            return
        msg = f"Completed {total - ctx.failed_files}/{total} file(s)"
        if ctx.failed_files:
            msg += f", {ctx.failed_files} failed"
        zip_path = self._build_batch_zip(task_id)
        state = self._store.get_task(task_id)
        self._store.update_task(
            task_id, status=TaskStage.COMPLETED.value, progress=100.0,
            total_progress=100.0, file_progress=100.0, message=msg,
            result_zip=zip_path,
            result_files=list(state.result_files or []) if state else [],
        )
        self._emit_event(task_id, TaskStage.COMPLETED.value, 100.0, msg)

    def _file_failure_recorded(self, task_id: str, ctx: _BatchContext) -> bool:
        """True when the current file was already recorded as failed by _fail_file."""
        state = self._store.get_task(task_id)
        if state is None or not state.file_failures:
            return False
        return any(f.get("file") == ctx.current_file for f in state.file_failures)

    def _agg(self, ctx: _BatchContext, file_progress: float) -> float:
        """Aggregate a per-file progress (0-100) into overall task progress (0-100)."""
        f = min(max(float(file_progress), 0.0), 100.0)
        if ctx.total_files <= 0:
            return f
        return (ctx.completed_files + f / 100.0) / ctx.total_files * 100.0

    def _emit_batch_progress(self, task_id: str, ctx: _BatchContext, message: str = "") -> None:
        """Persist aggregate batch progress and broadcast a low-level event.

        ``ctx.completed_files`` already counts the file that just finished, so
        the overall progress is simply ``completed / total``.
        """
        agg = self._agg(ctx, 0.0)
        self._store.update_task(
            task_id,
            stage=TaskStage.RENDERING.value,
            progress=agg,
            total_progress=agg,
            file_progress=100.0,
            completed_files=ctx.completed_files,
            failed_files=ctx.failed_files,
            message=message or f"Completed {ctx.current_file}",
        )
        event = TaskProgressEvent(
            task_id=task_id, stage=TaskStage.RENDERING.value, progress=agg,
            message=message or f"Completed {ctx.current_file}",
        )
        self._store.add_event(task_id, event)
        self._notify_event_listeners(event)

    def _batch_total(self, task_id: str) -> int:
        """Total files of a batch task (1 for single-file tasks)."""
        ctx = self._batch_ctx.get(task_id)
        return ctx.total_files if ctx else 1

    def _complete_file(
        self, task_id: str, result_files: List[Dict[str, str]], *,
        total_files: int = 1, message: str = "Completed", **extra: Any,
    ) -> None:
        """Record a per-file completion.

        Single-file tasks complete the whole task here (existing behaviour);
        batch tasks accumulate ``result_files`` and bump the aggregate progress.
        """
        if total_files <= 1 or task_id not in self._batch_ctx:
            # The ZIP is built from the stored result_files below; a caller
            # supplied result_zip (commonly a bare mono/dual PDF path) would
            # surface as a bogus "Download All (ZIP)" target, so drop it.
            extra.pop("result_zip", None)
            self._store.update_task(
                task_id, status=TaskStage.COMPLETED.value, progress=100.0,
                total_progress=100.0, file_progress=100.0,
                result_files=result_files,
                selected_file=(
                    extra.pop("selected_file", None)
                    or (result_files[0]["name"] if result_files else None)
                ),
                message=message,
                **extra,
            )
            self._ensure_result_zip(task_id)
            self._emit_event(task_id, TaskStage.COMPLETED.value, 100.0, message)
            return
        ctx = self._batch_ctx[task_id]
        ctx.completed_files += 1
        state = self._store.get_task(task_id)
        prev = list(state.result_files or []) if state else []
        upd: Dict[str, Any] = {
            "result_files": prev + list(result_files),
            "file_progress": 100.0,
            "completed_files": ctx.completed_files,
            "message": f"Completed {ctx.current_file}",
        }
        if extra.get("selected_file"):
            upd["selected_file"] = extra.pop("selected_file")
        for key in (
            "diagnostic_summary", "quality_scores", "node_overview",
            "ir_snapshots", "gate_verdicts", "processor_reports", "toc_ir_records",
            "diagnostic_report", "heal_status", "repair_records", "confidence_stats",
        ):
            if extra.get(key) is not None:
                upd[key] = extra.pop(key)
        self._store.update_task(task_id, **upd)
        self._emit_batch_progress(task_id, ctx, f"Completed {ctx.current_file}")

    def _fail_file(
        self, task_id: str, exc: Any, *, total_files: int = 1, message: Optional[str] = None,
    ) -> None:
        """Record a per-file failure.

        Single-file tasks transition to FAILED immediately (existing behaviour);
        batch tasks record the failure and continue with the next file.
        """
        error = str(exc)
        if total_files <= 1 or task_id not in self._batch_ctx:
            self._store.update_task(
                task_id, status=TaskStage.FAILED.value,
                error_message=error, message=message or f"Error: {error}",
            )
            self._emit_event(task_id, TaskStage.FAILED.value, 100.0, f"Failed: {error}")
            return
        ctx = self._batch_ctx[task_id]
        ctx.failed_files += 1
        state = self._store.get_task(task_id)
        failures = list(getattr(state, "file_failures", None) or [])
        failures.append({"file": ctx.current_file, "error": error})
        self._store.update_task(
            task_id, failed_files=ctx.failed_files, file_failures=failures,
            message=f"Failed {ctx.current_file}",
        )
        self._emit_batch_progress(task_id, ctx, f"Failed {ctx.current_file}: {error}")

    def _build_batch_zip(self, task_id: str) -> Optional[str]:
        """Package all completed result files into a single zip.

        Used by batch tasks at finish time AND by single-file tasks
        (``_ensure_result_zip``) so the "Download All (ZIP)" button always
        serves a genuine ZIP archive, never a bare mono/dual PDF.
        """
        import tempfile
        import zipfile

        state = self._store.get_task(task_id)
        if state is None or not state.result_files:
            return None
        zip_path = os.path.join(tempfile.gettempdir(), f"pdf2zh_task_{task_id}.zip")
        try:
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for rf in state.result_files:
                    p = rf.get("path") or ""
                    if p and os.path.exists(p):
                        zf.write(p, arcname=os.path.basename(p))
            return zip_path
        except Exception:
            logger.exception("Failed to build result zip for task %s", task_id)
            return None

    def _ensure_result_zip(self, task_id: str) -> Optional[str]:
        """Guarantee ``result_zip`` points at a real ZIP of the outputs.

        Builds the archive from the stored ``result_files``; if packaging
        fails, falls back to the first existing result file so the download
        button still has something to serve. Never raises.
        """
        zip_path = self._build_batch_zip(task_id)
        state = self._store.get_task(task_id)
        if state is None:
            return None
        if zip_path is None:
            zip_path = next(
                (
                    rf.get("path") for rf in (state.result_files or [])
                    if rf.get("path") and os.path.exists(rf.get("path"))
                ),
                None,
            ) or ""
        self._store.update_task(task_id, result_zip=zip_path)
        return zip_path

    def _sync_feature_flags(self, task_id: str,
                            config: Optional[ServiceConfig] = None) -> None:
        """V8.2：把 ServiceConfig.use_v4_* 同步到 v3 FeatureFlags 单例，
        并记录回退遥测（use_v4_engine=False 即 legacy 回退事件）。

        使用任务的解析后配置（引擎模式 preset 已折叠）而不是服务全局配置。
        """
        cfg = config or self.config
        try:
            from pdf2zh.v3.feature_flags import (
                FallbackTelemetry, FeatureFlags, get_feature_flags, set_feature_flags,
            )
            flags = get_feature_flags()
            flags.use_v4_engine = cfg.use_v4_engine
            flags.use_v4_translator = cfg.use_v4_translator
            flags.use_v4_layout = cfg.use_v4_layout
            flags.use_v4_repair = cfg.use_v4_repair
            flags.use_v4_gate = cfg.use_v4_gate
            flags.relink_links = cfg.relink_links
            flags.use_v4_image_engine = cfg.image_engine
            flags.use_v4_content_preservation = cfg.content_preservation
            flags.use_v4_processor_channels = cfg.processor_channels
            if hasattr(flags, "use_v4_fix_validate_loop"):
                flags.use_v4_fix_validate_loop = cfg.use_v4_fix_validate_loop
            if hasattr(flags, "max_repair_passes"):
                flags.max_repair_passes = cfg.max_repair_passes
            if not cfg.use_v4_engine:
                flags.use_v4_engine = False
                flags.use_v4_translator = flags.use_v4_translator or False
            flags.telemetry = FallbackTelemetry()
            flags.record_fallback({
                "reason": "legacy_mainline" if not cfg.use_v4_engine
                else "v4_enabled",
                "task_id": task_id,
                "run_evaluation": cfg.run_evaluation,
                "emit_ir": cfg.emit_ir,
                "use_v4_gate": cfg.use_v4_gate,
                "mode_choice": getattr(
                    self._store.get_task(task_id), "mode_choice", "") or "",
            })
            set_feature_flags(flags)
        except Exception:
            logger.debug("feature flag sync skipped", exc_info=True)

    def _execute_v4(self, task_id: str, request: TranslationRequest,
                    config: Optional[ServiceConfig] = None) -> None:
        """Execute with V4 RuntimeFacade pipeline."""
        from pdf2zh.v3.runtime import RuntimeFacade

        config = config or self.config
        total_files = self._batch_total(task_id)
        # Initialize diagnostics early to avoid NameError in error paths
        diagnostic_summary = ""
        quality_scores = {}
        diag_report: Optional[Dict[str, Any]] = None

        self._emit_event(task_id, TaskStage.PARSING.value, 10.0, "Parsing PDF...")
        rt = RuntimeFacade(config={
            "lang_in": request.source_lang,
            "lang_out": request.target_lang,
        })
        rt.load(request.source_path)
        if self._store.is_cancelled(task_id):
            return
        self._emit_event(task_id, TaskStage.ANALYZING.value, 30.0, "Analyzing...")
        rt.analyze()
        node_overview = self._collect_node_overview(rt)
        if node_overview:
            self._store.update_task(task_id, node_overview=node_overview)
        self._emit_event(task_id, TaskStage.PLANNING.value, 40.0, "Planning...")
        rt.plan()
        if self._store.is_cancelled(task_id):
            return
        self._emit_event(task_id, TaskStage.TRANSLATING.value, 50.0, "Translating...")
        if config.use_v4_translator:
            from pdf2zh.v3.translation_runtime import TranslationRuntime
            tr = TranslationRuntime()
            if rt.plans:
                tr.execute(rt.graph, rt.plans)
        else:
            rt.translate()
        if self._store.is_cancelled(task_id):
            return
        self._emit_event(task_id, TaskStage.LAYOUTING.value, 70.0, "Laying out...")
        rt.layout()
        if self._store.is_cancelled(task_id):
            return
        self._emit_event(task_id, TaskStage.RENDERING.value, 85.0, "Rendering...")
        output = rt.pipeline(request.source_path)
        self._emit_event(task_id, TaskStage.EVALUATING.value, 95.0, "Evaluating...")
        # -- V4 Diagnostic Data Collection --
        diagnostic_summary = ""
        quality_scores = {}
        try:
            from pdf2zh.v3.evaluator import QualityEvaluator
            ev = QualityEvaluator()
            report = ev.evaluate(rt.graph)
            if isinstance(report, dict) and isinstance(report.get("scores"), dict):
                quality_scores = {k: float(v) for k, v in report["scores"].items() if isinstance(v, (int, float))}
                issues = []
                for cat, sc in quality_scores.items():
                    issues.append(str(cat) + ": " + str(int(sc)) + "/100")
                col = quality_scores.get("collision_score", 100)
                if col < 90:
                    issues.append("Overlap")
                ts = quality_scores.get("translation_score", 100)
                if ts < 80:
                    issues.append("Low quality")
                diagnostic_summary = " | ".join(issues) if issues else "All checks passed"
            diag_src = getattr(ev, "_diagnostic", None)
            if diag_src is not None and hasattr(diag_src, "to_dict"):
                try:
                    drill = diag_src.to_dict()
                    if drill and drill.get("total"):
                        diag_report = drill
                except Exception:
                    diag_report = None
        except Exception:
            diagnostic_summary = "Diagnostics unavailable"

        out_dir = config.output_dir or os.path.dirname(request.source_path)
        basename = os.path.splitext(os.path.basename(request.source_path))[0]
        os.makedirs(out_dir, exist_ok=True)
        result_path = os.path.join(out_dir, f"{basename}-translated.pdf")
        if output:
            with open(result_path, "wb") as f:
                f.write(output)

        result_files = [{"name": f"{basename}-translated.pdf", "path": result_path}]
        # Preserve diagnostic data from evaluator (set above); fallback messages if empty
        try:
            diagnostic_summary
        except NameError:
            diagnostic_summary = ""
        if not diagnostic_summary:
            diagnostic_summary = "Completed via V4 pipeline"
        try:
            quality_scores
        except NameError:
            quality_scores = {}

        self._complete_file(
            task_id, result_files, total_files=total_files,
            selected_file=result_files[0]["name"],
            diagnostic_summary=diagnostic_summary,
            quality_scores=quality_scores,
            node_overview=node_overview or None,
            diagnostic_report=diag_report,
            preview_path=result_path if os.path.exists(result_path) else "",
            message="Completed (V4)",
        )

    def _collect_node_overview(self, rt) -> Optional[Dict[str, int]]:
        """Extract the document node overview from a V4 runtime graph.

        Pure side-channel: any failure falls back to ``None`` (the UI then
        renders the idle overview). Never raises.
        """
        try:
            graph = getattr(rt, "graph", None)
            if graph is None:
                return None
            pages = 0
            counts: Dict[str, int] = {"paragraphs": 0, "headings": 0,
                                      "figures": 0, "formulas": 0}
            for node in graph:
                kind = str(getattr(node, "kind", "") or "").lower()
                ntype = str(getattr(node, "type", "") or "").lower()
                tag = f"{kind}:{ntype}"
                if not pages and ("doc" in tag or "page" in tag):
                    pages += 1
                if "paragraph" in tag:
                    counts["paragraphs"] += 1
                elif "heading" in tag or "title" in tag:
                    counts["headings"] += 1
                elif "figure" in tag or "image" in tag or "table" in tag:
                    counts["figures"] += 1
                elif "formula" in tag or "equation" in tag:
                    counts["formulas"] += 1
            if pages:
                counts["pages"] = pages
            if any(v for k, v in counts.items() if k != "pages"):
                return counts
            return None
        except Exception:
            return None

    def _execute_legacy(self, task_id: str, request: TranslationRequest,
                        config: Optional[ServiceConfig] = None) -> None:
        """Execute with Legacy translate_stream pipeline."""
        from pdf2zh.high_level import translate_stream
        from pdf2zh.doclayout import ModelInstance, OnnxModel

        config = config or self.config
        total_files = self._batch_total(task_id)

        # Ensure layout model is loaded before translation
        if ModelInstance.value is None:
            try:
                ModelInstance.value = OnnxModel.load_available()
            except Exception as e:
                logger.error("Failed to load doclayout model: %s", e)
                raise

        # Initialize diagnostics early to avoid NameError in error paths
        diagnostic_summary = ""
        quality_scores = {}

        self._emit_event(task_id, TaskStage.PARSING.value, 10.0, "Parsing (Legacy)...")
        with open(request.source_path, "rb") as f:
            file_bytes = f.read()
        if self._store.is_cancelled(task_id):
            return
        self._emit_event(task_id, TaskStage.TRANSLATING.value, 50.0, "Translating...")
        logger.info("[task=%s] Translation phase starting...", task_id)
        # V8.3/V8.4: 主链路 IR 产出 + 写回前门控（side-channel）
        v3_output: Dict[str, Any] = {}
        # 并行进度回调：translate_stream 按已完成的页面分块回报（0-100），
        # 映射到翻译窗口 50→80；经 _emit_smooth 节流 + 单调不减后推给 UI，
        # 避免并行下进度条跳变。
        def _progress_cb(pct: float, msg: str) -> None:
            self._emit_smooth(
                task_id, TaskStage.TRANSLATING.value,
                50.0 + max(0.0, min(100.0, float(pct))) * 0.30,
                msg,
            )

        try:
            mode = (request.extra_config or {}).get("mode_choice") or "auto"
            extra_config = dict(request.extra_config or {})
            extra_config.pop("mode_choice", None)
            doc_dual, doc_mono = translate_stream(
                file_bytes,
                lang_in=request.source_lang,
                lang_out=request.target_lang,
                service=request.engine,
                vfont=request.vfont,
                vchar=request.vchar,
                thread=request.threads,
                skip_subset_fonts=request.skip_subset_fonts,
                ignore_cache=request.ignore_cache,
                model=ModelInstance.value,
                emit_ir=config.emit_ir,
                relayout_gate=(self._make_gate if config.use_v4_gate else None),
                v3_output=v3_output,
                relink_links=config.relink_links,
                image_engine=config.image_engine,
                content_preservation=config.content_preservation,
                emit_preservation=config.emit_preservation,
                processor_channels=config.processor_channels,
                progress_cb=_progress_cb,
                **legacy_mode_kwargs(mode),
                **extra_config,
            )
        except Exception as tx_exc:
            logger.error("[task=%s] translate_stream failed: %s", task_id, tx_exc, exc_info=True)
            self._fail_file(task_id, tx_exc, total_files=total_files)
            return
        if self._store.is_cancelled(task_id):
            return
        logger.info("[task=%s] translate_stream complete, merging output...", task_id)
        self._emit_event(task_id, TaskStage.RENDERING.value, 80.0, "Merging pages...")
        if doc_mono is None or doc_dual is None:
            logger.error("Page merging failed: translate_stream returned None")
            self._fail_file(task_id, "translate_stream returned None", total_files=total_files)
            return
        self._emit_event(task_id, TaskStage.RENDERING.value, 82.0, "Subsetting fonts...")
        # Track merge progress: subset_fonts + write can take minutes for large PDFs.
        # translate_stream handles the merge internally; we poll with short sleeps
        # to allow the UI to receive periodic progress updates.
        self._emit_event(task_id, TaskStage.RENDERING.value, 85.0, "Writing output files...")
        out_dir = config.output_dir or os.path.dirname(request.source_path)
        basename = os.path.splitext(os.path.basename(request.source_path))[0]
        os.makedirs(out_dir, exist_ok=True)
        logger.info("[task=%s] Merge OK: mono=%d bytes, dual=%d bytes", task_id, len(doc_mono), len(doc_dual))

        mono_path = os.path.join(out_dir, f"{basename}-mono.pdf")
        dual_path = os.path.join(out_dir, f"{basename}-dual.pdf")
        logger.info("[task=%s] Writing mono (%d bytes) and dual (%d bytes)...", task_id, len(doc_mono), len(doc_dual))
        import sys as _sys_write
        _sys_write.stdout.flush()
        with open(mono_path, "wb") as f:
            f.write(doc_mono)
        with open(dual_path, "wb") as f:
            f.write(doc_dual)
        logger.info("[task=%s] Output file write complete.", task_id)
        result_files = [
            {"name": f"{basename}-mono.pdf", "path": mono_path},
            {"name": f"{basename}-dual.pdf", "path": dual_path},
        ]
        # 文档级评测（阶段九）：源 PDF vs mono 译文
        if config.run_evaluation:
            try:
                from pdf2zh.evaluate import evaluate_translation
                self._emit_event(task_id, TaskStage.EVALUATING.value, 92.0,
                                 "Evaluating output...")
                eval_report = evaluate_translation(
                    request.source_path, mono_path,
                    target_lang=request.target_lang,
                    report_dir=config.evaluation_report_dir or None,
                    report_threshold=90.0,
                )
                quality_scores = {
                    "overall": float(eval_report.overall_score),
                    "geometry_score": float(eval_report.geometry.get("geometry_score", 0)),
                    "structure_score": float(eval_report.structure.get("structure_score", 0)),
                    "translation_score": float(eval_report.translation.get("translation_score", 0)),
                    "rendering_score": float(eval_report.rendering.get("rendering_score", 0)),
                    "collision_rate": float(eval_report.rendering.get("collision_rate", 0)),
                    "overflow_rate": float(eval_report.rendering.get("overflow_rate", 0)),
                    "residue_estimate": float(eval_report.translation.get("residue_estimate", 0)),
                }
                diag_parts = []
                for k in ("geometry_score", "structure_score", "translation_score", "rendering_score"):
                    diag_parts.append(f"{k}: {int(quality_scores[k])}/100")
                if quality_scores["collision_rate"] > 0.05:
                    diag_parts.append("Overlap")
                if quality_scores["overflow_rate"] > 0.05:
                    diag_parts.append("Overflow")
                if quality_scores["residue_estimate"] > 0.15:
                    diag_parts.append("Original residue")
                diagnostic_summary = " | ".join(diag_parts) if diag_parts else "All checks passed"
                self._emit_event(task_id, TaskStage.EVALUATING.value, 98.0,
                                 f"Evaluated: {eval_report.summary()}")
            except Exception as ev_exc:
                logger.warning("[task=%s] evaluation failed: %s", task_id, ev_exc)
        # 确保 diagnostic_summary 和 quality_scores 至少有一个默认值
        if 'diagnostic_summary' not in dir() or not diagnostic_summary:
            diagnostic_summary = "Legacy pipeline - V4 diagnostics not available"
        if 'quality_scores' not in dir() or not quality_scores:
            quality_scores = {}

        # V8.3/V8.4: 收集主链路 side-channel（IR 快照 + 写回门控裁决）
        ir_snapshots = v3_output.get("ir_snapshots") or None
        gate_verdicts = v3_output.get("gate_verdicts") or None
        if gate_verdicts:
            blocked = [str(p) for p, v in gate_verdicts.items()
                       if not v.get("writeback_allowed", True)]
            if blocked:
                diag_extra = f"Gate {len(blocked)} page(s) blocked write-back: {','.join(blocked)}"
                diagnostic_summary = f"{diagnostic_summary} | {diag_extra}" \
                    if diagnostic_summary else diag_extra
        # V9.0: Processor 语义通道（处理器报告 + TOC 结构化记录）回传 task state
        processor_reports = v3_output.get("processor_reports") or None
        toc_ir_records = v3_output.get("toc_ir_records") or None

        # 文档智能概况（side-channel）：优先 document_model 节点统计，退化到页数
        node_overview = self._collect_legacy_overview(v3_output, request.source_path)
        if node_overview:
            self._store.update_task(task_id, node_overview=node_overview)
        # 诊断报告 + 置信度统计 + 自愈行程（side-channel 模型，纯只读分析之外会
        # 在模型副本上运行修复闭环以产出 before/after 证据；渲染不受影响）。
        diag_report, heal_status, repair_records, confidence_stats = \
            self._collect_legacy_diagnostics(v3_output)
        if diag_report and heal_status:
            errs = int(diag_report.get("errors") or 0)
            warns = int(diag_report.get("warnings") or 0)
            heal = f"Heal: {heal_status.get('before_errors', '?')}→{heal_status.get('after_errors', '?')} (runs={heal_status.get('iterations', '?')})"
            diag_extra = f"Errors={errs} Warnings={warns} {heal}"
            if diagnostic_summary and "Errors=" not in diagnostic_summary:
                diagnostic_summary = f"{diagnostic_summary} | {diag_extra}"
            elif diagnostic_summary in ("", "Legacy pipeline - V4 diagnostics not available"):
                diagnostic_summary = diag_extra

        self._complete_file(
            task_id, result_files, total_files=total_files,
            selected_file=result_files[0]["name"],
            diagnostic_summary=diagnostic_summary,
            quality_scores=quality_scores,
            node_overview=node_overview or None,
            ir_snapshots=ir_snapshots,
            gate_verdicts=gate_verdicts,
            processor_reports=processor_reports,
            toc_ir_records=toc_ir_records,
            diagnostic_report=diag_report,
            heal_status=heal_status,
            repair_records=repair_records,
            confidence_stats=confidence_stats,
            preview_path=dual_path if os.path.exists(dual_path) else mono_path,
            message="Completed (Legacy)",
        )
        logger.info("[task=%s] Output files written successfully", task_id)

    def _collect_legacy_overview(
        self, v3_output: Dict[str, Any], source_path: str,
    ) -> Optional[Dict[str, int]]:
        """Build the document overview for the legacy pipeline.

        Uses the V11 document model node counts when available; otherwise
        falls back to the source page count. Pure side-channel, never raises.
        """
        try:
            dm = v3_output.get("document_model")
            counts: Dict[str, int] = {}
            pages = 0
            for node in dm:
                kind = str(getattr(node, "kind", "") or "").lower()
                if "page" in kind or "doc" in kind:
                    pages += 1
                elif "paragraph" in kind:
                    counts["paragraphs"] = counts.get("paragraphs", 0) + 1
                elif "heading" in kind or "title" in kind:
                    counts["headings"] = counts.get("headings", 0) + 1
                elif "figure" in kind or "table" in kind or "image" in kind:
                    counts["figures"] = counts.get("figures", 0) + 1
                elif "formula" in kind or "equation" in kind:
                    counts["formulas"] = counts.get("formulas", 0) + 1
            if pages:
                counts["pages"] = pages
            if counts:
                return counts
            if source_path and os.path.exists(source_path):
                import fitz as _fitz
                with _fitz.open(source_path) as src:
                    return {"pages": src.page_count}
            return None
        except Exception:
            try:
                if source_path and os.path.exists(source_path):
                    import fitz as _fitz
                    with _fitz.open(source_path) as src:
                        return {"pages": src.page_count}
            except Exception:
                pass
            return None

    def _collect_legacy_diagnostics(self, v3_output: Dict[str, Any]) -> tuple:
        """Collect legacy diagnostic report, confidence stats and self-heal summary.

        Pure side-channel: reads the V11 ``document_model`` (already annotated
        with analyze_document/annotate_confidence during the mainline). When
        error-level issues exist, runs the repair engine over the model to
        derive a before/after healing record. Never raises.
        """
        E_NOT_FOUND: tuple = (None, None, None, None)
        dm = None
        if v3_output:
            for key in ("document_model", "ir_model"):
                candidate = v3_output.get(key)
                if candidate is not None:
                    dm = candidate
                    break
        if dm is None:
            return E_NOT_FOUND
        meta = getattr(dm, "metadata", None) or {}
        diag = meta.get("diagnostics")
        if not isinstance(diag, dict):
            diag = None
        conf = meta.get("confidence_stats")
        if not isinstance(conf, dict) or not conf:
            conf = None
        issues = (diag or {}).get("issues") or []
        records: List[Dict[str, Any]] = []
        for i in issues:
            if not isinstance(i, dict):
                continue
            code = str(i.get("code") or "")
            records.append({
                "code": code,
                "node_id": str(i.get("node_id") or ""),
                "page": int(i.get("page") or i.get("page_num") or 0),
                "severity": str(i.get("severity") or "warning"),
                "message": str(i.get("message") or "")[:160],
                "action": _HEAL_ACTIONS.get(code, "人工复核"),
                "status": "applied" if code in _HEAL_ACTIONS else "manual",
            })
        heal = None
        if diag and int(diag.get("errors") or 0) > 0:
            try:
                from pdf2zh.v3.diagnostics import analyze_document
                from pdf2zh.v3.repair_engine import repair_loop
                res = repair_loop(dm, max_iterations=2)
                after = analyze_document(dm).to_dict()
                heal = {
                    "ran": True,
                    "iterations": int(res.get("iterations") or 0),
                    "before_errors": int(res.get("before_errors") or 0),
                    "after_errors": int(res.get("after_errors") or 0),
                    "improved": bool(res.get("improved")),
                    "admissible": bool(not after.get("errors")),
                    "final_report": after,
                }
            except Exception as exc:  # noqa: BLE001
                heal = {"ran": False, "error": str(exc)[:200]}
        # V1.23：Layout Inspector 侧通道 —— 逐 Paragraph 排版证据（Font
        # Resolution / 对齐 / Lv2 段拆 provenance）挂到诊断报告上供 GUI 渲染。
        layout = None
        try:
            from pdf2zh.v3.document_inspector import build_layout_report
            layout = build_layout_report(dm)
        except Exception:  # noqa: BLE001
            layout = None
        if isinstance(diag, dict) and layout:
            diag = dict(diag)
            diag["layout"] = layout
        return diag, heal, (records or None), conf

    def get_queue_position(self, task_id: str) -> int:
        state = self._store.get_task(task_id)
        if state is None:
            return -1
        if state.status != TaskStage.PENDING.value:
            return 0
        return max(0, self._active_count)

    def _make_gate(self, page_width: float = 612.0,
                   page_height: float = 792.0, margin: float = 50.0):
        """V8.4: 创建写回前重排版门控（按页面尺寸工厂）。

        阈值放宽（overlap_rate >= 5% 才触发重排）避免在真实文档上过度干预
        legacy 布局 —— 门控主要承担「写回安全护栏」：只在明显重叠时记录拦截。
        """
        from pdf2zh.v3.mainline_gate import MainlineRelayoutGate
        return MainlineRelayoutGate(
            page_width=page_width, page_height=page_height,
            margin=margin, threshold=0.05, max_passes=2,
        )

    def _emit_smooth(
        self, task_id: str, stage: str, progress: float,
        message: str = "", min_delta: float = 1.0,
    ) -> None:
        """Emit a progress event that is throttled and monotonically non-decreasing.

        Parallel pipelines report fine-grained progress (per completed chunk);
        raw emissions would flood the EventBus (each one wakes the browser via
        SSE). This folds them into smooth, never-backward steps so the UI
        progress bar conforms to the parallel-aggregation standard.

        Monotonicity is enforced on the *visible* value: in batch mode the
        per-file progress is aggregated first, so the clamp never goes
        backwards across files.
        """
        raw = max(0.0, min(100.0, float(progress)))
        terminal = stage in (
            TaskStage.COMPLETED.value,
            TaskStage.FAILED.value,
            TaskStage.CANCELLED.value,
        )
        batch = self._batch_ctx.get(task_id)
        visible = raw
        if batch is not None and not terminal:
            visible = self._agg(batch, raw)
        with self._progress_lock:
            prev = self._last_progress.get(task_id, -1.0)
            if prev < 0 or terminal or visible - prev >= min_delta:
                if visible < prev:
                    return
                self._last_progress[task_id] = visible
                self._emit_event(task_id, stage, raw, message)

    def _emit_event(
        self, task_id: str, stage: str, progress: float,
        message: str = "", node_count: int = 0, diag_count: int = 0,
    ) -> None:
        batch = self._batch_ctx.get(task_id)
        file_progress = progress
        # In batch mode, ``progress`` is the per-file progress; fold it into
        # the overall task progress. Terminal stages bypass aggregation.
        if (
            batch is not None
            and stage not in (
                TaskStage.COMPLETED.value,
                TaskStage.FAILED.value,
                TaskStage.CANCELLED.value,
            )
        ):
            progress = self._agg(batch, progress)
        event = TaskProgressEvent(
            task_id=task_id, stage=stage, progress=progress,
            current_node_count=node_count, diagnostics_count=diag_count,
            message=message,
        )
        self._store.add_event(task_id, event)
        if batch is not None:
            self._store.update_task(
                task_id, stage=stage, progress=progress,
                total_progress=progress, file_progress=file_progress,
                message=message,
            )
        else:
            self._store.update_task(task_id, stage=stage, progress=progress, message=message)
        self._notify_event_listeners(event)

    def _notify_event_listeners(self, event: TaskProgressEvent) -> None:
        """Invoke registered listeners (guarded, never raises)."""
        with self._listeners_lock:
            listeners = list(self._event_listeners)
        for listener in listeners:
            try:
                listener(event)
            except Exception:
                logger.exception(
                    "Event listener error for task %s", event.task_id
                )



__all__ = [
    "RuntimeService",
    "TranslationRequest",
    "TaskState",
    "TaskProgressEvent",
    "TaskStage",
    "ServiceConfig",
]

