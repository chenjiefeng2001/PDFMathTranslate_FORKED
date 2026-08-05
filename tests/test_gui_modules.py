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
# 9. App Shell / Design System (styles.py) Tests
# =============================================================================


class TestStylesModule:
    def test_token_parity_and_completeness(self):
        from pdf2zh.gui.styles import (
            LIGHT_TOKENS, DARK_TOKENS, TOKEN_KEYS,
        )
        assert set(LIGHT_TOKENS) == set(DARK_TOKENS)
        assert set(TOKEN_KEYS) <= set(LIGHT_TOKENS)

    def test_token_namespaces_are_ontological(self):
        """shadow/radius/spacing/typography/motion must NOT live under --color-*."""
        from pdf2zh.gui.styles import UI_CSS
        assert "--radius-sm" in UI_CSS
        assert "--shadow-sm" in UI_CSS
        assert "--space-1" in UI_CSS
        assert "--text-base" in UI_CSS
        assert "--motion-fast" in UI_CSS
        assert "--brand-gradient" in UI_CSS
        assert "--color-radius" not in UI_CSS
        assert "--color-shadow" not in UI_CSS

    def test_gradio_dark_vars_derived_from_tokens(self):
        """Gradio dark overrides are a single-source derivation of DARK_TOKENS."""
        from pdf2zh.gui.styles import (
            DARK_TOKENS, GRADIO_DARK_VARS, build_gradio_dark_vars,
        )
        assert GRADIO_DARK_VARS == build_gradio_dark_vars(DARK_TOKENS)
        # no unresolved "{token}" placeholders may leak into the CSS
        assert not any("{" in v for v in GRADIO_DARK_VARS.values())

    def test_component_css_uses_token_vars_only(self):
        from pdf2zh.gui.styles import COMPONENT_CSS, LIGHT_TOKENS
        assert "var(--color-" in COMPONENT_CSS
        assert "var(--radius-" in COMPONENT_CSS
        # hex colors may only appear inside the token palettes / gradio maps
        import re
        hexes = re.findall(r"#[0-9a-fA-F]{3,8}\b", COMPONENT_CSS)
        assert hexes == []

    def test_css_vars_all_defined_by_tokens(self):
        """Every var() referenced in the component CSS is emitted by tokens."""
        import re
        from pdf2zh.gui.styles import COMPONENT_CSS, UI_CSS, build_token_css, LIGHT_TOKENS
        vars_used = set(re.findall(r"var\((--[a-z0-9-]+)\)", COMPONENT_CSS))
        tokens = {
            m.rstrip(":")
            for m in re.findall(r"--[a-z0-9-]+:", build_token_css(LIGHT_TOKENS))
        }
        assert vars_used <= tokens
        # no legacy --color-shadow-* / --color-radius-* namespaces may remain
        assert "--color-shadow" not in UI_CSS
        assert "--color-radius" not in UI_CSS

    def test_dark_mode_selector_in_ui_css(self):
        from pdf2zh.gui.styles import UI_CSS
        assert 'html[data-theme="dark"]' in UI_CSS
        assert ".stepbar" in UI_CSS
        assert ".theme-toggle-btn" in UI_CSS

    def test_session_js_persists_theme_and_client(self):
        from pdf2zh.gui.styles import SESSION_JS
        assert "pdf2zh_theme" in SESSION_JS
        assert "pdf2zh_client_id" in SESSION_JS
        assert "pdf2zh_last_task_id" in SESSION_JS

    def test_toggle_theme_js_is_frontend_only(self):
        from pdf2zh.gui.styles import TOGGLE_THEME_JS
        assert "data-theme" in TOGGLE_THEME_JS
        assert "localStorage" in TOGGLE_THEME_JS

    def test_status_badge_html(self):
        from pdf2zh.gui.styles import build_status_badge_html
        assert "status-success" in build_status_badge_html("completed")
        assert "status-error" in build_status_badge_html("failed")
        assert "status-running" in build_status_badge_html("translating")
        assert "status-idle" in build_status_badge_html("idle")
        assert "v4 pipeline" in build_status_badge_html("translating", "v4 pipeline")

    def test_i18n_copy_parity(self):
        """Every copy entry has both languages; every stage label is covered."""
        from pdf2zh.gui.i18n import T, STAGE_LABELS, B
        for key, (zh, en) in T.items():
            assert zh and en, key
            assert " / " in B(key)
        for status, (zh, en) in STAGE_LABELS.items():
            assert zh and en, status


