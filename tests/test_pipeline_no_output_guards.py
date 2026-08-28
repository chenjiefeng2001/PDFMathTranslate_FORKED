"""产物缺失防护测试：空产物必须落 FAILED 而非 COMPLETED。

背景（2026-08-26 用户报告）：MinerU+BabelDOC 管线"直接完成但是没有任何输
出"。两类根因：

1. ``_collect_magicpdf_results`` 对空 ``result_files`` 也无条件落
   COMPLETED 终态 —— 解析/回退链路任何早期故障都被静默吞掉；
2. BabelDOC 伪代码保护的 MinerU VLM 分支在任务启动前为整份文档额外拉起
   子进程（默认超时 3600s），把任务长时间卡死在 "starting" 无任何进度。

覆盖（对应修复）：
- 空产物 → ``_fail_file``（FAILED 终态）且绝不调用 ``_complete_file``；
- 伪代码保护优先本地 PP-DocLayoutV2，MinerU 分支仅在 PP 不可用时尝试；
- MinerU 分支受有界预算约束，超时回退 base 模型而绝不阻塞主链路。
"""

import os
import unittest
from unittest.mock import patch

import pdf2zh.doclayout_pseudocode as pcp


class _FakeTaskStore:
    def __init__(self):
        self.tasks = {}

    def create_task(self, task_id):
        from pdf2zh.services.runtime_service import TaskState

        self.tasks[task_id] = TaskState(task_id=task_id)
        return task_id

    def get_task(self, task_id):
        return self.tasks.get(task_id)

    def is_cancelled(self, task_id):
        return False


class TestMagicpdfEmptyResultsFail(unittest.TestCase):
    """空产物不得落 COMPLETED —— 必须显式 FAILED 并给出排查指引。"""

    def test_empty_artifacts_mark_failed(self, ):
        from pdf2zh.services.runtime_service import RuntimeService

        svc = RuntimeService()
        svc._store = _FakeTaskStore()
        tid = "task_empty_magicpdf"
        svc._store.create_task(tid)

        completed = []
        failed = []

        with patch.object(svc, "_complete_file", lambda *a, **k: completed.append(a)), \
             patch.object(svc, "_fail_file", lambda *a, **k: failed.append((a, k))):
            with patch.dict(os.environ, {"PDF2ZH_NO_WARMUP": "1"}):
                svc._collect_magicpdf_results(tid, os.devnull, total=1)

        self.assertEqual(len(failed), 1)
        self.assertEqual(completed, [])
        msg = str(failed[0][0][1])
        self.assertIn("no output artifacts", msg)



    def test_legacy_fallback_pdfs_collected_when_magicpdf_empty(self):
        """magicpdf 子目录无产物但父目录有 legacy 降级 PDF → 收集并 COMPLETED。"""
        import pathlib
        import tempfile

        from pdf2zh.services.runtime_service import RuntimeService

        svc = RuntimeService()
        svc._store = _FakeTaskStore()
        tid = "task_fallback_pdfs"
        svc._store.create_task(tid)
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp)
            (out / "magicpdf").mkdir(parents=True, exist_ok=True)
            (out / "paper-mono.pdf").write_bytes(b"%PDF-mono")
            (out / "paper-dual.pdf").write_bytes(b"%PDF-dual")

            completed = []
            failed = []
            with patch.object(svc, "_complete_file", lambda *a, **k: completed.append((a, k))), \
                 patch.object(svc, "_fail_file", lambda *a, **k: failed.append((a, k))), \
                 patch.dict(os.environ, {"PDF2ZH_NO_WARMUP": "1"}):
                svc._collect_magicpdf_results(tid, str(out), total=1)

            self.assertEqual(failed, [])
            self.assertEqual(len(completed), 1)
            files = completed[0][0][1]
            names = [f["name"] for f in files]
            self.assertIn("paper-mono.pdf", names)
            self.assertIn("paper-dual.pdf", names)


class TestPseudoProtectPriority(unittest.TestCase):
    """PP-DocLayoutV2 优先；MinerU 分支仅作回退且有界预算。"""

    def setUp(self):
        pcp._fused_model = None

    @staticmethod
    def _make_det():
        class _Det:
            def detect_algorithm_boxes(self, image_rgb, page_index=None):
                return []

        return _Det()

    def test_paddle_tried_first_mineru_not_called(self):
        calls = {"mineru": 0}
        det = self._make_det()
        base = object()

        def _mineru(*a, **k):
            calls["mineru"] += 1
            raise AssertionError("MinerU branch must not run when PP is available")

        with patch.object(pcp, "_load_base_layout_model", lambda: base), \
             patch.object(pcp, "_try_build_algorithm_detector", lambda: det), \
             patch.object(pcp, "MinerUAlgorithmDetector", _mineru):
            model = pcp._build_with_mineru_or_paddle("fake.pdf")
        self.assertIsInstance(model, pcp.PseudoCodeProtectedLayoutModel)
        self.assertEqual(calls["mineru"], 0)

    def test_mineru_slow_returns_unprotected_model_immediately(self):
        """MinerU 慢 → 主线程**立即**返回无保护融合模型（不等待、不阻塞）。"""
        import time as _time

        base = object()

        def _slow_detector(path):
            _time.sleep(1.5)

        start = _time.monotonic()
        with patch.object(pcp, "_load_base_layout_model", lambda: base), \
             patch.object(pcp, "_try_build_algorithm_detector", lambda: None), \
             patch.dict(os.environ, {"PDF2ZH_PSEUDO_MINERU_BUDGET": "1"}), \
             patch.object(pcp, "MinerUAlgorithmDetector", _slow_detector):
            result = pcp._build_with_mineru_or_paddle("fake.pdf")
        elapsed = _time.monotonic() - start
        # 异步化后：绝不等待 MinerU（<0.2s 返回），detector 尚未注入
        self.assertIsInstance(result, pcp.PseudoCodeProtectedLayoutModel)
        self.assertIs(result.base_model, base)
        self.assertIsNone(result.detector)
        self.assertLess(elapsed, 0.5)

    def test_budget_env_floor_and_default(self):
        saved = os.environ.pop("PDF2ZH_PSEUDO_MINERU_BUDGET", None)
        try:
            os.environ["PDF2ZH_PSEUDO_MINERU_BUDGET"] = "-5"
            self.assertEqual(pcp.resolve_pseudo_mineru_budget(), 30)
            os.environ["PDF2ZH_PSEUDO_MINERU_BUDGET"] = "notanumber"
            self.assertEqual(pcp.resolve_pseudo_mineru_budget(), 240)
            os.environ.pop("PDF2ZH_PSEUDO_MINERU_BUDGET")
            self.assertEqual(pcp.resolve_pseudo_mineru_budget(), 240)
        finally:
            if saved is not None:
                os.environ["PDF2ZH_PSEUDO_MINERU_BUDGET"] = saved


