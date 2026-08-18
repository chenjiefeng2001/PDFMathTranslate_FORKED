"""Parallel engine V3 单测（P4）：errors 映射 / chunk 降级 / 窗口调度 / Ctrl+C 短路。

覆盖 ``pdf2zh.parallel`` 子包与 ``high_level``/``doclayout`` 的 V3 集成点：
- errors 异常分类映射（整体串行 vs chunk 级补跑的语义边界）
- ChunkTask 协议强制（threading.Event 等非 mp 同步原语拒绝）
- ChunkManifest 内存态生命周期
- TaskCoordinator Bounded in-flight 窗口调度 / 有限重试 / 增量降级
- BrokenProcessPool：启动即崩 → WorkerBootstrapError；运行中崩 → 未完成 chunk 进串行
- KeyboardInterrupt 短路（绝不进入串行兜底）
- worker 硬化：bootstrap 失败语义化、ORT 线程门控
- DocLayoutModel.ensure_model_prewarmed 预热入口
- high_level 兼容外壳委托（_translate_parallel / _translate_parallel_chunk）
"""

import concurrent.futures
import multiprocessing
import os
import threading
import time

import pytest

from concurrent.futures.process import BrokenProcessPool

from pdf2zh.parallel.chunk import ChunkManifest, ChunkResult, ChunkTask
from pdf2zh.parallel.coordinator import TaskCoordinator
from pdf2zh.parallel.errors import (
    PageProcessingError,
    ParallelError,
    ProtocolViolationError,
    WorkerBootstrapError,
    WorkerProcessError,
)
from pdf2zh.parallel import worker as parallel_worker


# ── 测试工具 ────────────────────────────────────────────────────────────
class RecordingExecutor:
    """包装线程池，记录 submit 次数与峰值 in-flight 数。"""

    def __init__(self, max_workers, initializer, initargs):
        self.inner = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
        self.submitted = 0
        self.inflight = 0
        self.peak_inflight = 0
        self._lock = threading.Lock()

    def submit(self, fn, *args, **kwargs):
        with self._lock:
            self.submitted += 1
            self.inflight += 1
            self.peak_inflight = max(self.peak_inflight, self.inflight)

        def wrapped():
            try:
                return fn(*args, **kwargs)
            finally:
                with self._lock:
                    self.inflight -= 1

        return self.inner.submit(wrapped)

    def shutdown(self, wait=False, cancel_futures=False):
        return self.inner.shutdown(wait=wait, cancel_futures=cancel_futures)


class PreFailedFuture(concurrent.futures.Future):
    """构造即已完成（带异常）的 future，用于模拟池 broken。"""

    def __init__(self, exc):
        super().__init__()
        self.set_exception(exc)


def _thread_executor_factory(max_workers, initializer, initargs):
    return RecordingExecutor(max_workers, initializer, initargs)


def _tasks(count):
    return [ChunkTask(chunk_pages=(i,), fp_bytes=b"") for i in range(count)]


# ── errors 映射 ─────────────────────────────────────────────────────────
class TestErrorTaxonomy:
    def test_all_subclasses_of_parallel_error(self):
        for exc in (
            WorkerBootstrapError,
            WorkerProcessError,
            PageProcessingError,
            ProtocolViolationError,
        ):
            assert issubclass(exc, ParallelError)

    def test_distinct_classes(self):
        classes = {
            ParallelError,
            WorkerBootstrapError,
            WorkerProcessError,
            PageProcessingError,
            ProtocolViolationError,
        }
        assert len(classes) == 5

    def test_parallel_error_is_plain_exception(self):
        assert issubclass(ParallelError, Exception)


