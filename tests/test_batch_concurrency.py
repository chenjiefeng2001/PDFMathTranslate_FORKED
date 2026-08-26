"""并发批处理（``PDF2ZH_BATCH_CONCURRENCY``）回归测试。

覆盖 ``RuntimeService._execute_batch_concurrent``：

1. 多文件有界并发执行，全部文件完成、结果入账；
2. 进度模型：线程槽把 per-file 百分比记入 ``progress_map``，
   总体 = Σ(map)/total —— 成功文件必须收敛到 100
   （执行器内部 ``_complete_file`` 不携带 file_path，依赖
   ``_run_one`` finally 的统一入账）；
3. 单文件失败不拖垮整批（其余文件照常完成，任务仍 COMPLETED）；
4. ``PDF2ZH_BATCH_CONCURRENCY=1`` 保持串行语义（调用顺序确定）；
5. 环境变量钳制：>4 取 4，非法值回退默认 2。
"""

from __future__ import annotations

import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from pdf2zh.services.runtime_service import (
    RuntimeService,
    TaskStage,
    TranslationRequest,
    _BatchContext,
)

FILES = ["/tmp/cc_a.pdf", "/tmp/cc_b.pdf", "/tmp/cc_c.pdf"]


def _make_service(tid: str) -> RuntimeService:
    svc = RuntimeService()
    svc._sweeper = None
    svc._store.create_task(tid)
    return svc


def _fake_legacy_factory(svc: RuntimeService, seen: list, fail_names=()):
    """模拟单文件执行器：经槽位上报进度 + 完成入账（不带 file_path）。"""

    def fake_legacy(task_id, request, config, cancel_event=None):
        name = os.path.basename(request.source_path)
        seen.append(name)
        if name in fail_names:
            raise RuntimeError(f"boom-{name}")
        svc._emit_smooth(task_id, TaskStage.TRANSLATING.value, 50.0)
        svc._complete_file(
            task_id,
            [{"name": name, "path": request.source_path}],
            total_files=svc._batch_total(task_id),
            message="Completed",
        )

    return fake_legacy


