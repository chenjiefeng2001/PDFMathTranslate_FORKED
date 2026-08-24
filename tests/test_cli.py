import importlib
import sys
import unittest
from unittest.mock import Mock, patch


class TestCliVersion(unittest.TestCase):
    _LAZY = ["pdf2zh", "pdf2zh.pdf2zh", "pdf2zh.high_level", "pdf2zh.doclayout"]

    def setUp(self):
        # 记录测试开始前的模块对象；tearDown 恢复，避免污染同进程后续测试
        # （同进程内 pop 后 patch 会 import 出第二个模块实例，patch 不生效）
        self._saved = {name: sys.modules.get(name) for name in self._LAZY}

    def tearDown(self):
        for name, mod in self._saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod

    def test_importing_package_does_not_eagerly_load_translation_pipeline(self):
        before = set(sys.modules)
        pkg = importlib.import_module("pdf2zh")

        self.assertEqual(pkg.__version__, "1.9.12")
        # diff-based: assert importing pdf2zh introduces no heavy modules,
        # regardless of what earlier tests already imported into sys.modules
        newly_imported = set(sys.modules) - before
        self.assertNotIn("pdf2zh.high_level", newly_imported)

    def test_version_flag_exits_before_loading_heavy_modules(self):
        cli = importlib.import_module("pdf2zh.pdf2zh")

        before = set(sys.modules)

        with self.assertRaises(SystemExit) as exit_context:
            cli.main(["-v"])

        self.assertEqual(exit_context.exception.code, 0)
        newly_imported = set(sys.modules) - before
        self.assertNotIn("pdf2zh.high_level", newly_imported)
        self.assertNotIn("pdf2zh.doclayout", newly_imported)


class TestCliInputValidation(unittest.TestCase):
    """CLI 输入存在性校验：引擎路由前给出明确错误而非下游 open() 混乱栈。"""

    def test_missing_pdf_raises_file_not_found(self):
        from pdf2zh.pdf2zh import main

        with (
            patch("pdf2zh.doclayout.set_backend"),
            self.assertRaises(FileNotFoundError) as ctx,
        ):
            main(["definitely_missing_input.pdf", "--parse-engine", "legacy"])
        self.assertIn("definitely_missing_input.pdf", str(ctx.exception))

    def test_directory_as_input_raises_file_not_found(self):
        # 非 --dir 模式下目录输入同样是无效 PDF（曾表现为 PermissionError）
        from pdf2zh.pdf2zh import main

        with (
            patch("pdf2zh.doclayout.set_backend"),
            patch("pdf2zh.doclayout.ModelInstance"),
            self.assertRaises(FileNotFoundError),
        ):
            main([".", "--parse-engine", "legacy"])


class TestDoclayoutModelLazyLoad(unittest.TestCase):
    """版面分析模型懒加载：从 CLI 全局入口下沉到 legacy/babeldoc 轨。"""

    def test_loads_only_when_singleton_empty(self):
        from pdf2zh import doclayout
        from pdf2zh.pdf2zh import _ensure_doclayout_model

        ns = Mock(onnx="")
        saved = doclayout.ModelInstance.value
        try:
            doclayout.ModelInstance.value = None
            with patch(
                "pdf2zh.doclayout.OnnxModel.load_available",
                return_value=Mock(name="model"),
            ) as load_avail:
                _ensure_doclayout_model(ns)
            load_avail.assert_called_once()

            # 幂等：单例已有值时不得重复加载
            with patch("pdf2zh.doclayout.OnnxModel.load_available") as load_avail2:
                _ensure_doclayout_model(ns)
            load_avail2.assert_not_called()
        finally:
            doclayout.ModelInstance.value = saved

    def test_explicit_onnx_rebuilds(self):
        from pdf2zh import doclayout
        from pdf2zh.pdf2zh import _ensure_doclayout_model

        saved = doclayout.ModelInstance.value
        try:
            existing = Mock(name="existing")
            doclayout.ModelInstance.value = existing
            explicit = Mock(name="explicit")
            with patch("pdf2zh.doclayout.OnnxModel", return_value=explicit) as ctor:
                _ensure_doclayout_model(Mock(onnx="layout.onnx"))
            ctor.assert_called_once_with("layout.onnx")
            self.assertIs(doclayout.ModelInstance.value, explicit)
        finally:
            doclayout.ModelInstance.value = saved


if __name__ == "__main__":
    unittest.main()
