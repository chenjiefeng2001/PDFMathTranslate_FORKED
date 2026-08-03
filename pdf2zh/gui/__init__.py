"""pdf2zh GUI — Modular Gradio Web UI.

Replaces the 909-line gui.py God Object with a clean MVC-like structure:

    app.py         — Entry point, Gradio Block layout
    state.py       — TaskState, global store, thread-safe state mgmt
    logger.py      — Thread-aware log handler (replaces _ThreadAwareLogHandler)
    worker.py      — Background translation via RuntimeService
    components/    — UI panels
      ├── upload_panel.py
      ├── config_panel.py
      ├── progress_panel.py
      ├── preview_panel.py
      └── diagnostic_panel.py   (NEW — V4 Diagnostic & Quality panel)

Usage:
    python -m pdf2zh.gui.app
      or
    from pdf2zh.gui.app import create_gui; create_gui().launch()
"""

from pdf2zh.gui.state import TaskState, GLOBAL_TASK_STORE, MAX_CONCURRENCY
from pdf2zh.gui.logger import ThreadAwareLogHandler, ThreadAwareStderr
from pdf2zh.gui.worker import background_translation_worker, submit_translation_task
from pdf2zh.gui.entry import setup_gui
from pdf2zh.gui.events import (
    EVENT_BUS,
    EventBus,
    TaskEvent,
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
from pdf2zh.gui.event_bridge import TaskEventBridge, EVENT_BRIDGE


__all__ = [
    "TaskState",
    "GLOBAL_TASK_STORE",
    "MAX_CONCURRENCY",
    "ThreadAwareLogHandler",
    "ThreadAwareStderr",
    "background_translation_worker",
    "submit_translation_task",
    "setup_gui",
    "EVENT_BUS",
    "EventBus",
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
    "TaskEventBridge",
    "EVENT_BRIDGE",
]
