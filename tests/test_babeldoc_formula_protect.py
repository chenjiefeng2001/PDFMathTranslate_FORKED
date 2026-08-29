"""BabelDOC 公式检测过度识别缓解补丁的单元测试。

覆盖：
- 开关解析（PDF2ZH_BABELDOC_FORMULA_PROTECT，默认开启）；
- ``_looks_like_misclassified_text``：CJK / 多英文单词 → 误判文本；真公式
  （符号/单字母串为主）→ 不转回；
- ``apply_babeldoc_formula_protect`` 幂等安装（babeldoc 可用时验证行为）。
"""

import os
import unittest

from pdf2zh.babeldoc_formula_protect import (
    _looks_like_misclassified_text,
    apply_babeldoc_formula_protect,
    get_babeldoc_formula_protect_enabled,
)


def _make_char(text: str) -> object:
    return type(
        "C",
        (),
        {
            "char_unicode": text,
            "formula_layout_id": 0,
        },
    )()


def _make_formula(text: str, y_offset: float = 0.0) -> object:
    return type(
        "F",
        (),
        {
            "pdf_character": [_make_char(ch) for ch in text],
            "y_offset": y_offset,
        },
    )()


class TestEnvSwitch(unittest.TestCase):
    def tearDown(self) -> None:
        os.environ.pop("PDF2ZH_BABELDOC_FORMULA_PROTECT", None)

    def test_default_on(self):
        os.environ.pop("PDF2ZH_BABELDOC_FORMULA_PROTECT", None)
        self.assertTrue(get_babeldoc_formula_protect_enabled())

    def test_off_values(self):
        for v in ("0", "off", "false", "no"):
            os.environ["PDF2ZH_BABELDOC_FORMULA_PROTECT"] = v
            self.assertFalse(get_babeldoc_formula_protect_enabled())

    def test_on_values(self):
        for v in ("1", "on", "true"):
            os.environ["PDF2ZH_BABELDOC_FORMULA_PROTECT"] = v
            self.assertTrue(get_babeldoc_formula_protect_enabled())


class TestMisclassifiedTextSignal(unittest.TestCase):
    """误判文本信号判定：CJK / 多英文单词 → True；真公式 → False。"""

    def test_cjk_text_is_misclassified(self):
        # 色块上的中文「含公式文本块」被 BabelDOC 判为公式 → 应转回翻译。
        self.assertTrue(
            _looks_like_misclassified_text(
                _make_formula("设 E = mc² 成立，则能量守恒。")
            )
        )

    def test_multi_english_words_is_misclassified(self):
        # 英文句子（≥2 个完整单词）→ 误判文本。
        self.assertTrue(
            _looks_like_misclassified_text(
                _make_formula("Let E = mc^2 denote the energy relation.")
            )
        )

    def test_true_formula_not_touched(self):
        # 真公式：符号/单字母/短串为主，无完整单词 → 不转回（保持公式）。
        self.assertFalse(_looks_like_misclassified_text(_make_formula("E = mc^2")))
        self.assertFalse(_looks_like_misclassified_text(_make_formula("x_i + y_j")))

    def test_pure_numbers_not_touched(self):
        self.assertFalse(_looks_like_misclassified_text(_make_formula("2.5, 3.7")))


class TestPatchInstall(unittest.TestCase):
    def setUp(self) -> None:
        os.environ.pop("PDF2ZH_BABELDOC_FORMULA_PROTECT", None)

    def tearDown(self) -> None:
        os.environ.pop("PDF2ZH_BABELDOC_FORMULA_PROTECT", None)

    def test_disabled_switch_skips_install(self):
        os.environ["PDF2ZH_BABELDOC_FORMULA_PROTECT"] = "0"
        try:
            from babeldoc.format.pdf.document_il.midend.styles_and_formulas import (
                StylesAndFormulas,
            )
        except Exception:  # noqa: BLE001
            self.skipTest("babeldoc not installed")

        apply_babeldoc_formula_protect()
        patched = getattr(StylesAndFormulas, "is_translatable_formula", None)
        # 未安装 → 仍是 BabelDOC 原始方法（模块名不含补丁模块）。
        self.assertFalse(
            "babeldoc_formula_protect" in getattr(patched, "__module__", ""),
            "开关关闭时不应安装补丁",
        )

    def test_install_is_idempotent_and_behavior(self):
        """babeldoc 可用时：幂等安装 + 含文本公式块转回翻译。"""
        try:
            from babeldoc.format.pdf.document_il.midend.styles_and_formulas import (
                StylesAndFormulas,
            )
        except Exception:  # noqa: BLE001 -- babeldoc 未装则跳过
            self.skipTest("babeldoc not installed")

        apply_babeldoc_formula_protect()
        apply_babeldoc_formula_protect()  # 幂等
        patched = getattr(
            StylesAndFormulas, "is_translatable_formula", None
        )
        # 原始方法名已被替换为补丁实现（模块级函数）。
        self.assertTrue(
            callable(patched)
            and "babeldoc_formula_protect" in getattr(patched, "__module__", ""),
            "is_translatable_formula 应被替换为补丁实现",
        )

        inst = StylesAndFormulas.__new__(StylesAndFormulas)
        inst.translation_config = None
        inst.font_mapper = None

        # 含英文句子的「公式块」→ 补丁判定可翻译（转回普通文本）。
        self.assertTrue(
            patched(inst, _make_formula("Let E = mc^2 denote energy."))
        )
        # 纯数学符号公式（无完整单词）→ 保持公式。
        self.assertFalse(patched(inst, _make_formula("x_i + y_j")))


if __name__ == "__main__":
    unittest.main()
