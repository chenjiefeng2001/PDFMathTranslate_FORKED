"""Typed domain events + thread-safe EventBus for the pdf2zh GUI (Stage 2).

Implements the "Worker publishes events -> EventBus -> UI consumes events"
architecture from the pdf2zh-next roadmap. The UI no longer re-reads the whole
``TaskState`` on every sync tick; instead the worker publishes *changes* and
the UI applies targeted (delta) updates.

Design principles (from the roadmap):

  * ``TaskState`` is *data*    -- it only describes *where* a task is.
  * ``TaskEvent`` is *change*  -- it only describes *what* changed.
  * The ``EventBus`` is the single integration point between the Worker
    (producer) and the UI / any other consumer.
  * Producers never know who is listening (Observer pattern / Dependency
    Inversion), so the same backend can drive the SSE transport (see
    ``notifier.py``) or a React/Vue frontend without changing the worker.

The UI transport pulls events with ``EventBus.events_since(task_id, last_seq)``
to fetch only NEW events and re-renders only the affected components (delta
update); it is woken by server push (SSE), never by a polling timer.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import (
    Any,
    Callable,
    Deque,
    Dict,
    List,
    Optional,
    Sequence,
    Set,
    Tuple,
)

logger = logging.getLogger(__name__)


# ── Domain events ────────────────────────────────────────────────────────────


@dataclass
class TaskEvent:
    """Base class for every task domain event.

    ``sequence`` is assigned by the EventBus at publish time and provides
    consumers with a stable, monotonically increasing delta cursor per task.
    """

    task_id: str
    timestamp: float = field(default_factory=time.time)
    sequence: int = -1

    @property
    def event_type(self) -> str:
        """Stable event discriminator (the concrete class name)."""
        return type(self).__name__

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a plain dict (includes inherited fields + type tag)."""
        data = asdict(self)
        data["event_type"] = self.event_type
        return data


@dataclass
class TaskStarted(TaskEvent):
    """A task has begun executing in the background worker."""


@dataclass
class TaskStageChanged(TaskEvent):
    """The task moved from one pipeline stage to another."""

    stage: str = ""
    prev_stage: str = ""
    progress: float = 0.0


@dataclass
class TaskProgressChanged(TaskEvent):
    """Incremental progress update (0..100)."""

    progress: float = 0.0
    stage: str = ""
    message: str = ""
    eta: float = 0.0
    """预计剩余秒数（0 = 未知）。"""


@dataclass
class TaskMessageChanged(TaskEvent):
    """Human-readable status message changed."""

    message: str = ""


@dataclass
class TaskPaused(TaskEvent):
    """Task was paused by the user."""


@dataclass
class TaskResumed(TaskEvent):
    """A paused task was resumed."""


@dataclass
class TaskSkipped(TaskEvent):
    """The current file was skipped by the user."""


@dataclass
class TaskCancelled(TaskEvent):
    """Task was cancelled (terminal state)."""

    message: str = ""


@dataclass
class TaskFailed(TaskEvent):
    """Task failed (terminal state)."""

    message: str = ""


@dataclass
class TaskFinished(TaskEvent):
    """Task completed successfully (terminal state)."""


@dataclass
class FileGenerated(TaskEvent):
    """Output files became available (result_files / zip)."""

    files: List[Dict[str, str]] = field(default_factory=list)
    zip_path: str = ""


@dataclass
class PreviewReady(TaskEvent):
    """A previewable output document became available."""

    preview_path: str = ""


@dataclass
class DiagnosticsUpdated(TaskEvent):
    """V4 diagnostics / quality scores were refreshed."""

    diagnostic_summary: str = ""
    quality_scores: Dict[str, float] = field(default_factory=dict)
    node_overview: Optional[Dict[str, int]] = None
    diagnostic_report: Optional[Dict[str, Any]] = None
    """结构化诊断报告（legacy: errors/warnings/admissible/issues；V4: evaluator 记录）。"""
    heal_status: Optional[Dict[str, Any]] = None
    """自愈行程摘要（ran/iterations/before_errors/after_errors/improved）。"""
    repair_records: Optional[List[Dict[str, Any]]] = None
    """自愈处置记录（issue -> 策略/状态）。"""
    confidence_stats: Optional[Dict[str, float]] = None
    """文档置信度统计（annotated/avg/min/max）。"""


#: Concrete domain event classes, in definition order.
ALL_EVENT_TYPES: Tuple[type, ...] = (
    TaskStarted,
    TaskStageChanged,
    TaskProgressChanged,
    TaskMessageChanged,
    TaskPaused,
    TaskResumed,
    TaskSkipped,
    TaskCancelled,
    TaskFailed,
    TaskFinished,
    FileGenerated,
    PreviewReady,
    DiagnosticsUpdated,
)

# ── EventBus ─────────────────────────────────────────────────────────────────


class _Subscription:
    """Internal subscription record with type/task filters."""

    __slots__ = ("sub_id", "handler", "event_types", "task_id")

    def __init__(
        self,
        sub_id: int,
        handler: Callable[[TaskEvent], None],
        event_types: Optional[Set[str]],
        task_id: Optional[str],
    ) -> None:
        self.sub_id = sub_id
        self.handler = handler
        self.event_types = event_types
        self.task_id = task_id

    def matches(self, event: TaskEvent) -> bool:
        if self.task_id is not None and event.task_id != self.task_id:
            return False
        if self.event_types is not None and event.event_type not in self.event_types:
            return False
        return True


