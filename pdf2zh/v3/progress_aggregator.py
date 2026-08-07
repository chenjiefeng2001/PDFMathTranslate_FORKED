"""Module: ProgressAggregator — V1.24 Work-Graph 工作量模型与进度聚合。

设计原则（并发系统进度统计标准）：

1. **Progress 来自 Work Graph，而不是 Thread**
   所有线程只上报事件（task 生命周期/partial），进度由 Aggregator 按权重统一统计，
   不依赖执行顺序 —— 快页/慢页交错完成不会造成进度跳变。

2. **每个 Task 有 Weight**
   ``Paragraph=10, Formula=25, Table=30, Image/OCR=60`` 等单元权重：
   一个公式可能比 20 个 Paragraph 还慢，所以不能按 Task 计数，只能按权重。

3. **Task 生命周期**
   ``Created -> Queued -> Running -> Finished``（外加 Failed/Skipped/Cancelled）。
   Running 期间可上报 Partial（0-100），翻译/OCR 等长任务不再「突然完成」。

4. **ProgressAggregator 唯一统计入口**
   ``total_weight / finished_weight / running_weight / queued_weight /
   failed_weight / percentage / eta``，而非 ``finished_task`` 计数。

5. **Pipeline Progress（阶段权重）**
   每个阶段（Parser/Semantic/Translation/Layout/Render）注册自己的工作量估计
   函数（``register_pass("Translation").estimated_cost(fn)``），Scheduler、
   Aggregator 与 UI 共享同一套工作量模型。

6. **进度平滑（指数滤波）**
   ``display += (real - display) * alpha``（默认 0.08），UI 看到的是 1,2,3...
   而不是 1,25,26,70 的跳变。

7. **ETA 预测**
   按已完工权重的推进速率（最近窗口滑动平均）外推剩余权重所需时间。

用法::

    from pdf2zh.v3.progress_aggregator import (
        ProgressAggregator, ProgressState, TaskLifecycle,
        build_work_graph, register_pass, estimate_pass_weight,
        estimate_document_weight, UNIT_WEIGHTS,
    )

    agg = ProgressAggregator()
    for task in build_work_graph({"pages": 100, "paragraphs": 1500}):
        agg.add_task(task.task_id, task.weight, stage=task.stage)

    agg.mark_running("Translation:page3", partial=35.0)
    agg.finish("Translation:page3")
    state = agg.get_state()   # ProgressState(percentage, eta, ...)
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional, Sequence, Tuple

# ── 单元权重（第二原则）────────────────────────────────────────────────────────
#: 文档节点单元权重：一个公式的翻译成本 ≈ 25 个文本单元，OCR/图片最重。
UNIT_WEIGHTS: Dict[str, float] = {
    "paragraph": 10.0,
    "heading": 4.0,
    "formula": 25.0,
    "table": 30.0,
    "image": 60.0,
    "ocr": 80.0,
    "page": 2.0,
    "line": 0.5,
}

#: 平滑系数（第七原则）：``display += (real - display) * alpha``。
DEFAULT_SMOOTHING_ALPHA = 0.08

#: 平滑基准步长（秒）：alpha 按「距上次更新经过的时间」折算 ——
#: 高频并行事件（每 100ms 一次）走满 0.08 的慢速平滑，稀疏的阶段切换
#: 事件（间隔数秒）自动加速收敛，避免进度条长期卡在小数值上。
SMOOTHING_STEP_SECONDS = 0.1

#: ETA 估计使用的时间窗口（秒）。
DEFAULT_ETA_WINDOW = 60.0

#: 默认流水线 Pass（第六原则）：Parser -> Semantic -> Translation -> Layout -> Render。
#: 每个 Pass 注册自己的工作量估计函数（estimated_cost），输入文档节点计数。
DEFAULT_PIPELINE_PASSES: List[str] = [
    "Parser",
    "SemanticAnalysis",
    "Translation",
    "Layout",
    "Render",
]

#: 默认 Pass 成本模型（doc_counts -> 成本分）。
#: 与用户给定的示例一致：Translation 以 token/节点数为准，公式/表格加权。
_PASS_COST_FNS: Dict[str, Callable[[Dict[str, int]], float]] = {
    "Parser": lambda c: c.get("pages", 1) * 4.0,
    "SemanticAnalysis": (
        lambda c: c.get("paragraphs", 0) * 2.0
        + c.get("formulas", 0) * 8.0
        + c.get("tables", 0) * 12.0
    ),
    "Translation": (
        lambda c: c.get("paragraphs", 0) * UNIT_WEIGHTS["paragraph"]
        + c.get("formulas", 0) * UNIT_WEIGHTS["formula"]
        + c.get("tables", 0) * UNIT_WEIGHTS["table"]
        + c.get("headings", 0) * UNIT_WEIGHTS["heading"]
        + c.get("images", 0) * UNIT_WEIGHTS["image"]
    ),
    "Layout": (
        lambda c: c.get("lines", 0) * UNIT_WEIGHTS["line"]
        + c.get("pages", 1) * UNIT_WEIGHTS["page"]
    ),
    "Render": lambda c: c.get("pages", 1) * 3.0,
}


# ── Task 生命周期（第三原则）────────────────────────────────────────────────────


class TaskLifecycle(str, Enum):
    CREATED = "created"
    QUEUED = "queued"
    RUNNING = "running"
    FINISHED = "finished"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


_TERMINAL = frozenset(
    {
        TaskLifecycle.FINISHED,
        TaskLifecycle.FAILED,
        TaskLifecycle.SKIPPED,
        TaskLifecycle.CANCELLED,
    }
)


@dataclass
class ProgressState:
    """聚合后的进度快照（第四原则）。"""

    total_weight: float = 0.0
    finished_weight: float = 0.0
    running_weight: float = 0.0
    queued_weight: float = 0.0
    failed_weight: float = 0.0
    percentage: float = 0.0
    """平滑后百分比（0-100），供 UI 直接渲染。"""
    raw_percentage: float = 0.0
    """未平滑百分比（0-100），供日志/测试/统计使用。"""
    eta: float = 0.0
    """预计剩余秒数；0 表示无法估计（数据不足）。"""
    finished_tasks: int = 0
    total_tasks: int = 0
    active_stage: str = ""

    def to_dict(self) -> Dict[str, float]:
        return {
            "total_weight": self.total_weight,
            "finished_weight": self.finished_weight,
            "running_weight": self.running_weight,
            "queued_weight": self.queued_weight,
            "failed_weight": self.failed_weight,
            "percentage": self.percentage,
            "raw_percentage": self.raw_percentage,
            "eta": self.eta,
            "finished_tasks": self.finished_tasks,
            "total_tasks": self.total_tasks,
            "active_stage": self.active_stage,
        }


@dataclass
class _WorkItem:
    task_id: str
    weight: float
    stage: str = ""
    lifecycle: TaskLifecycle = TaskLifecycle.QUEUED
    partial: float = 0.0
    """Running 期间的局部完成度（0-1）。"""
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None

    @property
    def effective_finished_weight(self) -> float:
        """任务对 finished_weight 的贡献。

        - Finished/Skipped：全部权重计入；
        - Failed：全部权重计入（failed_weight 单列，percentage 视作已消耗）；
        - Running：按 partial 比例计入（第五原则：Partial Progress）；
        - 其余：0。
        """
        if self.lifecycle in (
            TaskLifecycle.FINISHED,
            TaskLifecycle.FAILED,
            TaskLifecycle.SKIPPED,
        ):
            return self.weight
        if self.lifecycle == TaskLifecycle.RUNNING:
            return self.weight * min(max(self.partial, 0.0), 1.0)
        return 0.0


# ── Work Graph（第一原则）──────────────────────────────────────────────────────


@dataclass
class WorkGraphTask:
    task_id: str
    weight: float
    stage: str = ""
    dependencies: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        return {
            "task_id": self.task_id,
            "weight": self.weight,
            "stage": self.stage,
            "dependencies": list(self.dependencies),
        }


@dataclass
class WorkGraph:
    """加权任务图：Document -> Pipeline -> Tasks（纯声明，无执行）。"""

    tasks: List[WorkGraphTask] = field(default_factory=list)

    def add(self, task: WorkGraphTask) -> "WorkGraph":
        self.tasks.append(task)
        return self

    @property
    def total_weight(self) -> float:
        return sum(t.weight for t in self.tasks)

    def stage_weights(self) -> Dict[str, float]:
        out: Dict[str, float] = {}
        for t in self.tasks:
            out[t.stage] = out.get(t.stage, 0.0) + t.weight
        return out

    def stage_order(self) -> List[str]:
        seen: List[str] = []
        for t in self.tasks:
            if t.stage and t.stage not in seen:
                seen.append(t.stage)
        return seen


def build_work_graph(
    doc_counts: Dict[str, int],
    passes: Optional[Sequence[str]] = None,
) -> WorkGraph:
    """按文档节点计数 + Pass 成本模型构建加权 Work Graph。

    - 每个 Pass 产生任务；Translation 等重 Pass 按页拆分（页数已知时），
      这样「100 页 = 100 个 Translate Task」的工作量粒度可供线程池消费；
    - 权重来自 ``estimate_pass_weight``（Pass 成本 / 拆分数）；
    - 依赖：第 n 页的任务依赖上一 Pass 第 n 页（Pipeline 语义）。

    Args:
        doc_counts: 文档节点计数（pages/paragraphs/formulas/tables/headings/
            lines/images），缺省字段按 0/1 回退。
        passes: 参与构建的 Pass 名列表（默认 ``DEFAULT_PIPELINE_PASSES``）。

    Returns:
        WorkGraph（tasks + total_weight）。
    """
    counts = dict(doc_counts or {})
    pages = int(counts.get("pages") or 0)
    graph = WorkGraph()
    passes = list(passes or DEFAULT_PIPELINE_PASSES)
    prev_page_deps: Dict[int, str] = {}
    for pass_idx, pass_name in enumerate(passes):
        cost = estimate_pass_weight(pass_name, counts)
        if cost <= 0:
            continue
        if pages > 0 and pass_idx > 0:
            per_page = cost / pages
            for p in range(1, pages + 1):
                tid = f"{pass_name}:page{p}"
                deps = [prev_page_deps[p]] if p in prev_page_deps else []
                graph.add(WorkGraphTask(tid, per_page, stage=pass_name, dependencies=deps))
                prev_page_deps[p] = tid
        else:
            tid = f"{pass_name}:doc"
            graph.add(WorkGraphTask(tid, cost, stage=pass_name))
    return graph


# ── Pass 成本注册表（最后原则：register_pass(...).estimated_cost(fn)）──────────


class PassCostRegistration:
    """``register_pass("Translation").estimated_cost(fn)`` 的流式注册对象。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self._cost_fn: Optional[Callable[[Dict[str, int]], float]] = None

    def estimated_cost(
        self, fn: Callable[[Dict[str, int]], float]
    ) -> "PassCostRegistration":
        """注册工作量估计函数：``fn(doc_counts) -> float``。"""
        self._cost_fn = fn
        return self

    def cost(self, doc_counts: Dict[str, int]) -> float:
        if self._cost_fn is None:
            return 0.0
        try:
            val = float(self._cost_fn(dict(doc_counts or {})))
        except Exception:
            return 0.0
        return val if val > 0 else 0.0