# ── ChunkTask 协议强制 / CancelToken / ChunkManifest ────────────────────
class TestChunkTaskProtocol:
    def test_rejects_threading_event(self):
        with pytest.raises(ProtocolViolationError):
            ChunkTask(cancel_event=threading.Event())

    def test_rejects_mp_event(self):
        # mp.Event 从 3.12 起不可 pickle（spawn 下必崩），协议拒绝
        with pytest.raises(ProtocolViolationError):
            ChunkTask(cancel_event=multiprocessing.Event())

    def test_rejects_non_event_sync(self):
        for obj in ("nope", 42, object()):
            with pytest.raises(ProtocolViolationError):
                ChunkTask(cancel_event=obj)

    def test_accepts_cancel_token(self):
        from pdf2zh.parallel.chunk import CancelToken

        tok = CancelToken()
        task = ChunkTask(cancel_event=tok)
        assert task.cancel_event is tok

    def test_defaults_are_scalar(self):
        task = ChunkTask()
        assert task.cancel_event is None
        assert task.thread == 4
        assert task.chunk_pages == ()
        assert task.fp_bytes == b""


class TestCancelToken:
    def test_pickle_roundtrip(self):
        """spawn 冒烟回归：取消令牌必须可 pickle（mp.Event 不可）。"""
        import pickle

        from pdf2zh.parallel.chunk import CancelToken

        tok = CancelToken()
        assert not tok.is_set()
        tok2 = pickle.loads(pickle.dumps(tok))
        assert tok2.token == tok.token
        assert tok2.path == tok.path
        tok.set()
        try:
            assert tok.is_set()
            assert tok2.is_set()  # 重建实例共享同一标记文件
        finally:
            tok.clear()
        assert not tok.is_set()

    def test_clear_is_idempotent(self):
        from pdf2zh.parallel.chunk import CancelToken

        tok = CancelToken()
        tok.clear()
        tok.clear()
        assert not tok.is_set()

class TestChunkManifest:
    def test_lifecycle(self):
        m = ChunkManifest(4)
        assert m.pending_chunks == [0, 1, 2, 3]
        assert not m.is_complete
        m.mark_running(0)
        assert m.pending_chunks == [1, 2, 3]
        m.mark_ok(0)
        m.mark_failed(1)
        assert m.ok_count == 1
        assert m.failed_indices == [1]
        m.mark_ok(2)
        m.mark_ok(3)
        assert m.is_complete
        assert m.pending_chunks == []

    def test_failed_is_idempotent(self):
        m = ChunkManifest(2)
        m.mark_failed(0)
        m.mark_failed(0)
        assert m.failed_indices == [0]
        assert m.chunk_status[0] == "failed"

    def test_out_of_range_is_ignored(self):
        m = ChunkManifest(2)
        m.mark_ok(99)
        m.mark_failed(-1)
        assert m.ok_count == 0
        assert m.failed_indices == []

    def test_chunk_result_ok_property(self):
        assert ChunkResult(obj_patch={1: "x"}).ok
        assert not ChunkResult(error_message="boom").ok


# ── 窗口调度 ────────────────────────────────────────────────────────────
class TestWindowScheduling:
    def test_inflight_bounded_and_all_submitted(self):
        """8 个任务、max_workers=2 → 窗口 4；峰值 in-flight ≤4，全部提交。"""
        lock = threading.Lock()
        delays = []

        def slow_fn(task):
            idx = task.chunk_pages[0]
            with lock:
                delays.append(idx)
            time.sleep(0.05)
            return ChunkResult(obj_patch={idx: f"p{idx}"})

        coord = TaskCoordinator(max_workers=2, in_flight_multiplier=2)
        obj, obs, serial = coord.run(
            _tasks(8),
            executor_factory=_thread_executor_factory,
            task_fn=slow_fn,
        )
        assert serial == []
        assert sorted(obj) == list(range(8))
        assert not obs

    def test_peak_inflight_respects_window(self):
        def slow_fn(task):
            time.sleep(0.02)
            return ChunkResult(obj_patch={task.chunk_pages[0]: "x"})

        rec = RecordingExecutor(2, None, ())
        coord = TaskCoordinator(max_workers=2, in_flight_multiplier=2)
        coord.run(_tasks(6), executor_factory=lambda w, i, a: rec, task_fn=slow_fn)
        assert rec.submitted == 6
        assert rec.peak_inflight <= 4  # min(6, 2*2)=4

    def test_empty_tasks(self):
        coord = TaskCoordinator()
        obj, obs, serial = coord.run([])
        assert obj == {} and obs == [] and serial == []


