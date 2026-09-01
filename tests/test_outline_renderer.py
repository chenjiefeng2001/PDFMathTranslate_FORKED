"""Commit 6D 验收：PDF native Outline / Bookmark 重建。

覆盖 ``pdf2zh.v3.outline_renderer`` + ``high_level._apply_bookmarks``：

单元（outline_renderer）：
- translated title 出现在 Outline
- destination 使用 destination_page（非 page_number）
- page_number != destination_page 测试
- nested levels 成为嵌套书签
- heading_ref destination preferred
- 缺失 heading_ref 回退 destination_page
- printed page number 永不用作 destination

集成（_apply_bookmarks → 真实 PDF set_toc/get_toc）：
- visual TOC（semantic entries）无 native outline → 创建 Outline
- native outline 无 visual TOC → 保留并翻译
- visual TOC + native outline → 不产生重复（semantic 优先）
- 两者皆无 → 不创建合成 Outline
- multi-page TOC 保持顺序
- translated title 不影响 destination
- 既有 List/Code/Style 不回归（由既有测试保证）
"""

import unittest
from unittest.mock import patch

import pymupdf

from pdf2zh.high_level import _apply_bookmarks
from pdf2zh.v3.outline_renderer import (
    build_outline_toc,
    extract_outline_entries,
    resolve_entry_destination,
)


def _entry(
    title="Introduction",
    translated_title=None,
    level=0,
    destination_page=5,
    page_number="12",
    heading_ref=None,
):
    return {
        "title": title,
        "title_only": title,
        "translated_title": translated_title or f"译_{title}",
        "level": level,
        "destination_page": destination_page,
        "page_number": page_number,
        "heading_ref": heading_ref,
    }


def _toc_block(entries, page_num=0):
    return {
        "kind": "toc",
        "metadata": {"kind": "toc", "toc_entries": list(entries)},
        "blocks": [],
    }


def _document_model(entries, page_nums=(0,)):
    return {
        "pages": [{"page": p, "blocks": [_toc_block(entries, p)]} for p in page_nums],
        "relations": [],
        "metadata": {},
    }


def _heading_doc_model(entries, heading_page=7):
    """heading_ref → heading block on a different page (to test preference)."""
    toc_page = {"page": 0, "blocks": [_toc_block(entries, 0)]}
    head_page = {
        "page": heading_page,
        "blocks": [
            {
                "kind": "heading",
                "metadata": {"kind": "heading"},
                "blocks": [],
            }
        ],
    }
    return {"pages": [toc_page, head_page], "relations": [], "metadata": {}}


