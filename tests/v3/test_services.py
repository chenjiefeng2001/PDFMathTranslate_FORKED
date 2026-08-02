"""Headless tests for pdf2zh.services (RuntimeService).

Tests the unified service layer that replaces the Legacy 21-parameter
pattern and 13-tuple status update pattern.
"""

from __future__ import annotations

import os
import sys
import time
import threading
from typing import List

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from pdf2zh.services.runtime_service import (
    RuntimeService,
    TranslationRequest,
    TaskState,
    TaskProgressEvent,
    TaskStage,
    ServiceConfig,
    _TaskStore,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Data Model Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestTranslationRequest:
    def test_defaults(self):
        req = TranslationRequest(source_path="/tmp/test.pdf")
        assert req.source_path == "/tmp/test.pdf"
        assert req.target_lang == "zh-CN"
        assert req.source_lang == "auto"
        assert req.engine == "google"
        assert req.output_formats == ["pdf"]
        assert req.threads == 4

    def test_to_dict(self):
        req = TranslationRequest(
            source_path="/tmp/test.pdf", target_lang="en",
            source_lang="zh-CN", engine="openai", threads=8,
            skip_subset_fonts=True,
        )
        d = req.to_dict()
        assert d["lang_in"] == "zh-CN"
        assert d["lang_out"] == "en"
        assert d["service"] == "openai"
        assert d["thread"] == 8
        assert d["skip_subset_fonts"] is True

    def test_to_dict_with_extra(self):
        req = TranslationRequest(
            source_path="/tmp/test.pdf",
            extra_config={"prompt": "Translate academic paper"},
        )
        d = req.to_dict()
        assert d["prompt"] == "Translate academic paper"


class TestTaskProgressEvent:
    def test_defaults(self):
        ev = TaskProgressEvent(task_id="t1", stage="parsing", progress=50.0)
        assert ev.task_id == "t1"
        assert ev.stage == "parsing"
        assert ev.progress == 50.0
        assert ev.current_node_count == 0
        assert ev.message == ""

    def test_to_dict(self):
        ev = TaskProgressEvent(
            task_id="t1", stage="translating", progress=75.0,
            current_node_count=42, diagnostics_count=2, message="Working...",
        )
        d = ev.to_dict()
        assert d["task_id"] == "t1"
        assert d["stage"] == "translating"
        assert d["progress"] == 75.0
        assert d["current_node_count"] == 42


class TestTaskState:
    def test_defaults(self):
        s = TaskState(task_id="t1")
        assert s.task_id == "t1"
        assert s.status == TaskStage.PENDING.value
        assert s.progress == 0.0
        assert s.file_list == []
        assert s.result_files == []

# ═══════════════════════════════════════════════════════════════════════════════
# _TaskStore Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestTaskStore:
    def test_create_and_get(self):
        store = _TaskStore()
        s = store.create_task("t1")
        assert s.task_id == "t1"
        assert store.get_task("t1") is not None
        assert store.get_task("nonexistent") is None

    def test_update_task(self):
        store = _TaskStore()
        store.create_task("t1")
        updated = store.update_task("t1", status="running", progress=50.0)
        assert updated is not None
        assert updated.status == "running"
        assert updated.progress == 50.0

    def test_update_nonexistent(self):
        store = _TaskStore()
        assert store.update_task("nonexistent", status="done") is None

    def test_add_and_get_events(self):
        store = _TaskStore()
        store.create_task("t1")
        e1 = TaskProgressEvent(task_id="t1", stage="parsing", progress=10.0)
        e2 = TaskProgressEvent(task_id="t1", stage="translating", progress=50.0)
        store.add_event("t1", e1)
        store.add_event("t1", e2)
        assert len(store.get_events("t1")) == 2
        assert store.get_events("t1", since=1) == [e2]

    def test_cancel_task(self):
        store = _TaskStore()
        store.create_task("t1")
        assert store.is_cancelled("t1") is False
        store.cancel_task("t1")
        assert store.is_cancelled("t1") is True

    def test_remove_task(self):
        store = _TaskStore()
        store.create_task("t1")
        store.remove_task("t1")
        assert store.get_task("t1") is None

    def test_thread_safety(self):
        store = _TaskStore()
        store.create_task("t1")
        errors: List[Exception] = []

        def writer():
            try:
                for _ in range(50):
                    store.update_task("t1", progress=50.0)
                    e = TaskProgressEvent(task_id="t1", stage="test", progress=50.0)
                    store.add_event("t1", e)
            except Exception as ex:
                errors.append(ex)

        threads = [threading.Thread(target=writer) for _ in range(8)]
        for t in threads: t.start()
        for t in threads: t.join()
        assert not errors
        state = store.get_task("t1")
        assert state is not None


# ═══════════════════════════════════════════════════════════════════════════════
# RuntimeService Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestRuntimeService:
    def test_submit_task(self):
        svc = RuntimeService(ServiceConfig())
        req = TranslationRequest(source_path="/tmp/nonexistent.pdf")
        task_id = svc.submit_task(req)
        assert task_id.startswith("task_")
        state = svc.get_task_state(task_id)
        assert state is not None
        assert state.status in (TaskStage.PENDING.value, TaskStage.FAILED.value)

    def test_get_task_state_nonexistent(self):
        svc = RuntimeService()
        assert svc.get_task_state("nonexistent") is None

    def test_cancel_task(self):
        svc = RuntimeService()
        task_id = svc.submit_task(TranslationRequest(source_path="/tmp/test.pdf"))
        assert svc.cancel_task(task_id) is True
        state = svc.get_task_state(task_id)
        assert state.status == TaskStage.CANCELLED.value

    def test_cancel_nonexistent(self):
        svc = RuntimeService()
        assert svc.cancel_task("nonexistent") is False

    def test_subscribe_events(self):
        svc = RuntimeService()
        task_id = svc.submit_task(TranslationRequest(source_path="/tmp/nonexistent.pdf"))
        time.sleep(1.0)  # wait for background thread to fail
        events = list(svc.subscribe_events(task_id, poll_interval=0.1))
        assert len(events) >= 1

    def test_subscribe_events_nonexistent(self):
        svc = RuntimeService()
        events = list(svc.subscribe_events("nonexistent", poll_interval=0.1))
        assert events == []

    def test_get_queue_position(self):
        svc = RuntimeService()
        assert svc.get_queue_position("nonexistent") == -1


# ═══════════════════════════════════════════════════════════════════════════════
# TaskStage Enum Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestTaskStage:
    def test_values(self):
        assert TaskStage.PENDING.value == "pending"
        assert TaskStage.PARSING.value == "parsing"
        assert TaskStage.TRANSLATING.value == "translating"
        assert TaskStage.COMPLETED.value == "completed"
        assert TaskStage.CANCELLED.value == "cancelled"
        assert TaskStage.FAILED.value == "failed"

    def test_all_stages(self):
        values = [s.value for s in TaskStage]
        for expected in [
            "pending", "parsing", "normalizing", "analyzing", "planning",
            "translating", "layouting", "rendering", "evaluating",
            "repairing", "completed", "cancelled", "failed",
        ]:
            assert expected in values

    def test_to_dict(self):
        s = TaskState(
            task_id="t1", status="completed", progress=100.0,
            result_files=[{"name": "out.pdf", "path": "/tmp/out.pdf"}],
        )
        d = s.to_dict()
        assert d["status"] == "completed"
        assert d["progress"] == 100.0
        assert len(d["result_files"]) == 1


class TestServiceConfig:
    def test_defaults(self):
        cfg = ServiceConfig()
        assert cfg.max_concurrency == 4
        assert cfg.use_v4_engine is False
        assert cfg.output_dir == ""