# ── 有限重试 + 增量降级 ─────────────────────────────────────────────────
class TestChunkDegrade:
    def test_retry_once_then_serial(self):
        attempts = {1: 0}

        def flaky(task):
            idx = task.chunk_pages[0]
            if idx == 1:
                attempts[idx] += 1
                return ChunkResult(error_message="boom")
            return ChunkResult(obj_patch={idx: "ok"})

        coord = TaskCoordinator(max_workers=2, retry_limit=1)
        obj, obs, serial = coord.run(
            _tasks(3), executor_factory=_thread_executor_factory, task_fn=flaky
        )
        assert attempts[1] == 2  # 首次 + 重试 1 次
        assert serial == [1]
        assert sorted(obj) == [0, 2]

    def test_retry_can_recover(self):
        """第一次失败 → 重试成功 → 不进 serial。"""
        attempts = {1: 0}

        def flaky(task):
            idx = task.chunk_pages[0]
            if idx == 1:
                attempts[idx] += 1
                if attempts[idx] == 1:
                    return ChunkResult(error_message="transient")
            return ChunkResult(obj_patch={idx: "ok"})

        coord = TaskCoordinator(max_workers=2, retry_limit=1)
        obj, obs, serial = coord.run(
            _tasks(3), executor_factory=_thread_executor_factory, task_fn=flaky
        )
        assert serial == []
        assert sorted(obj) == [0, 1, 2]

    def test_worker_process_error_queued_for_serial(self):
        """worker 内抛非 KeyboardInterrupt 异常 → 按 PageProcessingError 处理。"""

        def exploding(task):
            idx = task.chunk_pages[0]
            if idx == 0:
                raise RuntimeError("worker died")
            return ChunkResult(obj_patch={idx: "ok"})

        coord = TaskCoordinator(max_workers=2, retry_limit=0)
        obj, obs, serial = coord.run(
            _tasks(3), executor_factory=_thread_executor_factory, task_fn=exploding
        )
        assert serial == [0]
        assert sorted(obj) == [1, 2]


# ── BrokenProcessPool / bootstrap / Ctrl+C ──────────────────────────────
class TestPoolFailure:
    def test_bootstrap_crash_raises_worker_bootstrap(self):
        """启动即崩（无任何 chunk 完成）→ 整体串行的语义错误。"""

        class FakeExecutor:
            def __init__(self, max_workers, initializer, initargs):
                self.max_workers = max_workers

            def submit(self, fn, *a, **k):
                return PreFailedFuture(BrokenProcessPool("spawn failed"))

            def shutdown(self, wait=False, cancel_futures=False):
                pass

        coord = TaskCoordinator(max_workers=2)
        with pytest.raises(WorkerBootstrapError):
            coord.run(
                _tasks(3),
                executor_factory=lambda w, i, a: FakeExecutor(w, i, a),
            )

    def test_midrun_crash_degrades_incrementally(self):
        """部分成功后池崩 → 已成功 chunk 保留，其余进串行补跑清单。"""
        from concurrent.futures import Future

        class SequenceExecutor:
            def __init__(self, max_workers, initializer, initargs):
                self.max_workers = max_workers
                self.calls = 0

            def submit(self, fn, *a, **k):
                self.calls += 1
                f = Future()
                if self.calls == 1:
                    f.set_result(ChunkResult(obj_patch={0: "ok0"}))
                else:
                    f.set_exception(BrokenProcessPool("worker crashed mid-run"))
                return f

            def shutdown(self, wait=False, cancel_futures=False):
                pass

        coord = TaskCoordinator(max_workers=2)
        obj, obs, serial = coord.run(
            _tasks(4),
            executor_factory=lambda w, i, a: SequenceExecutor(w, i, a),
        )
        assert obj == {0: "ok0"}
        assert set(serial) == {1, 2, 3}  # 剩余在途 chunk 全部串行补跑

    def test_protocol_violation_on_submit(self):
        """submit 期 pickle 违例 → ProtocolViolationError（整体串行语义）。"""

        class BadPickleExecutor:
            def __init__(self, max_workers, initializer, initargs):
                pass

            def submit(self, fn, *a, **k):
                import pickle

                raise pickle.PicklingError("cannot pickle 'threading.Event' object")

            def shutdown(self, wait=False, cancel_futures=False):
                pass

        coord = TaskCoordinator(max_workers=2)
        with pytest.raises(ProtocolViolationError):
            coord.run(
                _tasks(2),
                executor_factory=lambda w, i, a: BadPickleExecutor(w, i, a),
            )

    def test_keyboard_interrupt_short_circuits(self):
        """Ctrl+C：直接传播，绝不进串行兜底。"""

        def ki_fn(task):
            raise KeyboardInterrupt()

        coord = TaskCoordinator(max_workers=1)
        with pytest.raises(KeyboardInterrupt):
            coord.run(
                _tasks(2), executor_factory=_thread_executor_factory, task_fn=ki_fn
            )

    def test_progress_cb_monotonic(self):
        reports = []

        def ok_fn(task):
            return ChunkResult(obj_patch={task.chunk_pages[0]: "x"})

        coord = TaskCoordinator(max_workers=2)
        coord.run(
            _tasks(4),
            executor_factory=_thread_executor_factory,
            task_fn=ok_fn,
            progress_cb=lambda pct, msg: reports.append(pct),
        )
        assert reports and reports == sorted(reports)
        assert reports[-1] >= 99.0


