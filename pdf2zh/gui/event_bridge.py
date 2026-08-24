"""RuntimeService -> EventBus adapter (the "Worker publishes events" half).

The RuntimeService runs translation in a background thread and emits low-level
``TaskProgressEvent`` records. This bridge subscribes to that stream and
translates it into the typed domain events the GUI (and any other consumer)
subscribes to. Neither the service nor the UI knows about each other -- the
bridge is the only coupling point (Dependency Inversion).

This decouples the business logic from the UI transport: the worker only ever
``publish(es)``, the ``TaskStore`` only saves state, and the UI only consumes
events (pushed via SSE, see ``notifier.py``).
"""

from __future__ import annotations

import logging
import threading
from typing import Dict, Optional

from pdf2zh.services.runtime_service import (
    RuntimeNoticeEvent,
    RuntimeService,
    TaskProgressEvent,
    TaskStage,
)
from pdf2zh.gui.events import (
    EVENT_BUS,
    DiagnosticsUpdated,
    EventBus,
    FileGenerated,
    NoticeEmitted,
    PreviewReady,
    TaskCancelled,
    TaskFailed,
    TaskFinished,
    TaskMessageChanged,
    TaskProgressChanged,
    TaskStageChanged,
    TaskStarted,
)

logger = logging.getLogger(__name__)


