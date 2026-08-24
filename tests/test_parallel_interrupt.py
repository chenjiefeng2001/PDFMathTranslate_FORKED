"""Ctrl+C 旗标 + worker SIGINT 免疫 + coordinator 三处短路 单测（V3-3 迭代）。

回归用户日志场景：Windows GUI 下 gradio 在主线程吞掉 KeyboardInterrupt、
控制台 CTRL_C_EVENT 却广播杀死正在加载模型的 worker → BrokenProcessPool →
WorkerBootstrapError → 整文档串行兜底（Ctrl+C 反而触发最长执行路径）。

本测试覆盖：
- ``pdf2zh.parallel.interrupt``：旗标默认态 / 置位 / 复位 / handler 语义
  （记旗标后**仅第一次**抛 KeyboardInterrupt，不改变 gradio/CLI 关闭行为；
  解释器关闭期绝不抛）；
- ``pdf2zh.parallel.worker._ignore_ctrl_c_in_worker``：worker 对 SIGINT 免疫；
- ``TaskCoordinator``：提交前 / 运行中轮询 / 池崩三处遇中断旗标立即
  KeyboardInterrupt 短路（绝不进入串行兜底）；未中断时池崩语义不变。
"""

import concurrent.futures
import signal
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from concurrent.futures.process import BrokenProcessPool

from pdf2zh.parallel.chunk import ChunkResult, ChunkTask
from pdf2zh.parallel.coordinator import TaskCoordinator
from pdf2zh.parallel.errors import WorkerBootstrapError
from pdf2zh.parallel import interrupt as interrupt_mod
from pdf2zh.parallel import worker as parallel_worker


# ── 测试工具 ────────────────────────────────────────────────────────────
class ThreadedExecutor:
    """线程池 executor（避免 coordinator 测试真实 spawn 进程）。"""

    def __init__(self, max_workers, initializer, initargs):
        self.inner = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)

    def submit(self, fn, *args, **kwargs):
        return self.inner.submit(fn, *args, **kwargs)

    def shutdown(self, wait=False, cancel_futures=False):
        return self.inner.shutdown(wait=wait, cancel_futures=cancel_futures)


class PreFailedFuture(concurrent.futures.Future):
    """构造即已完成（带异常）的 future，用于模拟池 broken。"""

    def __init__(self, exc):
        super().__init__()
        self.set_exception(exc)


class BrokenPoolExecutor:
    """每个 submit 都返回 BrokenProcessPool —— 模拟“worker 加载中途全部被杀”。"""

    def __init__(self, max_workers, initializer, initargs):
        self.shutdown_called = False

    def submit(self, fn, *a, **k):
        return PreFailedFuture(BrokenProcessPool("worker died during init"))

    def shutdown(self, wait=False, cancel_futures=False):
        self.shutdown_called = True


def _tasks(count):
    return [ChunkTask(chunk_pages=(i,), fp_bytes=b"") for i in range(count)]


def _thread_executor_factory(max_workers, initializer, initargs):
    return ThreadedExecutor(max_workers, initializer, initargs)


# ── 旗标模块 ────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _clean_interrupt_flag():
    """每个测试前后复位进程级中断旗标，避免污染其他 coordinator 测试。"""
    interrupt_mod.reset_interrupt_flag()
    yield
    interrupt_mod.reset_interrupt_flag()


