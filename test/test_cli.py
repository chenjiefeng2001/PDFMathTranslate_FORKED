import importlib
import sys
import unittest


class TestCliVersion(unittest.TestCase):
    def tearDown(self):
        for module_name in [
            "pdf2zh",
            "pdf2zh.pdf2zh",
            "pdf2zh.high_level",
            "pdf2zh.doclayout",
        ]:
            sys.modules.pop(module_name, None)

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
