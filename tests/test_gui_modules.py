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

    def test_recent_lines_ring_buffer(self):
        handler = ThreadAwareLogHandler()
        for i in range(5):
            record = logging.LogRecord(
                name="test", level=logging.INFO,
                pathname="", lineno=0, msg=f"detail line {i}", args=None,
                exc_info=None,
            )
            handler.emit(record)
        lines = handler.recent_lines(max_lines=10)
        assert len(lines) == 5
        assert lines[-1] == "detail line 4"
        # max_lines truncates from the tail
        assert len(handler.recent_lines(max_lines=2)) == 2
        assert handler.recent_lines(max_lines=2)[-1] == "detail line 4"

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


class TestHealingDashboard:
    def test_healing_markdown_empty(self):
        from pdf2zh.gui.components.diagnostic_panel import build_healing_markdown
        result = build_healing_markdown()
        assert "诊断" in result or "diagnostic" in result.lower()

    def test_healing_markdown_legacy_report(self):
        from pdf2zh.gui.components.diagnostic_panel import build_healing_markdown
        result = build_healing_markdown(
            diagnostic_report={
                "errors": 2, "warnings": 1, "admissible": False,
                "issues": [],
            },
        )
        assert "errors=2" in result
        assert "warnings=1" in result
        assert "admissible=False" in result

    def test_healing_markdown_v4_report(self):
        from pdf2zh.gui.components.diagnostic_panel import build_healing_markdown
        result = build_healing_markdown(
            diagnostic_report={
                "total": 10, "passed": 8, "failed": 2, "pass_rate": 80.0,
                "records": [],
            },
        )
        assert "80.0" in result
        assert "failed=2/10" in result

    def test_healing_markdown_actions_and_run(self):
        from pdf2zh.gui.components.diagnostic_panel import build_healing_markdown
        result = build_healing_markdown(
            diagnostic_report={
                "errors": 1, "warnings": 0, "admissible": False, "issues": [],
            },
            repair_records=[
                {"code": "unicode_error", "node_id": "p1_0", "page": 1,
                 "severity": "error", "message": "Unicode 损坏",
                 "action": "Unicode 修复 (OCR 计划)", "status": "applied"},
            ],
            heal_status={
                "ran": True, "iterations": 1,
                "before_errors": 1, "after_errors": 0, "improved": True,
            },
            confidence_stats={"annotated": 12, "avg": 0.8, "min": 0.1, "max": 0.99},
        )
        assert "unicode_error" in result
        assert "Unicode" in result
        assert "before=1" in result
        assert "after=0" in result
        assert "annotated=12" in result

    def test_healing_markdown_failed_run(self):
        from pdf2zh.gui.components.diagnostic_panel import build_healing_markdown
        result = build_healing_markdown(
            diagnostic_summary="Diagnostics unavailable",
            heal_status={"ran": False, "error": "boom"},
        )
        assert "boom" in result


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
            progress=95.0,            current_node_count=128,
            diagnostics_count=3,
            message="Evaluating quality",
        )
        d = event.to_dict()
        assert d["stage"] == "evaluating"
        assert d["progress"] == 95.0
        assert d["current_node_count"] == 128
        assert d["diagnostics_count"] == 3

    def test_emit_smooth_monotonic_throttled(self):
        from pdf2zh.services.runtime_service import RuntimeService, TaskStage

        svc = RuntimeService()
        emitted = []

        def listener(ev):
            emitted.append(ev.progress)

        svc.add_event_listener(listener)
        # First emission always goes out; sub-1% steps within a stage are folded.
        svc._emit_smooth("t1", TaskStage.TRANSLATING.value, 50.0)
        svc._emit_smooth("t1", TaskStage.TRANSLATING.value, 50.4)
        svc._emit_smooth("t1", TaskStage.TRANSLATING.value, 51.0)
        svc._emit_smooth("t1", TaskStage.TRANSLATING.value, 79.9)
        # Progress never goes backwards even if a report dips.
        svc._emit_smooth("t1", TaskStage.TRANSLATING.value, 30.0)
        svc._emit_smooth("t1", TaskStage.COMPLETED.value, 100.0)
        assert emitted == [50.0, 51.0, 79.9, 100.0]
        assert emitted == sorted(emitted)

    def test_emit_smooth_terminal_always_emitted(self):
        from pdf2zh.services.runtime_service import RuntimeService, TaskStage

        svc = RuntimeService()
        emitted = []
        svc.add_event_listener(lambda ev: emitted.append(ev.progress))
        svc._emit_smooth("t2", TaskStage.FAILED.value, 100.0)
        assert emitted == [100.0]

    def test_emit_smooth_batch_aggregation_monotonic(self):
        from pdf2zh.services.runtime_service import RuntimeService, TaskStage

        svc = RuntimeService()
        with svc._batch_ctx_lock:
            svc._batch_ctx["t3"] = type("Ctx", (), {
                "total_files": 2, "completed_files": 0, "failed_files": 0,
                "current_file": "a.pdf",
            })()
        emitted = []
        svc.add_event_listener(lambda ev: emitted.append(ev.progress))
        # File 1 translating window: 50 -> 80 (aggregated 25 -> 40)
        svc._emit_smooth("t3", TaskStage.TRANSLATING.value, 50.0)
        svc._emit_smooth("t3", TaskStage.TRANSLATING.value, 80.0)
        assert emitted == [25.0, 40.0]
        # File 2 restarts at 50 (aggregated 25) -- must NOT go backwards.
        svc._emit_smooth("t3", TaskStage.TRANSLATING.value, 50.0)
        assert emitted == [25.0, 40.0]
        svc._emit_smooth("t3", TaskStage.TRANSLATING.value, 100.0)
        assert emitted == [25.0, 40.0, 50.0]

    def test_emit_smooth_forwards_message_on_backward_dip(self):
        # 回归：进度回退被钳制时，消息（如降级通知）必须仍透传到前端，
        # 否则前端停留在旧进度且看不到任何更新 —— 前后端数据断层。
        from pdf2zh.services.runtime_service import RuntimeService, TaskStage

        svc = RuntimeService()
        svc._store.create_task("tdc")
        events = []
        svc.add_event_listener(lambda ev: events.append(ev))
        svc._emit_smooth("tdc", TaskStage.TRANSLATING.value, 55.0, "Translating 2/2")
        svc._emit_smooth("tdc", TaskStage.TRANSLATING.value, 30.0, "degraded to CPU")
        # 进度单调（第二个事件克隆当前进度，不倒退），消息送达
        assert [e.progress for e in events] == [55.0, 55.0]
        assert events[-1].message == "degraded to CPU"
        state = svc.get_task_state("tdc")
        assert state.message == "degraded to CPU"
        # 消息事件后仍可继续正常前进
        svc._emit_smooth("tdc", TaskStage.RENDERING.value, 80.0, "Merging pages...")
        assert [e.progress for e in events] == [55.0, 55.0, 80.0]

    def test_worker_parse_env_lines(self):
        from pdf2zh.gui.worker import _parse_env_lines

        envs = _parse_env_lines(
            "OPENAI_API_KEY=sk-abc",
            "openai_api_base=https://x.example",
            "# comment line",
            "NO_EQUALS",
            "  ",
        )
        assert envs == {
            "openai_api_key": "sk-abc",
            "openai_api_base": "https://x.example",
        }
        assert _parse_env_lines() == {}
        assert _parse_env_lines(None, "") == {}

    def test_collect_legacy_diagnostics_no_errors(self):
        """No error-level issues: no repair run, records still produced."""
        from pdf2zh.services.runtime_service import RuntimeService

        class _StubModel:
            def __init__(self, metadata=None):
                self.metadata = metadata or {}

        svc = RuntimeService()
        dm = _StubModel({
            "diagnostics": {
                "errors": 0, "warnings": 1, "admissible": True,
                "issues": [
                    {"code": "toc_low_confidence", "node_id": "p1_0", "page": 1,
                     "severity": "warning", "message": "low toc"},
                ],
            },
            "confidence_stats": {"annotated": 3, "avg": 0.7},
        })
        diag, heal, recs, conf = svc._collect_legacy_diagnostics(
            {"document_model": dm}
        )
        assert diag == dm.metadata["diagnostics"]
        assert heal is None
        assert recs and recs[0]["code"] == "toc_low_confidence"
        assert "TOC" in recs[0]["action"]
        assert recs[0]["status"] == "applied"
        assert conf == {"annotated": 3, "avg": 0.7}

    def test_collect_legacy_diagnostics_repair_run(self):
        """Error-level issues trigger the repair loop with before/after evidence."""
        from pdf2zh.services.runtime_service import RuntimeService
        from pdf2zh.v3.document_model import DocumentModel
        from pdf2zh.v3.canonical_page import (
            BlockModel, GlyphModel, LineModel, PageModel, SpanModel,
        )

        glyph = GlyphModel(char="A", decode="notdef")
        span = SpanModel(text="A", glyphs=[glyph])
        line = LineModel(text="A", spans=[span])
        block = BlockModel(kind="paragraph", lines=[line])
        page = PageModel(page_num=1, blocks=[block])
        dm = DocumentModel(pages=[page])
        dm.metadata["diagnostics"] = {
            "errors": 1, "warnings": 0, "admissible": False,
            "issues": [
                {"code": "unicode_error", "node_id": "p1_0", "page": 1,
                 "severity": "error", "message": "Unicode 损坏", "evidence": {}},
            ],
        }
        svc = RuntimeService()
        diag, heal, recs, conf = svc._collect_legacy_diagnostics(
            {"document_model": dm}
        )
        assert diag is not None
        assert heal is not None
        assert heal["ran"] is True
        assert isinstance(heal["improved"], bool)
        assert recs and recs[0]["code"] == "unicode_error"
        assert "Unicode" in recs[0]["action"]

    def test_collect_legacy_diagnostics_no_model(self):
        from pdf2zh.services.runtime_service import RuntimeService
        svc = RuntimeService()
        diag, heal, recs, conf = svc._collect_legacy_diagnostics({})
        assert (diag, heal, recs, conf) == (None, None, None, None)

    def test_mode_presets_resolve(self):
        from pdf2zh.services.runtime_service import (
            resolve_mode_config, ServiceConfig,
        )
        base = ServiceConfig()
        # quick 快速：经典管线 + 关闭全部现代 side-channel
        c0 = resolve_mode_config("quick", base)
        assert c0.use_v4_engine is False
        assert c0.processor_channels is False
        assert c0.relink_links is False
        assert c0.use_v4_gate is False
        assert c0.run_evaluation is False
        assert c0.emit_ir is False
        # standard 标准 —— 经典管线 + 全部现代 side-channel（生产默认质量）
        c1 = resolve_mode_config("standard", base)
        assert c1.use_v4_engine is False
        assert c1.processor_channels is True
        assert c1.relink_links is True
        assert c1.emit_ir is True
        assert c1.use_v4_gate is False
        assert c1.run_evaluation is False
        # quality 高质量 —— legacy + 文档级评测 + 写回门控 + QA
        c2 = resolve_mode_config("quality", base)
        assert c2.use_v4_engine is False
        assert c2.run_evaluation is True
        assert c2.use_v4_gate is True
        assert c2.processor_channels is True
        # babeldoc 独立引擎 —— 保持调用方配置（空 preset）
        cb = resolve_mode_config("babeldoc", base)
        assert cb == base

    def test_mode_auto_and_unknown_preserve_base(self):
        from pdf2zh.services.runtime_service import (
            resolve_mode_config, ServiceConfig,
        )
        base = ServiceConfig(use_v4_engine=True, run_evaluation=True)
        for mode in ("auto", "", "bogus", None):
            resolved = resolve_mode_config(mode, base)
            assert resolved.use_v4_engine is True
            assert resolved.run_evaluation is True

    def test_legacy_mode_kwargs_mapping(self):
        from pdf2zh.services.runtime_service import legacy_mode_kwargs
        assert legacy_mode_kwargs("quick")["document_model"] is False
        assert legacy_mode_kwargs("standard")["document_model"] is True
        k2 = legacy_mode_kwargs("quality")
        assert k2["translation_qa"] is True
        assert k2["render_takeover"] is True
        assert legacy_mode_kwargs("babeldoc") == {}
        assert legacy_mode_kwargs("auto") == {}
        assert legacy_mode_kwargs(None) == {}

    def test_resolve_pipeline_maps_every_mode_to_working_pipeline(self):
        from pdf2zh.services.runtime_service import resolve_pipeline
        assert resolve_pipeline("auto") == "legacy"
        assert resolve_pipeline("quick") == "legacy"
        assert resolve_pipeline("standard") == "legacy"
        assert resolve_pipeline("quality") == "legacy"
        assert resolve_pipeline("babeldoc") == "babeldoc"
        assert resolve_pipeline(None) == "legacy"
        assert resolve_pipeline("bogus") == "legacy"

    def test_submit_task_records_mode_choice(self, monkeypatch):
        import tempfile
        from pdf2zh.services.runtime_service import (
            RuntimeService, TranslationRequest,
        )
        svc = RuntimeService()
        monkeypatch.setattr(svc, "_execute_task", lambda tid, req: None)
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(b"%PDF-1.4\n%%EOF\n")
            tmp = f.name
        tid = svc.submit_task(TranslationRequest(
            source_path=tmp, files=[tmp], target_lang="zh-CN",
            extra_config={"mode_choice": "standard"},
        ))
        state = svc.get_task_state(tid)
        assert state is not None
        assert state.mode_choice == "standard"
        default_tid = svc.submit_task(TranslationRequest(
            source_path=tmp, files=[tmp], target_lang="en",
        ))
        dstate = svc.get_task_state(default_tid)
        assert dstate.mode_choice == "auto"