# =============================================================================
# 10. StepBar Pipeline Tests (progress_panel.py)
# =============================================================================


class TestStepBar:
    def test_idle_stepbar(self):
        from pdf2zh.gui.components.progress_panel import build_stepbar_html
        html = build_stepbar_html("", 0.0)
        assert "stepbar" in html
        assert html.count("step-item") == 4  # four stages

    def test_stepbar_is_aria_annotated(self):
        from pdf2zh.gui.components.progress_panel import build_stepbar_html
        html = build_stepbar_html("translating", 40.0)
        assert 'role="list"' in html
        assert 'role="listitem"' in html
        assert 'aria-current="step"' in html

    def test_active_stage(self):
        from pdf2zh.gui.components.progress_panel import build_stepbar_html
        html = build_stepbar_html("translating", 40.0)
        assert "step-item active" in html
        assert "step-connector done" in html

    def test_completed_stepbar_all_done(self):
        from pdf2zh.gui.components.progress_panel import build_stepbar_html
        html = build_stepbar_html("completed", 100.0)
        assert html.count("step-item done") == 4

    def test_failed_stepbar_marks_error(self):
        from pdf2zh.gui.components.progress_panel import build_stepbar_html
        html = build_stepbar_html("failed", 10.0)
        assert "step-item error" in html

    def test_progress_bar_html_states(self):
        from pdf2zh.gui.components.progress_panel import build_progress_bar_html
        done = build_progress_bar_html("completed", 100.0, "all good")
        assert "progress-done" in done
        err = build_progress_bar_html("failed", 10.0, "boom")
        assert "progress-error" in err
        run = build_progress_bar_html("translating", 33.0, "working")
        assert "33.0%" in run

    def test_progress_bar_aria(self):
        from pdf2zh.gui.components.progress_panel import build_progress_bar_html
        html = build_progress_bar_html("translating", 45.0, "working")
        assert 'role="progressbar"' in html
        assert 'aria-valuenow="45.0"' in html


# =============================================================================
# 11. Upload Panel Summary Tests
# =============================================================================


class TestUploadPanelSummary:
    def test_build_file_summary_html_empty(self):
        from pdf2zh.gui.components.upload_panel import build_file_summary_html
        assert build_file_summary_html(None) == ""
        assert build_file_summary_html([]) == ""

    def test_build_file_summary_html_single(self):
        from pdf2zh.gui.components.upload_panel import build_file_summary_html
        html = build_file_summary_html(
            {"name": "tmp/paper.pdf", "size": 2048}
        )
        assert "paper.pdf" in html
        assert "已选择" in html
        assert "2.0 KB" in html

    def test_build_file_summary_html_multi(self):
        from pdf2zh.gui.components.upload_panel import build_file_summary_html
        html = build_file_summary_html(
            [
                {"name": "a.pdf", "size": 1024},
                {"name": "b.pdf", "size": 3072},
            ]
        )
        assert "a.pdf" in html
        assert "b.pdf" in html
        assert "× 2" in html

    def test_human_size(self):
        from pdf2zh.gui.components.upload_panel import _human_size
        assert _human_size(0) == "0 B"
        assert _human_size(1500) == "1.5 KB"

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

# =============================================================================
# 12. App Sync Contract Tests (app.py)
# =============================================================================


