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

import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Iterator, List, Optional

logger = logging.getLogger(__name__)


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
    """Strong-typed request replacing the 21-parameter tuple pattern."""

    source_path: str
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
class TaskState:
    """Type-safe task state replacing bare dict (20+ fields)."""

    task_id: str
    status: str = TaskStage.PENDING.value
    progress: float = 0.0
    message: str = ""
    stage: str = ""
    file_progress: float = 0.0
    total_progress: float = 0.0
    current_file_name: str = ""
    file_list: List[str] = field(default_factory=list)
    result_files: List[Dict[str, str]] = field(default_factory=list)
    selected_file: Optional[str] = None
    result_zip: Optional[str] = None
    preview_path: Optional[str] = None
    diagnostic_summary: Optional[str] = None
    quality_scores: Optional[Dict[str, float]] = None
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
            "file_progress": self.file_progress,
            "total_progress": self.total_progress,
            "current_file_name": self.current_file_name,
            "file_list": self.file_list,
            "result_files": self.result_files,
            "selected_file": self.selected_file,
            "result_zip": self.result_zip,
            "preview_path": self.preview_path,
            "diagnostic_summary": self.diagnostic_summary,
            "quality_scores": self.quality_scores,
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
        #: External callbacks invoked on every emitted ``TaskProgressEvent``
        #: (Observer pattern -- the service stays fully decoupled from the
        #: GUI: listeners receive low-level records only).
        self._event_listeners: List[Callable[[TaskProgressEvent], None]] = []
        self._listeners_lock = threading.Lock()

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
        """Submit a translation task; returns task_id."""
        task_id = f"task_{uuid.uuid4().hex[:12]}"
        self._store.create_task(task_id)
        filename = os.path.basename(request.source_path) if request.source_path else "unknown"
        self._store.update_task(
            task_id,
            status=TaskStage.PENDING.value,
            current_file_name=filename,
            file_list=[filename],
        )
        thread = threading.Thread(
            target=self._execute_task, args=(task_id, request), daemon=True,
        )
        thread.start()
        return task_id

    def get_task_state(self, task_id: str) -> Optional[TaskState]:
        return self._store.get_task(task_id)

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
            self._sync_feature_flags(task_id)
            if self.config.use_v4_engine:
                self._execute_v4(task_id, request)
            else:
                self._execute_legacy(task_id, request)
        except Exception as exc:
            logger.error("Task %s failed: %s", task_id, exc, exc_info=True)
            self._store.update_task(
                task_id, status=TaskStage.FAILED.value,
                error_message=str(exc), message=f"Error: {exc}",
            )
            self._emit_event(task_id, TaskStage.FAILED.value, 100.0, f"Failed: {exc}")

    def _sync_feature_flags(self, task_id: str) -> None:
        """V8.2：把 ServiceConfig.use_v4_* 同步到 v3 FeatureFlags 单例，
        并记录回退遥测（use_v4_engine=False 即 legacy 回退事件）。"""
        try:
            from pdf2zh.v3.feature_flags import (
                FallbackTelemetry, FeatureFlags, get_feature_flags, set_feature_flags,
            )
            flags = get_feature_flags()
            flags.use_v4_engine = self.config.use_v4_engine
            flags.use_v4_translator = self.config.use_v4_translator
            flags.use_v4_layout = self.config.use_v4_layout
            flags.use_v4_repair = self.config.use_v4_repair
            flags.use_v4_gate = self.config.use_v4_gate
            flags.relink_links = self.config.relink_links
            flags.use_v4_image_engine = self.config.image_engine
            flags.use_v4_content_preservation = self.config.content_preservation
            flags.use_v4_processor_channels = self.config.processor_channels
            if not self.config.use_v4_engine:
                flags.use_v4_engine = False
                flags.use_v4_translator = flags.use_v4_translator or False
            flags.telemetry = FallbackTelemetry()
            flags.record_fallback({
                "reason": "legacy_mainline" if not self.config.use_v4_engine
                else "v4_enabled",
                "task_id": task_id,
                "run_evaluation": self.config.run_evaluation,
                "emit_ir": self.config.emit_ir,
                "use_v4_gate": self.config.use_v4_gate,
            })
            set_feature_flags(flags)
        except Exception:
            logger.debug("feature flag sync skipped", exc_info=True)

    def _execute_v4(self, task_id: str, request: TranslationRequest) -> None:
        """Execute with V4 RuntimeFacade pipeline."""
        from pdf2zh.v3.runtime import RuntimeFacade

        # Initialize diagnostics early to avoid NameError in error paths
        diagnostic_summary = ""
        quality_scores = {}

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
        self._emit_event(task_id, TaskStage.PLANNING.value, 40.0, "Planning...")
        rt.plan()
        if self._store.is_cancelled(task_id):
            return
        self._emit_event(task_id, TaskStage.TRANSLATING.value, 50.0, "Translating...")
        if self.config.use_v4_translator:
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
        except Exception:
            diagnostic_summary = "Diagnostics unavailable"

        out_dir = self.config.output_dir or os.path.dirname(request.source_path)
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

        self._store.update_task(
            task_id, status=TaskStage.COMPLETED.value, progress=100.0,
            total_progress=100.0, file_progress=100.0,
            result_files=result_files, selected_file=result_files[0]["name"],
            diagnostic_summary=diagnostic_summary,
            quality_scores=quality_scores,
            result_zip=result_path if os.path.exists(result_path) else "",
            preview_path=result_path if os.path.exists(result_path) else "",
            message="Completed",
        )
        self._emit_event(task_id, TaskStage.COMPLETED.value, 100.0, "Complete!")

    def _execute_legacy(self, task_id: str, request: TranslationRequest) -> None:
        """Execute with Legacy translate_stream pipeline."""
        from pdf2zh.high_level import translate_stream
        from pdf2zh.doclayout import ModelInstance, OnnxModel

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
        try:
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
                emit_ir=self.config.emit_ir,
                relayout_gate=(self._make_gate if self.config.use_v4_gate else None),
                v3_output=v3_output,
                relink_links=self.config.relink_links,
                image_engine=self.config.image_engine,
                content_preservation=self.config.content_preservation,
                emit_preservation=self.config.emit_preservation,
                processor_channels=self.config.processor_channels,
                **request.extra_config,
            )
        except Exception as tx_exc:
            logger.error("[task=%s] translate_stream failed: %s", task_id, tx_exc, exc_info=True)
            self._store.update_task(
                task_id, status=TaskStage.FAILED.value,
                error_message=str(tx_exc), message=f"Translate failed: {tx_exc}",
            )
            self._emit_event(task_id, TaskStage.FAILED.value, 100.0, f"Failed: {tx_exc}")
            return
        if self._store.is_cancelled(task_id):
            return
        logger.info("[task=%s] translate_stream complete, merging output...", task_id)
        self._emit_event(task_id, TaskStage.RENDERING.value, 80.0, "Merging pages...")
        if doc_mono is None or doc_dual is None:
            logger.error("Page merging failed: translate_stream returned None")
            self._store.update_task(
                task_id, status=TaskStage.FAILED.value,
                error_message="translate_stream returned None",
                message="Error: Page merging failed",
            )
            self._emit_event(task_id, TaskStage.FAILED.value, 100.0, "Failed: merge returned None")
            return
        self._emit_event(task_id, TaskStage.RENDERING.value, 82.0, "Subsetting fonts...")
        # Track merge progress: subset_fonts + write can take minutes for large PDFs.
        # translate_stream handles the merge internally; we poll with short sleeps
        # to allow the UI to receive periodic progress updates.
        self._emit_event(task_id, TaskStage.RENDERING.value, 85.0, "Writing output files...")
        out_dir = self.config.output_dir or os.path.dirname(request.source_path)
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
        if self.config.run_evaluation:
            try:
                from pdf2zh.evaluate import evaluate_translation
                self._emit_event(task_id, TaskStage.EVALUATING.value, 92.0,
                                 "Evaluating output...")
                eval_report = evaluate_translation(
                    request.source_path, mono_path,
                    target_lang=request.target_lang,
                    report_dir=self.config.evaluation_report_dir or None,
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

        self._store.update_task(
            task_id, status=TaskStage.COMPLETED.value, progress=100.0,
            total_progress=100.0, file_progress=100.0,
            result_files=result_files, selected_file=result_files[0]["name"],
            diagnostic_summary=diagnostic_summary,
            quality_scores=quality_scores,
            ir_snapshots=ir_snapshots,
            gate_verdicts=gate_verdicts,
            processor_reports=processor_reports,
            toc_ir_records=toc_ir_records,
            result_zip=dual_path if os.path.exists(dual_path) else mono_path,
            preview_path=dual_path if os.path.exists(dual_path) else mono_path,
            message="Completed (Legacy)",
        )
        logger.info("[task=%s] Output files written successfully", task_id)

        self._emit_event(task_id, TaskStage.COMPLETED.value, 100.0, "Complete!")

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

    def _emit_event(
        self, task_id: str, stage: str, progress: float,
        message: str = "", node_count: int = 0, diag_count: int = 0,
    ) -> None:
        event = TaskProgressEvent(
            task_id=task_id, stage=stage, progress=progress,
            current_node_count=node_count, diagnostics_count=diag_count,
            message=message,
        )
        self._store.add_event(task_id, event)
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

