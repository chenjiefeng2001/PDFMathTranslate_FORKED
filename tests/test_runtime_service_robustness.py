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
from typing import List

import pytest

import pdf2zh.converter as converter_mod
from pdf2zh.converter import _TRANSLATE_RETRY_ATTEMPTS
from pdf2zh.services.runtime_service import (
    RuntimeService,
    TaskStage,
    TranslationRequest,
)


def _service() -> RuntimeService:
    svc = RuntimeService()
    # 用例直接操作 store，不需要后台清扫线程；构造后立即停止，避免线程泄漏。
    svc.shutdown()
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
                with pytest.MonkeyPatch.context():
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
            svc.shutdown()

    def test_prune_keeps_recent_tasks(self):
        svc = _service()
        try:
            tid = _terminal_task(svc)
            removed = svc._sweep_stale(time.time())
            assert removed == 0
            assert svc.get_task_state(tid) is not None
        finally:
            svc.shutdown()

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
            svc.shutdown()


class TestSweeperLifecycle:
    def test_shutdown_stops_sweeper_thread(self):
        svc = RuntimeService()
        assert svc._sweeper.is_alive()
        svc.shutdown(wait=True)
        assert not svc._sweeper.is_alive()

    def test_shutdown_is_idempotent(self):
        svc = RuntimeService()
        svc.shutdown(wait=True)
        svc.shutdown(wait=True)
        assert not svc._sweeper.is_alive()

    def test_retention_has_a_floor(self):
        """保留期过小会让正在收尾打包的任务被立即清掉。"""
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("PDF2ZH_TASK_RETENTION_SECONDS", "0")
            svc = RuntimeService()
        try:
            assert svc._retention_seconds >= 60.0
        finally:
            svc.shutdown()

    def test_sweep_keeps_zip_of_refreshed_task(self):
        """任务在清扫窗口内被刷新时，其结果 ZIP 不得被误删。"""
        import os
        import tempfile

        svc = _service()
        zip_path = os.path.join(tempfile.gettempdir(), "pdf2zh_task_task_racekept.zip")
        with open(zip_path, "wb") as fh:
            fh.write(b"PK\x05\x06" + b"\x00" * 18)
        try:
            tid = "task_racekept"
            svc._store.create_task(tid)
            svc._store.update_task(
                tid,
                status=TaskStage.COMPLETED.value,
                result_zip=zip_path,
            )
            state = svc._store.get_task(tid)
            assert state is not None
            state.updated_at = time.time() - 7200
            # Simulate a concurrent refresh landing between the sweeper's
            # candidate scan and the prune: the task survives, so must its zip.
            svc._store.update_task(tid, message="refreshed")
            svc._sweep_stale(time.time())
            assert svc.get_task_state(tid) is not None
            assert os.path.exists(zip_path)
        finally:
            svc.shutdown()
            if os.path.exists(zip_path):
                os.unlink(zip_path)

    def test_sweep_removes_zip_of_pruned_task(self):
        import os
        import tempfile

        svc = _service()
        zip_path = os.path.join(tempfile.gettempdir(), "pdf2zh_task_task_racegone.zip")
        with open(zip_path, "wb") as fh:
            fh.write(b"PK\x05\x06" + b"\x00" * 18)
        try:
            tid = "task_racegone"
            svc._store.create_task(tid)
            svc._store.update_task(
                tid,
                status=TaskStage.COMPLETED.value,
                result_zip=zip_path,
            )
            svc._store.get_task(tid).updated_at = time.time() - 7200
            svc._sweep_stale(time.time())
            assert svc.get_task_state(tid) is None
            assert not os.path.exists(zip_path)
        finally:
            svc.shutdown()
            if os.path.exists(zip_path):
                os.unlink(zip_path)


class TestEventCursor:
    def test_cursor_advances_correctly_after_ring_trim(self):
        """环形缓冲裁剪后游标必须用绝对序号推进，否则事件被反复重放。"""
        from pdf2zh.services.runtime_service import (
            _EVENT_RING_SIZE,
            TaskProgressEvent,
            _TaskStore,
        )

        store = _TaskStore()
        store.create_task("t_cursor")
        for i in range(_EVENT_RING_SIZE + 10):
            store.add_event(
                "t_cursor", TaskProgressEvent(task_id="t_cursor", stage="s", progress=i)
            )

        seen = []
        cursor = 0
        for _ in range(4):
            events, cursor = store.get_events_with_cursor("t_cursor", since=cursor)
            seen.extend(e.progress for e in events)
        assert seen == sorted(seen)
        assert len(seen) == len(set(seen))  # no duplicates across polls
        events, cursor2 = store.get_events_with_cursor("t_cursor", since=cursor)
        assert events == []  # fully drained