class TestMineruBudgetNonBlocking(unittest.TestCase):
    """预算超时必须**立即**返回，绝不阻塞等待后台 MinerU 探测线程。

    背景：MinerU 伪代码检测在 ``future.result(timeout=budget)`` 超时后若用
    ``with ThreadPoolExecutor`` 包裹，``__exit__`` 的 ``shutdown(wait=True)``
    仍会一直等正在跑的探测器线程干完（``adapter.parse`` 不受 ``cancel()``
    影响），预算形同虚设——BabelDOC 任务因此长时间卡在启动阶段、不产 PDF。
    """

    def setUp(self):
        pcp._fused_model = None

    def test_timeout_returns_without_joining_running_mineru_thread(self):
        import time as _time

        def _slow_mineru_detector(pdf_path):  # noqa: ARG001 -- 挂起远超预算
            _time.sleep(3)
            return object()

        base = object()
        start = _time.monotonic()
        with patch.object(pcp, "_load_base_layout_model", lambda: base), \
             patch.object(pcp, "_try_build_algorithm_detector", lambda: None), \
             patch.object(pcp, "MinerUAlgorithmDetector", _slow_mineru_detector), \
             patch.object(pcp, "resolve_pseudo_mineru_budget", lambda *a, **k: 1):
            result = pcp._build_with_mineru_or_paddle("fake.pdf")
        elapsed = _time.monotonic() - start
        # 异步化后立即返回（不 join 后台线程）：绝不等待 3s 的 MinerU 解析。
        self.assertIsInstance(result, pcp.PseudoCodeProtectedLayoutModel)
        self.assertIsNone(result.detector)
        self.assertLess(elapsed, 2.0)

    def test_mineru_detector_hot_attached_when_ready(self):
        """MinerU 后台解析完成 → ``attach_detector`` 热注入，后续保护生效。"""
        import time as _time

        import numpy as np
        from babeldoc.docvision.base_doclayout import YoloResult

        base = object()
        calls = []
        # 用事件闸门固定时序，消除“后台 attach 与阶段 1 的竞态”：
        # 后台线程在 attach 前必须等主线程放行。
        import threading

        gate = threading.Event()

        class _Geo:
            image = np.zeros((600, 600, 3), dtype=np.uint8)

            def px_len_to_pt(self, value, axis):
                return float(value)

        class _ReadyDetector:
            def detect_algorithm_boxes(self, image_rgb, page_index=None):
                calls.append(page_index)
                return [(100.0, 100.0, 400.0, 300.0)]

        def _gated_detector(path):
            assert gate.wait(5), "main thread never released the gate"
            return _ReadyDetector()

        with patch.object(pcp, "_load_base_layout_model", lambda: base), \
             patch.object(pcp, "_try_build_algorithm_detector", lambda: None), \
             patch.object(pcp, "MinerUAlgorithmDetector", _gated_detector):
            result = pcp._build_with_mineru_or_paddle("fake.pdf")
        self.assertIsInstance(result, pcp.PseudoCodeProtectedLayoutModel)
        # BabelDOC OnnxModel 全文档共享同一 names dict
        names = {0: "plain text"}

        def _protect_one(page_number):
            r = YoloResult(
                names=names,
                boxes_data=np.array(
                    [[100, 100, 400, 300, 0.5, 0]], dtype=np.float32
                ),
            )
            result._protect_page(_Geo(), r, page_number=page_number)
            return names.get(int(r.boxes[0].cls))

        # 阶段 1：MinerU 尚未就绪 → 任务运行中的前置页面无保护（不阻塞启动）
        self.assertEqual(_protect_one(0), "plain text")
        gate.set()  # 放行后台线程完成 MinerU 解析 + 热注入
        # 后台线程异步热注入：轮询等待（默认 5s 内应完成）
        deadline = _time.monotonic() + 5
        while result.detector is None and _time.monotonic() < deadline:
            _time.sleep(0.05)
        self.assertIsNotNone(result.detector)
        self.assertIsInstance(result.detector, _ReadyDetector)
        # MinerU 检测器接受 page_index，能力标志随热注入同步刷新
        self.assertTrue(result._detector_accepts_page_index)
        # 阶段 2：热注入完成后，后续页面带 page_index 正常获得伪代码保护
        self.assertEqual(_protect_one(7), "algorithm")
        self.assertEqual(calls, [7])


if __name__ == "__main__":
    unittest.main()