class PassCostRegistry:
    """Pass 工作量模型注册表。

    Scheduler / ProgressAggregator / UI 共享同一套成本模型 ——
    新增 OCR、图片翻译或新翻译后端时，只需为新 Pass 提供 estimated_cost，
    不需要重写进度统计逻辑。
    """

    def __init__(self, seed: Optional[Dict[str, Callable[[Dict[str, int]], float]]] = None) -> None:
        self._passes: Dict[str, PassCostRegistration] = {}
        if seed:
            for name, fn in seed.items():
                self.register(name).estimated_cost(fn)

    def register(self, name: str) -> PassCostRegistration:
        reg = self._passes.get(name)
        if reg is None:
            reg = PassCostRegistration(name)
            self._passes[name] = reg
        return reg

    def estimate(self, name: str, doc_counts: Dict[str, int]) -> float:
        reg = self._passes.get(name)
        return reg.cost(doc_counts) if reg is not None else 0.0

    def costs(self, doc_counts: Dict[str, int]) -> Dict[str, float]:
        return {name: reg.cost(doc_counts) for name, reg in self._passes.items()}

    @property
    def names(self) -> List[str]:
        return list(self._passes.keys())

    def clear(self) -> None:
        self._passes.clear()

    def restore_defaults(self) -> None:
        self.clear()
        for name, fn in _PASS_COST_FNS.items():
            self.register(name).estimated_cost(fn)


