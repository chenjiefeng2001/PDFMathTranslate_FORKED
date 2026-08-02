"""Headless tests for pdf2zh.gui modules.

Tests the state management, logging, and worker modules that replace
the Legacy 909-line gui.py God Object.
"""

from __future__ import annotations

import os
import sys
import time
import threading
import logging

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from pdf2zh.gui.state import (
    TaskState,
    GLOBAL_TASK_STORE,
    MAX_CONCURRENCY,
)
from pdf2zh.gui.logger import (
    ThreadAwareLogHandler,
    ThreadAwareStderr,
    get_handler,
    get_stderr_capture,
)


# ═══════════════════════════════════════════════════════════════════════════════
# State Management Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestTaskState:
    def test_defaults(self):
        s = TaskState(task_id="t1")
        assert s.task_id == "t1"
        assert s.status == "idle"
        assert s.progress == 0.0
        assert s.file_list == []
        assert s.result_files == []
        assert s.cancelled is not None
        assert s.paused is not None
        assert s.skip is not None

    def test_with_values(self):
        s = TaskState(
            task_id="t1", status="running", progress=50.0,
            file_list=["doc.pdf"],
            result_files=[{"name": "out.pdf", "path": "/tmp/out.pdf"}],
        )
        assert s.progress == 50.0
        assert len(s.result_files) == 1

    def test_to_dict(self):
        s = TaskState(task_id="t1", status="running")
        d = s.to_dict()
        assert d["task_id"] == "t1"
        assert d["status"] == "running"
        # threading.Event objects should not be in dict
        assert "cancelled" not in d
        assert "paused" not in d
        assert "skip" not in d


class TestGlobalTaskStore:
    def test_set_and_get(self):
        GLOBAL_TASK_STORE.set("t1", TaskState(task_id="t1"))
        state = GLOBAL_TASK_STORE.get("t1")
        assert state is not None
        assert state.task_id == "t1"

    def test_get_nonexistent(self):
        assert GLOBAL_TASK_STORE.get("nonexistent") is None

    def test_update(self):
        GLOBAL_TASK_STORE.set("t1", TaskState(task_id="t1"))
        GLOBAL_TASK_STORE.update("t1", progress=75.0, status="running")
        state = GLOBAL_TASK_STORE.get("t1")
        assert state is not None
        assert state.progress == 75.0
        assert state.status == "running"

    def test_update_nonexistent(self):
        assert GLOBAL_TASK_STORE.update("nonexistent", status="done") is None

    def test_remove(self):
        GLOBAL_TASK_STORE.set("t1", TaskState(task_id="t1"))
        GLOBAL_TASK_STORE.remove("t1")
        assert GLOBAL_TASK_STORE.get("t1") is None

    def test_list_tasks(self):
        GLOBAL_TASK_STORE.set("t1", TaskState(task_id="t1"))
        GLOBAL_TASK_STORE.set("t2", TaskState(task_id="t2"))
        tasks = GLOBAL_TASK_STORE.list_tasks()
        assert "t1" in tasks
        assert "t2" in tasks

    def test_queue_operations(self):
        GLOBAL_TASK_STORE.queue_push("t1")
        GLOBAL_TASK_STORE.queue_push("t2")
        assert GLOBAL_TASK_STORE.queue_position("t1") == 0
        assert GLOBAL_TASK_STORE.queue_position("t2") == 1
        GLOBAL_TASK_STORE.queue_remove("t1")
        GLOBAL_TASK_STORE.queue_clear()
        assert GLOBAL_TASK_STORE.queue_position("t2") == -1

    def test_thread_safety(self):
        errors: list[Exception] = []
        def worker(i: int):
            try:
                tid = f"t{i}"
                GLOBAL_TASK_STORE.set(tid, TaskState(task_id=tid))
                for _ in range(20):
                    GLOBAL_TASK_STORE.update(tid, progress=50.0)
                    GLOBAL_TASK_STORE.queue_push(tid)
                GLOBAL_TASK_STORE.remove(tid)
            except Exception as ex:
                errors.append(ex)
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads: t.start()
        for t in threads: t.join()
        assert not errors

    def test_max_concurrency(self):
        assert MAX_CONCURRENCY == 4