class TestConcurrentBatchExecution:
    def test_all_files_complete_and_results_recorded(self):
        svc = _make_service("t_cc_ok")
        seen: list = []
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(svc, "_execute_legacy", _fake_legacy_factory(svc, seen))
            svc._execute_batch(
                "t_cc_ok",
                TranslationRequest(source_path=FILES[0], files=FILES),
                FILES,
                svc.config,
            )
        assert sorted(seen) == sorted(os.path.basename(f) for f in FILES)
        state = svc.get_task_state("t_cc_ok")
        assert state.status == TaskStage.COMPLETED.value
        # 每个文件的结果都被入账（并发下 result_files 读改写不互丢）
        names = sorted(r["name"] for r in (state.result_files or []))
        assert names == sorted(os.path.basename(f) for f in FILES)

    def test_progress_map_converges_to_100(self):
        # 回归：执行器内部 _complete_file 不带 file_path，若成功路径不入账，
        # progress_map 停在最后一次上报值（50），总体进度永远到不了 100。
        svc = _make_service("t_cc_prog")
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("PDF2ZH_BATCH_CONCURRENCY", "2")
            mp.setattr(svc, "_execute_legacy", _fake_legacy_factory(svc, []))
            svc._execute_batch(
                "t_cc_prog",
                TranslationRequest(source_path=FILES[0], files=FILES),
                FILES,
                svc.config,
            )
        ctx = svc._batch_ctx["t_cc_prog"]
        assert len(ctx.progress_map) == len(FILES)
        assert set(ctx.progress_map.values()) == {100.0}
        state = svc.get_task_state("t_cc_prog")
        assert float(state.progress) == 100.0

    def test_single_failure_does_not_kill_batch(self):
        svc = _make_service("t_cc_fail")
        seen: list = []
        fail = {os.path.basename(FILES[1])}
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                svc,
                "_execute_legacy",
                _fake_legacy_factory(svc, seen, fail_names=fail),
            )
            svc._execute_batch(
                "t_cc_fail",
                TranslationRequest(source_path=FILES[0], files=FILES),
                FILES,
                svc.config,
            )
        assert sorted(seen) == sorted(os.path.basename(f) for f in FILES)
        state = svc.get_task_state("t_cc_fail")
        assert state.status == TaskStage.COMPLETED.value
        assert state.failed_files == 1
        failures = [f["file"] for f in (state.file_failures or [])]
        assert failures == [FILES[1]]
        ctx = svc._batch_ctx["t_cc_fail"]
        assert set(ctx.progress_map.values()) == {100.0}

    def test_failed_file_resets_shared_layout_model(self):
        # 回归：单文件执行失败后必须回收进程级版面模型单例，否则损坏的
        # InferenceSession 会被同批次后续文件复用，表现为「某文件出错后
        # 后续文件都不再翻译」。
        svc = _make_service("t_cc_reset")
        released: list = []
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                svc,
                "_execute_legacy",
                _fake_legacy_factory(svc, [], fail_names={os.path.basename(FILES[1])}),
            )
            import pdf2zh.doclayout as dl

            mp.setattr(dl, "release_model_instance", lambda: released.append(True))
            svc._execute_batch(
                "t_cc_reset",
                TranslationRequest(source_path=FILES[0], files=FILES),
                FILES,
                svc.config,
            )
        assert released, "shared layout model must be released after a file failure"
        state = svc.get_task_state("t_cc_reset")
        assert state.status == TaskStage.COMPLETED.value

    def test_slot_progress_routes_linear(self):
        # 槽位活跃时：原始百分比入 progress_map，总体 = Σ/total（线性），
        # 不经 stage 权重聚合器。
        svc = _make_service("t_cc_slot")
        ctx = _BatchContext(total_files=2)
        with svc._batch_ctx_lock:
            svc._batch_ctx["t_cc_slot"] = ctx
        svc._slot_begin("t_cc_slot", "/tmp/cc_a.pdf", ctx)
        try:
            svc._emit_smooth("t_cc_slot", TaskStage.TRANSLATING.value, 50.0)
            assert ctx.progress_map["/tmp/cc_a.pdf"] == 50.0
            state = svc.get_task_state("t_cc_slot")
            assert float(state.progress) == 25.0
            # 同文件进度回退被 max 钳制
            svc._emit_smooth("t_cc_slot", TaskStage.TRANSLATING.value, 30.0)
            assert ctx.progress_map["/tmp/cc_a.pdf"] == 50.0
        finally:
            svc._slot_end()
        # 槽位释放后不再路由（串行聚合路径），map 不变
        svc._emit_smooth("t_cc_slot", TaskStage.TRANSLATING.value, 90.0)
        assert ctx.progress_map["/tmp/cc_a.pdf"] == 50.0


class TestConcurrencyEnv:
    def test_env_1_keeps_serial_order(self):
        svc = _make_service("t_cc_serial")
        order: list = []

        def fake_legacy(task_id, request, config, cancel_event=None):
            order.append(os.path.basename(request.source_path))

        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("PDF2ZH_BATCH_CONCURRENCY", "1")
            mp.setattr(svc, "_execute_legacy", fake_legacy)
            svc._execute_batch(
                "t_cc_serial",
                TranslationRequest(source_path=FILES[0], files=FILES),
                FILES,
                svc.config,
            )
        assert order == [os.path.basename(f) for f in FILES]

    @pytest.mark.parametrize(
        ("raw", "expected_k"),
        [("99", 4), ("garbage", 2), ("0", None)],
    )
    def test_env_clamping(self, caplog, raw, expected_k):
        svc = _make_service("t_cc_env")
        calls: list = []
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("PDF2ZH_BATCH_CONCURRENCY", raw)
            mp.setattr(svc, "_execute_legacy", _fake_legacy_factory(svc, calls))
            with caplog.at_level(
                logging.INFO, logger="pdf2zh.services.runtime_service"
            ):
                svc._execute_batch(
                    "t_cc_env",
                    TranslationRequest(source_path=FILES[0], files=FILES[:2]),
                    FILES[:2],
                    svc.config,
                )
        if expected_k is None:
            # 0 -> 钳为 1 -> 串行路径，无并发日志
            assert not any(
                "batch concurrent execution" in r.message for r in caplog.records
            )
        else:
            recs = [
                r for r in caplog.records if "batch concurrent execution" in r.message
            ]
            assert len(recs) == 1
            assert f"K={expected_k}" in recs[0].getMessage()