class TestInterruptFlag:
    def test_default_false(self):
        assert interrupt_mod.is_interrupted() is False

    def test_mark_and_reset(self):
        interrupt_mod.mark_interrupted()
        assert interrupt_mod.is_interrupted() is True
        interrupt_mod.reset_interrupt_flag()
        assert interrupt_mod.is_interrupted() is False

    def test_install_guard_installs_handler(self):
        old = signal.getsignal(signal.SIGINT)
        try:
            interrupt_mod.install_interrupt_guard()
            assert signal.getsignal(signal.SIGINT) == interrupt_mod._on_sigint
        finally:
            signal.signal(signal.SIGINT, old)

    def test_install_guard_idempotent(self):
        old = signal.getsignal(signal.SIGINT)
        try:
            interrupt_mod.install_interrupt_guard()
            first = signal.getsignal(signal.SIGINT)
            interrupt_mod.install_interrupt_guard()
            assert signal.getsignal(signal.SIGINT) is first
        finally:
            signal.signal(signal.SIGINT, old)

    def test_handler_sets_flag_and_raises_keyboard_interrupt(self):
        """handler 语义：置位旗标后继续抛 KeyboardInterrupt（不改变关闭行为）。"""
        interrupt_mod.reset_interrupt_flag()
        with pytest.raises(KeyboardInterrupt):
            interrupt_mod._on_sigint(signal.SIGINT, None)

    def test_handler_raises_only_once(self):
        """handler 只抛一次：第一次 KeyboardInterrupt 触发关闭；后续 Ctrl+C 只置旗标
        （否则 server.close / thread.join / atexit 清理会被二次打断）。"""
        interrupt_mod.reset_interrupt_flag()
        with pytest.raises(KeyboardInterrupt):
            interrupt_mod._on_sigint(signal.SIGINT, None)
        # 第二次（关闭流程中）：不抛，只置位
        interrupt_mod._on_sigint(signal.SIGINT, None)  # 不应抛异常
        assert interrupt_mod.is_interrupted() is True
        # reset 后重新武装（测试隔离）
        interrupt_mod.reset_interrupt_flag()
        with pytest.raises(KeyboardInterrupt):
            interrupt_mod._on_sigint(signal.SIGINT, None)

    def test_handler_finalizing_never_raises(self, monkeypatch):
        """解释器关闭期（atexit / threading shutdown）handler 绝不抛异常：
        ``concurrent.futures`` 的进程清理 join 不能被 KeyboardInterrupt 打断。"""
        interrupt_mod.reset_interrupt_flag()
        monkeypatch.setattr(interrupt_mod, "_interpreter_is_finalizing", lambda: True)
        interrupt_mod._on_sigint(signal.SIGINT, None)  # 不应抛
        assert interrupt_mod.is_interrupted() is True
        # 关闭期不消耗“只抛一次”守卫：结束后仍可正常触发一次
        monkeypatch.setattr(interrupt_mod, "_interpreter_is_finalizing", lambda: False)
        with pytest.raises(KeyboardInterrupt):
            interrupt_mod._on_sigint(signal.SIGINT, None)

    def test_reset_rearms_raise_guard(self):
        """reset_interrupt_flag 同时复位“只抛一次”守卫。"""
        interrupt_mod.reset_interrupt_flag()
        with pytest.raises(KeyboardInterrupt):
            interrupt_mod._on_sigint(signal.SIGINT, None)
        interrupt_mod._on_sigint(signal.SIGINT, None)  # 第二次不抛
        assert interrupt_mod.is_interrupted() is True
        interrupt_mod.reset_interrupt_flag()
        assert interrupt_mod.is_interrupted() is False
        with pytest.raises(KeyboardInterrupt):
            interrupt_mod._on_sigint(signal.SIGINT, None)

        assert interrupt_mod.is_interrupted() is True

    def test_is_interrupted_thread_safe(self):
        """后台线程读取旗标（coordinator 的真实读取路径）。"""
        result = {}

        def reader():
            # 短轮询：主线程置位前 False、置位后 True
            for _ in range(100):
                if interrupt_mod.is_interrupted():
                    result["seen"] = True
                    return
                time.sleep(0.01)
            result["seen"] = False

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        time.sleep(0.05)
        interrupt_mod.mark_interrupted()
        t.join(2.0)
        assert result.get("seen") is True

    def test_cancel_only_task_running_never_exits(self):
        # GUI cancel_only + 任务运行中（_exit_armed False）：任何次数 Ctrl+C 都
        # 不抛 KeyboardInterrupt（应用不退出），只取消任务 —— 防止 Windows 终端
        # 对单次 Ctrl+C 重复投递事件导致“没按却退出”。
        old_mode = interrupt_mod._cancel_only_mode
        try:
            interrupt_mod.reset_interrupt_flag()
            interrupt_mod._cancel_only_mode = True
            interrupt_mod._on_sigint(signal.SIGINT, None)  # 第一次：取消任务
            assert interrupt_mod.is_interrupted() is True
            assert interrupt_mod._first_ctrl_c_handled is True
            assert interrupt_mod._exit_armed is False
            # 任务尚未落终态：继续多次也不抛（不会关闭应用）
            interrupt_mod._on_sigint(signal.SIGINT, None)
            interrupt_mod._on_sigint(signal.SIGINT, None)
            assert interrupt_mod.is_interrupted() is True
            assert interrupt_mod._exit_armed is False
        finally:
            interrupt_mod._cancel_only_mode = old_mode
            interrupt_mod.reset_interrupt_flag()

    def test_cancel_only_exit_armed_raises(self):
        # 任务已落终态（mark_exit_pending → _exit_armed=True）：下一次 Ctrl+C
        # 抛 KeyboardInterrupt（用户主动关闭应用）。
        old_mode = interrupt_mod._cancel_only_mode
        try:
            interrupt_mod.reset_interrupt_flag()
            interrupt_mod._cancel_only_mode = True
            interrupt_mod.mark_exit_pending()
            assert interrupt_mod._exit_armed is True
            with pytest.raises(KeyboardInterrupt):
                interrupt_mod._on_sigint(signal.SIGINT, None)
            # 关闭流程中：不再抛
            interrupt_mod._on_sigint(signal.SIGINT, None)
        finally:
            interrupt_mod._cancel_only_mode = old_mode
            interrupt_mod.reset_interrupt_flag()

    def test_cancel_only_normal_completion_ctrl_c_immediate(self):
        # 正常完成（未收到过 Ctrl+C）：mark_exit_pending 清空防抖时间戳，
        # 任务结束后的第一次 Ctrl+C 立即生效（不被 0.8s 防抖吞掉）。
        old_mode = interrupt_mod._cancel_only_mode
        try:
            interrupt_mod.reset_interrupt_flag()
            interrupt_mod._cancel_only_mode = True
            interrupt_mod._last_sigint_ts = time.monotonic()  # 模拟刚收到过事件
            interrupt_mod.mark_exit_pending()  # 未置旗标 → 清空防抖
            assert interrupt_mod._last_sigint_ts == 0.0
            with pytest.raises(KeyboardInterrupt):
                interrupt_mod._on_sigint(signal.SIGINT, None)
        finally:
            interrupt_mod._cancel_only_mode = old_mode
            interrupt_mod.reset_interrupt_flag()

    def test_cancel_only_cancelled_completion_keeps_debounce(self):
        # 取消完成（刚收到过 Ctrl+C，旗标已置位）：mark_exit_pending 保留防抖
        # 时间戳，取消瞬间终端的重复事件投递被合并、不误关闭；窗口外才关闭。
        old_mode = interrupt_mod._cancel_only_mode
        try:
            interrupt_mod.reset_interrupt_flag()
            interrupt_mod._cancel_only_mode = True
            interrupt_mod._on_sigint(signal.SIGINT, None)  # 用户取消任务
            assert interrupt_mod.is_interrupted() is True
            interrupt_mod.mark_exit_pending()
            assert interrupt_mod._exit_armed is True
            # 取消后 0.8s 内终端的重复投递：不抛（防抖延续）
            interrupt_mod._on_sigint(signal.SIGINT, None)
            assert not interrupt_mod._raise_once.is_set()
            # 窗口外（>0.8s）的下一次：关闭
            interrupt_mod._last_sigint_ts = 0.0
            with pytest.raises(KeyboardInterrupt):
                interrupt_mod._on_sigint(signal.SIGINT, None)
        finally:
            interrupt_mod._cancel_only_mode = old_mode
            interrupt_mod.reset_interrupt_flag()

    def test_cancel_only_debounce_merges_repeated_events(self):
        # Windows 终端对单次 Ctrl+C 可能重复投递：0.8s 窗口内的第二次事件被合并
        # （即使空闲态 _exit_armed 已置位，也不会把“同一次按键”当成第二次关闭）。
        old_mode = interrupt_mod._cancel_only_mode
        try:
            interrupt_mod.reset_interrupt_flag()
            interrupt_mod._cancel_only_mode = True
            interrupt_mod.mark_exit_pending()  # 空闲态：正常下次按即关闭
            with pytest.raises(KeyboardInterrupt):
                interrupt_mod._on_sigint(signal.SIGINT, None)  # 用户按一次：关闭
            # 终端重复投递同一事件（防抖窗口内）：不抛、不打断关闭流程
            interrupt_mod._on_sigint(signal.SIGINT, None)
        finally:
            interrupt_mod._cancel_only_mode = old_mode
            interrupt_mod.reset_interrupt_flag()

    def test_cancel_only_reset_rearms(self):
        # cancel-only 下 reset 后重新武装：下一次 Ctrl+C 又是第一次（只取消不退出）。
        old_mode = interrupt_mod._cancel_only_mode
        try:
            interrupt_mod.reset_interrupt_flag()
            interrupt_mod._cancel_only_mode = True
            interrupt_mod._on_sigint(signal.SIGINT, None)
            assert interrupt_mod._first_ctrl_c_handled is True
            interrupt_mod.reset_interrupt_flag()
            assert interrupt_mod._first_ctrl_c_handled is False
            interrupt_mod._on_sigint(signal.SIGINT, None)  # 重新武装后的第一次：不抛
            assert interrupt_mod.is_interrupted() is True
        finally:
            interrupt_mod._cancel_only_mode = old_mode
            interrupt_mod.reset_interrupt_flag()

    def test_cancel_only_finalizing_never_raises(self, monkeypatch):
        # cancel-only 模式 + 解释器关闭期：绝不抛（先置旗标直接返回）。
        old_mode = interrupt_mod._cancel_only_mode
        try:
            interrupt_mod.reset_interrupt_flag()
            interrupt_mod._cancel_only_mode = True
            monkeypatch.setattr(
                interrupt_mod, "_interpreter_is_finalizing", lambda: True
            )
            interrupt_mod._on_sigint(signal.SIGINT, None)
            assert interrupt_mod.is_interrupted() is True
        finally:
            interrupt_mod._cancel_only_mode = old_mode
            interrupt_mod.reset_interrupt_flag()

    def test_install_guard_sets_cancel_only_mode(self):
        # install_interrupt_guard(cancel_only=...) 持久化模式标记。
        old_mode = interrupt_mod._cancel_only_mode
        try:
            interrupt_mod.install_interrupt_guard(cancel_only=True)
            assert interrupt_mod._cancel_only_mode is True
            interrupt_mod.install_interrupt_guard(cancel_only=False)
            assert interrupt_mod._cancel_only_mode is False
        finally:
            interrupt_mod._cancel_only_mode = old_mode
            interrupt_mod.reset_interrupt_flag()

    def test_mark_exit_pending_allows_exit(self):
        # 任务落终态后 mark_exit_pending：空闲状态下一次 Ctrl+C 即关闭（不取消模式）。
        old_mode = interrupt_mod._cancel_only_mode
        try:
            interrupt_mod.reset_interrupt_flag()
            interrupt_mod._cancel_only_mode = True
            interrupt_mod.mark_exit_pending()
            assert interrupt_mod._first_ctrl_c_handled is True
            with pytest.raises(KeyboardInterrupt):
                interrupt_mod._on_sigint(signal.SIGINT, None)  # 空闲态第一次即关闭
            interrupt_mod._on_sigint(signal.SIGINT, None)  # 关闭流程中不抛
        finally:
            interrupt_mod._cancel_only_mode = old_mode
            interrupt_mod.reset_interrupt_flag()

    def test_mark_exit_pending_cleared_by_reset(self):
        # 新任务提交（on_translate reset）清除 mark_exit_pending：回到运行中语义。
        old_mode = interrupt_mod._cancel_only_mode
        try:
            interrupt_mod.reset_interrupt_flag()
            interrupt_mod._cancel_only_mode = True
            interrupt_mod.mark_exit_pending()
            interrupt_mod.reset_interrupt_flag()
            assert interrupt_mod._first_ctrl_c_handled is False
            interrupt_mod._on_sigint(signal.SIGINT, None)  # 新任务运行中第一次：只取消
            assert interrupt_mod.is_interrupted() is True
            assert interrupt_mod._first_ctrl_c_handled is True
        finally:
            interrupt_mod._cancel_only_mode = old_mode
            interrupt_mod.reset_interrupt_flag()

    def test_execute_task_finally_marks_exit_pending(self, monkeypatch):
        # runtime_service._execute_task 落终态后 finally 调 mark_exit_pending：
        # 任务完成/取消/失败后，下一次 Ctrl+C 直接退出。
        from pdf2zh.services import runtime_service as rs
        from pdf2zh.services.runtime_service import TaskStage

        import types as _types

        class _FakeStore:
            def __init__(self, terminal_status):
                self._status = terminal_status

            def create_task(self, task_id):
                pass

            def get_task(self, task_id):
                return _types.SimpleNamespace(status=self._status)

            def is_cancelled(self, task_id):
                return False

            def get_cancel_event(self, task_id):
                return None

            def update_task(self, task_id, **kwargs):
                pass

            def add_event(self, task_id, event):
                pass

        from pdf2zh.parallel import interrupt as im

        old_mode = im._cancel_only_mode
        try:
            im.reset_interrupt_flag()
            im._cancel_only_mode = True
            svc = rs.RuntimeService(config=None)
            svc._store = _FakeStore(TaskStage.COMPLETED.value)
            monkeypatch.setattr(svc, "_emit_event", lambda *a, **k: None)
            monkeypatch.setattr(svc, "_sync_feature_flags", lambda *a, **k: None)
            monkeypatch.setattr(
                rs,
                "resolve_mode_config",
                lambda mode, config: _types.SimpleNamespace(use_v4_engine=False),
            )
            monkeypatch.setattr(svc, "_execute_legacy", lambda *a, **k: None)
            svc._execute_task(
                "t_terminal",
                _types.SimpleNamespace(
                    extra_config={"mode_choice": "auto"},
                    resolved_files=lambda: [1],
                ),
            )
            assert im._first_ctrl_c_handled is True
            with pytest.raises(KeyboardInterrupt):
                im._on_sigint(signal.SIGINT, None)  # 空闲态一次即关闭
        finally:
            im._cancel_only_mode = old_mode
            im.reset_interrupt_flag()


