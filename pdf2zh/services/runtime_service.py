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
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    """Read a float env var with a safe fallback ('' / garbage -> default)."""
    try:
        rawnum = os.environ.get(name) or ""
        return float(rawnum) if rawnum.strip() else default
    except (TypeError, ValueError):
        return default


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
# GUI「引擎模式」下拉的每个模式 → 具体管线。每个模式是 ServiceConfig 字段的
# 覆盖集合；"auto" 表示保持调用方配置（语义上等价于默认模式）。
# 落地管线只有两条（都是完整实现、可验证的路径）：
#   - legacy：``translate_stream`` 经典管线（auto / quick / standard / quality）
#   - babeldoc：``_execute_babeldoc``（BabelDOC 排版引擎）
# 不再暴露 V4 引擎模式（v3/v4）：V4 RuntimeFacade 仍是占位原型（placeholder
# 翻译 + 占位渲染），且曾因 DocumentGraph 迭代死锁导致任务卡死、队列锁死。
MODE_PRESETS: Dict[str, Dict[str, Any]] = {
    # auto 自动：保持调用方配置（默认完整 legacy 管线，见 ServiceConfig）
    "auto": {},
    # quick 快速：经典管线 + 关闭全部现代 side-channel（最快、干预最少）
    "quick": {
        "use_v4_engine": False,
        "use_v4_translator": False,
        "use_v4_layout": False,
        "use_v4_repair": False,
        "use_v4_fix_validate_loop": False,
        "use_v4_gate": False,
        "run_evaluation": False,
        "emit_ir": False,
        "relink_links": False,
        "image_engine": False,
        "content_preservation": False,
        "processor_channels": False,
    },
    # standard 标准：经典管线 + 全部现代 side-channel + 文档模型（生产默认）
    "standard": {
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
    # quality 高质量：标准 + 文档级评测 + 写回门控 + QA/渲染接管/几何簇
    "quality": {
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
    # babeldoc BabelDOC 排版引擎：独立执行路径（runtime_service._execute_babeldoc），
    # 不叠加任何 legacy/V4 预设 —— 空 preset 使 resolve_mode_config 原样返回 base。
    "babeldoc": {},
}

#: 引擎模式 → 执行管线。所有模式都映射到完整实现的管线；V4 占位引擎
#: （use_v4_engine）不再由任何 GUI 模式触发。
MODE_PIPELINES: Dict[str, str] = {
    "auto": "legacy",
    "quick": "legacy",
    "standard": "legacy",
    "quality": "legacy",
    "babeldoc": "babeldoc",
}

# ── 引擎健康熔断 ─────────────────────────────────────────────────────────────
#
# BabelDOC 每次任务（或批量任务的每个文件）都会做一次 translator 健康检查
# （next 内核 ``get_translator`` → ``translate("Hello")``）。当翻译服务被限流
# （HTTP 429 / reCAPTCHA 挑战，例如 Google 对某些 IP/代理的临时封锁）时，健康
# 检查会持续失败数分钟；若不做熔断，批量任务会对每一个文件重复 health check
# 并反复失败，表现为"任务一直刷屏失败 / 看似卡死"。这里用一个进程级冷却表：
# 限流类失败后，冷却期内同一 (engine, lang_in, lang_out, envs) 的 BabelDOC
# 任务直接快速失败，不再重复探测，直到冷却期结束再尝试。
_ENGINE_COOLDOWN_SECONDS = 60.0


def _is_rate_limited_error(exc: Any) -> bool:
    """Heuristic: is this a translation-service rate-limit / CAPTCHA failure?

    BabelDOC fast-fails deterministic service blocks (HTTP 429 / reCAPTCHA
    challenge) through ``TranslateEngineSettingError`` / ``RateLimitedError``
    with self-describing messages. We match on the message so both the
    next-kernel and the legacy adapter error paths are covered without
    importing their internals.
    """
    text = str(exc)
    lowered = text.lower()
    return (
        "rate-limited" in lowered
        or "rate limited" in lowered
        or "captcha" in lowered
        or "http 429" in lowered
    )



def resolve_pipeline(mode_choice: Optional[str]) -> str:
    """Map a GUI engine-mode value to the concrete execution pipeline.

    Returns ``"legacy"`` (``translate_stream``) or ``"babeldoc"``
    (``_execute_babeldoc``). Unknown/empty modes fall back to ``"legacy"``
    so a future flag never leaves a task without a working pipeline.
    """
    mode = (mode_choice or "auto").strip().lower() or "auto"
    return MODE_PIPELINES.get(mode, "legacy")


#: legacy 模式注入 translate_stream 的额外模态 kwargs（babeldoc 模式不走此表）。
MODE_LEGACY_KWARGS: Dict[str, Dict[str, Any]] = {
    "quick": {"document_model": False, "toc_split": True,
              "render_takeover": False, "translation_qa": False,
              "geometry_cluster": False, "observability": False,
              "pipeline_dump": False},
    "standard": {"document_model": True, "toc_split": True},
    "quality": {"document_model": True, "toc_split": True,
                "render_takeover": True, "translation_qa": True,
                "geometry_cluster": True},
}

# ── V1.24 工作量模型（Work Graph）─────────────────────────────────────────────
#
# 阶段权重（第六原则：Pipeline Progress）：进度 = 阶段权重 × 阶段内完成度，
# 不再是魔法数字 10/30/40/50/70/85/95。各阶段累计值刻意与历史检查点一致：
#
#     parsing [0,10] analyzing [10,30] planning [30,40] translating [40,70]
#     layouting [70,85] rendering [85,95] evaluating [95,100]
#
# 文档页数已知后（_update_aggregator_weights）会按页数重排 translating /
# layouting / rendering 权重（页数越多翻译占比越高），进度始终来自工作量模型。
_STAGE_WEIGHTS: Dict[str, float] = {
    "parsing": 10.0,
    "analyzing": 20.0,
    "planning": 10.0,
    "translating": 30.0,
    "layouting": 15.0,
    "rendering": 10.0,
    "evaluating": 5.0,
}

#: 阶段顺序（决定累计区间）。
_STAGE_ORDER: List[str] = list(_STAGE_WEIGHTS.keys())

_STAGE_BOUNDS: Dict[str, Tuple[float, float]] = {}
_cum = 0.0
for _stage_name in _STAGE_ORDER:
    _w = _STAGE_WEIGHTS[_stage_name]
    _STAGE_BOUNDS[_stage_name] = (_cum, _cum + _w)
    _cum += _w


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
    backend: str = "auto"
    """ONNX 版面分析推理后端（auto/cpu/cuda/dml），每次任务开始时按此值应用。

    与 CLI ``--backend`` 语义一致：``auto`` 保持调用方/默认行为（CPU 优先），
    ``cuda``/``dml`` 显式开启 GPU 加速，GPU 不可用或崩溃时自动回退 CPU。
    """

    output_dir: str = ""
    """结果文件输出目录（用户自定义下载位置）。空串回落到
    ``ServiceConfig.output_dir``，再回落到源文件所在目录。"""
    parse_engine: str = "auto"
    """解析引擎（auto/legacy/babeldoc/magicpdf），与 CLI ``--parse-engine`` 一致。

    ``auto`` 保持历史语义（mode_choice 决定 legacy/babeldoc）；``magicpdf``
    走 MinerU/magic-pdf 解析链路（引擎缺失时自动熔断降级 legacy）。
    """

    magicpdf_ocr: bool = False
    """magicpdf 解析是否强制开启 OCR（对应 CLI ``--magicpdf-ocr``，等价
    ``magicpdf_ocr_mode="on"``）。保留以兼容旧调用方。"""

    magicpdf_ocr_mode: str = "auto"
    """magicpdf 解析的 OCR 三态（auto/on/off，对应 CLI ``--magicpdf-ocr-mode``）。

    ``auto``（默认）预检命中扫描/损坏信号才自动开启 OCR；``on`` 强制开启；
    ``off`` 用户显式关闭 OCR，预检命中也绝不强制开启。``magicpdf_ocr``
    （bool）为 ``True`` 时等价 ``on`` 并优先于本字段。
    """

    glossary_files: List[str] = field(default_factory=list)
    """专业词表 CSV 路径列表（babeldoc 解析链路生效，格式 source,target[,tgt_lng]）。

    与 CLI ``--glossary-files`` / API ``glossaries`` 上传字段一致；批量任务
    经 ``dataclasses.replace`` 自动携带到每个子请求。legacy/magicpdf 链路
    当前忽略该字段（Phase 3 接入 legacy 后处理钉死）。
    """



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
    #: V1.24：预计剩余秒数（ProgressAggregator 按已完工权重速率外推；0=未知）。
    eta: float = 0.0
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "stage": self.stage,
            "progress": self.progress,
            "current_node_count": self.current_node_count,
            "diagnostics_count": self.diagnostics_count,
            "message": self.message,
            "eta": self.eta,
            "timestamp": self.timestamp,
        }


@dataclass
class RuntimeNoticeEvent:
    """Structured runtime notice (severity/title/detail/tip).

    Orthogonal to the progress channel: notices describe *process-level*
    health (backend degradation, cache migration, fallback decisions, ...)
    and are never clamped/smoothed by the monotonic progress semantics.
    Delivered through the same listener pipeline as ``TaskProgressEvent``.
    """

    task_id: str
    severity: str = "info"
    """info | warning | error"""
    title: str = ""
    detail: str = ""
    tip: str = ""
    message: str = ""
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "severity": self.severity,
            "title": self.title,
            "detail": self.detail,
            "tip": self.tip,
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
    eta: float = 0.0
    """V1.24：预计剩余秒数（0 = 未知/已完成）。"""
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
            "eta": self.eta,
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

    def get_cancel_event(self, task_id: str) -> Optional[threading.Event]:
        """Return the per-task cancel Event (None when the task is unknown).

        The event is signalled by ``cancel_task``/``skip_task``, giving the
        translation pipeline a real interruptible cancellation hook.
        """
        with self._lock:
            return self._cancel_events.get(task_id)

    def prune_terminated(self, max_age: float, now: float) -> int:
        """Remove terminal-state tasks older than ``max_age`` seconds.

        Bounds the in-memory task store, per-task event list and cancel
        events so long-running service processes do not accumulate
        unbounded memory across many translation jobs (S2).

        Returns the number of removed tasks.
        """
        terminal = (
            TaskStage.COMPLETED.value,
            TaskStage.CANCELLED.value,
            TaskStage.FAILED.value,
        )
        with self._lock:
            stale = [
                tid for tid, st in self._tasks.items()
                if st.status in terminal and (now - st.updated_at) > max_age
            ]
            for tid in stale:
                self._tasks.pop(tid, None)
                self._events.pop(tid, None)
                self._cancel_events.pop(tid, None)
            return len(stale)

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
        #: S2: 终态任务保留时长（秒）；超龄任务由 sweeper 定期清理。
        self._retention_seconds = _env_float("PDF2ZH_TASK_RETENTION_SECONDS", 3600.0)
        #: S2: 后台清扫线程间隔（秒）。
        self._sweep_interval = max(10.0, _env_float("PDF2ZH_SWEEP_INTERVAL", 60.0))
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
        #: V1.24：每个任务一个 ProgressAggregator（权重式工作量模型）。
        #: 仅 submit_task 创建的真实任务启用；测试直连 _emit_event 的路径
        #: 不经过聚合器，保持历史行为。
        self._aggregators: Dict[str, Any] = {}
        self._aggregator_lock = threading.Lock()
        #: 每个任务当前的阶段权重表（文档页数已知后重排 translating 等）。
        self._task_stage_weights: Dict[str, Dict[str, float]] = {}
        self._stage_weights_lock = threading.Lock()
        #: 引擎健康熔断：限流类失败（HTTP 429 / CAPTCHA）后进入冷却期；冷却期
        #: 内同一引擎组合的 BabelDOC 任务直接快速失败，避免批量任务对每个文件
        #: 重复 health check 反复失败刷屏。见 ``_ENGINE_COOLDOWN_SECONDS``。
        self._engine_cooldown_until: Dict[Tuple[Any, ...], float] = {}
        self._engine_cooldown_error_cache: Dict[Tuple[Any, ...], str] = {}
        self._engine_cooldown_lock = threading.Lock()

        #: S2: 后台清扫线程（终态任务内存清理；daemon 不阻塞退出）。
        self._sweeper = threading.Thread(
            target=self._sweeper_loop, name="pdf2zh-task-sweeper", daemon=True,
        )
        self._sweeper.start()

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
        self._init_aggregator(task_id)
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

    def get_task_events(self, task_id: str, since: int = 0) -> List[TaskProgressEvent]:
        """按游标读取任务事件（REST/SSE 层 Last-Event-ID 断线续传用）。

        ``since`` 为已消费事件数；返回 ``events[since:]``。与
        ``subscribe_events`` 的内部游标语义一致。
        """
        return self._store.get_events(task_id, since=since)

    def _apply_request_backend(self, task_id: str, request: TranslationRequest) -> None:
        """Apply the requested ONNX layout-inference backend for this task.

        The backend preference is process-global (the same knob as the CLI
        ``--backend`` flag). When the requested value differs from the currently
        active one we call ``set_backend`` (which also syncs the choice to
        BabelDOC's internal ONNX sessions) and reset the cached ``ModelInstance``
        singleton so the next load rebuilds the session with the new providers
        (``resolve_providers`` auto-falls-back to CPU when a GPU provider is
        unavailable, so an explicit cuda/dml request never hard-fails).
        """
        from pdf2zh.doclayout import ModelInstance, get_backend, set_backend

        wanted = (request.backend or "auto").strip().lower() or "auto"
        if wanted not in ("auto", "cpu", "cuda", "dml"):
            wanted = "auto"
        current = get_backend() or "auto"
        if current != wanted:
            logger.info(
                "[task=%s] ONNX backend switch: %s -> %s (applied on next model load)",
                task_id, current, wanted,
            )
            set_backend(wanted)
            # 全局已缓存的 ONNX session 基于旧 provider 构建；重置后
            # _execute_legacy/_execute_babeldoc 会按新后端重新加载模型。
            ModelInstance.value = None
            # 8.2.1 Warm Process Pool：worker 后端在建池时固定（initializer
            # 只在建池时执行一次）；后端切换后旧池的 worker 仍持有旧 provider，
            # 必须重建池。未启用时 shutdown 为幂等空操作。
            try:
                from pdf2zh.parallel.pool import shutdown_shared_pool  # noqa: PLC0415

                shutdown_shared_pool()
            except Exception:  # noqa: BLE001 -- 池清理失败不阻断任务
                logger.debug(
                    "[task=%s] warm pool shutdown on backend switch failed", task_id,
                )

    def _execute_task(self, task_id: str, request: TranslationRequest) -> None:
        """Internal: run translation in background thread."""
        try:
            self._emit_event(task_id, TaskStage.PARSING.value, 5.0, "Starting...")
            if self._store.is_cancelled(task_id):
                return
            # 按用户选择的 ONNX 推理后端初始化版面分析（auto/cpu/cuda/dml）。
            # 必须在模型加载（ModelInstance）之前生效：后端变化时重置全局单例，
            # 使本任务按新 provider 重建 ONNX session（GPU 不可用自动回退 CPU）。
            self._apply_request_backend(task_id, request)

            mode = (request.extra_config or {}).get("mode_choice") or "auto"
            self._store.update_task(task_id, mode_choice=mode)
            task_config = resolve_mode_config(mode, self.config)
            self._sync_feature_flags(task_id, task_config)
            files = request.resolved_files()
            cancel_event = self._store.get_cancel_event(task_id)
            # 解析引擎路由（--parse-engine 语义）：magicpdf 优先于 mode_choice；
            # babeldoc 显式值等价 mode_choice=babeldoc，保持历史行为不变。
            parse_engine = (getattr(request, "parse_engine", "auto") or "auto").lower()
            if parse_engine == "magicpdf":
                # MinerU/magic-pdf 解析引擎独立执行路径（引擎缺失熔断降级 legacy）。
                self._execute_magicpdf(task_id, request, task_config)
            elif parse_engine == "babeldoc" or resolve_pipeline(mode) == "babeldoc":
                # BabelDOC 布局引擎独立执行路径：单文件直跑，批量走 _execute_batch
                # （其 per-file 分发同样识别 babeldoc 模式）。
                if len(files) > 1:
                    self._execute_batch(task_id, request, files, task_config, cancel_event)
                else:
                    self._execute_babeldoc(task_id, request, task_config)
            elif len(files) > 1:
                self._execute_batch(task_id, request, files, task_config, cancel_event)
            elif task_config.use_v4_engine:
                self._execute_v4(task_id, request, task_config)
            else:
                self._execute_legacy(task_id, request, task_config, cancel_event)
        except KeyboardInterrupt:
            # V3-4：Ctrl+C 中断（GUI 场景由 parallel.interrupt 旗标桥接，后台
            # 翻译线程经 coordinator 短路抛 KeyboardInterrupt）。按“用户取消”
            # 语义落终态 —— 绝不打印线程级未处理异常（threading.excepthook），
            # 也绝不进入任何串行兜底（translate_stream 已 except KeyboardInterrupt: raise）。
            logger.info("[task=%s] interrupted by Ctrl+C; task cancelled", task_id)
            self._store.update_task(
                task_id, status=TaskStage.CANCELLED.value,
                error_message="Interrupted by user", message="Cancelled by user",
            )
            self._emit_event(task_id, TaskStage.CANCELLED.value, 100.0, "Cancelled by user")
        except Exception as exc:
            logger.error("Task %s failed: %s", task_id, exc, exc_info=True)
            self._store.update_task(
                task_id, status=TaskStage.FAILED.value,
                error_message=str(exc), message=f"Error: {exc}",
            )
            self._emit_event(task_id, TaskStage.FAILED.value, 100.0, f"Failed: {exc}")
        finally:
            # V3-5：任务已落终态（COMPLETED/CANCELLED/FAILED，含单/批量/v4 全路径）——
            # 此后无活动任务。GUI cancel_only 模式下“下一次 Ctrl+C 即关闭应用”
            # （翻译运行中的第一次 Ctrl+C 只取消任务、不退出；任务结束后空闲态
            #  用户再按一次即视为主动关闭，无需连续按两次）。新任务提交时
            # on_translate 会 reset_interrupt_flag() 清除该标记，恢复运行中语义。
            state = self._store.get_task(task_id)
            if state and state.status in (
                TaskStage.COMPLETED.value,
                TaskStage.CANCELLED.value,
                TaskStage.FAILED.value,
            ):
                try:
                    from pdf2zh.parallel.interrupt import mark_exit_pending

                    mark_exit_pending()
                except Exception:  # noqa: BLE001 -- 仅为标记，绝不干扰任务落终态
                    pass

    def _execute_batch(
        self, task_id: str, request: TranslationRequest, files: List[str],
        config: ServiceConfig, cancel_event: Optional[threading.Event] = None,
    ) -> None:
        """Execute a multi-file batch sequentially, aggregating overall progress.

        Per-file results are accumulated into ``result_files`` and failures are
        recorded per-file (task continues with the next file). The task only
        reaches a terminal stage once every file has been processed.
        """
        mode = (request.extra_config or {}).get("mode_choice") or "auto"
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
            self._reset_aggregator(task_id)
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
                # 路由必须与 _execute_task 完全一致：parse_engine 显式值优先，
                # 否则按 mode_choice 的管线预设。此前这里只看 mode_choice——
                # 批量任务默认 mode_choice="auto" 时，即使 GUI/CLI 显式选择了
                # parse_engine="babeldoc"，逐文件也会被错误路由到 legacy 管线，
                # 导致扫描 PDF 的 OCR 走了 legacy/magic-pdf 而不是 BabelDOC
                # 本身的扫描检测 + OCR workaround 管线。
                parse_engine = (
                    getattr(request, "parse_engine", "auto") or "auto"
                ).lower()
                if parse_engine == "babeldoc" or resolve_pipeline(mode) == "babeldoc":
                    self._execute_babeldoc(task_id, sub_request, config)
                elif config.use_v4_engine:
                    self._execute_v4(task_id, sub_request, config)
                else:
                    self._execute_legacy(task_id, sub_request, config, cancel_event)
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
            if self._store.is_cancelled(task_id):
                # Single-file task cancelled by the user: drop the late completion
                # so a worker finishing after the cancel cannot resurrect the
                # task (CANCELLED -> COMPLETED).
                logger.info("[task=%s] cancelled; dropping late completion", task_id)
                return
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
            if self._store.is_cancelled(task_id):
                # Single-file task cancelled by the user: keep the terminal state
                # set by the canceller instead of overwriting it with a failure
                # report (CANCELLED -> FAILED).
                logger.info("[task=%s] cancelled; dropping late failure report", task_id)
                return
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

    def _resolve_out_dir(self, request: TranslationRequest,
                         config: Optional[ServiceConfig]) -> str:
        """输出目录优先级：请求级 output_dir > 服务级 config > 源文件目录。"""
        cfg = config or self.config
        custom = (getattr(request, "output_dir", "") or "").strip()
        if custom:
            return custom
        if cfg.output_dir:
            return cfg.output_dir
        return os.path.dirname(request.source_path)

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
        # V1.24：页数已知 -> 重排阶段权重（页数越多翻译占比越高）
        self._update_aggregator_weights(task_id, node_overview or {})
        self._emit_event(task_id, TaskStage.PLANNING.value, 40.0, "Planning...")
        rt.plan()
        if self._store.is_cancelled(task_id):
            return
        self._emit_event(task_id, TaskStage.TRANSLATING.value, 50.0, "Translating...")
        if config.use_v4_translator:
            from pdf2zh.v3.translation_runtime import TranslationRuntime
            tr = TranslationRuntime()
            if rt.plans:
                # rt.plans 是 {node_id: TranslationPlan}，合并为单 plan 后执行；
                # 不能把整个 dict 当作 TranslationPlan 传入
                # （历史 bug：'dict' object has no attribute 'node_ids'）。
                from pdf2zh.v3.planner import TranslationPlan
                plan = TranslationPlan(node_ids=list(rt.plans.keys()))
                tr.execute(rt.graph, plan)
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

        out_dir = self._resolve_out_dir(request, config)
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
            for node in getattr(graph, "nodes", None) or ():
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
                        config: Optional[ServiceConfig] = None,
                        cancel_event: Optional[threading.Event] = None) -> None:
        """Execute with Legacy translate_stream pipeline."""
        from pdf2zh.high_level import translate_stream
        from pdf2zh.doclayout import ModelInstance, OnnxModel

        config = config or self.config
        total_files = self._batch_total(task_id)

        # Ensure layout model is loaded before translation
        from pdf2zh.doclayout import is_cpu_degraded, try_rearm_gpu
        if is_cpu_degraded():
            rearmed = try_rearm_gpu()
            if rearmed:
                logger.warning(
                    "[task=%s] Process backend was CPU-degraded (previous GPU worker "
                    "crash); auto-rearming GPU for this task (transient fault retry). "
                    "A second crash will keep the backend on CPU for this session.",
                    task_id,
                )
                ModelInstance.value = None  # GPU session 在崩溃后已重置，重载
            else:
                logger.warning(
                    "[task=%s] Process backend is CPU-degraded (previous GPU worker crash); "
                    "layout inference will run on CPU for this task. Restart the service or "
                    "re-run with --backend auto (pdf2zh CLI) to retry GPU.",
                    task_id,
                )
                self._emit_notice(
                    task_id,
                    "warning",
                    "Process backend is CPU-degraded",
                    "A previous GPU-backed parallel worker crash degraded layout inference "
                    "to CPU for this task.",
                    "Restart the service or re-run with --backend auto to retry GPU.",
                )
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
        # V1.24：按页数预置阶段权重（翻译占比随页数上调）
        self._prime_legacy_weights(task_id, request.source_path)
        self._emit_event(task_id, TaskStage.TRANSLATING.value, 50.0, "Translating...")
        logger.info("[task=%s] Translation phase starting...", task_id)
        # V8.3/V8.4: 主链路 IR 产出 + 写回前门控（side-channel）
        v3_output: Dict[str, Any] = {}
        # 并行进度回调：translate_stream 按已完成的页面分块回报（0-100），
        # 映射到翻译窗口 30→70（阶段权重 translating=[40,70]；经聚合器
        # 按权重折算 + 指数平滑后推给 UI），避免并行下进度条跳变。
        def _progress_cb(pct: float, msg: str) -> None:
            self._emit_smooth(
                task_id, TaskStage.TRANSLATING.value,
                30.0 + max(0.0, min(100.0, float(pct))) * 0.40,
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
                cancellation_event=cancel_event,
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
        except KeyboardInterrupt:
            # V3-4：Ctrl+C 中断 —— 绝不当作“翻译失败”（不写 FAILED 终态），
            # 传播给 _execute_task 统一按“用户取消”落终态。
            raise
        except Exception as tx_exc:
            if self._store.is_cancelled(task_id):
                # User cancel: the pipeline raised CancelledError (or similar);
                # keep the terminal state already set.
                logger.info(
                    "[task=%s] cancelled during translation; aborting pipeline",
                    task_id,
                )
                return
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
        out_dir = self._resolve_out_dir(request, config)
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

    def _execute_magicpdf(
        self, task_id: str, request: TranslationRequest,
        config: Optional[ServiceConfig] = None,
    ) -> None:
        """MinerU/magic-pdf 解析引擎执行路径（--parse-engine magicpdf）。

        把 TranslationRequest 映射为 CLI 风格 Namespace（用 pdf2zh.parse_args
        补齐全部默认字段，保证引擎缺失时 run_magicpdf_main 的熔断降级
        ``_run_legacy_kernel`` 拿到完整字段）：解析→桥接→翻译→转储。
        进度经事件流上报；异常落 FAILED 终态。
        """
        from pdf2zh.magicpdf_cli import run_magicpdf_main
        from pdf2zh.pdf2zh import parse_args

        files = request.resolved_files()
        total = len(files)
        if not files:
            self._fail_file(task_id, "No source file provided", total_files=0)
            return
        try:
            ns = parse_args([files[0]])
        except Exception as exc:  # noqa: BLE001 -- 参数构造失败直接落失败
            logger.error("[task=%s] magicpdf args build failed: %s", task_id, exc)
            self._fail_file(task_id, f"magicpdf args error: {exc}", total_files=total)
            return
        ns.files = files
        out_dir = self._resolve_out_dir(request, config) or os.path.dirname(os.path.abspath(files[0]))
        ns.output = out_dir
        ns.backend = request.backend or "auto"
        ns.magicpdf_ocr = bool(request.magicpdf_ocr)
        ns.magicpdf_ocr_mode = getattr(request, "magicpdf_ocr_mode", "auto") or "auto"
        ns.service = request.engine or "google"
        ns.lang_in = request.source_lang or "auto"
        ns.lang_out = request.target_lang or "zh-CN"
        ns.pages = request.page_range
        ns.thread = request.threads
        ns.vfont = request.vfont or ""
        ns.vchar = request.vchar or ""
        ns.skip_subset_fonts = request.skip_subset_fonts
        ns.ignore_cache = request.ignore_cache
        extra = request.extra_config or {}
        ns.prompt = extra.get("prompt") or ""
        self._emit_event(task_id, TaskStage.PARSING.value, 10.0,
                         "magic-pdf/MinerU parsing...")
        try:
            rc = run_magicpdf_main(ns)
        except Exception as exc:  # noqa: BLE001
            logger.error("[task=%s] magicpdf engine failed: %s",
                          task_id, exc, exc_info=True)
            self._fail_file(task_id, f"magicpdf engine failed: {exc}",
                           total_files=total)
            return
        if rc != 0:
            self._fail_file(task_id, f"magicpdf engine returned {rc}",
                           total_files=total)
            return
        # magicpdf 产物为 {output}/magicpdf/ 下的 JSON 转储 + 译后 mono PDF
        # （run_magicpdf_main 默认渲染 {stem}_mono.pdf）。二者都作为结果文件
        # 收集供下载，且译后 PDF 优先作为选中/预览对象 —— 否则用户只能下载
        # 到一堆 JSON，看不到翻译产物。
        result_files: List[Dict[str, str]] = []
        pdf_entry: Optional[Dict[str, str]] = None
        try:
            magic_dir = os.path.join(out_dir, "magicpdf")
            if os.path.isdir(magic_dir):
                for name in sorted(os.listdir(magic_dir)):
                    if not (name.endswith(".json") or name.endswith(".pdf")):
                        continue
                    path = os.path.join(magic_dir, name)
                    entry = {"name": name, "path": path}
                    result_files.append(entry)
                    if name.endswith(".pdf") and pdf_entry is None:
                        pdf_entry = entry
        except Exception:  # noqa: BLE001 -- 结果收集失败不影响落终态
            pass
        self._complete_file(
            task_id, result_files, total_files=total,
            selected_file=(
                pdf_entry["name"] if pdf_entry is not None
                else (result_files[0]["name"] if result_files else None)
            ),
            preview_path=(pdf_entry["path"] if pdf_entry is not None else None),
            message="Completed (MagicPDF)",
        )
        logger.info("[task=%s] magicpdf engine complete", task_id)

    # ── 引擎健康熔断辅助 ───────────────────────────────────────────────────────

    def _engine_key(self, request: TranslationRequest) -> Tuple[Any, ...]:
        """Identity key for the engine-cooldown table.

        Includes the env overrides so a user switching API keys / proxies (e.g.
        a per-task env line) is not blocked by an unrelated recent failure.
        """
        envs = sorted((request.extra_config or {}).get("envs", {}).items())
        return (
            (request.engine or "google").lower(),
            request.source_lang or "auto",
            request.target_lang or "zh-CN",
            tuple(envs),
        )

    def _engine_cooldown_error(self, key: Tuple[Any, ...]) -> Optional[str]:
        """Return the cached rate-limit error while an engine is cooling down.

        ``None`` means the engine is not in cooldown (healthy or past the
        cooldown window) and a normal health-check attempt may proceed.
        """
        with self._engine_cooldown_lock:
            until = self._engine_cooldown_until.get(key, 0.0)
            if until > time.time():
                return self._engine_cooldown_error_cache.get(
                    key, "translation service is temporarily unavailable"
                )
        return None

    def _mark_engine_unavailable(self, key: Tuple[Any, ...], error: Any) -> None:
        """Enter engine cooldown after a rate-limit / CAPTCHA failure."""
        with self._engine_cooldown_lock:
            self._engine_cooldown_until[key] = time.time() + _ENGINE_COOLDOWN_SECONDS
            self._engine_cooldown_error_cache[key] = str(error)

    def _execute_babeldoc(
        self, task_id: str, request: TranslationRequest,
        config: Optional[ServiceConfig] = None,
    ) -> None:
        """Execute with the BabelDOC (YADT) layout engine (mode='babeldoc').

        Prefers the modified pdf2zh_next kernel pipeline (engine settings are
        mapped onto a ``pdf2zh_next.SettingsModel`` and driven through
        ``create_babeldoc_config`` + BabelDOC ``async_translate``). When the
        kernel is missing or the selected engine has no kernel mapping, falls
        back to the legacy ``build_translator``-wrapper pipeline. Progress
        events are forwarded through the work-graph aggregator; cancellation
        is cooperative (BabelDOC aborts at its next checkpoint).
        """
        import asyncio

        from pdf2zh.babeldoc_adapter import (
            BabeldocNotInstalledError,
            run_babeldoc_translation,
        )
        try:
            from pdf2zh.babeldoc_next_adapter import (
                BabeldocNextUnavailableError,
                run_babeldoc_next_translation,
            )
        except Exception:  # noqa: BLE001 -- next-kernel import is best effort
            BabeldocNextUnavailableError = None
            run_babeldoc_next_translation = None

        config = config or self.config
        total_files = self._batch_total(task_id)
        engine_key = self._engine_key(request)
        cooldown_error = self._engine_cooldown_error(engine_key)
        if cooldown_error is not None:
            # 引擎正处于限流冷却期：直接快速失败，不再重复 health check。
            # 批量任务因此只失败一次（首个文件触发冷却），后续文件秒级跳过。
            logger.warning(
                "[task=%s] engine %s recently rate-limited (%s); fast-failing "
                "instead of re-running the translator health check",
                task_id, request.engine, cooldown_error,
            )
            self._fail_file(
                task_id,
                "Translation engine is temporarily unavailable "
                f"({cooldown_error}). Please retry later or switch to another "
                "translation engine / proxy.",
                total_files=total_files,
            )
            return

        self._emit_event(task_id, TaskStage.PARSING.value, 5.0,
                         "BabelDOC engine starting...")
        if self._store.is_cancelled(task_id):
            return

        extra = request.extra_config or {}
        envs = dict(extra.get("envs") or {})
        prompt = extra.get("prompt")
        ocr_mode = extra.get("ocr_mode")
        glossary_files = list(request.glossary_files or [])
        out_dir = self._resolve_out_dir(request, config)
        def _forward_progress(stage: str, pct: float, msg: str) -> None:
            # _emit_smooth throttles the 0.2s BabelDOC event cadence into
            # monotone progress steps while still forwarding stage msgs.
            self._emit_smooth(task_id, stage, pct, msg)

        try:
            result_files = None
            engine_label = "BabelDOC (pdf2zh_next kernel)"
            if run_babeldoc_next_translation is not None:
                try:
                    result_files = run_babeldoc_next_translation(
                        source_path=request.source_path,
                        lang_in=request.source_lang,
                        lang_out=request.target_lang,
                        service=request.engine,
                        pages=request.page_range or None,
                        envs=envs,
                        prompt=prompt,
                        ignore_cache=request.ignore_cache,
                        qps=request.threads or 4,
                        output_dir=out_dir,
                        progress_cb=_forward_progress,
                        cancelled_check=lambda: self._store.is_cancelled(task_id),
                        debug=bool(getattr(config, "debug", False)),
                        ocr_mode=ocr_mode,
                        glossary_files=glossary_files,
                    )
                except BabeldocNextUnavailableError as exc:
                    logger.info(
                        "[task=%s] pdf2zh_next kernel unavailable for engine "
                        "%r (%s); falling back to the legacy BabelDOC pipeline",
                        task_id, request.engine, exc,
                    )
                    result_files = None
            if result_files is None:
                engine_label = "BabelDOC (legacy layout engine)"
                result_files = run_babeldoc_translation(
                    source_path=request.source_path,
                    lang_in=request.source_lang,
                    lang_out=request.target_lang,
                    service=request.engine,
                    pages=request.page_range or None,
                    envs=envs,
                    prompt=prompt,
                    ignore_cache=request.ignore_cache,
                    qps=request.threads or 4,
                    output_dir=out_dir,
                    progress_cb=_forward_progress,
                    cancelled_check=lambda: self._store.is_cancelled(task_id),
                    debug=bool(getattr(config, "debug", False)),
                    ocr_mode=ocr_mode,
                    glossary_files=glossary_files,
                )
        except BabeldocNotInstalledError as exc:
            self._fail_file(task_id, exc, total_files=total_files)
            return
        except asyncio.CancelledError:
            return  # cooperative cancel already left the task in CANCELLED
        except Exception as exc:
            if self._store.is_cancelled(task_id):
                return  # user cancelled during the run; keep terminal state
            if _is_rate_limited_error(exc):
                # 确定性服务端封锁（HTTP 429 / CAPTCHA）：进入冷却期，后续
                # 文件/任务直接快速失败，不再逐文件重复 health check 刷屏。
                self._mark_engine_unavailable(engine_key, exc)
                logger.warning(
                    "[task=%s] BabelDOC engine %s rate-limited: %s",
                    task_id, request.engine, exc,
                )
            else:
                logger.error("[task=%s] BabelDOC failed: %s", task_id, exc,
                             exc_info=True)
            self._fail_file(task_id, exc, total_files=total_files)
            return
        if self._store.is_cancelled(task_id):
            return
        if not result_files:
            self._fail_file(
                task_id, "BabelDOC produced no output files",
                total_files=total_files,
            )
            return
        dual = next(
            (f["path"] for f in result_files if "dual" in f["name"]),
            result_files[0]["path"],
        )
        self._complete_file(
            task_id, result_files, total_files=total_files,
            selected_file=result_files[0]["name"],
            preview_path=dual if os.path.exists(dual) else result_files[0]["path"],
            diagnostic_summary=engine_label,
            message="Completed (BabelDOC)",
        )

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

    # ── V1.24 ProgressAggregator 集成（Work Graph 工作量模型） ────────────────

    def _init_aggregator(self, task_id: str, alpha: float = 0.08) -> None:
        """为任务创建 ProgressAggregator（失败时静默回退 legacy 进度）。

        全量注册阶段任务（percentage 以全部阶段权重为分母，而非已见任务）；
        阶段任务按需 lazy 注册的百分比会随已注册权重膨胀，故此处预先注册。
        """
        try:
            from pdf2zh.v3.progress_aggregator import ProgressAggregator
            agg = ProgressAggregator(alpha=alpha)
            with self._stage_weights_lock:
                self._task_stage_weights[task_id] = dict(_STAGE_WEIGHTS)
            for name in _STAGE_ORDER:
                agg.add_task(f"stage:{name}", _STAGE_WEIGHTS[name], stage=name)
            with self._aggregator_lock:
                self._aggregators[task_id] = agg
        except Exception:
            logger.debug("ProgressAggregator init skipped", exc_info=True)

    def _reset_aggregator(self, task_id: str) -> None:
        """清空聚合器（批次模式：每个文件处理前调用，避免阶段进度回退）。

        批次整体进度由 ``_BatchContext`` 在事件层继续聚合，全局单调性不受影响。
        """
        with self._aggregator_lock:
            agg = self._aggregators.get(task_id)
            if agg is not None:
                agg.reset()
            self._task_stage_weights[task_id] = dict(_STAGE_WEIGHTS)

    def _update_aggregator_weights(self, task_id: str, counts: Dict[str, int]) -> None:
        """文档页数/节点数已知后重排阶段权重（页数越多翻译占比越高）。

        权重表变更时重置聚合器并按新权重重注册阶段任务 —— 已完成的阶段
        由后续事件的「隐含完成」逻辑自动补齐，进度不回退。
        纯 side-channel：聚合器不存在或计数无效时静默跳过。
        """
        with self._stage_weights_lock:
            weights = self._task_stage_weights.get(task_id)
            if weights is None:
                return
        pages = int(counts.get("pages") or 0)
        if pages <= 0:
            return
        w = dict(weights)
        w["translating"] = min(60.0, max(20.0, 10.0 + pages * 1.0))
        w["layouting"] = min(25.0, max(10.0, 5.0 + pages * 0.3))
        w["rendering"] = min(20.0, max(6.0, 3.0 + pages * 0.2))
        total = sum(w.values())
        if total > 0:
            w = {k: v / total * 100.0 for k, v in w.items()}
        with self._stage_weights_lock:
            self._task_stage_weights[task_id] = w
        with self._aggregator_lock:
            agg = self._aggregators.get(task_id)
            if agg is not None:
                agg.reset()
                for name in _STAGE_ORDER:
                    agg.add_task(f"stage:{name}", w.get(name, 0.0), stage=name)

    def _prime_legacy_weights(self, task_id: str, source_path: str) -> None:
        """legacy 路径：翻译前用页数预置阶段权重（fitz 快速只读页数）。"""
        if task_id not in self._aggregators or not source_path:
            return
        try:
            import fitz as _fitz
            with _fitz.open(source_path) as src:
                self._update_aggregator_weights(
                    task_id, {"pages": int(src.page_count)}
                )
        except Exception:
            pass

    def _stage_bounds(self, task_id: str) -> Dict[str, Tuple[float, float]]:
        """当前任务的阶段累计区间（按权重表计算）。"""
        with self._stage_weights_lock:
            weights = self._task_stage_weights.get(task_id, _STAGE_WEIGHTS)
        bounds: Dict[str, Tuple[float, float]] = {}
        cum = 0.0
        for name in _STAGE_ORDER:
            w = weights.get(name, 0.0)
            bounds[name] = (cum, cum + w)
            cum += w
        return bounds

    def _map_stage_progress(self, task_id: str, stage: str, progress: float):
        """把绝对百分比映射到工作量模型：阶段权重 × 阶段内完成度。

        - 阶段任务按需注册（ensure_task：权重只在该任务首次出现时生效）；
        - 事件进度达到阶段区间终点 -> finish（权重全额入账）；
        - 否则作为 Running 的局部进度（Partial Progress）按权重折算；
        - 返回 (weighted_progress, eta)：weighted 已过指数平滑，eta 按
          已完工权重速率外推（0 = 无法估计）。
        """
        from pdf2zh.v3.progress_aggregator import ProgressAggregator  # noqa: F401

        with self._aggregator_lock:
            agg = self._aggregators.get(task_id)
        if agg is None:
            return float(progress), 0.0
        terminal = stage in (
            TaskStage.COMPLETED.value,
            TaskStage.FAILED.value,
            TaskStage.CANCELLED.value,
        )
        if terminal:
            if stage == TaskStage.COMPLETED.value:
                return 100.0, 0.0
            return float(progress), 0.0
        task_key = f"stage:{stage}"
        weights = self._task_stage_weights.get(task_id, _STAGE_WEIGHTS)
        agg.ensure_task(task_key, weights.get(stage, 0.0), stage=stage)
        bounds = self._stage_bounds(task_id)
        start, end = bounds.get(stage, (0.0, 100.0))
        p = max(0.0, min(100.0, float(progress)))
        # 事件乱序/跳阶时进度不塌方（第一原则：进度来自 Work Graph，不是事件
        # 顺序）：凡区间终点已 ≤ p 的阶段，一律视为已隐含完成。
        for name, (s, e) in bounds.items():
            if name == stage or e > p + 1e-9:
                continue
            agg.ensure_task(f"stage:{name}", weights.get(name, 0.0), stage=name)
            agg.finish(f"stage:{name}")
        if p >= end:
            agg.finish(task_key)
        else:
            weight = max(end - start, 1e-9)
            frac = min(max((p - start) / weight, 0.0), 1.0)
            agg.update_partial(task_key, frac * 100.0)
        state = agg.get_state()
        return state.percentage, state.eta

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
            if visible < prev:
                # 进度不允许倒退（前端进度条单调），但消息必须透传：
                # 否则降级通知/阶段说明等低进度或同进度消息会被直接丢弃，
                # 前端停留在旧进度上以为任务卡死 —— 前后端数据断层。
                if message:
                    self._emit_message_event(task_id, stage, message)
                return
            if prev < 0 or terminal or visible - prev >= min_delta:
                self._last_progress[task_id] = visible
                self._emit_event(task_id, stage, raw, message)

    def _emit_message_event(self, task_id: str, stage: str, message: str) -> None:
        """仅透传消息的进度事件：进度沿用当前已入账值，不倒退、不重映射。

        用于进度被 ``_emit_smooth`` 钳制（前进度 > 新进度）但内容仍需
        到达前端的场景（并行降级、阶段切换说明等）。事件携带当前
        store 里的 progress/stage/eta，前端进度条保持单调，消息正常渲染。
        """
        state = self._store.get_task(task_id)
        current = float(self._last_progress.get(task_id, -1.0))
        if current < 0:
            current = float(getattr(state, "progress", 0.0) or 0.0)
        cur_stage = getattr(state, "stage", "") or stage
        cur_eta = float(getattr(state, "eta", 0.0) or 0.0)
        event = TaskProgressEvent(
            task_id=task_id,
            stage=cur_stage,
            progress=current,
            message=message,
            eta=cur_eta,
        )
        self._store.add_event(task_id, event)
        self._store.update_task(task_id, message=message)
        self._notify_event_listeners(event)

    def _emit_event(
        self, task_id: str, stage: str, progress: float,
        message: str = "", node_count: int = 0, diag_count: int = 0,
    ) -> None:
        # V1.24：真实任务走 ProgressAggregator —— 绝对百分比先映射到
        # 「阶段权重 × 阶段内完成度」的工作量模型，再经指数平滑输出。
        eta = 0.0
        if task_id in self._aggregators:
            progress, eta = self._map_stage_progress(task_id, stage, progress)
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
            message=message, eta=eta,
        )
        self._store.add_event(task_id, event)
        if batch is not None:
            self._store.update_task(
                task_id, stage=stage, progress=progress, eta=eta,
                total_progress=progress, file_progress=file_progress,
                message=message,
            )
        else:
            self._store.update_task(
                task_id, stage=stage, progress=progress, eta=eta, message=message,
            )
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

    # ── S2 后台清扫（内存上限） ──────────────────────────

    def _sweep_stale(self, now: float) -> int:
        """S2: prune terminal tasks older than the retention window.

        Also drops the per-task aggregator / batch / weight / progress maps
        whose owner task no longer exists -- these grew unboundedly with
        every finished job in long-running service processes.
        """
        removed = self._store.prune_terminated(self._retention_seconds, now)
        if removed:
            logger.info(
                "Pruned %d terminated task(s) older than %.0fs",
                removed, self._retention_seconds,
            )
        with self._batch_ctx_lock:
            for tid in list(self._batch_ctx):
                if self._store.get_task(tid) is None:
                    self._batch_ctx.pop(tid, None)
        with self._progress_lock:
            for tid in list(self._last_progress):
                if self._store.get_task(tid) is None:
                    self._last_progress.pop(tid, None)
        with self._aggregator_lock:
            for tid in list(self._aggregators):
                if self._store.get_task(tid) is None:
                    self._aggregators.pop(tid, None)
        with self._stage_weights_lock:
            for tid in list(self._task_stage_weights):
                if self._store.get_task(tid) is None:
                    self._task_stage_weights.pop(tid, None)
        return removed

    def _sweeper_loop(self) -> None:
        """Daemon background loop: bounded-memory cleanup of terminal tasks."""
        while True:
            time.sleep(self._sweep_interval)
            try:
                self._sweep_stale(time.time())
            except Exception:  # noqa: BLE001 -- the sweeper must never die
                logger.exception("task sweeper iteration failed")

    def _emit_notice(
        self,
        task_id: str,
        severity: str,
        title: str,
        detail: str = "",
        tip: str = "",
    ) -> None:
        """Emit a structured runtime notice (independent of the progress stream).

        Notices ride the same listener pipeline so the GUI bridge can forward
        them, but they never clamp against the monotonic progress channel (see
        the CPU-degrade case which produced a 0%-progress + message event and
        was previously swallowed by the smooth-progress guard).
        """
        event = RuntimeNoticeEvent(
            task_id=task_id,
            severity=severity,
            title=title,
            detail=detail,
            tip=tip,
            message=f"[{severity}] {title}",
        )
        self._store.add_event(task_id, event)
        self._notify_event_listeners(event)



__all__ = [
    "RuntimeService",
    "TranslationRequest",
    "TaskState",
    "TaskProgressEvent",
    "RuntimeNoticeEvent",
    "TaskStage",
    "ServiceConfig",
]

