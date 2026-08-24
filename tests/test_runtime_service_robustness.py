"""Robustness regression tests for the S1/S2 long-run protections.

S1: converter translation retry must be bounded (no infinite retry loops
    that stall a task in the "translating" stage forever).
S2: terminated-task store / aggregator / batch / progress maps must be
    pruned so long-running service processes do not accumulate memory.
Also: cancel requests must be plumbed to the pipeline's cancellation
hook (late completions must not resurrect a cancelled task).

NOTE: tasks are created directly through the store (no background worker
thread) so the tests are deterministic and thread-race-free.
"""

import os
import threading
import time

import pytest

import pdf2zh.converter as converter_mod
from pdf2zh.converter import _TRANSLATE_RETRY_ATTEMPTS
from pdf2zh.services.runtime_service import (
    RuntimeService,
    TaskStage,
)
from pdf2zh.services.runtime_service import (
    RuntimeService,
    TaskStage,
    TranslationRequest,
)


def _service() -> RuntimeService:
    svc = RuntimeService()
    return svc


def _terminal_task(svc: RuntimeService, status: str = TaskStage.FAILED.value) -> str:
    tid = "task_robust"
    svc._store.create_task(tid)
    svc._store.update_task(tid, status=status)
    return tid


class TestS1BoundedTranslateRetry:
    def test_retry_attempts_default_is_bounded(self):
        assert 1 <= _TRANSLATE_RETRY_ATTEMPTS <= 10

    def test_retry_attempts_respects_env_override(self):
        # NOTE: exercise the resolver function directly -- reloading the
        # module would rebind TranslateConverter and break other test files'
        # patch targets in the same pytest process.
        cases = (
            ("1", 1),
            ("7", 7),
            ("0", 3),  # non-positive -> default
            ("-3", 3),  # non-positive -> default
            ("garbage", 3),  # unparseable -> default
        )
        missing = os.environ.pop("PDF2ZH_TRANSLATE_RETRY", None)
        try:
            for value, expected in cases:
                with pytest.MonkeyPatch.context() as mp:
                    os.environ["PDF2ZH_TRANSLATE_RETRY"] = value
                    assert converter_mod._translate_retry_attempts() == expected
        finally:
            if missing is None:
                os.environ.pop("PDF2ZH_TRANSLATE_RETRY", None)
            else:
                os.environ["PDF2ZH_TRANSLATE_RETRY"] = missing


class TestS2StorePruning:
    def test_prune_removes_old_terminated_tasks(self):
        svc = _service()
        try:
            tid = _terminal_task(svc)
            assert svc.get_task_state(tid) is not None
            state = svc._store.get_task(tid)
            state.updated_at = time.time() - 7200  # beyond retention
            removed = svc._sweep_stale(time.time())
            assert removed == 1
            assert svc.get_task_state(tid) is None
            assert tid not in svc._aggregators
            assert tid not in svc._last_progress
        finally:
            svc._sweeper = None

    def test_prune_keeps_recent_tasks(self):
        svc = _service()
        try:
            tid = _terminal_task(svc)
            removed = svc._sweep_stale(time.time())
            assert removed == 0
            assert svc.get_task_state(tid) is not None
        finally:
            svc._sweeper = None

    def test_prune_keeps_running_tasks(self):
        svc = _service()
        try:
            tid = _terminal_task(svc, status=TaskStage.TRANSLATING.value)
            state = svc._store.get_task(tid)
            state.updated_at = time.time() - 7200  # old but still RUNNING
            removed = svc._sweep_stale(time.time())
            assert removed == 0
            assert svc.get_task_state(tid) is not None
        finally:
            svc._sweeper = None


class TestTaskCancellation:
    def test_late_completion_after_cancel_is_dropped(self):
        svc = _service()
        try:
            tid = _terminal_task(svc, status=TaskStage.PARSING.value)
            svc.cancel_task(tid)
            # A worker finishing after the cancel must not resurrect the task.
            svc._complete_file(
                tid,
                [{"name": "x-mono.pdf", "path": "x-mono.pdf"}],
                message="Completed (Legacy)",
            )
            state = svc.get_task_state(tid)
            assert state.status == TaskStage.CANCELLED.value
            assert not (state.result_files or [])
        finally:
            svc._sweeper = None

    def test_cancel_event_plumbing(self):
        svc = _service()
        try:
            tid = _terminal_task(svc, status=TaskStage.PARSING.value)
            ev = svc._store.get_cancel_event(tid)
            assert isinstance(ev, threading.Event) and not ev.is_set()
            svc.cancel_task(tid)
            assert ev.is_set()
        finally:
            svc._sweeper = None


class TestExecuteTaskKeyboardInterrupt:
    """V3-4：Ctrl+C 时 KeyboardInterrupt 在后台翻译线程按“用户取消”落终态。

    回归用户日志：GUI 下 coordinator 短路抛 KeyboardInterrupt 后逃逸到
    ``_execute_task`` 线程顶层（``Exception in thread Thread-5``）。现在
    ``_execute_task`` 显式捕获并按 CANCELLED 落终态 —— 不打印线程级未处理
    异常，也不误判为 FAILED。
    """

    def test_keyboard_interrupt_marks_task_cancelled(self, monkeypatch):
        svc = _service()
        tid = "task_kbint_legacy"
        svc._store.create_task(tid)
        req = TranslationRequest(source_path="dummy.pdf", files=[])

        def _boom(*a, **k):
            raise KeyboardInterrupt("Parallel engine aborted: Ctrl+C received")

        monkeypatch.setattr(svc, "_execute_legacy", _boom)
        svc._execute_task(tid, req)
        state = svc._store.get_task(tid)
        assert state is not None
        assert state.status == TaskStage.CANCELLED.value
        assert state.error_message == "Interrupted by user"

    def test_ordinary_exception_still_fails_task(self, monkeypatch):
        """非中断异常语义不变：仍 FAILED。"""
        svc = _service()
        tid = "task_err_legacy"
        svc._store.create_task(tid)
        req = TranslationRequest(source_path="dummy.pdf", files=[])

        def _boom(*a, **k):
            raise RuntimeError("boom")

        monkeypatch.setattr(svc, "_execute_legacy", _boom)
        svc._execute_task(tid, req)
        state = svc._store.get_task(tid)
        assert state.status == TaskStage.FAILED.value

    def test_batch_keyboard_interrupt_propagates_to_cancelled(self, monkeypatch):
        """batch 模式：单文件循环内的 KeyboardInterrupt 中断整批并按 CANCELLED 落终态。"""
        svc = _service()
        tid = "task_kbint_batch"
        svc._store.create_task(tid)
        req = TranslationRequest(source_path="", files=["a.pdf", "b.pdf"])

        def _boom(*a, **k):
            raise KeyboardInterrupt("aborted")

        monkeypatch.setattr(svc, "_execute_legacy", _boom)
        svc._execute_task(tid, req)
        state = svc._store.get_task(tid)
        assert state.status == TaskStage.CANCELLED.value