class TestOutlineRendererUnit(unittest.TestCase):
    """outline_renderer 纯逻辑。"""

    # ── 1. translated title appears ─────────────────────────────────
    def test_translated_title_in_outline(self):
        entries = [_entry(title="Introduction", translated_title="引言")]
        out = build_outline_toc(entries)
        self.assertEqual(out[0][1], "引言")

    # ── 2/3. destination uses destination_page, != page_number ──────
    def test_destination_uses_destination_page(self):
        entries = [_entry(destination_page=15, page_number="12")]
        out = build_outline_toc(entries)
        self.assertEqual(out[0][2], 15)
        self.assertNotEqual(out[0][2], 12)  # printed page never used

    def test_printed_page_number_never_destination(self):
        # destination_page missing → falls back to default (1), not page_number
        entries = [_entry(destination_page=None, page_number="42")]
        out = build_outline_toc(entries)
        self.assertEqual(out[0][2], 1)
        self.assertNotEqual(out[0][2], 42)

    # ── 4. nested levels become nested bookmarks ────────────────────
    def test_nested_levels(self):
        entries = [
            _entry(title="Introduction", level=0, destination_page=1),
            _entry(title="Background", level=1, destination_page=2),
            _entry(title="Motivation", level=1, destination_page=3),
            _entry(title="Method", level=0, destination_page=10),
            _entry(title="Dataset", level=1, destination_page=11),
            _entry(title="Training", level=2, destination_page=12),
            _entry(title="Hyperparameters", level=3, destination_page=13),
        ]
        out = build_outline_toc(entries)
        self.assertEqual([o[0] for o in out], [1, 2, 2, 1, 2, 3, 4])

    # ── 5. heading_ref destination preferred ────────────────────────
    def test_heading_ref_preferred(self):
        # entry points to page 5 via destination_page, but heading block lives
        # at 0-based page 7 → heading destination 8 is preferred
        entries = [_entry(destination_page=5, page_number="12", heading_ref="p7_0")]
        model = _heading_doc_model(entries, heading_page=7)
        extracted = extract_outline_entries(model)
        self.assertEqual(resolve_entry_destination(extracted[0]), 8)  # 7 + 1

    # ── 6. missing heading_ref falls back ───────────────────────────
    def test_missing_heading_ref_falls_back(self):
        entries = [_entry(destination_page=5, page_number="12", heading_ref=None)]
        model = _heading_doc_model(entries, heading_page=7)
        extracted = extract_outline_entries(model)
        self.assertEqual(resolve_entry_destination(extracted[0]), 5)

    # ── extract: multi-page order preserved ─────────────────────────
    def test_multipage_order_preserved(self):
        model = {
            "pages": [
                {"page": 0, "blocks": [_toc_block([_entry(title="A First")], 0)]},
                {"page": 2, "blocks": [_toc_block([_entry(title="B Second")], 2)]},
            ],
            "relations": [],
            "metadata": {},
        }
        entries = extract_outline_entries(model)
        self.assertEqual([e["title"] for e in entries], ["A First", "B Second"])

    # ── translated title does not affect destination ────────────────
    def test_translation_does_not_change_destination(self):
        a = build_outline_toc([_entry(destination_page=7, translated_title="译A")])
        b = build_outline_toc([_entry(destination_page=7, translated_title="译B")])
        self.assertEqual(a[0][2], b[0][2])
        self.assertNotEqual(a[0][1], b[0][1])

    # ── no entries → empty ──────────────────────────────────────────
    def test_no_entries_empty(self):
        self.assertEqual(build_outline_toc([]), [])
        self.assertEqual(extract_outline_entries(None), [])