#: 进程级默认注册表（含默认 Pass 成本模型）。
default_pass_registry = PassCostRegistry(seed=dict(_PASS_COST_FNS))


def register_pass(name: str) -> PassCostRegistration:
    """注册（或取回）一个 Pass 的成本模型。

    Usage::

        register_pass("OCR").estimated_cost(
            lambda c: c.get("images", 0) * UNIT_WEIGHTS["ocr"]
        )
    """
    return default_pass_registry.register(name)


def estimate_pass_weight(pass_name: str, doc_counts: Dict[str, int]) -> float:
    """按注册的 Pass 成本模型估计某 Pass 的总权重。"""
    return default_pass_registry.estimate(pass_name, doc_counts)


def estimate_document_weight(
    doc_counts: Dict[str, int],
    unit_weights: Optional[Dict[str, float]] = None,
) -> float:
    """按单元权重直接估算整篇文档的工作量（不经过 Pass 模型）。

    计数键同时接受单数/复数形式（``image`` / ``images``）。
    """
    w = dict(unit_weights or UNIT_WEIGHTS)
    total = 0.0
    for key, weight in w.items():
        count = doc_counts.get(key)
        if count is None:
            count = doc_counts.get(key + "s")
        total += weight * float(count or 0)
    return total


# ── ProgressAggregator（第四/五/七/八原则）────────────────────────────────────


