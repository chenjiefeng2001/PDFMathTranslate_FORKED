"""
Tests for the BabelDOC TOC-line (dot leader + page number) formula protect patch.

BabelDOC translates a whole TOC row like ``1 G. Müller .......... 27`` as a plain
text paragraph, so the dot leaders and the right-aligned page number get rewritten
by the machine translator. This patch reuses ``pdf2zh.toc`` detection to split the
"dot leaders + page number" tail into a ``PdfFormula`` composition that BabelDOC
keeps untouched (placeholder round-trip) and renders in place.

Covers:
  1. ``_toc_split_index``: dot-leader rows, space-separated page column, prose.
  2. ``_try_protect_line``: a TOC line is split into title + formula with fake
     ``formula_layout_id``; plain lines are left untouched.
  3. ``get_babeldoc_toc_protect_enabled``: env switch parsing (default on).
  4. ``apply_babeldoc_toc_protect``: idempotent patch install/restore on the real
     BabelDOC ``ParagraphFinder`` (skipped when babeldoc is absent).
  5. V1.24 merged-line split: when BabelDOC merges several TOC entries that share
     one physical row into a single ``PdfLine`` (``13.62 ... 388 13.63 ... 389``),
     every entry is split into its own title line + dot-leader/page formula
     (``_split_merged_toc_line`` / ``_try_protect_merged_line``).
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from pdf2zh import babeldoc_toc_protect as btp


def _mk_box(x, y, w=8.0, h=12.0):
    from babeldoc.format.pdf.document_il import Box

    return Box(x, y, x + w, y + h)


def _mk_char(char, x, y):
    """构造一个 BabelDOC PdfCharacter（带几何，供 track / update_formula_data）。"""
    from babeldoc.format.pdf.document_il import PdfCharacter

    return PdfCharacter(
        char_unicode=char,
        visual_bbox=SimpleNamespace(box=_mk_box(x, y)),
        xobj_id=1,
        vertical=False,
    )


def _mk_line(text, *, x0=72.0, y=120.0, char_w=8.0):
    """按字符顺序构造一行 PdfLine（每字符 8pt 宽，行宽 = 8*len）。"""
    from babeldoc.format.pdf.document_il import PdfLine

    chars = []
    x = x0
    for ch in text:
        chars.append(_mk_char(ch, x, y))
        x += char_w
    return PdfLine(pdf_character=chars)


def _mk_comp(text, *, x0=72.0, y=120.0):
    from babeldoc.format.pdf.document_il import PdfParagraphComposition

    return PdfParagraphComposition(pdf_line=_mk_line(text, x0=x0, y=y))


def _mk_page(*comps, width=600.0):
    """构造最小 Page 兼容对象（box 宽 600，右缘 672）。"""
    from babeldoc.format.pdf.document_il import Box

    return SimpleNamespace(
        box=Box(72, 72, 72 + width, 400),
        pdf_paragraph=[
            SimpleNamespace(
                box=Box(72, 100, 400, 200),
                pdf_paragraph_composition=list(comps),
                unicode="",
                layout_id=7,
                layout_label="plain text",
            )
        ],
    )


def _mk_chars(text, *, y=120.0, char_w=8.0):
    """按字符顺序构造 PdfCharacter 列表（每字符 char_w 宽，行宽 = char_w*len）。"""
    return [_mk_char(ch, 72.0 + i * char_w, y) for i, ch in enumerate(text)]


class TestMergedTocLine:
    """V1.24：BabelDOC 把同一物理行的多个目录条目合并成一条 PdfLine 时逐条拆分。"""

    MERGED = (
        "13.62 Symmetry: implicit treatment . . . .. 388 "
        "13.63 Symmetry: explicit treatment . . . .. 389 "
        "13.64 Treatment of positive definiteness . . . .. 389 "
        "*13.65 Information matrix . . . .. 390"
    )

    def test_split_merged_toc_line_returns_all_entries(self):
        chars = _mk_chars(self.MERGED)
        track = btp._build_offset_track(chars)
        entries = btp._split_merged_toc_line(self.MERGED, track, 600.0)
        assert len(entries) == 4
        titles = [e[0] for e in entries]
        assert titles[0].startswith("13.62 Symmetry")
        assert titles[1].startswith("13.63 Symmetry")
        assert titles[2].startswith("13.64 Treatment")
        assert titles[3].startswith("*13.65 Information matrix")
        assert [e[2] for e in entries] == ["388", "389", "389", "390"]

    def test_single_entry_line_not_merged(self):
        chars = _mk_chars("1 G. Müller .......... 27")
        track = btp._build_offset_track(chars)
        assert btp._split_merged_toc_line(
            "1 G. Müller .......... 27", track, 600.0
        ) == []

    def test_prose_line_not_merged(self):
        chars = _mk_chars("This is a normal sentence with 12 words and 3 numbers.")
        track = btp._build_offset_track(chars)
        assert btp._split_merged_toc_line(
            "This is a normal sentence with 12 words and 3 numbers.",
            track,
            600.0,
        ) == []

    def test_try_protect_merged_line_splits_every_entry(self):
        comp = _mk_comp(self.MERGED)
        page = _mk_page(comp)
        finder = SimpleNamespace(
            update_line_data=lambda *a, **k: None,
        )
        blocks = btp._try_protect_merged_line(finder, page, comp, 0)
        assert blocks is not None
        assert len(blocks) == 4
        for blk in blocks:
            assert len(blk) == 2
            assert blk[0].pdf_line is not None
            assert blk[1].pdf_formula is not None
        # 每条标题不再含点线/页码残留；公式以点线开头
        joined = ""
        for blk in blocks:
            title = "".join(
                c.char_unicode or "" for c in blk[0].pdf_line.pdf_character
            )
            formula = "".join(
                c.char_unicode or "" for c in blk[1].pdf_formula.pdf_character
            )
            joined += " " + title
            assert formula.lstrip().startswith(".")
            assert all(
                c.formula_layout_id for c in blk[1].pdf_formula.pdf_character
            )
        assert "388" not in joined and "389" not in joined and "390" not in joined

    def test_merged_line_page_level_split(self):
        from babeldoc.format.pdf.document_il import Box

        comp = _mk_comp(self.MERGED)
        page = _mk_page(comp)

        def update_line_data(line):
            chars = line.pdf_character
            line.box = Box(
                min(c.visual_bbox.box.x for c in chars),
                min(c.visual_bbox.box.y for c in chars),
                max(c.visual_bbox.box.x2 for c in chars),
                max(c.visual_bbox.box.y2 for c in chars),
            )

        def update_paragraph_data(paragraph, update_unicode=False):
            chars = []
            for c in paragraph.pdf_paragraph_composition:
                if c.pdf_line:
                    chars.extend(c.pdf_line.pdf_character)
                elif c.pdf_formula:
                    chars.extend(c.pdf_formula.pdf_character)
            if chars:
                paragraph.box = Box(
                    min(c.visual_bbox.box.x for c in chars),
                    min(c.visual_bbox.box.y for c in chars),
                    max(c.visual_bbox.box.x2 for c in chars),
                    max(c.visual_bbox.box.y2 for c in chars),
                )

        btp._protect_toc_lines_in_page(
            SimpleNamespace(
                update_line_data=update_line_data,
                update_paragraph_data=update_paragraph_data,
            ),
            page,
        )
        paras = page.pdf_paragraph
        assert len(paras) == 4
        for p in paras:
            comps = p.pdf_paragraph_composition
            assert len(comps) == 2
            assert comps[0].pdf_line is not None
            assert comps[1].pdf_formula is not None


class TestTocSplitIndex:
    def test_dot_leader_row(self):
        idx = btp._toc_split_index("1 G. Müller .......... 27")
        assert idx is not None
        assert idx > 0
        assert "1 G. Müller .......... 27"[:idx].rstrip() == "1 G. Müller"
        assert "1 G. Müller .......... 27"[idx:].lstrip().startswith("...")

    def test_space_page_column(self):
        idx = btp._toc_split_index("1 Introduction 4")
        assert idx is not None
        assert "1 Introduction 4"[idx:] == "4"

    def test_plain_prose(self):
        assert btp._toc_split_index("This is a normal sentence.") is None

    def test_decimal_tail(self):
        # 1.5 结尾不是目录页码（无点线、无 2+ 点线引导）
        assert btp._toc_split_index("Cost is 1.5") is None

    def test_leader_without_title(self):
        # 只有点线+页码、无标题：detect 判定 title 长度不足，不保护
        assert btp._toc_split_index(".... 27") is None


class TestTryProtectLine:
    def _finder(self):
        def update_line_data(line):
            chars = line.pdf_character
            from babeldoc.format.pdf.document_il import Box

            line.box = Box(
                min(c.visual_bbox.box.x for c in chars),
                min(c.visual_bbox.box.y for c in chars),
                max(c.visual_bbox.box.x2 for c in chars),
                max(c.visual_bbox.box.y2 for c in chars),
            )

        def update_paragraph_data(paragraph, update_unicode=False):
            chars = []
            for comp in paragraph.pdf_paragraph_composition:
                if comp.pdf_line:
                    chars.extend(comp.pdf_line.pdf_character)
                elif comp.pdf_formula:
                    chars.extend(comp.pdf_formula.pdf_character)
            if chars:
                from babeldoc.format.pdf.document_il import Box

                paragraph.box = Box(
                    min(c.visual_bbox.box.x for c in chars),
                    min(c.visual_bbox.box.y for c in chars),
                    max(c.visual_bbox.box.x2 for c in chars),
                    max(c.visual_bbox.box.y2 for c in chars),
                )
                paragraph.unicode = "".join(c.char_unicode or "" for c in chars)

        return SimpleNamespace(
            update_line_data=update_line_data,
            update_paragraph_data=update_paragraph_data,
        )

    def test_toc_line_split_into_formula(self):
        # 页码接近页面右缘（page width 600 → 右 20% 起于 480）
        text = "1 G. Müller .......... 27"
        comp = _mk_comp(text)
        page = _mk_page(comp)
        result = btp._try_protect_line(self._finder(), page, comp, 0)

        assert result is not None, "TOC 行应被保护"
        assert len(result) == 2
        line_comp, formula_comp = result

        # 标题行仅保留标题字符
        title = "".join(c.char_unicode or "" for c in line_comp.pdf_line.pdf_character)
        assert title == "1 G. Müller "

        # 公式包含点线 + 页码
        formula = formula_comp.pdf_formula
        assert formula is not None
        formula_text = "".join(c.char_unicode or "" for c in formula.pdf_character)
        assert formula_text.startswith("..")
        assert formula_text.endswith("27")
        # 所有公式字符打上非空 formula_layout_id（防 is_translatable_formula 转回文本）
        assert all(c.formula_layout_id for c in formula.pdf_character)
        # 公式 box 覆盖点线+页码几何
        assert formula.box.x < formula.box.x2
        assert formula.pdf_character  # 至少有一个字符

    def test_plain_line_untouched(self):
        comp = _mk_comp("This is a normal sentence.")
        page = _mk_page(comp)
        assert btp._try_protect_line(self._finder(), page, comp, 0) is None

    def test_toc_protect_lines_in_page(self):
        from babeldoc.format.pdf.document_il import Box
        from babeldoc.format.pdf.document_il import PdfParagraph

        toc = _mk_comp("1 G. Müller .......... 27")
        plain = _mk_comp("This is a normal sentence.", y=200.0)
        page = _mk_page(toc, plain)

        btp._protect_toc_lines_in_page(self._finder(), page)

        # TOC 行被拆成独立段落 [标题行, 公式]；普通行保留在原段落。
        paras = page.pdf_paragraph
        assert len(paras) == 2
        toc_para = paras[0]
        plain_para = paras[1]
        comps = toc_para.pdf_paragraph_composition
        assert len(comps) == 2
        assert comps[0].pdf_line is not None
        assert comps[1].pdf_formula is not None
        # 标题行仅保留标题字符
        assert "".join(
            c.char_unicode or "" for c in comps[0].pdf_line.pdf_character
        ) == "1 G. Müller "
        # 公式含点线+页码
        assert "".join(
            c.char_unicode or "" for c in comps[1].pdf_formula.pdf_character
        ).startswith("..")
        # 普通段落保持原文
        assert "".join(
            c.char_unicode or "" for c in plain_para.pdf_paragraph_composition[0].pdf_line.pdf_character
        ) == "This is a normal sentence."
        # 独立段落 box 已重算（非零）
        assert toc_para.box.x < toc_para.box.x2


class TestEnabledSwitch:
    def test_defaults_to_enabled(self, monkeypatch):
        monkeypatch.delenv(btp._ENV_TOC_PROTECT, raising=False)
        assert btp.get_babeldoc_toc_protect_enabled() is True

    def test_explicit_disable(self, monkeypatch):
        for value in ("0", "off", "false", "no"):
            monkeypatch.setenv(btp._ENV_TOC_PROTECT, value)
            assert btp.get_babeldoc_toc_protect_enabled() is False

    def test_explicit_enable(self, monkeypatch):
        for value in ("1", "on", "true"):
            monkeypatch.setenv(btp._ENV_TOC_PROTECT, value)
            assert btp.get_babeldoc_toc_protect_enabled() is True


class TestPatchLifecycle:
    def test_apply_is_idempotent_and_restorable(self):
        try:
            from babeldoc.format.pdf.document_il.midend.paragraph_finder import (  # noqa: PLC0415
                ParagraphFinder,
            )
        except Exception:
            pytest.skip("babeldoc not installed")
        original = ParagraphFinder.process_page
        try:
            assert btp.apply_babeldoc_toc_protect() is True
            assert btp.apply_babeldoc_toc_protect() is True  # 幂等
            assert ParagraphFinder.process_page is not original
        finally:
            assert btp.reset_babeldoc_toc_protect() is True
            assert ParagraphFinder.process_page is original

    def test_disabled_switch_keeps_original_pipeline(self, monkeypatch):
        """关闭开关时，包装后的 process_page 仍执行原始逻辑、不做保护。"""
        try:
            from babeldoc.format.pdf.document_il.midend.paragraph_finder import (  # noqa: PLC0415
                ParagraphFinder,
            )
        except Exception:
            pytest.skip("babeldoc not installed")

        original = ParagraphFinder.process_page
        calls = {"original": 0, "protect": 0}

        def fake_original(self_, page):
            calls["original"] += 1

        def fake_protect(self_, page):
            calls["protect"] += 1

        monkeypatch.setattr(ParagraphFinder, "process_page", fake_original)
        try:
            assert btp.apply_babeldoc_toc_protect() is True
            monkeypatch.setenv(btp._ENV_TOC_PROTECT, "0")
            try:
                btp._patched_process_page(object(), SimpleNamespace())
            finally:
                monkeypatch.delenv(btp._ENV_TOC_PROTECT, raising=False)
            assert calls["original"] == 1
            assert calls["protect"] == 0
        finally:
            ParagraphFinder.process_page = original
            btp.reset_babeldoc_toc_protect()

