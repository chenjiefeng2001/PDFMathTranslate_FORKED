import importlib
import sys
import unittest


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

        self.assertEqual(pkg.__version__, "1.9.11")
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


if __name__ == "__main__":
    unittest.main()
