"""V3-3 真实 spawn 冒烟：worker SIGINT 免疫 + 运行中中断旗标短路。

与单测（线程池假 executor）不同，这里用真实 ProcessPoolExecutor(spawn)
验证：
1. ``_ignore_ctrl_c_in_worker`` 在 spawn 子进程内确实把 SIGINT 设为 SIG_IGN；
2. TaskCoordinator 在 chunk 运行中收到中断旗标 → KeyboardInterrupt 短路，
   不进入串行兜底、不等待 chunk 结束。

运行：python script/_smoke_ctrl_c.py
"""

from __future__ import annotations

import concurrent.futures
import multiprocessing as mp
import signal
import sys
import threading
import time

sys.path.insert(0, "")

from pdf2zh.parallel.chunk import ChunkResult, ChunkTask
from pdf2zh.parallel.coordinator import TaskCoordinator
from pdf2zh.parallel.interrupt import (
    is_interrupted,
    mark_interrupted,
    reset_interrupt_flag,
)


def _light_worker_init(backend=None):
    """轻量 initializer：只验证 SIGINT 免疫（不加载 doclayout 模型）。"""
    from pdf2zh.parallel.worker import _ignore_ctrl_c_in_worker

    _ignore_ctrl_c_in_worker()


def _spawn_probe(result_q):
    """spawn 子进程内报告 SIGINT 处置。"""
    import signal as _s

    from pdf2zh.parallel.worker import _ignore_ctrl_c_in_worker

    _ignore_ctrl_c_in_worker()
    result_q.put(_s.getsignal(_s.SIGINT) is _s.SIG_IGN)


def _slow_chunk(task):
    time.sleep(3.0)
    return ChunkResult(obj_patch={task.chunk_pages[0]: "x"})


#: 记录真实 ProcessPoolExecutor 引用，供中断后检查 worker 是否被硬杀。
_EXECUTOR_HOLDER: dict = {}


def _executor_factory(max_workers, initializer, initargs):
    ex = concurrent.futures.ProcessPoolExecutor(
        max_workers=max_workers, initializer=initializer, initargs=initargs or ()
    )
    _EXECUTOR_HOLDER["ex"] = ex
    return ex


def _tasks(count):
    return [ChunkTask(chunk_pages=(i,), fp_bytes=b"") for i in range(count)]


def main() -> int:
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    p = ctx.Process(target=_spawn_probe, args=(q,))
    p.start()
    sigint_ignored = q.get(timeout=30)
    p.join()
    print(f"[1] spawn worker SIGINT==SIG_IGN: {sigint_ignored}")
    if not sigint_ignored:
        print("FAIL: worker 未忽略 SIGINT")
        return 1

    reset_interrupt_flag()
    coord = TaskCoordinator(max_workers=2)

    def _late_interrupt():
        time.sleep(0.5)
        mark_interrupted()

    t = threading.Thread(target=_late_interrupt, daemon=True)
    t.start()
    t0 = time.monotonic()
    aborted_ok = False
    try:
        coord.run(
            _tasks(3),
            executor_factory=_executor_factory,
            initializer=_light_worker_init,
            task_fn=_slow_chunk,
        )
    except KeyboardInterrupt:
        elapsed = time.monotonic() - t0
        print(
            f"[2] coordinator aborted via KeyboardInterrupt in {elapsed:.2f}s (chunk sleeps 3s)"
        )
        aborted_ok = elapsed < 2.5
        # V3-5：中断路径应硬杀 worker —— 不允许 worker 继续跑完 chunk（3s）。
        # terminate 已由 ``Ctrl+C: force-terminated N ...`` 日志确认；此处只做
        # 防御性检查（coordinator.shutdown 可能已清空 _processes 为 None）。
        ex = _EXECUTOR_HOLDER.get("ex")
        if ex is not None:
            time.sleep(0.3)  # 等 terminate 生效
            procs = list((getattr(ex, "_processes", None) or {}).values())
            alive = [p for p in procs if p.is_alive()]
            print(
                f"[2b] workers force-terminated: alive_after={len(alive)} ({len(procs)} tracked)"
            )
            if alive:
                aborted_ok = False
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: 未按 KeyboardInterrupt 短路，而是 {type(exc).__name__}: {exc}")
        reset_interrupt_flag()
        return 3
    reset_interrupt_flag()
    if not aborted_ok:
        print("FAIL: coordinator 未中断（不该跑完）")
        return 4
    rc1 = _smoke_handler_raise_once()
    if rc1:
        return rc1
    rc2 = _smoke_execute_task_cancelled()
    if rc2:
        return rc2
    rc3 = _smoke_terminate_workers()
    if rc3:
        return rc3
    print("[smoke] all checks passed")
    return 0