# ── worker 硬化 ─────────────────────────────────────────────────────────
class TestWorkerHardening:
    def test_bootstrap_onnx_import_failure_raises(self, monkeypatch):
        """onnxruntime provider 探测失败 → WorkerBootstrapError（不再静默降级）。"""
        import onnxruntime as ort

        def raiser():
            raise ImportError("DLL load failed")

        monkeypatch.setattr("pdf2zh.doclayout._ort_available_providers", raiser)
        monkeypatch.setattr(parallel_worker, "_register_ort_dll_dir", lambda: None)
        with pytest.raises(WorkerBootstrapError):
            parallel_worker.init_worker_process("cpu")

    def test_bootstrap_success_path(self, monkeypatch):
        """正常 bootstrap：模型加载到 ModelInstance 全局单例。"""
        import pdf2zh.doclayout as dl

        calls = {"load": 0}

        def fake_load():
            calls["load"] += 1
            return object()

        monkeypatch.setattr(dl.ModelInstance, "value", None)
        monkeypatch.setattr(
            dl.OnnxModel, "load_available", staticmethod(fake_load)
        )
        monkeypatch.setattr(parallel_worker, "_register_ort_dll_dir", lambda: None)
        parallel_worker.init_worker_process("cpu")
        assert calls["load"] == 1
        assert dl.ModelInstance.value is not None


# ── ORT 线程门控 ────────────────────────────────────────────────────────
class TestSessionThreadGate:
    def test_gate_off_by_default(self):
        os.environ.pop("PDF2ZH_WORKER_ORT_THREADS", None)
        from pdf2zh.doclayout import _configure_session_options

        opts = _configure_session_options()
        assert opts.intra_op_num_threads != 1  # 默认不限制

    def test_gate_on_limits_threads(self, monkeypatch):
        monkeypatch.setenv("PDF2ZH_WORKER_ORT_THREADS", "1")
        from pdf2zh.doclayout import _configure_session_options

        opts = _configure_session_options()
        assert opts.intra_op_num_threads == 1
        assert opts.inter_op_num_threads == 1
        assert opts.execution_mode is not None


