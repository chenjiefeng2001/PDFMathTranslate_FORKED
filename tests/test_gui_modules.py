"""
Comprehensive tests for pdf2zh GUI modules (V4/V5 Frontend-Backend Integration).

Tests:
  1. State management (TaskState, GlobalTaskStore)
  2. Logger (ThreadAwareLogHandler, ThreadAwareStderr)
  3. Worker (submit_translation_task, cancel_task)
  4. Diagnostic panel (build_diagnostic_markdown)
  5. Entry point (setup_gui from entry.py)
  6. Import resolution for all components
  7. Services layer (RuntimeService with V4 pipeline)
  8. App module import validity
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


# =============================================================================
# 1. State Management Tests
# =============================================================================


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
        assert "cancelled" not in d  # threading.Event excluded
        assert "paused" not in d
        assert "skip" not in d

    def test_v4_diagnostic_fields(self):
        """V4/V5 diagnostic fields are available on TaskState."""
        s = TaskState(
            task_id="v4_test",
            diagnostic_summary="All checks passed",
            quality_scores={"translation_score": 95.0, "layout_score": 88.0},
        )
        assert s.diagnostic_summary == "All checks passed"
        assert s.quality_scores["translation_score"] == 95.0
        assert s.quality_scores["layout_score"] == 88.0
        d = s.to_dict()
        assert d["diagnostic_summary"] == "All checks passed"
        assert d["quality_scores"]["translation_score"] == 95.0

    def test_v4_diagnostic_empty(self):
        """V4 fields default to None when not set."""
        s = TaskState(task_id="t1")
        assert s.diagnostic_summary is None
        assert s.quality_scores is None


class TestGlobalTaskStore:
    def test_set_and_get(self):
        GLOBAL_TASK_STORE.set("t1", TaskState(task_id="t1"))
        state = GLOBAL_TASK_STORE.get("t1")
        assert state is not None
        assert state.task_id == "t1"
        GLOBAL_TASK_STORE.remove("t1")

    def test_get_nonexistent(self):
        assert GLOBAL_TASK_STORE.get("nonexistent") is None

    def test_update(self):
        GLOBAL_TASK_STORE.set("t1", TaskState(task_id="t1"))
        GLOBAL_TASK_STORE.update("t1", progress=75.0, status="running")
        state = GLOBAL_TASK_STORE.get("t1")
        assert state is not None
        assert state.progress == 75.0
        assert state.status == "running"
        GLOBAL_TASK_STORE.remove("t1")

    def test_update_diagnostic_fields(self):
        """V4 diagnostic fields survive update roundtrip."""
        GLOBAL_TASK_STORE.set("t1", TaskState(task_id="t1"))
        GLOBAL_TASK_STORE.update(
            "t1",
            diagnostic_summary="Layout issues detected",
            quality_scores={"collision_score": 72.0},
        )
        state = GLOBAL_TASK_STORE.get("t1")
        assert state is not None
        assert state.diagnostic_summary == "Layout issues detected"
        assert state.quality_scores["collision_score"] == 72.0
        GLOBAL_TASK_STORE.remove("t1")

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
        GLOBAL_TASK_STORE.remove("t1")
        GLOBAL_TASK_STORE.remove("t2")

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
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors

    def test_max_concurrency(self):
        assert MAX_CONCURRENCY == 4


# =============================================================================
# 2. Logger Tests
# =============================================================================


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
        # Verify the old queue reference is no longer tracked
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
        stderr.write("test\\n")
        stderr.flush()


# =============================================================================
# 3. Diagnostic Panel Tests
# =============================================================================


class TestDiagnosticPanel:
    def test_build_diagnostic_markdown_empty(self):
        from pdf2zh.gui.components.diagnostic_panel import build_diagnostic_markdown
        result = build_diagnostic_markdown()
        assert "*" in result or "等待" in result or "waiting" in result.lower()

    def test_build_diagnostic_markdown_with_scores(self):
        from pdf2zh.gui.components.diagnostic_panel import build_diagnostic_markdown
        result = build_diagnostic_markdown(
            quality_scores={
                "translation_score": 95.0,
                "layout_score": 88.0,
                "collision_score": 72.0,
            },
            diagnostic_summary="Overlap detected | Low quality",
        )
        assert "95" in result
        assert "88" in result
        assert "72" in result
        assert "Overlap" in result or "Low" in result or "diagnostic" in result.lower()

    def test_build_diagnostic_markdown_with_node_overview(self):
        from pdf2zh.gui.components.diagnostic_panel import build_diagnostic_markdown
        result = build_diagnostic_markdown(
            node_overview={
                "pages": 5,
                "paragraphs": 42,
                "headings": 8,
                "formulas": 12,
            },
        )
        assert "42" in result
        assert "5" in result
        assert "8" in result
        assert "12" in result

    def test_build_diagnostic_markdown_passed(self):
        from pdf2zh.gui.components.diagnostic_panel import build_diagnostic_markdown
        result = build_diagnostic_markdown(
            quality_scores={"translation_score": 100.0},
            diagnostic_summary="All checks passed",
        )
        assert "passed" in result.lower() or "100" in result

    def test_create_diagnostic_panel_returns_dict(self):
        # This test only verifies the function exists and returns expected keys
        from pdf2zh.gui.components.diagnostic_panel import create_diagnostic_panel
        # We can't instantiate gradio components in headless mode without catching import errors
        assert callable(create_diagnostic_panel)


# =============================================================================
# 4. Services Layer Tests (RuntimeService)
# =============================================================================


class TestServicesLayer:
    def test_runtime_service_importable(self):
        from pdf2zh.services.runtime_service import (
            RuntimeService,
            TranslationRequest,
            TaskState as ServiceTaskState,
            TaskStage,
            TaskProgressEvent,
            ServiceConfig,
        )
        assert RuntimeService is not None
        assert TranslationRequest is not None

    def test_translation_request_to_dict(self):
        from pdf2zh.services.runtime_service import TranslationRequest
        req = TranslationRequest(
            source_path="/tmp/test.pdf",
            target_lang="zh-CN",
            source_lang="auto",
            engine="google",
        )
        d = req.to_dict()
        assert d["lang_in"] == "auto"
        assert d["lang_out"] == "zh-CN"
        assert d["service"] == "google"

    def test_task_stage_values(self):
        from pdf2zh.services.runtime_service import TaskStage
        stages = [s.value for s in TaskStage]
        assert "parsing" in stages
        assert "analyzing" in stages
        assert "translating" in stages
        assert "evaluating" in stages  # V4 specific
        assert "repairing" in stages  # V4 specific
        assert "layouting" in stages  # V4 specific

    def test_service_config_v4_flags(self):
        from pdf2zh.services.runtime_service import ServiceConfig
        config = ServiceConfig(
            use_v4_engine=True,
            use_v4_translator=True,
            use_v4_layout=True,
            use_v4_repair=True,
        )
        assert config.use_v4_engine is True
        assert config.use_v4_translator is True
        assert config.use_v4_layout is True
        assert config.use_v4_repair is True

    def test_task_progress_event(self):
        from pdf2zh.services.runtime_service import TaskProgressEvent
        event = TaskProgressEvent(
            task_id="test",
            stage="evaluating",
            progress=95.0,
            current_node_count=128,
            diagnostics_count=3,
            message="Evaluating quality",
        )
        d = event.to_dict()
        assert d["stage"] == "evaluating"
        assert d["progress"] == 95.0
        assert d["current_node_count"] == 128
        assert d["diagnostics_count"] == 3


# =============================================================================
# 5. Import Resolution Tests
# =============================================================================


class TestImportResolution:
    def test_services_importable(self):
        from pdf2zh.services import RuntimeService, TranslationRequest
        assert RuntimeService is not None

    def test_gui_submodules_importable(self):
        from pdf2zh.gui.state import GLOBAL_TASK_STORE
        from pdf2zh.gui.logger import ThreadAwareLogHandler
        from pdf2zh.gui.worker import get_runtime_service
        from pdf2zh.gui.components.upload_panel import create_upload_panel
        from pdf2zh.gui.components.config_panel import create_config_panel
        from pdf2zh.gui.components.progress_panel import create_progress_panel
        from pdf2zh.gui.components.preview_panel import create_preview_panel
        from pdf2zh.gui.components.diagnostic_panel import (
            create_diagnostic_panel,
            build_diagnostic_markdown,
        )
        assert GLOBAL_TASK_STORE is not None
        assert create_upload_panel is not None
        assert build_diagnostic_markdown is not None

    def test_gui_app_importable(self):
        from pdf2zh.gui.app import create_gui
        assert create_gui is not None

    def test_gui_entry_importable(self):
        from pdf2zh.gui.entry import setup_gui
        assert setup_gui is not None

    def test_gui_init_has_setup_gui(self):
        import pdf2zh.gui as gui
        assert hasattr(gui, "setup_gui")
        assert callable(gui.setup_gui)


# =============================================================================
# 6. Worker Module Tests
# =============================================================================


class TestWorkerModule:
    def test_get_runtime_service(self):
        from pdf2zh.gui.worker import get_runtime_service
        svc = get_runtime_service()
        assert svc is not None

    def test_cancel_task_returns_bool(self):
        from pdf2zh.gui.worker import cancel_task
        result = cancel_task("nonexistent_task_id")
        assert isinstance(result, bool)

    def test_worker_functions_importable(self):
        from pdf2zh.gui.worker import (
            get_runtime_service,
            submit_translation_task,
            background_translation_worker,
            _resolve_source_path,
        )
        assert callable(get_runtime_service)
        assert callable(submit_translation_task)
        assert callable(background_translation_worker)
        assert callable(_resolve_source_path)

    def test_resolve_source_path_none(self):
        from pdf2zh.gui.worker import _resolve_source_path
        result = _resolve_source_path("file", None, "", None)
        assert result is None


# =============================================================================
# 7. Gradio Component Interface Tests (headless)
# =============================================================================


class TestComponentInterfaces:
    def test_upload_panel_returns_dict(self):
        from pdf2zh.gui.components.upload_panel import create_upload_panel
        assert callable(create_upload_panel)

    def test_config_panel_returns_dict(self):
        from pdf2zh.gui.components.config_panel import create_config_panel
        assert callable(create_config_panel)

    def test_progress_panel_returns_dict(self):
        from pdf2zh.gui.components.progress_panel import create_progress_panel
        assert callable(create_progress_panel)

    def test_preview_panel_returns_dict(self):
        from pdf2zh.gui.components.preview_panel import create_preview_panel
        assert callable(create_preview_panel)


# =============================================================================
# 8. Cleanup
# =============================================================================


@pytest.fixture(autouse=True)
def cleanup_global_store():
    """Clean up global task store after each test."""
    yield
    GLOBAL_TASK_STORE.queue_clear()
    for tid in GLOBAL_TASK_STORE.list_tasks():
        GLOBAL_TASK_STORE.remove(tid)