class ProgressAggregator:
    """加权进度聚合器：所有线程只上报事件，Aggregator 统一统计。

    线程安全；支持 Task 生命周期 + 局部进度 + 阶段聚合 + 指数平滑 + ETA。
    """

    def __init__(
        self,
        alpha: float = DEFAULT_SMOOTHING_ALPHA,
        eta_window: float = DEFAULT_ETA_WINDOW,
        now_fn: Optional[Callable[[], float]] = None,
    ) -> None:
        # RLock：查询方法会在持锁状态下再次调用 _finished_weight_raw 等
        # 内部统计，可重入锁避免聚合逻辑内部死锁。
        self._lock = threading.RLock()
        self._items: Dict[str, _WorkItem] = {}
        self._alpha = float(alpha)
        self._eta_window = float(eta_window)
        self._now = now_fn or time.time
        self._display: float = 0.0
        self._last_smooth_at: Optional[float] = None
        self._history: List[Tuple[float, float]] = []
        """(timestamp, cumulative finished_weight) 采样，用于 ETA 速率估计。"""
        self._total_weight: float = 0.0
        self._started_at: Optional[float] = None

    # ── 生命周期 API ────────────────────────────────────────────────────────

    def add_task(
        self,
        task_id: str,
        weight: float,
        stage: str = "",
        *,
        queued: bool = True,
    ) -> None:
        """注册一个任务（重复注册返回既有任务，不叠加权重）。"""
        weight = max(0.0, float(weight))
        with self._lock:
            if task_id in self._items:
                return
            self._items[task_id] = _WorkItem(
                task_id=task_id,
                weight=weight,
                stage=stage,
                lifecycle=TaskLifecycle.QUEUED if queued else TaskLifecycle.CREATED,
            )
            self._total_weight += weight

    def ensure_task(self, task_id: str, weight: float, stage: str = "") -> None:
        """幂等注册（已存在则忽略），供运行期按需补齐任务。"""
        self.add_task(task_id, weight, stage=stage)

    def mark_queued(self, task_id: str) -> None:
        self._transition(task_id, TaskLifecycle.QUEUED)

    def mark_running(self, task_id: str, partial: float = 0.0) -> None:
        """进入 Running；partial 为启动时的局部完成度（0-100）。"""
        with self._lock:
            item = self._items.get(task_id)
            if item is None:
                return
            item.lifecycle = TaskLifecycle.RUNNING
            if item.started_at is None:
                item.started_at = self._now()
            self._started_at = self._started_at or self._now()
            item.partial = min(max(partial, 0.0), 100.0) / 100.0

    def update_partial(self, task_id: str, percent: float) -> None:
        """上报局部完成度（0-100；第五原则：API 已发送 60%、收到返回 80%）。"""
        with self._lock:
            item = self._items.get(task_id)
            if item is None:
                return
            item.partial = min(max(percent, 0.0), 100.0) / 100.0
            if item.lifecycle == TaskLifecycle.QUEUED:
                item.lifecycle = TaskLifecycle.RUNNING
            if item.lifecycle == TaskLifecycle.RUNNING and item.started_at is None:
                item.started_at = self._now()
            self._started_at = self._started_at or self._now()
            self._record_sample_locked()

    def finish(self, task_id: str) -> None:
        """任务完成：全部权重计入 finished_weight。"""
        self._transition(task_id, TaskLifecycle.FINISHED)

    def fail(self, task_id: str) -> None:
        """任务失败：权重计入 failed_weight（percentage 视作已消耗）。"""
        self._transition(task_id, TaskLifecycle.FAILED)

    def skip(self, task_id: str) -> None:
        """任务跳过：权重计入 finished_weight（进度不卡死）。"""
        self._transition(task_id, TaskLifecycle.SKIPPED)

    def cancel(self, task_id: str) -> None:
        """任务取消：不计入任何完成量（remaining 保留）。"""
        self._transition(task_id, TaskLifecycle.CANCELLED)

    def _transition(self, task_id: str, new_state: TaskLifecycle) -> None:
        with self._lock:
            item = self._items.get(task_id)
            if item is None or item.lifecycle in _TERMINAL:
                return
            item.lifecycle = new_state
            if new_state in (TaskLifecycle.RUNNING,):
                item.started_at = item.started_at or self._now()
            if new_state in _TERMINAL:
                item.finished_at = self._now()
            if new_state in (
                TaskLifecycle.FINISHED,
                TaskLifecycle.FAILED,
                TaskLifecycle.SKIPPED,
            ):
                self._record_sample_locked()

    # ── 查询 API ─────────────────────────────────────────────────────────────

    @property
    def total_weight(self) -> float:
        with self._lock:
            return self._total_weight

    @property
    def remaining_weight(self) -> float:
        return max(0.0, self.total_weight - self._finished_weight_raw())

    @property
    def finished_weight(self) -> float:
        return self._finished_weight_raw()

    @property
    def running_weight(self) -> float:
        with self._lock:
            return sum(
                i.weight for i in self._items.values()
                if i.lifecycle == TaskLifecycle.RUNNING
            )

    @property
    def queued_weight(self) -> float:
        with self._lock:
            return sum(
                i.weight for i in self._items.values()
                if i.lifecycle in (TaskLifecycle.CREATED, TaskLifecycle.QUEUED)
            )

    @property
    def failed_weight(self) -> float:
        with self._lock:
            return sum(
                i.weight for i in self._items.values()
                if i.lifecycle == TaskLifecycle.FAILED
            )

    @property
    def raw_percentage(self) -> float:
        total = self.total_weight
        if total <= 0:
            return 0.0
        return min(100.0, self._finished_weight_raw() / total * 100.0)

    @property
    def percentage(self) -> float:
        """平滑后百分比（第七原则：指数滤波）。"""
        with self._lock:
            return self._smooth_locked()

    @property
    def eta(self) -> float:
        """预计剩余秒数；0 = 无法估计。"""
        with self._lock:
            self._prune_history_locked()
            if len(self._history) < 2:
                return 0.0
            (t0, w0), (t1, w1) = self._history[0], self._history[-1]
            dt = t1 - t0
            rate = (w1 - w0) / dt if dt > 0 else 0.0
            if rate <= 0:
                return 0.0
            remaining = max(0.0, self._total_weight - w1)
            return remaining / rate

    def get_state(self) -> ProgressState:
        """一次性快照：百分比（平滑）+ ETA + 各权重统计。"""
        with self._lock:
            total = self._total_weight
            done = self._finished_weight_raw()
            queued = sum(
                i.weight for i in self._items.values()
                if i.lifecycle in (TaskLifecycle.CREATED, TaskLifecycle.QUEUED)
            )
            running = sum(
                i.weight for i in self._items.values()
                if i.lifecycle == TaskLifecycle.RUNNING
            )
            failed = sum(
                i.weight for i in self._items.values()
                if i.lifecycle == TaskLifecycle.FAILED
            )
            raw = (done / total * 100.0) if total > 0 else 0.0
            display = self._smooth_locked()
            self._prune_history_locked()
            if len(self._history) >= 2:
                (t0, w0), (t1, w1) = self._history[0], self._history[-1]
                dt = t1 - t0
                rate = (w1 - w0) / dt if dt > 0 else 0.0
                eta = (max(0.0, total - w1) / rate) if rate > 0 else 0.0
            else:
                eta = 0.0
            active = self._active_stage_locked()
            finished_tasks = sum(
                1 for i in self._items.values()
                if i.lifecycle in (TaskLifecycle.FINISHED, TaskLifecycle.SKIPPED)
            )
            return ProgressState(
                total_weight=total,
                finished_weight=done,
                running_weight=running,
                queued_weight=queued,
                failed_weight=failed,
                percentage=display,
                raw_percentage=raw,
                eta=eta,
                finished_tasks=finished_tasks,
                total_tasks=len(self._items),
                active_stage=active,
            )

    def stage_breakdown(self) -> Dict[str, Dict[str, float]]:
        """按阶段聚合（第六原则：Pipeline Progress）。"""
        with self._lock:
            out: Dict[str, Dict[str, float]] = {}
            for item in self._items.values():
                entry = out.setdefault(
                    item.stage, {"total": 0.0, "done": 0.0, "active": 0.0}
                )
                entry["total"] += item.weight
                entry["done"] += item.effective_finished_weight
                if item.lifecycle == TaskLifecycle.RUNNING:
                    entry["active"] += item.weight
            return out

    def stage_progress(self, stage: str) -> float:
        """某阶段的完成度（0-1），供 StepBar 内部子进度渲染。"""
        bd = self.stage_breakdown().get(stage)
        if not bd or bd["total"] <= 0:
            return 0.0
        return min(1.0, bd["done"] / bd["total"])

    def reset(self) -> None:
        """清空全部任务与统计（批次处理每个文件之间调用）。"""
        with self._lock:
            self._items.clear()
            self._total_weight = 0.0
            self._display = 0.0
            self._last_smooth_at = None
            self._history.clear()
            self._started_at = None

    def task_state(self, task_id: str) -> Optional[Dict[str, object]]:
        """单个任务的运行时状态（生命周期/权重/局部进度）。"""
        with self._lock:
            item = self._items.get(task_id)
            if item is None:
                return None
            return {
                "task_id": item.task_id,
                "weight": item.weight,
                "stage": item.stage,
                "lifecycle": item.lifecycle.value,
                "partial": round(item.partial * 100.0, 1),
            }

    # ── 内部实现 ─────────────────────────────────────────────────────────────

    def _finished_weight_raw(self) -> float:
        with self._lock:
            return sum(i.effective_finished_weight for i in self._items.values())

    def _record_sample_locked(self) -> None:
        """在权重推进时记录 (ts, cumulative finished) 采样用于 ETA。"""
        total = self._finished_weight_raw()
        now = self._now()
        if self._history and self._history[-1][1] == total:
            return
        self._history.append((now, total))
        self._prune_history_locked()

    def _prune_history_locked(self) -> None:
        window = self._eta_window
        if not window or window <= 0 or len(self._history) < 2:
            return
        cutoff = self._now() - window
        self._history = [
            p for p in self._history if p[0] >= cutoff
        ]
        if len(self._history) == 1:
            self._history = []

    def _smooth_locked(self) -> float:
        """时间基指数平滑：``display += (real - display) * alpha_eff``。

        ``alpha_eff`` 按距上次更新的真实时间折算（基准步长 100ms）：
        高频并行回报（每步 100ms）用满 ``alpha`` 的慢速平滑 —— UI 看到
        1,2,3,...；稀疏的阶段切换事件（间隔数秒）自动加速收敛到真实值，
        避免进度条长时间停在低数值。显示值单调不减；全部权重完成时归 100。
        """
        total = self._total_weight
        real = (self._finished_weight_raw() / total * 100.0) if total > 0 else 0.0
        now = self._now()
        last = self._last_smooth_at
        self._last_smooth_at = now
        if real >= 100.0 and total > 0:
            self._display = 100.0
        elif real > self._display:
            if last is None:
                alpha_eff = self._alpha  # 首次更新按一个平滑步长起步
            else:
                dt = max(0.0, now - last)
                steps = dt / SMOOTHING_STEP_SECONDS
                alpha_eff = 1.0 - (1.0 - self._alpha) ** steps
            if alpha_eff > 0.0:
                self._display += (real - self._display) * min(alpha_eff, 1.0)
        return min(100.0, max(0.0, self._display))

    def _active_stage_locked(self) -> str:
        for item in self._items.values():
            if item.lifecycle == TaskLifecycle.RUNNING:
                return item.stage
        return ""