class TestApplyBookmarksPdf(unittest.TestCase):
    """_apply_bookmarks → 真实 PDF 的 set_toc/get_toc。"""

    def _build_docs(self, with_native_toc=False):
        doc_zh = pymupdf.open()
        doc_en = pymupdf.open()
        doc_zh.new_page(width=612, height=792)
        doc_zh.new_page(width=612, height=792)
        doc_en.new_page(width=612, height=792)
        doc_en.new_page(width=612, height=792)
        doc_en.new_page(width=612, height=792)
        doc_en.new_page(width=612, height=792)
        native = None
        if with_native_toc:
            native = pymupdf.open()
            native.new_page(width=612, height=792)
            native.set_toc([[1, "Native Only Title", 1]])
        return doc_zh, doc_en, native

    def _apply(self, doc_zh, doc_en, semantic_entries=None, native=None):
        stream = native.tobytes() if native is not None else b"%PDF-1.4 fake"

        class _Tr:
            def translate(self, s):
                return f"T:{s}"

        result = _apply_bookmarks(
            doc_zh,
            doc_en,
            stream,
            service="stub",
            lang_in="en",
            lang_out="zh-CN",
            envs={},
            prompt=None,
            ignore_cache=True,
            semantic_toc_entries=semantic_entries,
        )
        # 用与 _apply_bookmarks 相同的翻译器约定做 native-only 断言时，
        # 直接注入 translator 的路径在真实环境会构建翻译器 —— 这里用
        # 简单打桩：native-only 标题翻译在调用方（translate_stream）已
        # 由真实翻译器完成，单测只验证结构不崩、页码正确。
        return result

    # ── 7. visual TOC without native outline creates Outline ────────
    def test_semantic_only_creates_outline(self):
        doc_zh, doc_en, native = self._build_docs(with_native_toc=False)
        entries = [
            _entry(title="Introduction", translated_title="引言", destination_page=1),
            _entry(title="Method", translated_title="方法", destination_page=2),
        ]
        self._apply(doc_zh, doc_en, semantic_entries=entries)
        toc = doc_zh.get_toc()
        self.assertEqual(len(toc), 2)
        self.assertEqual([t[1] for t in toc], ["引言", "方法"])
        self.assertEqual([t[2] for t in toc], [1, 2])

    # ── 8. native outline without visual TOC stays functional ───────
    def test_native_only_stays_functional(self):
        doc_zh, doc_en, native = self._build_docs(with_native_toc=True)
        # 无 semantic entries → 回退 native outline 路径：翻译标题后重建。
        with patch(
            "pdf2zh.high_level.build_translator",
            return_value=type(
                "_Stub", (object,), {"translate": lambda self, s: f"T:{s}"}
            )(),
        ):
            self._apply(doc_zh, doc_en, semantic_entries=None, native=native)
        toc = doc_zh.get_toc()
        self.assertEqual(len(toc), 1)
        self.assertEqual(toc[0][1], "T:Native Only Title")
        self.assertEqual(toc[0][2], 1)

    # ── 9. visual TOC + native outline → no duplicates (semantic wins) ──
    def test_semantic_and_native_no_duplicates(self):
        doc_zh, doc_en, native = self._build_docs(with_native_toc=True)
        entries = [
            _entry(title="Introduction", translated_title="引言", destination_page=1),
            _entry(title="Method", translated_title="方法", destination_page=2),
        ]
        self._apply(doc_zh, doc_en, semantic_entries=entries, native=native)
        toc = doc_zh.get_toc()
        # semantic 优先：2 条语义条目（而非 native 1 条 + 语义 2 条 = 3 条）
        self.assertEqual(len(toc), 2)
        self.assertEqual([t[1] for t in toc], ["引言", "方法"])

    # ── 10. neither → no synthetic outline ──────────────────────────
    def test_none_no_outline(self):
        doc_zh, doc_en, native = self._build_docs(with_native_toc=False)
        self._apply(doc_zh, doc_en, semantic_entries=None, native=native)
        self.assertEqual(doc_zh.get_toc(), [])
        self.assertEqual(doc_en.get_toc(), [])

    # ── 11. multi-page TOC preserves ordering ───────────────────────
    def test_multipage_toc_ordering(self):
        doc_zh, doc_en, native = self._build_docs(with_native_toc=False)
        entries = [
            _entry(title="A First", translated_title="译_A", destination_page=1),
            _entry(title="B Second", translated_title="译_B", destination_page=2),
            _entry(title="C Third", translated_title="译_C", destination_page=2),
        ]
        self._apply(doc_zh, doc_en, semantic_entries=entries)
        toc = doc_zh.get_toc()
        self.assertEqual([t[1] for t in toc], ["译_A", "译_B", "译_C"])

    # ── 12. translated title does not affect destination ────────────
    def test_translation_no_destination_change(self):
        doc_zh, doc_en, native = self._build_docs(with_native_toc=False)
        a = [_entry(title="X", translated_title="译X", destination_page=2)]
        b = [_entry(title="X", translated_title="译X2", destination_page=2)]
        da = pymupdf.open()
        da.new_page()
        db = pymupdf.open()
        db.new_page()
        ea = pymupdf.open()
        ea.new_page()
        eb = pymupdf.open()
        eb.new_page()
        self._apply(da, ea, semantic_entries=a)
        self._apply(db, eb, semantic_entries=b)
        self.assertEqual(da.get_toc()[0][2], db.get_toc()[0][2])
        self.assertNotEqual(da.get_toc()[0][1], db.get_toc()[0][1])

    # ── 13. printed page number never used as bookmark destination ──
    def test_page_number_never_destination_in_pdf(self):
        doc_zh, doc_en, native = self._build_docs(with_native_toc=False)
        entries = [_entry(destination_page=None, page_number="42")]
        self._apply(doc_zh, doc_en, semantic_entries=entries)
        toc = doc_zh.get_toc()
        self.assertEqual(toc[0][2], 1)
        self.assertNotEqual(toc[0][2], 42)


if __name__ == "__main__":
    unittest.main()