# ── 预热入口 ────────────────────────────────────────────────────────────
class TestEnsureModelPrewarmed:
    def test_missing_path_returns_none(self, monkeypatch):
        import pdf2zh.doclayout as dl

        monkeypatch.setattr(dl, "get_doclayout_onnx_model_path", lambda: None)
        assert dl.DocLayoutModel.ensure_model_prewarmed() is None

    def test_non_existent_file_returns_none(self, monkeypatch, tmp_path):
        import pdf2zh.doclayout as dl

        missing = str(tmp_path / "missing.onnx")
        monkeypatch.setattr(dl, "get_doclayout_onnx_model_path", lambda: missing)
        assert dl.DocLayoutModel.ensure_model_prewarmed() is None

    def test_compiled_provider_skips_cache_but_returns_path(self, monkeypatch, tmp_path):
        import pdf2zh.doclayout as dl

        pth = tmp_path / "model.onnx"
        pth.write_bytes(b"model")
        monkeypatch.setattr(dl, "get_doclayout_onnx_model_path", lambda: str(pth))
        monkeypatch.setattr(
            dl, "resolve_providers", lambda backend: ["CoreMLExecutionProvider"]
        )
        assert dl.DocLayoutModel.ensure_model_prewarmed() == str(pth)

    def test_cache_reuse_hits_cached(self, monkeypatch, tmp_path):
        import pdf2zh.doclayout as dl

        pth = tmp_path / "model.onnx"
        pth.write_bytes(b"model")
        monkeypatch.setattr(dl, "get_doclayout_onnx_model_path", lambda: str(pth))
        monkeypatch.setattr(
            dl, "resolve_providers", lambda backend: ["CPUExecutionProvider"]
        )
        monkeypatch.setattr(
            dl._OptimizedCache, "acquire", lambda self: str(pth) + ".optimized"
        )
        assert dl.DocLayoutModel.ensure_model_prewarmed() == str(pth) + ".optimized"


# ── high_level 兼容外壳 ─────────────────────────────────────────────────
class TestHighLevelDelegation:
    def test_translate_parallel_chunk_delegates(self, monkeypatch):
        import pdf2zh.high_level as hl

        calls = {}

        def fake_execute(task):
            calls["pages"] = task.chunk_pages
            calls["envs_str"] = task.envs_str
            return ChunkResult(obj_patch={1: "x"})

        monkeypatch.setattr(parallel_worker, "execute_chunk", fake_execute)
        result = hl._translate_parallel_chunk([1, 2], b"pdf", envs_str='{"a":1}')
        assert calls["pages"] == (1, 2)
        assert calls["envs_str"] == '{"a":1}'
        assert result == ({1: "x"}, None)

    def test_translate_parallel_uses_task_coordinator(self, monkeypatch):
        import io

        import pdf2zh.high_level as hl
        import pdf2zh.parallel.coordinator as coord_mod
        import pdf2zh.parallel.pool as pool_mod

        seen = {}
        shared = {"broken": 0}

        class FakeSharedPool:
            def get(self):
                return object()

            def mark_broken(self):
                shared["broken"] += 1

        monkeypatch.setenv("PDF2ZH_WARM_POOL", "1")
        monkeypatch.setattr(
            pool_mod, "get_shared_pool", lambda *a, **k: FakeSharedPool()
        )

        class FakeCoordinator:
            def __init__(self, max_workers=4, **kw):
                self.max_workers = max_workers

            def run(
                self,
                tasks,
                progress_cb=None,
                initializer=None,
                initargs=(),
                executor_factory=None,
                task_fn=None,
                reuse_executor=False,
                pool_owner=None,
            ):
                # 8.2.1 Warm Pool：调用方需显式声明复用池 + 注入 executor 工厂
                seen["reuse"] = reuse_executor
                seen["factory"] = executor_factory is not None
                seen["owner"] = pool_owner is not None
                return ({100: "patch"}, [{"bundle": "obs"}], [])

        monkeypatch.setattr(coord_mod, "TaskCoordinator", FakeCoordinator)
        try:
            fp = io.BytesIO(b"pdf")
            locs = {"doc_zh": type("D", (), {"page_count": 6})(), "thread": 4}
            obj_patch = hl._translate_parallel(fp, locs, workers=2)
        finally:
            pool_mod.shutdown_shared_pool()
        # 6 页 / 2 worker → chunk_size=3 → chunks=[[0,1,2],[3,4,5]]
        assert obj_patch[100] == "patch"
        assert "__obs__" in obj_patch
        assert seen["reuse"] is True
        assert seen["factory"] and seen["owner"]

    def test_init_worker_process_delegates(self, monkeypatch):
        import pdf2zh.high_level as hl

        seen = {}

        def fake_init(backend):
            seen["backend"] = backend

        monkeypatch.setattr(parallel_worker, "init_worker_process", fake_init)
        hl._init_worker_process("cpu")
        assert seen["backend"] == "cpu"

