"""mono/dual naming after the document merge (high_level.translate_stream).

A 325-page scanned book produced a 650-page ``-mono.pdf``. Root cause: the
merge inserts the translated document into the original one and interleaves
them, so afterwards

    doc_en = [orig, zh, orig, zh, ...]   2N pages  -> this is "dual"
    doc_zh = [zh, zh, ...]               N pages   -> this is "mono"

but the write-out assigned ``doc_dual = doc_zh`` / ``doc_mono = doc_en``.
``_execute_legacy`` then wrote those bytes to ``{stem}-dual.pdf`` /
``{stem}-mono.pdf`` verbatim, so the user got an interleaved document
labelled "mono" -- which reads as duplicated/overlapping pages.

These tests pin the mapping using the real merge primitives and the real
write-out statements, without needing the layout model or a translator.
"""

import unittest

import pymupdf

from pdf2zh.high_level import _interleave_dual_pages, _splice_mono_pages


def _src(n=3):
    doc = pymupdf.open()
    for i in range(n):
        doc.new_page(width=200, height=200).insert_text((20, 40), f"ORIGINAL {i}")
    return doc.tobytes()


def _translated(n=3):
    doc = pymupdf.open()
    for i in range(n):
        doc.new_page(width=200, height=200).insert_text((20, 40), f"TRANSLATED {i}")
    return doc.tobytes()


def _slice_dual(n=3):
    """``_interleave_dual_pages`` 期望的 slice_dual：0 基奇数位是译页。"""
    merged = pymupdf.open()
    src = pymupdf.open(stream=_src(n), filetype="pdf")
    tr = pymupdf.open(stream=_translated(n), filetype="pdf")
    for i in range(n):
        merged.insert_pdf(src, from_page=i, to_page=i)
        merged.insert_pdf(tr, from_page=i, to_page=i)
    out = merged.tobytes()
    merged.close()
    src.close()
    tr.close()
    return out


def _texts(data):
    doc = pymupdf.open(stream=data, filetype="pdf")
    try:
        return [" ".join(doc[i].get_text().split()) for i in range(doc.page_count)]
    finally:
        doc.close()


class TestInterleaveProducesDoublePageCount(unittest.TestCase):
    def test_interleave_doubles_pages(self):
        out = _interleave_dual_pages(_src(), _slice_dual(), [0, 1, 2])
        doc = pymupdf.open(stream=out, filetype="pdf")
        try:
            self.assertEqual(
                doc.page_count, 6, "交错产物必须是 2N 页（325 页源 → 650 页）"
            )
        finally:
            doc.close()

    def test_interleave_never_drops_or_repeats_a_page(self):
        out = _interleave_dual_pages(_src(), _slice_dual(), [0, 1, 2])
        texts = _texts(out)
        originals = [t for t in texts if t.startswith("ORIGINAL")]
        translated = [t for t in texts if t.startswith("TRANSLATED")]
        self.assertEqual(len(originals), 3, "原文侧必须恰好 N 页")
        self.assertEqual(len(translated), 3, "译文侧必须恰好 N 页")
        self.assertEqual(len(set(originals)), 3, "原文页不得重复")
        self.assertEqual(len(set(translated)), 3, "译文页不得重复")

    def test_splice_keeps_page_count(self):
        out = _splice_mono_pages(_src(), _translated(), [0, 1, 2])
        doc = pymupdf.open(stream=out, filetype="pdf")
        try:
            self.assertEqual(doc.page_count, 3, "原位回贴必须保持页数（mono = N 页）")
        finally:
            doc.close()


class TestWriteOutAssignsTheRightDocumentToEachName(unittest.TestCase):
    """The two write-out statements are the defect site; pin their operands."""

    def test_dual_comes_from_the_merged_document(self):
        import inspect

        import pdf2zh.high_level as hl

        src = inspect.getsource(hl.translate_stream)
        self.assertIn(
            "doc_dual = doc_en.write(",
            src,
            "dual 必须取自 merge 后的 doc_en（2N 页交错）",
        )
        self.assertIn("doc_mono = doc_zh.write(", src, "mono 必须取自 doc_zh（纯译文）")

    def test_no_swapped_assignment_remains(self):
        import inspect

        import pdf2zh.high_level as hl

        src = inspect.getsource(hl.translate_stream)
        self.assertNotIn("doc_dual = doc_zh.write(", src)
        self.assertNotIn("doc_mono = doc_en.write(", src)

    def test_return_order_stays_dual_then_mono(self):
        """调用方按位置解包 ``doc_dual, doc_mono = translate_stream(...)``。"""
        import inspect

        import pdf2zh.high_level as hl

        src = inspect.getsource(hl.translate_stream)
        self.assertIn("return (doc_dual, doc_mono)", src)


class TestLegacyWriteOutKeepsNamesAligned(unittest.TestCase):
    """runtime_service 落盘处不能把变量名与文件名交叉。"""

    def test_paths_and_variables_match(self):
        import inspect

        from pdf2zh.services import runtime_service

        src = inspect.getsource(runtime_service.RuntimeService._execute_legacy)
        self.assertIn("doc_dual, doc_mono = translate_stream(", src)
        mono_path_at = src.index("mono_path = os.path.join")
        dual_path_at = src.index("dual_path = os.path.join")
        mono_write_at = src.index("f.write(doc_mono)")
        dual_write_at = src.index("f.write(doc_dual)")
        self.assertLess(mono_path_at, mono_write_at)
        self.assertLess(dual_path_at, dual_write_at)
        # result_files 也必须按同一顺序声明
        listing = src[src.index("result_files = [") :]
        self.assertLess(listing.index("mono.pdf"), listing.index("dual.pdf"))


if __name__ == "__main__":
    unittest.main()