def _smoke_handler_raise_once() -> int:
    """[3] V3-4：handler 只抛一次 —— 关闭流程中后续 Ctrl+C 不打断。"""
    import signal as _sig

    from pdf2zh.parallel.interrupt import _on_sigint, reset_interrupt_flag

    reset_interrupt_flag()
    first_raised = second_raised = False
    try:
        _on_sigint(_sig.SIGINT, None)
    except KeyboardInterrupt:
        first_raised = True
    try:
        _on_sigint(_sig.SIGINT, None)
    except KeyboardInterrupt:
        second_raised = True
    print(f"[3] handler raises-once: first={first_raised} second={second_raised}")
    ok = first_raised and not second_raised and is_interrupted()
    reset_interrupt_flag()
    return 0 if ok else 5


def _smoke_execute_task_cancelled() -> int:
    """[4] V3-4：_execute_task 捕获 KeyboardInterrupt → 任务落 CANCELLED（不再打印
    ``Exception in thread`` 未处理异常，也不误判 FAILED）。"""
    from pdf2zh.services.runtime_service import (
        RuntimeService,
        TaskStage,
        TranslationRequest,
    )

    reset_interrupt_flag()

    def _boom(*a, **k):
        raise KeyboardInterrupt("Parallel engine aborted: Ctrl+C received")

    svc = RuntimeService()
    svc._store.create_task("smoke_kbint")
    svc._execute_legacy = _boom  # type: ignore[method-assign]
    svc._execute_task(
        "smoke_kbint", TranslationRequest(source_path="dummy.pdf", files=[])
    )
    st = svc._store.get_task("smoke_kbint")
    ok = st is not None and st.status == TaskStage.CANCELLED.value
    print(
        f"[4] _execute_task KeyboardInterrupt -> CANCELLED: {ok} (status={st.status if st else None})"
    )
    return 0 if ok else 6


def _smoke_terminate_workers() -> int:
    """[5] V3-5：``_force_terminate_workers`` 直接硬杀运行中的 worker。

    不经 coordinator，直接用真实 spawn 池提交 3s 慢任务，1s 后强制 terminate：
    2 个 worker 应立即退出（不再等 3s chunk 跑完）。
    """
    ex = concurrent.futures.ProcessPoolExecutor(max_workers=2)
    try:
        ex.submit(_slow_chunk, ChunkTask(chunk_pages=(0,), fp_bytes=b""))
        ex.submit(_slow_chunk, ChunkTask(chunk_pages=(1,), fp_bytes=b""))
        time.sleep(1.0)  # 确保 worker 已进入 3s 慢任务
        n = TaskCoordinator._force_terminate_workers(ex)
        time.sleep(0.3)  # 等 terminate 生效
        procs = list((getattr(ex, "_processes", None) or {}).values())
        alive = [p for p in procs if p.is_alive()]
        ok = n >= 2 and not alive
        print(f"[5] force-terminate running workers: n={n} alive_after={len(alive)}")
        return 0 if ok else 7
    finally:
        try:
            ex.shutdown(wait=False, cancel_futures=True)
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    sys.exit(main())