class TestSubmitDedupWindow:
    def test_concurrent_identical_submits_share_one_task(self):
        """指纹登记与任务创建之间不得存在可被重复提交的窗口。

        若 create_task 在去重锁之外执行，第二个并发提交会看到指纹却查不到任务，
        从而另起一个重复任务（同一份文件被翻译两次）。
        """
        svc = _service()
        try:
            svc._execute_task = lambda *a, **k: None  # keep the task in flight
            gate = threading.Event()
            real_create = svc._store.create_task

            def _slow_create(task_id):
                gate.wait(5.0)
                real_create(task_id)

            svc._store.create_task = _slow_create  # type: ignore[method-assign]
            req = TranslationRequest(source_path="/tmp/dedup-concurrent.pdf")

            first: List[str] = []
            t1 = threading.Thread(target=lambda: first.append(svc.submit_task(req)))
            t1.start()
            time.sleep(0.2)  # first submit is now parked inside create_task

            second: List[str] = []
            t2 = threading.Thread(target=lambda: second.append(svc.submit_task(req)))
            t2.start()
            time.sleep(0.2)
            gate.set()
            t1.join(5)
            t2.join(5)

            assert first and second
            assert first[0] == second[0]
        finally:
            svc.shutdown()


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
            svc.shutdown()

    def test_cancel_event_plumbing(self):
        svc = _service()
        try:
            tid = _terminal_task(svc, status=TaskStage.PARSING.value)
            ev = svc._store.get_cancel_event(tid)
            assert isinstance(ev, threading.Event) and not ev.is_set()
            svc.cancel_task(tid)
            assert ev.is_set()
        finally:
            svc.shutdown()


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


class TestLongResultFilenameHash:
    """下载文件名过长修复：超长结果文件名改写为 哈希+特殊命名。

    Issue: 下载文件名 = 磁盘 basename，源文件名超长（如几百字标题）时
    ``{stem}-mono.pdf`` 超过浏览器/OS 文件名上限导致完全不可用。修复：
    ``_complete_file`` 统一把超长结果文件名改写为 ``{hash}-{suffix}``，
    短名保持可读原样（不影响既有行为）。
    """

    def test_short_result_name_rewrites_long_names(self):
        svc = _service()
        try:
            long_stem = "a" * 200
            mono = svc._short_result_name(f"{long_stem}-mono.pdf", "/tmp/src.pdf")
            assert len(mono) <= 32
            assert mono.endswith("-mono.pdf")
            assert mono != f"{long_stem}-mono.pdf"
            # JSON 转储无 -mono/-dual 后缀时只保留扩展名
            dump = svc._short_result_name(f"{long_stem}.json", "/tmp/src.pdf")
            assert dump.endswith(".json")
            assert len(dump) <= 20
            # 短名保持原样
            assert (
                svc._short_result_name("doc-mono.pdf", "/tmp/src.pdf") == "doc-mono.pdf"
            )
            # 同一源路径 -> 确定性哈希（重试/缓存友好）
            a = svc._short_result_name(f"{long_stem}-dual.pdf", "/tmp/src.pdf")
            b = svc._short_result_name(f"{long_stem}-dual.pdf", "/tmp/src.pdf")
            assert a == b
            # 不同源路径 -> 不同哈希（批量任务互不覆盖）
            c = svc._short_result_name(f"{long_stem}-dual.pdf", "/tmp/other.pdf")
            assert a != c
        finally:
            svc.shutdown()

    def test_complete_file_renames_and_remaps_preview(self, tmp_path):
        svc = _service()
        try:
            tid = "task_longname"
            svc._store.create_task(tid)
            long_stem = "T" * 180
            mono = tmp_path / f"{long_stem}-mono.pdf"
            dual = tmp_path / f"{long_stem}-dual.pdf"
            mono.write_bytes(b"%PDF-1.7 fake mono")
            dual.write_bytes(b"%PDF-1.7 fake dual")
            svc._complete_file(
                tid,
                [
                    {"name": mono.name, "path": str(mono)},
                    {"name": dual.name, "path": str(dual)},
                ],
                selected_file=mono.name,
                preview_path=str(dual),
                message="Completed",
            )
            ts = svc.get_task_state(tid)
            assert ts.status == TaskStage.COMPLETED.value
            new_names = [rf["name"] for rf in ts.result_files]
            assert all(len(n) <= 32 for n in new_names)
            assert any(n.endswith("-mono.pdf") for n in new_names)
            assert any(n.endswith("-dual.pdf") for n in new_names)
            # 磁盘上的原文件名已被改写
            assert not mono.exists() and not dual.exists()
            for rf in ts.result_files:
                assert os.path.exists(rf["path"])
            # preview_path / selected_file 已重映射到改写后的路径/名称
            dual_entry = next(
                rf for rf in ts.result_files if rf["name"].endswith("-dual.pdf")
            )
            assert ts.preview_path == dual_entry["path"]
            assert ts.selected_file == new_names[0]
        finally:
            svc.shutdown()
