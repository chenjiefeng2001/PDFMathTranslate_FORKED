"""Warm Process Pool 单元测试（报告 §8.2.1）。

覆盖 ``pdf2zh.parallel.pool``：
- ``PDF2ZH_WARM_POOL`` 未启用时 ``get_shared_pool`` 返回 ``None``（旧行为）；
- 启用后懒创建 / 复用 / worker 数归一化；
- ``mark_broken`` 后下次 ``get`` 重建；
- backend / workers 变化自动重建；
- 幂等 shutdown；
- ``TaskCoordinator.run`` 的 ``reuse_executor`` / ``pool_owner`` 契约
  （复用池时任务结束不 shutdown；异常传播时标记 broken）。
"""

import concurrent.futures
import os

import pytest

import pdf2zh.parallel.pool as pool_mod
from pdf2zh.parallel.chunk import ChunkResult, ChunkTask
from pdf2zh.parallel.coordinator import TaskCoordinator


def _dummy_initializer(backend=None):  # 可 pickle 的 dummy（避免 worker 加载模型）
    pass


class _FakePoolExecutor:
    def __init__(self):
        self.shutdown_calls = 0
        self.submits = 0

    def submit(self, fn, *args, **kwargs):
        self.submits += 1
        future = concurrent.futures.Future()
        future.set_result(fn(*args, **kwargs))
        return future

    def shutdown(self, **kwargs):
        self.shutdown_calls += 1


class _FakePoolOwner:
    def __init__(self):
        self.broken = 0

    def mark_broken(self):
        self.broken += 1


# ── 开关 / 生命周期 ─────────────────────────────────────────────────────
def test_disabled_returns_none(monkeypatch):
    monkeypatch.delenv("PDF2ZH_WARM_POOL", raising=False)
    assert pool_mod.get_shared_pool(4, "cpu") is None


def test_enabled_creates_and_reuses(monkeypatch):
    monkeypatch.setenv("PDF2ZH_WARM_POOL", "1")
    monkeypatch.setattr(pool_mod, "init_worker_process", _dummy_initializer)
    try:
        sp = pool_mod.get_shared_pool(4, "cpu")
        assert sp is not None
        ex1 = sp.get()
        ex2 = sp.get()
        assert ex1 is ex2  # 懒创建且复用
        assert sp._max_workers == 4
        assert sp._backend == "cpu"
    finally:
        pool_mod.shutdown_shared_pool()


def test_mark_broken_rebuilds(monkeypatch):
    monkeypatch.setenv("PDF2ZH_WARM_POOL", "1")
    monkeypatch.setattr(pool_mod, "init_worker_process", _dummy_initializer)
    try:
        sp = pool_mod.get_shared_pool(2, "cpu")
        ex1 = sp.get()
        sp.mark_broken()
        ex2 = sp.get()
        assert ex2 is not ex1  # broken 后重建
    finally:
        pool_mod.shutdown_shared_pool()


def test_backend_or_workers_change_rebuilds(monkeypatch):
    monkeypatch.setenv("PDF2ZH_WARM_POOL", "1")
    monkeypatch.setattr(pool_mod, "init_worker_process", _dummy_initializer)
    try:
        sp1 = pool_mod.get_shared_pool(4, "cpu")
        ex1 = sp1.get()
        sp2 = pool_mod.get_shared_pool(4, "gpu")  # backend 变化
        assert sp2 is not sp1
        ex2 = sp2.get()
        assert ex2 is not ex1
        sp3 = pool_mod.get_shared_pool(8, "gpu")  # workers 变化
        assert sp3 is not sp2
    finally:
        pool_mod.shutdown_shared_pool()


def test_shutdown_idempotent(monkeypatch):
    monkeypatch.setenv("PDF2ZH_WARM_POOL", "1")
    monkeypatch.setattr(pool_mod, "init_worker_process", _dummy_initializer)
    pool_mod.shutdown_shared_pool()  # 未创建时调用安全
    try:
        sp = pool_mod.get_shared_pool(2, "cpu")
        sp.get()
        sp.shutdown()
        assert sp._executor is None
        sp.shutdown()  # 幂等
        assert sp._executor is None
    finally:
        pool_mod.shutdown_shared_pool()


def test_workers_normalization(monkeypatch):
    monkeypatch.setenv("PDF2ZH_WARM_POOL", "1")
    monkeypatch.setattr(pool_mod, "init_worker_process", _dummy_initializer)
    try:
        sp = pool_mod.get_shared_pool(0, "cpu")  # <2 → 归一化
        assert sp._max_workers >= 2
    finally:
        pool_mod.shutdown_shared_pool()


# ── TaskCoordinator reuse_executor / pool_owner 契约 ───────────────────
def test_coordinator_reuse_keeps_executor_running():
    coord = TaskCoordinator(max_workers=2)
    tasks = [
        ChunkTask(chunk_pages=(0, 1), pages=(0,)),
        ChunkTask(chunk_pages=(2, 3), pages=(2,)),
    ]
    executor = _FakePoolExecutor()
    owner = _FakePoolOwner()

    obj, obs, serial = coord.run(
        tasks,
        executor_factory=lambda *a, **k: executor,
        task_fn=lambda t: ChunkResult(
            obj_patch={"chunk%d" % t.chunk_pages[0]: t.chunk_pages}
        ),
        reuse_executor=True,
        pool_owner=owner,
    )
    assert executor.shutdown_calls == 0  # 复用池：任务结束不 shutdown
    assert owner.broken == 0  # 正常完成不标记
    assert serial == []
    assert obj["chunk0"] == (0, 1)
    assert obj["chunk2"] == (2, 3)


def test_coordinator_no_reuse_shuts_down():
    coord = TaskCoordinator(max_workers=2)
    tasks = [ChunkTask(chunk_pages=(0, 1), pages=(0,))]
    executor = _FakePoolExecutor()
    owner = _FakePoolOwner()

    coord.run(
        tasks,
        executor_factory=lambda *a, **k: executor,
        task_fn=lambda t: ChunkResult(obj_patch={}),
        reuse_executor=False,
        pool_owner=owner,
    )
    assert executor.shutdown_calls == 1  # 旧行为：每次任务 shutdown
    assert owner.broken == 0


def test_coordinator_exception_marks_pool_broken():
    coord = TaskCoordinator(max_workers=2)
    tasks = [ChunkTask(chunk_pages=(0, 1), pages=(0,))]
    owner = _FakePoolOwner()

    class _PickleViolatingExecutor(_FakePoolExecutor):
        def submit(self, fn, *args, **kwargs):
            raise TypeError("cannot pickle 'X' object")

    from pdf2zh.parallel.errors import ProtocolViolationError

    executor = _PickleViolatingExecutor()
    with pytest.raises(ProtocolViolationError):
        coord.run(
            tasks,
            executor_factory=lambda *a, **k: executor,
            task_fn=lambda t: ChunkResult(obj_patch={}),
            reuse_executor=True,
            pool_owner=owner,
        )
    assert owner.broken >= 1  # 异常传播 → 保守标记重建