# ── worker SIGINT 免疫 ──────────────────────────────────────────────────
class TestWorkerCtrlCIgnore:
    def test_ignore_ctrl_c_sets_sig_ign(self):
        old = signal.getsignal(signal.SIGINT)
        try:
            parallel_worker._ignore_ctrl_c_in_worker()
            assert signal.getsignal(signal.SIGINT) is signal.SIG_IGN
        finally:
            signal.signal(signal.SIGINT, old)

    def test_ignore_ctrl_c_idempotent(self):
        old = signal.getsignal(signal.SIGINT)
        try:
            parallel_worker._ignore_ctrl_c_in_worker()
            parallel_worker._ignore_ctrl_c_in_worker()
            assert signal.getsignal(signal.SIGINT) is signal.SIG_IGN
        finally:
            signal.signal(signal.SIGINT, old)


# ── coordinator 三处短路 ────────────────────────────────────────────────
class TestCoordinatorInterruptShortCircuit:
    def test_aborts_before_submit_when_interrupted(self):
        """提交前已置位 → 首个 _submit 即 KeyboardInterrupt，池被 shutdown。"""
        interrupt_mod.mark_interrupted()
        executor = BrokenPoolExecutor(2, None, ())
        coord = TaskCoordinator(max_workers=2)
        with pytest.raises(KeyboardInterrupt):
            coord.run(
                _tasks(4),
                executor_factory=lambda w, i, a: executor,
                task_fn=lambda t: ChunkResult(obj_patch={}),
            )
        assert executor.shutdown_called is True

    def test_aborts_inflight_poll_when_interrupted(self):
        """chunk 运行中按 Ctrl+C → wait 轮询（0.5s 粒度）短路，不等 chunk 结束。"""

        def slow_fn(task):
            time.sleep(3.0)  # 比轮询粒度长得多：中断必须先于 chunk 完成
            return ChunkResult(obj_patch={task.chunk_pages[0]: "x"})

        def late_setter():
            time.sleep(0.2)
            interrupt_mod.mark_interrupted()

        t = threading.Thread(target=late_setter, daemon=True)
        t.start()
        coord = TaskCoordinator(max_workers=2)
        t0 = time.monotonic()
        with pytest.raises(KeyboardInterrupt):
            coord.run(
                _tasks(4), executor_factory=_thread_executor_factory, task_fn=slow_fn
            )
        elapsed = time.monotonic() - t0
        assert elapsed < 2.0  # 0.2s 置位 + 0.5s 轮询 → 远早于 3s 的 chunk 完成

    def test_aborts_pool_broken_when_interrupted(self):
        """回归用户日志：池崩 + 已按 Ctrl+C → KeyboardInterrupt（绝不 WorkerBootstrapError）。"""
        interrupt_mod.mark_interrupted()
        coord = TaskCoordinator(max_workers=2)
        with pytest.raises(KeyboardInterrupt):
            coord.run(
                _tasks(3), executor_factory=lambda w, i, a: BrokenPoolExecutor(w, i, a)
            )

    def test_pool_broken_without_interrupt_keeps_bootstrap_semantics(self):
        """未中断时池崩语义不变：仍抛 WorkerBootstrapError（整体串行兜底入口）。"""
        coord = TaskCoordinator(max_workers=2)
        with pytest.raises(WorkerBootstrapError):
            coord.run(
                _tasks(3), executor_factory=lambda w, i, a: BrokenPoolExecutor(w, i, a)
            )

    def test_normal_run_unaffected_by_flag_module(self):
        """旗标默认 False：正常任务完整跑完，不进任何短路分支。"""

        def ok_fn(task):
            return ChunkResult(obj_patch={task.chunk_pages[0]: "ok"})

        coord = TaskCoordinator(max_workers=2)
        obj, obs, serial = coord.run(
            _tasks(4), executor_factory=_thread_executor_factory, task_fn=ok_fn
        )
        assert serial == []
        assert sorted(obj) == [0, 1, 2, 3]
        assert not obs


