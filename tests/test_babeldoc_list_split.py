"""
Tests for the BabelDOC numbered-list-item paragraph split patch.

BabelDOC merges consecutive ``1. XXX`` / ``2. XXX`` list items into a single
``PdfParagraph`` (doclayout sees one ``plain text`` box), then translates the
whole list as one paragraph — breaking the page layout. This patch splits such
rows into independent paragraphs inside ``process_independent_paragraphs``.

Covers:
  1. ``is_list_item_line``: numeric / parenthesised / letter numbering,
     and non-matching rows (decimal numbers, years, plain prose).
  2. ``get_babeldoc_list_split_enabled``: env switch parsing (default on).
  3. ``apply_babeldoc_list_split``: idempotent patch install/restore on the
     real BabelDOC ``ParagraphFinder`` (skipped when babeldoc is absent).
  4. ``_split_list_items_in_paragraphs``: splits a multi-row paragraph at the
     first numbered row, keeps the leading prose, and leaves a single-row
     paragraph untouched.
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from pdf2zh import babeldoc_list_split as bls


def _mk_line(*texts: str):
    """构造一个 PdfLine 兼容对象（pdf_character 列表）。"""
    return SimpleNamespace(
        pdf_character=[SimpleNamespace(char_unicode=t) for t in texts],
    )


class TestIsListItemLine:
    def test_numeric_dot(self):
        assert bls.is_list_item_line(_mk_line("1.", " ", "First item"))

    def test_numeric_bracket(self):
        assert bls.is_list_item_line(_mk_line("1)", " ", "Second"))

    def test_parenthesised_number(self):
        assert bls.is_list_item_line(_mk_line("(2)", " ", "Item"))

    def test_ideographic_comma(self):
        assert bls.is_list_item_line(_mk_line("3、", "中文"))

    def test_letter(self):
        assert bls.is_list_item_line(_mk_line("a.", " ", "sub item"))
        assert bls.is_list_item_line(_mk_line("A)", " ", "sub item"))

    def test_rejects_decimal(self):
        # 1.5 不是列表编号（编号后必须跟空白）
        assert not bls.is_list_item_line(_mk_line("1.5", " ", "million"))

    def test_rejects_year(self):
        assert not bls.is_list_item_line(_mk_line("2024.", " ", "was"))

    def test_rejects_plain_prose(self):
        assert not bls.is_list_item_line(_mk_line("The", " ", "results"))


class TestEnabledSwitch:
    def test_defaults_to_enabled(self, monkeypatch):
        monkeypatch.delenv(bls._ENV_SPLIT_LIST, raising=False)
        assert bls.get_babeldoc_list_split_enabled() is True

    def test_explicit_disable(self, monkeypatch):
        for value in ("0", "off", "false", "no"):
            monkeypatch.setenv(bls._ENV_SPLIT_LIST, value)
            assert bls.get_babeldoc_list_split_enabled() is False

    def test_explicit_enable(self, monkeypatch):
        for value in ("1", "on", "true"):
            monkeypatch.setenv(bls._ENV_SPLIT_LIST, value)
            assert bls.get_babeldoc_list_split_enabled() is True


class TestSplitListItems:
    def _finder(self):
        """最小 ParagraphFinder 兼容对象：update_paragraph_data 重算 box/unicode。"""
        from babeldoc.format.pdf.document_il import Box

        def update_paragraph_data(paragraph, update_unicode=False):
            chars = []
            for comp in paragraph.pdf_paragraph_composition:
                if comp.pdf_line:
                    chars.extend(comp.pdf_line.pdf_character)
            if chars:
                xs = [c.visual_bbox.box.x for c in chars]
                ys = [c.visual_bbox.box.y for c in chars]
                xs2 = [c.visual_bbox.box.x2 for c in chars]
                ys2 = [c.visual_bbox.box.y2 for c in chars]
                paragraph.box = Box(min(xs), min(ys), max(xs2), max(ys2))
                paragraph.unicode = "".join(c.char_unicode or "" for c in chars)
                paragraph.xobj_id = chars[0].xobj_id

        finder = SimpleNamespace(update_paragraph_data=update_paragraph_data)
        return finder

    def _para(self, *texts, layout_id=7, layout_label="plain text", xobj=1):
        from babeldoc.format.pdf.document_il import Box
        from babeldoc.format.pdf.document_il import PdfParagraph
        from babeldoc.format.pdf.document_il import PdfParagraphComposition
        from babeldoc.format.pdf.document_il import PdfCharacter
        from babeldoc.format.pdf.document_il import PdfLine

        comps = []
        for i, text in enumerate(texts):
            chars = []
            x = 72.0
            for j, ch in enumerate(text):
                chars.append(
                    PdfCharacter(
                        char_unicode=ch,
                        visual_bbox=SimpleNamespace(
                            box=Box(
                                x,
                                100.0 + i * 20.0,
                                x + 8.0,
                                100.0 + i * 20.0 + 12.0,
                            ),
                        ),
                        xobj_id=xobj,
                        vertical=False,
                    )
                )
                x += 8.0
            comps.append(PdfParagraphComposition(pdf_line=PdfLine(pdf_character=chars)))
        return PdfParagraph(
            box=Box(72, 100, 400, 200),
            pdf_paragraph_composition=comps,
            unicode="",
            debug_id="test",
            layout_label=layout_label,
            layout_id=layout_id,
        )

    def test_splits_multi_item_list(self):
        finder = self._finder()
        paragraphs = [
            self._para(
                "1. This is list item number 1",
                "2. This is list item number 2",
                "3. This is list item number 3",
            )
        ]
        bls._split_list_items_in_paragraphs(finder, paragraphs)
        # 每个列表项独立成段
        assert len(paragraphs) == 3
        assert [p.unicode for p in paragraphs] == [
            "1. This is list item number 1",
            "2. This is list item number 2",
            "3. This is list item number 3",
        ]
        for p in paragraphs:
            assert p.layout_id == 7
            assert p.layout_label == "plain text"

    def test_keeps_leading_prose(self):
        finder = self._finder()
        paragraphs = [
            self._para(
                "Intro line before the list",
                "1. First item",
                "2. Second item",
            )
        ]
        bls._split_list_items_in_paragraphs(finder, paragraphs)
        assert [p.unicode for p in paragraphs] == [
            "Intro line before the list",
            "1. First item",
            "2. Second item",
        ]

    def test_single_row_paragraph_untouched(self):
        finder = self._finder()
        paragraphs = [self._para("1. Single item")]
        bls._split_list_items_in_paragraphs(finder, paragraphs)
        assert len(paragraphs) == 1
        # 单行段落本就独立成段，无需拆分（unicode 由 BabelDOC 后续流程设置）
        assert len(paragraphs[0].pdf_paragraph_composition) == 1

    def test_non_list_paragraph_untouched(self):
        finder = self._finder()
        paragraphs = [
            self._para(
                "The year 2024. was good",
                "Results show improvement",
            )
        ]
        bls._split_list_items_in_paragraphs(finder, paragraphs)
        assert len(paragraphs) == 1


class TestPatchLifecycle:
    def test_apply_is_idempotent_and_restorable(self):
        try:
            from babeldoc.format.pdf.document_il.midend.paragraph_finder import (  # noqa: PLC0415
                ParagraphFinder,
            )
        except Exception:
            pytest.skip("babeldoc not installed")
        original = ParagraphFinder.process_independent_paragraphs
        try:
            assert bls.apply_babeldoc_list_split() is True
            assert bls.apply_babeldoc_list_split() is True  # 幂等
            assert ParagraphFinder.process_independent_paragraphs is not original
        finally:
            assert bls.reset_babeldoc_list_split() is True
            assert ParagraphFinder.process_independent_paragraphs is original


def _line_chars_text(line) -> str:
    """行 composition 的文本（含 dummy 容错）。"""
    return "".join((c.char_unicode or "") for c in line.pdf_character)


class TestProtectListPrefixes:
    """``_protect_list_prefixes_in_paragraphs``：编号前缀 → 公式 composition。"""

    def _finder(self):
        from babeldoc.format.pdf.document_il import Box

        def update_line_data(line):
            xs = [c.visual_bbox.box.x for c in line.pdf_character]
            line.box = Box(min(xs), 100.0, max(xs) + 8.0, 112.0)

        def update_paragraph_data(paragraph, update_unicode=False):
            chars = []
            for comp in paragraph.pdf_paragraph_composition:
                if comp.pdf_line:
                    chars.extend(comp.pdf_line.pdf_character)
            paragraph.unicode = "".join(c.char_unicode or "" for c in chars)
            if chars:
                xs = [c.visual_bbox.box.x for c in chars]
                ys = [c.visual_bbox.box.y for c in chars]
                xs2 = [c.visual_bbox.box.x2 for c in chars]
                ys2 = [c.visual_bbox.box.y2 for c in chars]
                paragraph.box = Box(min(xs), min(ys), max(xs2), max(ys2))

        return SimpleNamespace(
            update_line_data=update_line_data,
            update_paragraph_data=update_paragraph_data,
        )

    def _para(self, text):
        from babeldoc.format.pdf.document_il import Box
        from babeldoc.format.pdf.document_il import PdfParagraph
        from babeldoc.format.pdf.document_il import PdfParagraphComposition
        from babeldoc.format.pdf.document_il import PdfCharacter
        from babeldoc.format.pdf.document_il import PdfLine

        chars = []
        x = 72.0
        for ch in text:
            chars.append(
                PdfCharacter(
                    char_unicode=ch,
                    visual_bbox=SimpleNamespace(
                        box=Box(x, 100.0, x + 8.0, 112.0),
                    ),
                    xobj_id=1,
                    vertical=False,
                )
            )
            x += 8.0
        return PdfParagraph(
            box=Box(72, 100, 400, 200),
            pdf_paragraph_composition=[
                PdfParagraphComposition(pdf_line=PdfLine(pdf_character=chars)),
            ],
            unicode="",
            debug_id="test",
            layout_label="plain text",
            layout_id=7,
        )

    def test_numeric_prefix_becomes_formula(self):
        paragraphs = [self._para("1. First item")]
        bls._protect_list_prefixes_in_paragraphs(self._finder(), paragraphs)
        comps = paragraphs[0].pdf_paragraph_composition
        assert len(comps) == 2
        formula = comps[0].pdf_formula
        assert formula is not None
        assert _line_chars_text(comps[0].pdf_formula) == "1."
        # 行收缩为正文
        assert _line_chars_text(comps[1].pdf_line) == " First item"
        # 假 formula_layout_id / line_id 打标（防转回普通文本 / 防误合并）
        assert all(c.formula_layout_id for c in formula.pdf_character)
        assert formula.line_id < 0

    def test_full_line_number_not_protected(self):
        # 整行都是编号（无正文可翻译）：保持原样，不保护
        paragraphs = [self._para("（一）")]
        bls._protect_list_prefixes_in_paragraphs(self._finder(), paragraphs)
        comps = paragraphs[0].pdf_paragraph_composition
        assert len(comps) == 1
        assert comps[0].pdf_formula is None
        assert comps[0].pdf_line is not None

    def test_plain_line_untouched(self):
        paragraphs = [self._para("Intro text here")]
        bls._protect_list_prefixes_in_paragraphs(self._finder(), paragraphs)
        comps = paragraphs[0].pdf_paragraph_composition
        assert len(comps) == 1
        assert comps[0].pdf_formula is None
        assert _line_chars_text(comps[0].pdf_line) == "Intro text here"


class TestMergeContinuationLines:
    """``_merge_continuation_lines_in_paragraphs``：缩进续行合并回列表项。"""

    def _finder(self):
        def update_paragraph_data(paragraph, update_unicode=False):
            return None

        return SimpleNamespace(update_paragraph_data=update_paragraph_data)

    def _mk_char(self, ch, x, xobj=1):
        from babeldoc.format.pdf.document_il import Box

        return SimpleNamespace(
            char_unicode=ch,
            visual_bbox=SimpleNamespace(box=Box(x, 100.0, x + 8.0, 112.0)),
            xobj_id=xobj,
            vertical=False,
        )

    def _para(self, text, x0=72.0, layout_id=7, label="plain text"):
        from babeldoc.format.pdf.document_il import Box
        from babeldoc.format.pdf.document_il import PdfParagraph
        from babeldoc.format.pdf.document_il import PdfParagraphComposition
        from babeldoc.format.pdf.document_il import PdfLine

        chars = [self._mk_char(ch, x0 + i * 8.0) for i, ch in enumerate(text)]
        return PdfParagraph(
            box=Box(x0, 100, x0 + 8.0 * len(text), 112),
            pdf_paragraph_composition=[
                PdfParagraphComposition(pdf_line=PdfLine(pdf_character=chars)),
            ],
            unicode="",
            debug_id="test",
            layout_label=label,
            layout_id=layout_id,
        )

    def test_merges_indented_continuation(self):
        paragraphs = [
            self._para("1. First item"),
            self._para("continued line", x0=80.0),
        ]
        bls._merge_continuation_lines_in_paragraphs(self._finder(), paragraphs)
        assert len(paragraphs) == 1
        assert len(paragraphs[0].pdf_paragraph_composition) == 2

    def test_keeps_next_list_item(self):
        paragraphs = [
            self._para("1. First item"),
            self._para("2. Second item"),
        ]
        bls._merge_continuation_lines_in_paragraphs(self._finder(), paragraphs)
        assert len(paragraphs) == 2

    def test_keeps_different_layout(self):
        paragraphs = [
            self._para("1. First item"),
            self._para("new paragraph", layout_id=8),
        ]
        bls._merge_continuation_lines_in_paragraphs(self._finder(), paragraphs)
        assert len(paragraphs) == 2


class TestPrefixCharCount:
    def test_empty_chars(self):
        assert bls._prefix_char_count([], "1.") == 0

    def test_empty_prefix(self):
        from types import SimpleNamespace

        chars = [SimpleNamespace(char_unicode="x")]
        assert bls._prefix_char_count(chars, "") == 0

    def test_count_matches_prefix_len(self):
        from types import SimpleNamespace

        chars = [SimpleNamespace(char_unicode=c) for c in "1. First"]
        assert bls._prefix_char_count(chars, "1.") == 2

    def test_returns_all_when_overshoot(self):
        from types import SimpleNamespace

        chars = [SimpleNamespace(char_unicode=c) for c in "12"]
        # 前缀文本比字符总长还长：返回全部字符数
        assert bls._prefix_char_count(chars, "999.") == 2


class TestPatchedPIPDegradesGracefully:
    """``_patched_process_independent_paragraphs`` 任一环节失败都不阻断翻译。"""

    def test_post_step_exception_is_swallowed(self, monkeypatch):
        from babeldoc.format.pdf.document_il import Box
        from babeldoc.format.pdf.document_il import PdfParagraph

        original_called = []

        def fake_original(self_, paragraphs, median_width):
            original_called.append(True)
            # 原逻辑结束后段落仍存在
            assert len(paragraphs) == 1

        def boom(self_, paragraphs):
            raise RuntimeError("deadlock-style failure")

        monkeypatch.setattr(bls, "_ORIGINAL_PIP", fake_original)
        monkeypatch.setattr(bls, "_split_list_items_in_paragraphs", boom)
        monkeypatch.setattr(bls, "_merge_continuation_lines_in_paragraphs", boom)
        monkeypatch.setattr(bls, "_protect_list_prefixes_in_paragraphs", boom)
        monkeypatch.setattr(
            bls,
            "get_babeldoc_list_split_enabled",
            lambda: True,
        )

        paragraphs = [PdfParagraph(box=Box(0, 0, 1, 1), unicode="")]
        # 不抛异常（原段落保留，翻译继续）
        bls._patched_process_independent_paragraphs(None, paragraphs, 10.0)
        assert original_called == [True]
        assert len(paragraphs) == 1
