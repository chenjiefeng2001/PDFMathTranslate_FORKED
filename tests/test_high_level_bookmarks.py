# -*- coding: utf-8 -*-
"""无头回归测试：PDF 书签（/Outlines）翻译与重建（P0-3）。

- 读取源 PDF outline 树
- 用与正文一致的翻译器翻译书签标题
- mono 文档（doc_zh）书签页码不变
- dual 文档（doc_en，双语交错 en0,zh0,en1,zh1,...）书签页码映射为 2n-1
- 翻译失败 / 无书签 / 无翻译器时安全跳过，不抛异常
"""
import unittest
from unittest.mock import Mock, patch

import fitz

from pdf2zh.high_level import _apply_bookmarks


def make_pdf_with_toc():
    doc = fitz.open()
    for _ in range(6):
        doc.new_page()
    doc.set_toc([
        [1, "Introduction", 1],
        [2, "Overview", 2],
        [1, "Chapter Two", 4],
    ])
    return doc


class BookmarkApplyTest(unittest.TestCase):
    def test_translate_and_write_back_pages(self):
        src = make_pdf_with_toc()
        data = src.tobytes()
        src.close()

        doc_zh = fitz.open(stream=data, filetype="pdf")
        # dual 文档在真实管线中是双语交错 en0,zh0,en1,zh1,...（2x 页），
        # 这里模拟 12 页，使 2n-1 页码映射（1,3,7）均在页数范围内。
        doc_en = fitz.open()
        for _ in range(12):
            doc_en.new_page()

        stub = Mock()
        stub.translate = Mock(side_effect=lambda t: "T:" + t)
        with patch("pdf2zh.high_level.build_translator", return_value=stub):
            _apply_bookmarks(
                doc_zh, doc_en, data,
                service="stub", lang_in="en", lang_out="zh",
                envs=None, prompt=None,
            )

        # mono：页码不变，标题已翻译
        self.assertEqual(doc_zh.get_toc(), [
            [1, "T:Introduction", 1],
            [2, "T:Overview", 2],
            [1, "T:Chapter Two", 4],
        ])
        # dual：页码映射 2n-1（英文页），标题已翻译
        self.assertEqual(doc_en.get_toc(), [
            [1, "T:Introduction", 1],
            [2, "T:Overview", 3],
            [1, "T:Chapter Two", 7],
        ])
        doc_zh.close()
        doc_en.close()

    def test_no_outline_skips(self):
        doc = fitz.open()
        doc.new_page()
        data = doc.tobytes()
        doc.close()

        doc_zh = fitz.open(stream=data, filetype="pdf")
        doc_en = fitz.open(stream=data, filetype="pdf")

        stub = Mock()
        stub.translate = Mock(side_effect=lambda t: "T:" + t)
        with patch("pdf2zh.high_level.build_translator", return_value=stub):
            _apply_bookmarks(
                doc_zh, doc_en, data,
                service="stub", lang_in="en", lang_out="zh",
                envs=None, prompt=None,
            )
        self.assertEqual(doc_zh.get_toc(), [])
        doc_zh.close()
        doc_en.close()

    def test_translator_init_failure_skips(self):
        src = make_pdf_with_toc()
        data = src.tobytes()
        src.close()

        doc_zh = fitz.open(stream=data, filetype="pdf")
        doc_en = fitz.open(stream=data, filetype="pdf")

        with patch("pdf2zh.high_level.build_translator",
                   side_effect=ValueError("Unsupported translation service")):
            _apply_bookmarks(
                doc_zh, doc_en, data,
                service="bad_service", lang_in="en", lang_out="zh",
                envs=None, prompt=None,
            )
        # 失败仅跳过：不抛异常，保留原书签（未被覆盖）
        self.assertEqual(doc_zh.get_toc(), [
            [1, "Introduction", 1],
            [2, "Overview", 2],
            [1, "Chapter Two", 4],
        ])
        doc_zh.close()
        doc_en.close()

    def test_title_translate_failure_keeps_original(self):
        src = make_pdf_with_toc()
        data = src.tobytes()
        src.close()

        doc_zh = fitz.open(stream=data, filetype="pdf")
        doc_en = fitz.open(stream=data, filetype="pdf")

        stub = Mock()
        stub.translate = Mock(side_effect=RuntimeError("network down"))
        with patch("pdf2zh.high_level.build_translator", return_value=stub):
            _apply_bookmarks(
                doc_zh, doc_en, data,
                service="stub", lang_in="en", lang_out="zh",
                envs=None, prompt=None,
            )
        self.assertEqual(doc_zh.get_toc()[0], [1, "Introduction", 1])
        doc_zh.close()
        doc_en.close()


if __name__ == "__main__":
    unittest.main()
