"""GPU 并发调控器与 CUDA 生命周期隔离工具测试。

覆盖 ``pdf2zh/gpu_governor.py``：

- ``GPUConcurrencyGovernor``：有界并发上限（信号量语义），默认并发度 1；
- ``get_governor``：作用域单例 + PID 绑定（fork 子进程自动重建独立 governor）；
- ``fork_cuda_degrade_backend``：仅当 fork 子进程 + 严格开关开启时降级 GPU 后端；
- ``mark/cuda/reset_cuda``：CUDA 初始化标记按 PID 隔离；
- 进程本地线程预算与应用。
"""

import multiprocessing as _mp
import os

import pytest

from pdf2zh import gpu_governor as gg


def test_governor_default_concurrency_is_one():
    gov = gg.GPUConcurrencyGovernor("test-default", max_concurrent=1)
    assert gov.max_concurrent == 1
    # 并发度 1 = 完全串行（返回后立即能再次进入）。
    assert gov.acquire() is True
    gov.release()
    assert gov.acquire() is True
    gov.release()


def test_governor_bounded_concurrency():
    gov = gg.GPUConcurrencyGovernor("test-bounded", max_concurrent=2)
    assert gov.max_concurrent == 2
    # 两个并发名额可用。
    assert gov.acquire(timeout=0.01) is True
    assert gov.acquire(timeout=0.01) is True
    # 第三个在超时内拿不到。
    assert gov.acquire(timeout=0.01) is False
    gov.release()
    gov.release()
    assert gov.acquire(timeout=0.01) is True
    gov.release()


def test_governor_run_and_context_manager():
    gov = gg.GPUConcurrencyGovernor("test-ctx", max_concurrent=1)
    sentinel = {}

    def _fn():
        sentinel["ok"] = True
        return 42

    assert gov.run(_fn) == 42
    assert sentinel["ok"] is True
    with gg.GPUConcurrencyGovernor("test-ctx2", max_concurrent=1) as g2:
        assert g2.acquire(timeout=0.01) is False  # 已被 with 占用


def test_concurrency_from_env(monkeypatch):
    monkeypatch.setenv(gg._ENV_GPU_CONCURRENCY, "3")
    gov = gg.GPUConcurrencyGovernor("test-env")
    assert gov.max_concurrent == 3
    # scope 专项覆盖优先级更高。
    monkeypatch.setenv("PDF2ZH_GPU_CONCURRENCY_TEST_SCOPE", "5")
    gov2 = gg.GPUConcurrencyGovernor("test-scope")
    assert gov2.max_concurrent == 5


def test_get_governor_singleton_same_pid():
    a = gg.get_governor("test-single")
    b = gg.get_governor("test-single")
    assert a is b


def test_get_governor_isolates_by_scope():
    a = gg.get_governor("scope-a")
    b = gg.get_governor("scope-b")
    assert a is not b


def test_cuda_initialized_follows_pid():
    gg.reset_cuda_process_guard()
    assert gg.cuda_initialized() is False
    gg.mark_cuda_initialized()
    assert gg.cuda_initialized() is True
    gg.reset_cuda_process_guard()
    assert gg.cuda_initialized() is False


def test_fork_degrade_noop_in_main_process(monkeypatch):
    monkeypatch.setenv("PDF2ZH_STRICT_FORK_CUDA", "1")
    # 主进程（不在 multiprocessing 子进程）→ 不降级。
    monkeypatch.setattr(gg, "in_multiprocessing_process", lambda: False)
    assert gg.fork_cuda_degrade_backend("cuda") == "cuda"
    assert gg.fork_cuda_degrade_backend("dml") == "dml"
    assert gg.fork_cuda_degrade_backend("cpu") == "cpu"


def test_fork_degrade_only_with_strict_enabled(monkeypatch):
    monkeypatch.setattr(gg, "in_multiprocessing_process", lambda: True)
    monkeypatch.setattr(gg, "current_start_method", lambda: "fork")
    # 严格开关未开启 → 不降级（仅日志）。
    monkeypatch.delenv("PDF2ZH_STRICT_FORK_CUDA", raising=False)
    assert gg.fork_cuda_degrade_backend("cuda") == "cuda"
    # 开启 → 降级。
    monkeypatch.setenv("PDF2ZH_STRICT_FORK_CUDA", "1")
    assert gg.fork_cuda_degrade_backend("cuda") == "cpu"
    assert gg.fork_cuda_degrade_backend("dml") == "cpu"


def test_fork_degrade_skips_non_fork(monkeypatch):
    monkeypatch.setattr(gg, "in_multiprocessing_process", lambda: True)
    monkeypatch.setattr(gg, "current_start_method", lambda: "spawn")
    assert gg.fork_cuda_degrade_backend("cuda") == "cuda"


def test_suggest_concurrency_for_vram():
    assert gg.suggest_concurrency_for_vram(0) == 1
    assert gg.suggest_concurrency_for_vram(6 * 1024) == 1
    assert gg.suggest_concurrency_for_vram(10 * 1024) == 2
    assert gg.suggest_concurrency_for_vram(16 * 1024) == 3
    assert gg.suggest_concurrency_for_vram(48 * 1024) == 4


def test_thread_budget_apply_uses_setdefault(monkeypatch):
    monkeypatch.delenv("OMP_NUM_THREADS", raising=False)
    monkeypatch.delenv("MKL_NUM_THREADS", raising=False)
    monkeypatch.delenv("OPENBLAS_NUM_THREADS", raising=False)
    gg.apply_process_local_thread_budget("test-budget", budget=4)
    assert os.environ.get("OMP_NUM_THREADS") == "4"
    assert os.environ.get("MKL_NUM_THREADS") == "4"
    assert os.environ.get("OPENBLAS_NUM_THREADS") == "4"
    # 已显式设置的值优先（setdefault，不覆盖）。
    monkeypatch.setenv("OMP_NUM_THREADS", "2")
    gg.apply_process_local_thread_budget("test-budget2", budget=4)
    assert os.environ.get("OMP_NUM_THREADS") == "2"