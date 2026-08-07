# -*- coding: utf-8 -*-
"""BrokenProcessPool 自动降级到 CPU 的回归测试。

背景：spawn 出的 worker 在 DirectML/CUDA 推理时进程被终止（原生崩溃/显存
耗尽），concurrent.futures 只能抛出 BrokenProcessPool 而无法给出 Python
异常原因。此时本轮任务已回退串行，但若 GUI/CLI 持续使用 auto 后端，后续
任务会反复崩溃 —— 因此崩溃后应把模块级后端自动降级为 CPU。
"""
import unittest

from concurrent.futures.process import BrokenProcessPool

from pdf2zh.doclayout import ModelInstance, get_backend, resolve_providers, set_backend
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


def _dummy():
    class _Dummy:
        pass
    return _Dummy()


if __name__ == "__main__":
    unittest.main()
