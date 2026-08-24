"""Step 1.1 — 解析引擎环境探测（engine_env）单元测试。

覆盖：
- python_version / mineru_supported：Py3.13 → 支持 MinerU 3.x，Py3.14+ → 不支持；
- prefer_mineru / backend_hint：Py 版本与环境变量 PDF2ZH_MINERU_PREFER；
- probe_mineru / probe_magicpdf：缺依赖返回 None；
- available_backend：未安装引擎时返回 (hint, False)；
- resolve_device：环境变量优先于参数；
- mineru_install_hint：按 Python 版本给出安装建议。
"""
import os
import unittest
from unittest.mock import patch

from pdf2zh.engine_env import (
    MINERU_MAX_PY,
    MINERU_MIN_PY,
    available_backend,
    backend_hint,
    mineru_install_hint,
    mineru_supported,
    prefer_mineru,
    probe_magicpdf,
    probe_mineru,
    python_version,
    resolve_device,
)


class TestVersionProbe(unittest.TestCase):
    def test_python_version_shape(self):
        major, minor = python_version()
        self.assertGreaterEqual(major, 3)
        self.assertGreaterEqual(minor, 8)

    def test_mineru_supported_current(self):
        # 当前解释器（>=3.14 或 3.10-3.13）与常量保持一致
        cur = python_version()
        self.assertEqual(
            mineru_supported(), MINERU_MAX_PY >= cur >= MINERU_MIN_PY
        )

    @patch("pdf2zh.engine_env.python_version", return_value=(3, 13))
    def test_mineru_supported_py313(self, *_):
        # MinerU >=3.1 官方支持 Py3.13（requires-python <3.14）
        self.assertTrue(mineru_supported())

    @patch("pdf2zh.engine_env.python_version", return_value=(3, 14))
    def test_mineru_unsupported_py314(self, *_):
        self.assertFalse(mineru_supported())

    @patch("pdf2zh.engine_env.python_version", return_value=(3, 11))
    def test_mineru_supported_py311(self, *_):
        self.assertTrue(mineru_supported())


class TestBackendHint(unittest.TestCase):
    @patch("pdf2zh.engine_env.python_version", return_value=(3, 11))
    def test_prefer_mineru(self, *_):
        self.assertTrue(prefer_mineru())
        self.assertEqual(backend_hint(), "mineru")

    @patch("pdf2zh.engine_env.python_version", return_value=(3, 13))
    def test_py313_prefers_mineru(self, *_):
        self.assertTrue(prefer_mineru())
        self.assertEqual(backend_hint(), "mineru")

    @patch("pdf2zh.engine_env.python_version", return_value=(3, 14))
    def test_magicpdf_fallback_beyond_range(self, *_):
        self.assertFalse(prefer_mineru())
        self.assertEqual(backend_hint(), "magicpdf")

    def test_env_disables_mineru(self):
        with patch.dict(os.environ, {"PDF2ZH_MINERU_PREFER": "0"}):
            self.assertFalse(prefer_mineru())


class TestProbe(unittest.TestCase):
    @patch("pdf2zh.engine_env._find_spec", return_value=None)
    def test_probe_missing_returns_none(self, *_):
        # 未安装 magic-pdf/mineru 时应返回 None 而非抛错
        # （patch _find_spec 模拟“模块缺失”，与真实安装状态无关）
        self.assertIsNone(probe_mineru())
        self.assertIsNone(probe_magicpdf())

    def test_probe_installed_returns_module(self):
        # 本环境若安装引擎，探测应返回模块对象而非 None（容错）
        if __import__("importlib.util").util.find_spec("magic_pdf"):
            self.assertIsNotNone(probe_magicpdf())

    def test_available_backend_returns_tuple(self):
        backend, ok = available_backend()
        self.assertIn(backend, ("mineru", "magicpdf"))
        self.assertIsInstance(ok, bool)
        if ok:  # 引擎已安装时断言名称匹配探测结果
            self.assertEqual(backend, "mineru" if probe_mineru() else "magicpdf")


class TestDevice(unittest.TestCase):
    def test_default(self):
        self.assertEqual(resolve_device("auto"), "auto")

    def test_argument(self):
        self.assertEqual(resolve_device("cuda"), "cuda")

    def test_env_wins(self):
        with patch.dict(os.environ, {"PDF2ZH_MAGICPDF_DEVICE": "cpu"}):
            self.assertEqual(resolve_device("cuda"), "cpu")


class TestInstallHint(unittest.TestCase):
    @patch("pdf2zh.engine_env.python_version", return_value=(3, 14))
    def test_beyond_range_magicpdf_hint(self, *_):
        self.assertIn("magic-pdf", mineru_install_hint())

    @patch("pdf2zh.engine_env.python_version", return_value=(3, 13))
    def test_py313_mineru_hint(self, *_):
        self.assertIn("mineru", mineru_install_hint())
        self.assertIn("pipeline", mineru_install_hint())


if __name__ == "__main__":
    unittest.main()