# =============================================================================
# 4b. v1.20 sync-fix & packaging tests
# =============================================================================


class TestV120ZipPackaging:
    def test_single_file_completion_builds_real_zip(self, tmp_path, monkeypatch):
        import zipfile

        from pdf2zh.services.runtime_service import RuntimeService, TaskStage

        monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
        mono = tmp_path / "doc-mono.pdf"
        dual = tmp_path / "doc-dual.pdf"
        mono.write_bytes(b"%PDF-1.7 fake mono")
        dual.write_bytes(b"%PDF-1.7 fake dual")

        svc = RuntimeService()
        svc._store.create_task("t_zip")
        emitted = []
        svc.add_event_listener(lambda ev: emitted.append(ev.stage))
        svc._complete_file(
            "t_zip",
            [
                {"name": "doc-mono.pdf", "path": str(mono)},
                {"name": "doc-dual.pdf", "path": str(dual)},
            ],
            selected_file="doc-mono.pdf",
            preview_path=str(dual),
            diagnostic_summary="ok",
            message="Completed",
        )

        ts = svc.get_task_state("t_zip")
        assert ts.status == TaskStage.COMPLETED.value
        # Download-All must serve a genuine ZIP, not a bare mono/dual PDF.
        assert ts.result_zip and ts.result_zip.endswith(".zip")
        assert os.path.exists(ts.result_zip)
        with zipfile.ZipFile(ts.result_zip) as zf:
            names = sorted(zf.namelist())
        assert names == ["doc-dual.pdf", "doc-mono.pdf"]
        # Preview is not clobbered by the packaging step.
        assert ts.preview_path == str(dual)
        assert emitted == [TaskStage.COMPLETED.value]

    def test_single_file_completion_ignores_caller_result_zip(self, tmp_path):
        from pdf2zh.services.runtime_service import RuntimeService

        mono = tmp_path / "a-mono.pdf"
        mono.write_bytes(b"%PDF-1.7 fake")
        svc = RuntimeService()
        svc._store.create_task("t_legacy")
        svc._complete_file(
            "t_legacy",
            [{"name": "a-mono.pdf", "path": str(mono)}],
            result_zip=str(mono),  # bogus legacy caller value -> must be dropped
            message="Completed (Legacy)",
        )
        ts = svc.get_task_state("t_legacy")
        assert ts.result_zip.endswith(".zip")
        assert ts.result_zip != str(mono)

    def test_list_task_ids_creation_order_and_update_task_state(self):
        from pdf2zh.services.runtime_service import RuntimeService

        svc = RuntimeService()
        svc._store.create_task("a")
        svc._store.create_task("b")
        assert svc.list_task_ids() == ["a", "b"]
        svc.update_task_state("b", selected_file="out.pdf")
        assert svc.get_task_state("b").selected_file == "out.pdf"

    def test_batch_zip_still_built_at_finish(self, tmp_path):
        import zipfile

        from pdf2zh.services.runtime_service import RuntimeService

        svc = RuntimeService()
        svc._store.create_task("t_batch")
        ctx = type("Ctx", (), {
            "total_files": 2, "completed_files": 1, "failed_files": 0,
            "current_file": "b.pdf",
        })()
        with svc._batch_ctx_lock:
            svc._batch_ctx["t_batch"] = ctx
        a = tmp_path / "a.pdf"
        a.write_bytes(b"%PDF-1.7 a")
        svc._store.update_task(
            "t_batch", result_files=[{"name": "a.pdf", "path": str(a)}],
        )
        zip_path = svc._build_batch_zip("t_batch")
        assert zip_path and zip_path.endswith(".zip")
        with zipfile.ZipFile(zip_path) as zf:
            assert zf.namelist() == ["a.pdf"]