# ── Executor 桥接：TaskGraph(weights) -> ProgressAggregator ──────────────────


def bind_taskgraph(aggregator: ProgressAggregator, tasks: Sequence[object]) -> None:
    """把 ``scheduler.TaskGraph`` 的任务（含 weight 字段）注册进聚合器。

    配合 ``Executor(..., progress_cb=...)`` 使用：Executor 在每个生命周期
    转折点回调，本函数产生的回调把事件转发给 Aggregator。

    Usage::

        agg = ProgressAggregator()
        graph = TaskGraph()
        task = Task("parse", "Parse", weight=40.0)
        graph.add_task(task)
        executor = Executor(graph, progress_cb=make_progress_cb(agg))
        executor.run_all()
    """
    for task in tasks:
        weight = float(getattr(task, "weight", 1.0) or 1.0)
        stage = str(getattr(task, "module", "") or getattr(task, "name", "") or "")
        aggregator.add_task(
            getattr(task, "id", str(id(task))),
            weight,
            stage=stage,
        )


def make_progress_cb(aggregator: ProgressAggregator) -> Callable[[object, str, float], None]:
    """构造 Executor 进度回调：``(task, status, partial) -> aggregator 事件``。

    状态字面量：``running`` / ``finished`` / ``failed`` / ``skipped`` /
    ``partial``（partial 参数为 0-100）。
    """

    def _cb(task: object, status: str, partial: float = 0.0) -> None:
        tid = getattr(task, "id", str(id(task)))
        if status == "running":
            aggregator.mark_running(tid, partial=partial)
        elif status == "partial":
            aggregator.update_partial(tid, partial)
        elif status == "finished":
            aggregator.finish(tid)
        elif status == "failed":
            aggregator.fail(tid)
        elif status == "skipped":
            aggregator.skip(tid)

    return _cb


__all__ = [
    "TaskLifecycle",
    "ProgressState",
    "ProgressAggregator",
    "WorkGraphTask",
    "WorkGraph",
    "build_work_graph",
    "PassCostRegistration",
    "PassCostRegistry",
    "default_pass_registry",
    "register_pass",
    "estimate_pass_weight",
    "estimate_document_weight",
    "UNIT_WEIGHTS",
    "DEFAULT_SMOOTHING_ALPHA",
    "DEFAULT_ETA_WINDOW",
    "DEFAULT_PIPELINE_PASSES",
    "bind_taskgraph",
    "make_progress_cb",
]