class EventBus:
    """Thread-safe publish/subscribe bus with per-task delta cursors.

    * ``publish`` assigns a global monotonic sequence to every event and
      appends it to the task's history (a bounded ``deque``).
    * Subscribers may filter by event type and/or task id.
    * ``events_since(task_id, last_seq)`` gives consumers a cheap, idempotent
      delta cursor -- the exact primitive the SSE wake transport needs.
    """

    def __init__(self, max_history_per_task: int = 500) -> None:
        self._lock = threading.RLock()
        self._seq_counter = 0
        self._next_sub_id = 1
        self._subscriptions: Dict[int, _Subscription] = {}
        self._history: Dict[str, Deque[TaskEvent]] = {}
        self._last_seq: Dict[str, int] = {}
        self._max_history = max_history_per_task

    # ── publish side (Worker) ────────────────────────────────────────────────

    def publish(self, event: TaskEvent) -> TaskEvent:
        """Publish an event: assign sequence, append history, notify subscribers.

        Subscribers are notified *outside* the internal lock so a subscriber
        is free to publish (no re-entrant deadlock) without breaking ordering.
        """
        with self._lock:
            self._seq_counter += 1
            event.sequence = self._seq_counter
            history = self._history.setdefault(
                event.task_id, deque(maxlen=self._max_history)
            )
            history.append(event)
            self._last_seq[event.task_id] = event.sequence
            subs = list(self._subscriptions.values())
        for sub in subs:
            if sub.matches(event):
                try:
                    sub.handler(event)
                except Exception:
                    logger.exception("Event subscriber error for %s", event.event_type)
        return event

    # ── subscribe side (UI / any consumer) ──────────────────────────────────

    def subscribe(
        self,
        handler: Callable[[TaskEvent], None],
        event_types: Optional[Sequence[str]] = None,
        task_id: Optional[str] = None,
    ) -> int:
        """Register ``handler``; returns a subscription id for ``unsubscribe``.

        Args:
            handler: callable invoked synchronously on matching publishes.
            event_types: optional iterable of event type names to filter by
                (e.g. ``("TaskProgressChanged", "TaskStageChanged")``).
            task_id: optional task id to filter by.
        """
        types: Optional[Set[str]] = (
            set(event_types) if event_types is not None else None
        )
        with self._lock:
            sub_id = self._next_sub_id
            self._next_sub_id += 1
            self._subscriptions[sub_id] = _Subscription(sub_id, handler, types, task_id)
            return sub_id

    def unsubscribe(self, sub_id: int) -> bool:
        """Remove a subscription; returns True if it existed."""
        with self._lock:
            return self._subscriptions.pop(sub_id, None) is not None

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscriptions)

    # ── delta cursors (UI) ───────────────────────────────────────────────────

    def last_sequence(self, task_id: str) -> int:
        """Highest sequence published for ``task_id`` so far (0 if none)."""
        with self._lock:
            return self._last_seq.get(task_id, 0)

    def events_since(self, task_id: str, last_sequence: int) -> List[TaskEvent]:
        """Events for ``task_id`` with ``sequence > last_sequence``, in order."""
        with self._lock:
            history = self._history.get(task_id)
            if not history:
                return []
            return [e for e in history if e.sequence > last_sequence]

    def has_events(self, task_id: str, last_sequence: int) -> bool:
        """True when at least one event is available past the given cursor."""
        with self._lock:
            return self._last_seq.get(task_id, 0) > last_sequence

    def replay(self, task_id: str, limit: int = 0) -> List[TaskEvent]:
        """Return the full event history for ``task_id`` (chronological order).

        Args:
            limit: optional maximum number of (most recent) events to return.
        """
        with self._lock:
            history = self._history.get(task_id)
            if not history:
                return []
            items = list(history)
            if limit and limit > 0:
                return items[-limit:]
            return items

    # ── lifecycle / tests ────────────────────────────────────────────────────

    def clear(self, task_id: Optional[str] = None) -> None:
        """Drop event history for a task (or for all tasks when ``None``)."""
        with self._lock:
            if task_id is None:
                self._history.clear()
                self._last_seq.clear()
            else:
                self._history.pop(task_id, None)
                self._last_seq.pop(task_id, None)

    def reset(self) -> None:
        """Reset the whole bus (history, sequences and subscriptions)."""
        with self._lock:
            self._seq_counter = 0
            self._next_sub_id = 1
            self._subscriptions.clear()
            self._history.clear()
            self._last_seq.clear()


#: Application-wide singleton bus shared by the Worker bridge and the UI.
EVENT_BUS = EventBus()


__all__ = [
    "TaskEvent",
    "TaskStarted",
    "TaskStageChanged",
    "TaskProgressChanged",
    "TaskMessageChanged",
    "TaskPaused",
    "TaskResumed",
    "TaskSkipped",
    "TaskCancelled",
    "TaskFailed",
    "TaskFinished",
    "FileGenerated",
    "PreviewReady",
    "DiagnosticsUpdated",
    "ALL_EVENT_TYPES",
    "EventBus",
    "EVENT_BUS",
]