# ═══════════════════════════════════════════════════════════════════════════════
# Logger Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestThreadAwareLogHandler:
    def test_register_and_get_queue(self):
        handler = ThreadAwareLogHandler()
        tid = handler.register_thread()
        q = handler.get_queue(tid)
        assert q is not None
        assert q.empty()

    def test_unregister_thread(self):
        handler = ThreadAwareLogHandler()
        tid = handler.register_thread()
        q = handler.get_queue(tid)
        assert q is not None
        handler.unregister_thread(tid)
        # After unregister, get_queue auto-creates a new queue
        assert q is not handler.get_queue(tid)

    def test_emit_progress_message(self):
        handler = ThreadAwareLogHandler()
        tid = handler.register_thread()
        record = logging.LogRecord(
            name="test", level=logging.INFO,
            pathname="", lineno=0, msg="Progress: 50%", args=None, exc_info=None,
        )
        handler.emit(record)
        q = handler.get_queue(tid)
        assert q is not None
        msgs = []
        while not q.empty():
            msgs.append(q.get_nowait())
        progress_msgs = [m for m in msgs if "Progress:" in m]
        assert len(progress_msgs) >= 1

    def test_emit_non_progress(self):
        handler = ThreadAwareLogHandler()
        tid = handler.register_thread()
        record = logging.LogRecord(
            name="test", level=logging.INFO,
            pathname="", lineno=0, msg="General log", args=None, exc_info=None,
        )
        handler.emit(record)
        q = handler.get_queue(tid)
        assert q is not None
        assert q.empty()

    def test_get_handler_singleton(self):
        h1 = get_handler()
        h2 = get_handler()
        assert h1 is h2

    def test_stderr_write(self):
        stderr = get_stderr_capture()
        assert stderr is not None
        stderr.write("test\n")
        stderr.flush()


# ═══════════════════════════════════════════════════════════════════════════════
# Import Resolution Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestImportResolution:
    def test_services_importable(self):
        from pdf2zh.services import RuntimeService, TranslationRequest, TaskState
        assert RuntimeService is not None

    def test_gui_submodules_importable(self):
        from pdf2zh.gui.state import GLOBAL_TASK_STORE
        from pdf2zh.gui.logger import ThreadAwareLogHandler
        from pdf2zh.gui.worker import get_runtime_service
        from pdf2zh.gui.components.upload_panel import create_upload_panel
        from pdf2zh.gui.components.config_panel import create_config_panel
        from pdf2zh.gui.components.progress_panel import create_progress_panel
        from pdf2zh.gui.components.preview_panel import create_preview_panel
        from pdf2zh.gui.components.diagnostic_panel import create_diagnostic_panel
        assert GLOBAL_TASK_STORE is not None
        assert create_upload_panel is not None

    def test_gui_app_importable(self):
        from pdf2zh.gui.app import create_gui
        assert create_gui is not None

        GLOBAL_TASK_STORE.set("t1", TaskState(task_id="t1"))
        GLOBAL_TASK_STORE.update("t1", progress=75.0, status="running")
        state = GLOBAL_TASK_STORE.get("t1")
        assert state is not None
        assert state.progress == 75.0
        assert state.status == "running"

    def test_update_nonexistent(self):
        assert GLOBAL_TASK_STORE.update("nonexistent", status="done") is None

    def test_remove(self):
        GLOBAL_TASK_STORE.set("t1", TaskState(task_id="t1"))
        GLOBAL_TASK_STORE.remove("t1")
        assert GLOBAL_TASK_STORE.get("t1") is None