class TestV120DetailedLogs:
    def test_get_handler_raises_root_level_to_info(self):
        from pdf2zh.gui.logger import get_handler

        root = logging.getLogger()
        prev = root.level
        try:
            get_handler()
            assert root.level == logging.NOTSET or root.level <= logging.INFO
            assert get_handler() in root.handlers
        finally:
            root.setLevel(prev)

    def test_info_records_flow_into_ring_through_root(self):
        from pdf2zh.gui.logger import get_handler

        root = logging.getLogger()
        prev = root.level
        try:
            get_handler()
            root.setLevel(logging.INFO)
            marker = f"[task=t_live] v1.20 detail line"
            log = logging.getLogger("pdf2zh.gui.app")
            log.info("%s", marker)
            lines = get_handler().recent_lines()
            assert any(marker in ln for ln in lines)
        finally:
            root.setLevel(prev)


class TestV120GuiSync:
    def test_resolve_current_task_id_falls_back_to_runtime(self, monkeypatch):
        import pdf2zh.gui.app as app
        from pdf2zh.gui.state import GLOBAL_TASK_STORE
        from pdf2zh.services.runtime_service import RuntimeService

        GLOBAL_TASK_STORE.remove("t_idle")
        svc = RuntimeService()
        svc._store.create_task("live_task_42")
        monkeypatch.setattr(app, "get_runtime_service", lambda: svc)
        assert app._resolve_current_task_id("") == "live_task_42"
        assert app._resolve_current_task_id("unknown") == "live_task_42"

    def test_render_message_changed_keeps_status_label(self, monkeypatch):
        import pdf2zh.gui.app as app
        from pdf2zh.gui.events import TaskMessageChanged
        from pdf2zh.gui.i18n import B
        from pdf2zh.services.runtime_service import RuntimeService

        svc = RuntimeService()
        svc._store.create_task("t_msg")
        svc._store.update_task("t_msg", status="translating", progress=55.0)
        monkeypatch.setattr(app, "get_runtime_service", lambda: svc)

        acc = app._DeltaAccumulator()
        ev = TaskMessageChanged(task_id="t_msg", message="slicing pages 55%")
        app._render_message_changed(acc, ev)
        md = acc._updates["status_markdown"]["value"]
        assert B("label_status") in md
        assert "55%" in md
        assert "slicing pages 55%" in md

    def test_render_progress_changed_updates_status_text(self, monkeypatch):
        import pdf2zh.gui.app as app
        from pdf2zh.gui.events import TaskProgressChanged
        from pdf2zh.gui.i18n import B

        acc = app._DeltaAccumulator()
        ev = TaskProgressChanged(
            task_id="t_p", progress=42.0, stage="translating", message="x"
        )
        app._render_progress_changed(acc, ev)
        md = acc._updates["status_markdown"]["value"]
        assert B("label_status") in md and "42.0%" in md

    def test_on_select_writes_runtime_store(self, monkeypatch):
        import pdf2zh.gui.app as app
        from pdf2zh.gui.state import GLOBAL_TASK_STORE, TaskState
        from pdf2zh.services.runtime_service import RuntimeService

        svc = RuntimeService()
        svc._store.create_task("t_sel")
        GLOBAL_TASK_STORE.set("t_sel", TaskState(task_id="t_sel"))
        monkeypatch.setattr(app, "get_runtime_service", lambda: svc)
        app.on_select_file("t_sel", "doc-dual.pdf")
        assert svc.get_task_state("t_sel").selected_file == "doc-dual.pdf"
        assert GLOBAL_TASK_STORE.get("t_sel").selected_file == "doc-dual.pdf"

    def test_entry_registers_logs_route(self, monkeypatch):
        import pdf2zh.gui.entry as entry

        called = []
        for name in ("_register_preview_route", "_register_events_route", "_register_logs_route"):
            def stub(*args, name=name, **kwargs):
                called.append(name)
            monkeypatch.setattr("pdf2zh.gui.app." + name, stub)
        entry._register_custom_routes(None)
        assert "_register_logs_route" in called
        assert called == ["_register_preview_route", "_register_events_route", "_register_logs_route"]


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


    def test_resolve_source_paths_multi(self):
        from pdf2zh.gui.worker import _resolve_source_paths
        paths = _resolve_source_paths(
            "file", ["/tmp/a.pdf", "/tmp/b.pdf"], "", None,
        )
        assert paths == ["/tmp/a.pdf", "/tmp/b.pdf"]

    def test_resolve_source_paths_single(self):
        from pdf2zh.gui.worker import _resolve_source_paths
        assert _resolve_source_paths("file", "/tmp/a.pdf", "", None) == ["/tmp/a.pdf"]

    def test_resolve_source_paths_empty(self):
        from pdf2zh.gui.worker import _resolve_source_paths
        assert _resolve_source_paths("file", None, "", None) == []
        assert _resolve_source_paths("file", [], "", None) == []

    def test_resolve_source_paths_filedata_objects(self):
        from pdf2zh.gui.worker import _resolve_source_paths

        class FakeFile:
            def __init__(self, path):
                self.path = path

        assert _resolve_source_paths(
            "file", [FakeFile("/tmp/a.pdf"), FakeFile("/tmp/b.pdf")], "", None,
        ) == ["/tmp/a.pdf", "/tmp/b.pdf"]


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

    def test_build_file_summary_html_path_strings(self):
        from pdf2zh.gui.components.upload_panel import build_file_summary_html
        html = build_file_summary_html(["tmp/a.pdf", "tmp/b.pdf"])
        assert "a.pdf" in html
        assert "b.pdf" in html
        assert "× 2" in html

    def test_build_file_summary_html_single_path(self):
        from pdf2zh.gui.components.upload_panel import build_file_summary_html
        html = build_file_summary_html("tmp/paper.pdf")
        assert "paper.pdf" in html

    def test_build_file_summary_html_filedata_objects(self):
        from pdf2zh.gui.components.upload_panel import build_file_summary_html

        class FakeFile:
            def __init__(self, name, size):
                self.name = name
                self.size = size

        html = build_file_summary_html(
            [FakeFile("a.pdf", 1024), FakeFile("b.pdf", 3072)]
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
        # full payload frame: SSE id cursor + complete JSON event
        assert payload.startswith("id: 1\n")
        assert "data: {" in payload
        assert '"task_id": "t1"' in payload
        assert '"seq": 1' in payload
        assert '"event_type": "TaskStarted"' in payload
        notifier.stop()

    def test_notifier_frame_carries_full_event_payload(self):
        import json
        from pdf2zh.gui.events import EventBus, TaskProgressChanged
        from pdf2zh.gui.notifier import _format_frame

        bus = EventBus()
        ev = bus.publish(
            TaskProgressChanged(task_id="t1", progress=42.5, stage="render", message="hi")
        )
        frame = _format_frame(ev)
        lines = frame.splitlines()
        assert lines[0] == "id: 1"
        assert lines[1].startswith("data: ")
        data = json.loads(lines[1][len("data: "):])
        assert data["event_type"] == "TaskProgressChanged"
        assert data["task_id"] == "t1"
        assert data["seq"] == 1
        assert data["progress"] == 42.5
        assert data["stage"] == "render"
        assert data["message"] == "hi"
        assert frame.endswith("\n\n")

    def test_notifier_replay_frames_after_last_event_id(self):
        import json

        from pdf2zh.gui.events import EventBus, TaskMessageChanged, TaskProgressChanged
        from pdf2zh.gui.notifier import _replay_frames

        bus = EventBus()
        bus.publish(TaskProgressChanged(task_id="t1", progress=10.0))  # seq 1
        bus.publish(TaskMessageChanged(task_id="t1", message="first"))  # seq 2
        bus.publish(TaskProgressChanged(task_id="t2", progress=50.0))  # seq 3
        bus.publish(TaskMessageChanged(task_id="t2", message="second"))  # seq 4
        frames = _replay_frames(bus.events_after(2))
        assert len(frames) == 2
        seen = []
        for frame in frames:
            lines = frame.splitlines()
            seen.append((int(lines[0].split(":")[1].strip()),
                         json.loads(lines[1][len("data: "):])["event_type"]))
        assert seen == [(3, "TaskProgressChanged"), (4, "TaskMessageChanged")]

    def test_sse_stream_replays_missed_events_on_reconnect(self):
        import asyncio
        from types import SimpleNamespace

        from pdf2zh.gui.events import EventBus, TaskMessageChanged, TaskStarted
        from pdf2zh.gui.notifier import EventNotifier

        bus = EventBus()
        notifier = EventNotifier(bus)

        async def run():
            bus.publish(TaskStarted(task_id="t1"))  # seq 1 published while "offline"
            bus.publish(TaskMessageChanged(task_id="t1", message="old"))  # seq 2
            resp = await notifier.sse_stream(
                SimpleNamespace(headers={"last-event-id": "1"})
            )
            stream = resp.body_iterator
            chunks = []
            try:
                while len(chunks) < 2:
                    chunks.append(await anext(stream))
            except StopAsyncIteration:
                pass
            finally:
                await stream.aclose()
            return chunks

        chunks = asyncio.run(run())
        assert chunks[0] == "retry: 1000\n\n"
        assert chunks[1].startswith("id: 2\n")  # missed event replayed
        assert '"message": "old"' in chunks[1]

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


# =============================================================================
# 13b. Notice channel (runtime -> bridge -> domain event -> renderer) Tests
# =============================================================================


class TestNoticeChannel:
    def _stub_service(self):
        class StubService:
            def __init__(self):
                self.listeners = []

            def add_event_listener(self, cb):
                self.listeners.append(cb)

            def remove_event_listener(self, cb):
                if cb in self.listeners:
                    self.listeners.remove(cb)

            def get_task_state(self, tid):
                return None

        return StubService()

    def test_bridge_maps_runtime_notice_to_notice_emitted(self):
        from pdf2zh.gui.event_bridge import TaskEventBridge
        from pdf2zh.gui.events import EventBus
        from pdf2zh.services.runtime_service import (
            RuntimeNoticeEvent,
            TaskProgressEvent,
            TaskStage,
        )

        bus = EventBus()
        stub = self._stub_service()
        bridge = TaskEventBridge(bus=bus, service=stub)
        bridge.start()
        got = []
        bus.subscribe(got.append)

        stub.listeners[0](
            RuntimeNoticeEvent(
                task_id="t1", severity="warning", title="CPU degraded",
                detail="worker crashed", tip="retry --backend auto",
            )
        )
        stub.listeners[0](
            TaskProgressEvent(
                task_id="t1", stage=TaskStage.TRANSLATING.value,
                progress=20.0, message="hi",
            )
        )
        types = [e.event_type for e in got]
        assert "NoticeEmitted" in types
        assert "TaskProgressChanged" in types
        notice = next(e for e in got if e.event_type == "NoticeEmitted")
        assert notice.task_id == "t1"
        assert notice.severity == "warning"
        assert notice.title == "CPU degraded"
        assert notice.detail == "worker crashed"
        assert notice.tip == "retry --backend auto"
        bridge.stop()

    def test_notice_emitted_is_registered_event_type(self):
        from pdf2zh.gui.events import ALL_EVENT_TYPES

        assert "NoticeEmitted" in {t.__name__ for t in ALL_EVENT_TYPES}

    def test_renderer_surfaces_and_persists_notice(self):
        import pdf2zh.gui.app as app
        from pdf2zh.gui.events import NoticeEmitted, TaskMessageChanged

        app._ACTIVE_NOTICES.clear()
        try:
            acc = app._DeltaAccumulator()
            app._render_notice_emitted(
                acc,
                NoticeEmitted(
                    task_id="t9", severity="warning",
                    title="CPU degraded", tip="restart --backend auto",
                ),
            )
            assert "⚠️" in app._ACTIVE_NOTICES.get("t9", "")
            assert "status_badge" in acc._updates
            md = acc._updates["status_markdown"]["value"]
            assert "CPU degraded" in md

            # A later plain message render keeps the notice visible.
            acc2 = app._DeltaAccumulator()
            app._render_message_changed(
                acc2, TaskMessageChanged(task_id="t9", message="busy")
            )
            assert "⚠️" in acc2._updates["status_markdown"]["value"]
        finally:
            app._ACTIVE_NOTICES.clear()


# =============================================================================
# 9. backend Radio 依据执行级探测过滤不可用 GPU（P0-2）
# =============================================================================


class TestBackendChoicesFilter:
    """GUI backend Radio 只展示真正可用的后端（DirectML/CUDA 静默回退时隐藏）。"""

    def test_only_cpu_when_no_gpu_usable(self, monkeypatch):
        from pdf2zh.gui.components.config_panel import _available_backend_choices

        monkeypatch.setattr(
            "pdf2zh.doclayout.get_runtime_provider_status",
            lambda: {
                "onnxruntime": "1.28.0",
                "cuda": False,
                "dml": False,
                "available": ["AzureExecutionProvider", "CPUExecutionProvider"],
                "effective": ["CPUExecutionProvider"],
            },
        )
        labels = [value for _, value in _available_backend_choices()]
        assert labels == ["auto", "cpu"]

    def test_cuda_included_when_usable(self, monkeypatch):
        from pdf2zh.gui.components.config_panel import _available_backend_choices

        monkeypatch.setattr(
            "pdf2zh.doclayout.get_runtime_provider_status",
            lambda: {
                "onnxruntime": "1.28.0",
                "cuda": True,
                "dml": False,
                "available": ["CUDAExecutionProvider", "CPUExecutionProvider"],
                "effective": ["CUDAExecutionProvider", "CPUExecutionProvider"],
            },
        )
        labels = [value for _, value in _available_backend_choices()]
        assert labels == ["auto", "cpu", "cuda"]

    def test_dml_included_when_usable(self, monkeypatch):
        from pdf2zh.gui.components.config_panel import _available_backend_choices

        monkeypatch.setattr(
            "pdf2zh.doclayout.get_runtime_provider_status",
            lambda: {
                "onnxruntime": "1.28.0",
                "cuda": False,
                "dml": True,
                "available": ["AzureExecutionProvider", "CPUExecutionProvider"],
                "effective": ["AzureExecutionProvider", "CPUExecutionProvider"],
            },
        )
        labels = [value for _, value in _available_backend_choices()]
        assert labels == ["auto", "cpu", "dml"]

    def test_status_markdown_mentions_hidden_backends(self, monkeypatch):
        from pdf2zh.gui.components.config_panel import backend_status_markdown

        monkeypatch.setattr(
            "pdf2zh.doclayout.get_runtime_provider_status",
            lambda: {
                "onnxruntime": "1.28.0",
                "cuda": False,
                "dml": False,
                "available": ["AzureExecutionProvider", "CPUExecutionProvider"],
                "effective": ["CPUExecutionProvider"],
            },
        )
        md = backend_status_markdown()
        assert "不可用" in md or "unavailable" in md
        assert "已从后端选项隐藏" in md