class TestAppSyncContract:
    def test_sync_status_arity_matches_sync_outputs(self):
        import pdf2zh.gui.app as app
        # Building the Blocks wires every sync output; a mismatch would raise.
        app.create_gui()
        # 16 legacy slots + stepbar rail + header badge + panel badge + retry.
        idle = app._idle_updates()
        assert len(idle) == 20

    def test_sync_components_has_retry(self):
        import pdf2zh.gui.app as app
        assert "retry_btn" in app._SYNC_COMPONENTS
        assert len(app._SYNC_COMPONENTS) == len(app._idle_updates())

    def test_create_gui_wires_new_components(self):
        import inspect
        import pdf2zh.gui.app as app
        app.create_gui()
        src = inspect.getsource(app.create_gui)
        assert "build_stepbar_html" in src
        assert "build_status_badge_html" in src
        assert "header_badge" in src
        assert 'pc["status_badge"]' in src

    def test_result_selector_replaces_legacy_dropdown(self):
        import inspect
        import pdf2zh.gui.app as app
        src = inspect.getsource(app.create_gui)
        assert "result_selector" in src
        assert "result_files_dropdown" not in src

    def test_theme_toggle_frontend_only(self):
        import inspect
        import pdf2zh.gui.app as app
        src = inspect.getsource(app.create_gui)
        assert "js=TOGGLE_THEME_JS" in src
        assert "theme_toggle" in src

    def test_sync_is_event_driven_no_timer(self):
        """SSE push replaces the polling Timer: create_gui wires the hidden
        sync-trigger button and must not construct any gr.Timer."""
        import inspect
        import pdf2zh.gui.app as app
        app.create_gui()
        src = inspect.getsource(app.create_gui)
        assert "gr.Timer" not in src
        assert 'elem_id="sync-trigger"' in src
        assert "_drain_events_flat" in src

    def test_events_route_registers_on_live_app(self):
        """_register_events_route adds /gui/events to the FastAPI app."""
        import pdf2zh.gui.app as app
        gui = app.create_gui()
        app._register_events_route(gui)
        paths = [getattr(r, "path", None) for r in gui.app.routes]
        assert "/gui/events" in paths

    def test_session_js_has_sse_client(self):
        from pdf2zh.gui.styles import SESSION_JS
        assert "EventSource" in SESSION_JS
        assert "/gui/events" in SESSION_JS
        assert "sync-trigger" in SESSION_JS


# =============================================================================
# 13. EventNotifier (SSE push transport) Tests
# =============================================================================


class TestEventNotifier:
    def test_notifier_fans_out_published_events(self):
        import asyncio
        from pdf2zh.gui.events import EventBus, TaskStarted
        from pdf2zh.gui.notifier import EventNotifier

        bus = EventBus()
        notifier = EventNotifier(bus)
        notifier.start()

        async def run():
            q = notifier.connect()
            bus.publish(TaskStarted(task_id="t1"))
            payload = await asyncio.wait_for(q.get(), timeout=1.0)
            return payload

        payload = asyncio.run(run())
        assert '"t1"' in payload
        assert '"seq":' in payload
        notifier.stop()

    def test_notifier_disconnect_stops_delivery(self):
        import asyncio
        from pdf2zh.gui.events import EventBus, TaskStarted
        from pdf2zh.gui.notifier import EventNotifier

        bus = EventBus()
        notifier = EventNotifier(bus)
        notifier.start()

        async def run():
            q = notifier.connect()
            notifier.disconnect(q)
            bus.publish(TaskStarted(task_id="t2"))
            assert notifier.subscriber_count() == 0
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(q.get(), timeout=0.2)

        asyncio.run(run())
        notifier.stop()

    def test_notifier_start_is_idempotent(self):
        from pdf2zh.gui.events import EventBus
        from pdf2zh.gui.notifier import EventNotifier

        bus = EventBus()
        notifier = EventNotifier(bus)
        notifier.start()
        notifier.start()
        assert notifier._sub_id is not None
        notifier.stop()
        assert notifier._sub_id is None