class TaskEventBridge:
    """Translate RuntimeService task events into domain events on the bus."""

    #: terminal pipeline stages (no further progress is expected after these)
    _TERMINAL_STAGES = frozenset(
        {
            TaskStage.COMPLETED.value,
            TaskStage.CANCELLED.value,
            TaskStage.FAILED.value,
        }
    )

    def __init__(
        self,
        bus: Optional[EventBus] = None,
        service: Optional[RuntimeService] = None,
    ) -> None:
        self._bus = bus or EVENT_BUS
        self._service = service
        self._listening = False
        self._lock = threading.Lock()
        self._last_stage: Dict[str, str] = {}
        self._started: set = set()
        #: Per-task last-published node overview (avoids duplicate publishes).
        self._last_overview: Dict[str, Optional[Dict]] = {}

    @property
    def service(self) -> RuntimeService:
        """Resolve the RuntimeService lazily (avoids import-time singletons)."""
        if self._service is None:
            from pdf2zh.gui.worker import get_runtime_service

            self._service = get_runtime_service()
        return self._service

    @property
    def listening(self) -> bool:
        return self._listening

    @property
    def last_stage(self) -> Dict[str, str]:
        """Per-task last-seen pipeline stage (diagnostics / tests)."""
        return dict(self._last_stage)

    def start(self) -> None:
        """Attach to the RuntimeService event stream (idempotent)."""
        with self._lock:
            if self._listening:
                return
            self.service.add_event_listener(
                self._on_event_record
            )  # 统一分流 progress/notice
            self._listening = True
            logger.info("TaskEventBridge attached to RuntimeService event stream")

    def stop(self) -> None:
        """Detach from the RuntimeService event stream (idempotent)."""
        with self._lock:
            if not self._listening:
                return
            try:
                self.service.remove_event_listener(self._on_event_record)
            except Exception:
                logger.exception("Failed to detach TaskEventBridge")
            self._listening = False
            logger.info("TaskEventBridge detached from RuntimeService")

    # ── worker side: one low-level record -> N domain events ────────────────

    def _on_event_record(self, event: Any) -> None:
        """Dispatch a runtime record: notices to a dedicated channel, the
        remaining low-level records to the domain-event translation."""
        if isinstance(event, RuntimeNoticeEvent):
            self._on_notice_event(event)
        else:
            self._on_progress_event(event)

    def _on_notice_event(self, event: RuntimeNoticeEvent) -> None:
        """Translate a structured runtime notice into the ``NoticeEmitted``
        domain event (severity/title/detail/tip), independent of progress."""
        try:
            self._bus.publish(
                NoticeEmitted(
                    task_id=event.task_id,
                    severity=event.severity,
                    title=event.title,
                    detail=event.detail,
                    tip=event.tip,
                    message=event.message,
                )
            )
        except Exception:
            logger.exception(
                "TaskEventBridge failed to publish notice for task %s",
                getattr(event, "task_id", "?"),
            )

    def _on_progress_event(self, event: TaskProgressEvent) -> None:
        """Translate a low-level progress record into domain events."""
        try:
            tid = event.task_id
            stage = event.stage
            prev_stage = self._last_stage.get(tid)

            if tid not in self._started and stage != TaskStage.PENDING.value:
                self._started.add(tid)
                self._bus.publish(TaskStarted(task_id=tid))

            if stage != prev_stage:
                self._bus.publish(
                    TaskStageChanged(
                        task_id=tid,
                        stage=stage,
                        prev_stage=prev_stage or "",
                        progress=event.progress,
                    )
                )
                self._last_stage[tid] = stage

            self._bus.publish(
                TaskProgressChanged(
                    task_id=tid,
                    progress=event.progress,
                    stage=stage,
                    message=event.message,
                    eta=event.eta,
                )
            )
            if event.message:
                self._bus.publish(
                    TaskMessageChanged(task_id=tid, message=event.message)
                )

            if stage == TaskStage.COMPLETED.value:
                self._bus.publish(TaskFinished(task_id=tid))
                self._publish_outputs(tid)
            elif stage == TaskStage.FAILED.value:
                self._bus.publish(TaskFailed(task_id=tid, message=event.message))
            elif stage == TaskStage.CANCELLED.value:
                self._bus.publish(TaskCancelled(task_id=tid, message=event.message))
            self._publish_live_diagnostics(tid)
        except Exception:
            logger.exception(
                "TaskEventBridge failed to translate event for task %s",
                getattr(event, "task_id", "?"),
            )

    def _publish_live_diagnostics(self, task_id: str) -> None:
        """Publish DiagnosticsUpdated as soon as an overview becomes available.

        The runtime attaches ``node_overview`` (and, later, quality scores)
        to the task state mid-run; this forwards them immediately so the
        document-intelligence panel updates live instead of only at the end.
        """
        state = self.service.get_task_state(task_id)
        if state is None:
            return
        overview = (
            dict(state.node_overview)
            if isinstance(state.node_overview, dict) and state.node_overview
            else None
        )
        if overview is None:
            return
        if self._last_overview.get(task_id) == overview:
            return
        self._last_overview[task_id] = overview
        self._bus.publish(
            DiagnosticsUpdated(
                task_id=task_id,
                diagnostic_summary=state.diagnostic_summary or "",
                quality_scores=dict(state.quality_scores or {}),
                node_overview=overview,
                diagnostic_report=getattr(state, "diagnostic_report", None),
                heal_status=getattr(state, "heal_status", None),
                repair_records=getattr(state, "repair_records", None),
                confidence_stats=getattr(state, "confidence_stats", None),
            )
        )

    def _publish_outputs(self, task_id: str) -> None:
        """Snapshot the terminal task state once to announce the outputs.

        The RuntimeService applies ``result_files`` / ``preview_path`` to the
        store *before* emitting the COMPLETED event, so a single read here is
        enough -- no polling required.
        """
        state = self.service.get_task_state(task_id)
        if state is None:
            return
        if state.result_files:
            self._bus.publish(
                FileGenerated(
                    task_id=task_id,
                    files=list(state.result_files),
                    zip_path=state.result_zip or "",
                )
            )
        if state.preview_path:
            self._bus.publish(
                PreviewReady(task_id=task_id, preview_path=state.preview_path)
            )
        if state.diagnostic_summary or state.quality_scores:
            self._bus.publish(
                DiagnosticsUpdated(
                    task_id=task_id,
                    diagnostic_summary=state.diagnostic_summary or "",
                    quality_scores=dict(state.quality_scores or {}),
                    node_overview=(
                        dict(state.node_overview)
                        if isinstance(state.node_overview, dict)
                        else None
                    ),
                    diagnostic_report=getattr(state, "diagnostic_report", None),
                    heal_status=getattr(state, "heal_status", None),
                    repair_records=getattr(state, "repair_records", None),
                    confidence_stats=getattr(state, "confidence_stats", None),
                )
            )


#: Application-wide singleton bridge.
EVENT_BRIDGE = TaskEventBridge()


__all__ = ["TaskEventBridge", "EVENT_BRIDGE"]