# ── 中断即终止 worker（V3-5：Ctrl+C 后不再跑完 chunk 才退出）──────────────────
class TestForceTerminateWorkers:
    """``_force_terminate_workers``：中断路径硬杀所有存活 worker。

    回归用户 2026-08-10 21:46 日志：Ctrl+C 后任务正确落 CANCELLED，但 4 个
    worker 仍各跑完 54 页 chunk（2:15~2:19）才退出 —— ``shutdown(cancel_futures)``
    只能取消未开始的 future。现在中断路径直接 terminate worker 进程。
    """

    def test_force_terminate_skips_missing_processes(self):
        """假 executor（无 ``_processes``，如测试注入的线程池）→ 0 且不抛。"""
        assert TaskCoordinator._force_terminate_workers(MagicMock()) == 0
        assert TaskCoordinator._force_terminate_workers(SimpleNamespace()) == 0

    def test_force_terminate_kills_only_alive(self):
        class FakeProc:
            def __init__(self, alive):
                self.alive = alive
                self.terminated = False

            def is_alive(self):
                return self.alive

            def terminate(self):
                self.terminated = True

        a1, a2, dead = FakeProc(True), FakeProc(True), FakeProc(False)
        executor = SimpleNamespace(_processes={1: a1, 2: dead, 3: a2})
        n = TaskCoordinator._force_terminate_workers(executor)
        assert n == 2
        assert a1.terminated and a2.terminated
        assert not dead.terminated

    def test_interrupt_path_calls_force_terminate(self, monkeypatch):
        """旗标短路路径：_force_terminate_workers 被调用（随后才 shutdown）。"""
        calls = []
        monkeypatch.setattr(
            TaskCoordinator,
            "_force_terminate_workers",
            staticmethod(lambda executor: calls.append(executor) or 0),
        )
        interrupt_mod.mark_interrupted()
        coord = TaskCoordinator(max_workers=2)
        with pytest.raises(KeyboardInterrupt):
            coord.run(
                _tasks(3), executor_factory=lambda w, i, a: BrokenPoolExecutor(w, i, a)
            )
        assert len(calls) == 1

    def test_direct_keyboard_interrupt_also_terminates(self, monkeypatch):
        """不经旗标、直接抛 KeyboardInterrupt（异常传播路径）同样 terminate。"""

        def boom_fn(task):
            raise KeyboardInterrupt("direct")

        calls = []
        monkeypatch.setattr(
            TaskCoordinator,
            "_force_terminate_workers",
            staticmethod(lambda executor: calls.append(executor) or 0),
        )
        coord = TaskCoordinator(max_workers=2)
        with pytest.raises(KeyboardInterrupt):
            coord.run(
                _tasks(3), executor_factory=_thread_executor_factory, task_fn=boom_fn
            )
        assert len(calls) == 1

    def test_normal_completion_does_not_terminate(self, monkeypatch):
        """正常完成：不 terminate（worker 自然退出）。"""

        def ok_fn(task):
            return ChunkResult(obj_patch={task.chunk_pages[0]: "ok"})

        calls = []
        monkeypatch.setattr(
            TaskCoordinator,
            "_force_terminate_workers",
            staticmethod(lambda executor: calls.append(executor) or 0),
        )
        coord = TaskCoordinator(max_workers=2)
        coord.run(_tasks(4), executor_factory=_thread_executor_factory, task_fn=ok_fn)
        assert calls == []

    def test_pool_broken_without_interrupt_does_not_terminate(self, monkeypatch):
        """未中断的池崩（→ 串行兜底）：不 terminate（worker 随池崩退出）。"""

        calls = []
        monkeypatch.setattr(
            TaskCoordinator,
            "_force_terminate_workers",
            staticmethod(lambda executor: calls.append(executor) or 0),
        )
        coord = TaskCoordinator(max_workers=2)
        with pytest.raises(WorkerBootstrapError):
            coord.run(
                _tasks(3), executor_factory=lambda w, i, a: BrokenPoolExecutor(w, i, a)
            )
        assert calls == []
