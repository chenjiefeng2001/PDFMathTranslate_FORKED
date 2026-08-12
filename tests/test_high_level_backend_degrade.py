"""BrokenProcessPool 自动降级到 CPU 的回归测试。

背景：spawn 出的 worker 在 DirectML/CUDA 推理时进程被终止（原生崩溃/显存
耗尽），concurrent.futures 只能抛出 BrokenProcessPool 而无法给出 Python
异常原因。此时本轮任务已回退串行，但若 GUI/CLI 持续使用 auto 后端，后续
任务会反复崩溃 —— 因此崩溃后应把模块级后端自动降级为 CPU。
"""
import unittest
from concurrent.futures.process import BrokenProcessPool

from pdf2zh.doclayout import (
    ModelInstance,
    get_backend,
    is_cpu_degraded,
    mark_cpu_degraded,
    resolve_providers,
    set_backend,
    try_rearm_gpu,
)
from pdf2zh.high_level import _degrade_backend_on_crash


class BackendDegradeTest(unittest.TestCase):
    def setUp(self):
        self._old = get_backend()
        self._old_model = ModelInstance.value
        set_backend("auto")
        ModelInstance.value = None

    def tearDown(self):
        set_backend(self._old if self._old else "auto")
        ModelInstance.value = self._old_model

    def test_broken_pool_degrades_auto_to_cpu(self):
        self.assertTrue(_degrade_backend_on_crash(BrokenProcessPool()))
        self.assertEqual(get_backend(), "cpu")

    def test_broken_pool_keeps_cpu(self):
        set_backend("cpu")
        self.assertFalse(_degrade_backend_on_crash(BrokenProcessPool()))
        self.assertEqual(get_backend(), "cpu")

    def test_other_errors_do_not_degrade(self):
        self.assertFalse(_degrade_backend_on_crash(RuntimeError("boom")))
        self.assertIsNone(get_backend())  # 仍为 auto

    def test_broken_pool_degrades_dml_to_cpu(self):
        set_backend("dml")
        self.assertTrue(_degrade_backend_on_crash(BrokenProcessPool()))
        self.assertEqual(get_backend(), "cpu")

    def test_degrade_resets_cached_model(self):
        # 模拟主进程已用 GPU 加载过 session
        ModelInstance.value = _dummy()
        _degrade_backend_on_crash(BrokenProcessPool())
        self.assertIsNone(ModelInstance.value)

    def test_degrade_resolves_to_pure_cpu(self):
        _degrade_backend_on_crash(BrokenProcessPool())
        providers = resolve_providers(get_backend())
        self.assertIn("CPUExecutionProvider", providers)
        for p in providers:
            self.assertNotIn("Dml", p)
            self.assertNotIn("CUDA", p)

    def test_degrade_is_idempotent_and_flagged(self):
        # 第一次降级生效；重复调用（例如多次任务崩溃）不再重复重置
        self.assertTrue(_degrade_backend_on_crash(BrokenProcessPool()))
        self.assertTrue(is_cpu_degraded())
        self.assertFalse(_degrade_backend_on_crash(BrokenProcessPool()))
        self.assertTrue(is_cpu_degraded())

    def test_explicit_backend_clears_degrade_flag(self):
        # 用户显式 set_backend("auto"/"dml") = 主动恢复尝试，清除降级标记
        _degrade_backend_on_crash(BrokenProcessPool())
        self.assertTrue(is_cpu_degraded())
        set_backend("dml")
        self.assertFalse(is_cpu_degraded())
        self.assertEqual(get_backend(), "dml")

    def test_progress_cb_receives_degrade_notice(self):
        got = []
        _degrade_backend_on_crash(
            BrokenProcessPool(), progress_cb=lambda pct, msg: got.append((pct, msg))
        )
        self.assertEqual(len(got), 1)
        self.assertIn("degraded", got[0][1].lower())
        self.assertIn("cpu", got[0][1].lower())

    def test_mark_cpu_degraded_returns_true_once(self):
        set_backend("auto")
        self.assertTrue(mark_cpu_degraded())
        self.assertFalse(mark_cpu_degraded())
        self.assertEqual(get_backend(), "cpu")
        set_backend("auto")
        self.assertFalse(is_cpu_degraded())

    def test_try_rearm_gpu_allows_one_automatic_retry(self):
        # 第 1 次崩溃 → 下一任务自动重试 GPU 一次
        set_backend("auto")
        mark_cpu_degraded()
        self.assertTrue(is_cpu_degraded())
        self.assertTrue(try_rearm_gpu())
        self.assertFalse(is_cpu_degraded())
        self.assertIsNone(get_backend())  # 重新回到 auto 探测

    def test_try_rearm_gpu_backs_off_after_second_crash(self):
        # 第 2 次崩溃后不再自动重试，保持 CPU 直到显式 set_backend
        set_backend("auto")
        mark_cpu_degraded()
        try_rearm_gpu()
        mark_cpu_degraded()
        self.assertTrue(is_cpu_degraded())
        self.assertFalse(try_rearm_gpu())
        self.assertEqual(get_backend(), "cpu")

    def test_explicit_set_backend_resets_crash_streak(self):
        set_backend("auto")
        mark_cpu_degraded()
        try_rearm_gpu()
        mark_cpu_degraded()
        self.assertTrue(is_cpu_degraded())
        set_backend("dml")  # 显式重启后端：清除降级 + 重置连续计数
        self.assertFalse(is_cpu_degraded())
        self.assertTrue(mark_cpu_degraded())  # 新一轮计数可再次自动重试


def _dummy():
    class _Dummy:
        pass
    return _Dummy()


if __name__ == "__main__":
    unittest.main()
