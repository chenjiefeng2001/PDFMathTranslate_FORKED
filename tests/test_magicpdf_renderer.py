"""magicpdf 渲染接管（render_plan → PDF）单元测试。

覆盖：
- 合成 render_plan（段落译文 / 公式原文 / 代码原文 / 空文本）→ PDF bytes；
- 页数、文本层内容、坐标翻转（v3 y 向上 → PDF 左上原点）正确；
- 多页 + page_sizes 定制、缺失页回退默认尺寸；
- 空 plan / 缺 dst_box / 非法字号兜底；
- ``output_path`` 落盘可打开；
- ``run_magicpdf_main`` 集成：默认产出 ``{stem}_mono.pdf``，
  ``magicpdf_render=False`` 不产出。
"""
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

import pymupdf

from pdf2zh.v3.magicpdf_renderer import render_plan_to_pdf

# v3 坐标系（左下原点、y 向上）：段落译文 + 公式原文 + 代码原文 + 空文本 +
# 第二页标题（缺 page_sizes → 默认 612x792）。
PLAN = [
    {
        "block_id": "p0_0", "page": 0, "kind": "paragraph",
        "text": "Hello", "translated": "你好",
        "render_path": "translate_refit",
        "src_box": [50, 700, 550, 720], "dst_box": [50, 700, 550, 720],
        "font_size": 12.0,
    },
    {
        "block_id": "p0_1", "page": 0, "kind": "formula",
        "text": "x = a + b", "translated": "x = a + b",
        "render_path": "preserve_float",
        "src_box": [200, 600, 400, 620], "dst_box": [200, 600, 400, 620],
        "font_size": 14.0,
    },
    {
        "block_id": "p0_2", "page": 0, "kind": "code",
        "text": "def f():", "translated": "def f():",
        "render_path": "preserve_float",
        "src_box": [50, 550, 300, 570], "dst_box": [50, 550, 300, 570],
        "font_size": 10.0,
    },
    # 空文本块：应被安全跳过。
    {
        "block_id": "p0_3", "page": 0, "kind": "paragraph",
        "text": "", "translated": "",
        "render_path": "translate_refit",
        "src_box": [0, 0, 0, 0], "dst_box": [0, 0, 0, 0],
        "font_size": 12.0,
    },
    {
        "block_id": "p1_0", "page": 1, "kind": "heading",
        "text": "Title", "translated": "标题",
        "render_path": "translate_refit",
        "src_box": [50, 750, 550, 770], "dst_box": [50, 750, 550, 770],
        "font_size": 18.0,
    },
]

PAGE_SIZES = {0: [612.0, 792.0]}



class TestRenderPlanToPdf(unittest.TestCase):
    def test_render_bytes_and_text(self):
        pdf, stats = render_plan_to_pdf(PLAN, page_sizes=PAGE_SIZES)
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertEqual(stats["pages"], 2)
        self.assertEqual(stats["blocks"], 4)  # 空文本块被跳过
        self.assertEqual(stats["glyphs"], len("你好") + len("x = a + b")
                         + len("def f():") + len("标题"))

        doc = pymupdf.open(stream=pdf, filetype="pdf")
        self.assertEqual(doc.page_count, 2)
        page0 = doc[0]
        text0 = page0.get_text()
        self.assertIn("你好", text0)
        self.assertIn("x = a + b", text0)
        self.assertIn("def f():", text0)
        page1 = doc[1]
        self.assertIn("标题", page1.get_text())
        doc.close()

    def test_coordinate_flip_v3_to_pdf(self):
        # dst_box v3 [50, 700, 550, 720]（左下原点 y 向上），页高 792 →
        # PDF rect（左上原点 y 向下）= (50, 72, 550, 92)。
        pdf, _ = render_plan_to_pdf(PLAN[:1], page_sizes=PAGE_SIZES)
        doc = pymupdf.open(stream=pdf, filetype="pdf")
        words = doc[0].get_text("words")
        hit = [w for w in words if "你好" in w[4]]
        self.assertTrue(hit, words)
        # bbox: x0, y0, x1, y1 —— 文本应位于页面底部区域（从顶部算 y≈72-95）。
        self.assertGreaterEqual(hit[0][1], 70)
        self.assertLessEqual(hit[0][1], 100)
        doc.close()

    def test_multi_page_sizes_and_default(self):
        pdf, stats = render_plan_to_pdf(PLAN, page_sizes=PAGE_SIZES)
        doc = pymupdf.open(stream=pdf, filetype="pdf")
        self.assertEqual(doc.page_count, 2)
        self.assertEqual(round(doc[0].rect.width), 612)
        self.assertEqual(round(doc[0].rect.height), 792)
        # 第 2 页无 page_sizes → 默认 612x792。
        self.assertEqual(round(doc[1].rect.width), 612)
        self.assertEqual(round(doc[1].rect.height), 792)
        doc.close()

    def test_empty_plan(self):
        # 空 plan 也产出 1 个空页，保证 PDF 可打开。
        pdf, stats = render_plan_to_pdf([], page_sizes=PAGE_SIZES)
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertEqual(stats["pages"], 1)
        doc = pymupdf.open(stream=pdf, filetype="pdf")
        self.assertEqual(doc.page_count, 1)
        doc.close()

    def test_missing_dst_box_and_bad_font_size_fallbacks(self):
        entries = [
            {
                "block_id": "p0_0", "page": 0, "kind": "paragraph",
                "text": "Falls back", "translated": "回退",
                "src_box": [10, 100, 200, 120], "font_size": "bad",
            },
            {
                "block_id": "p0_1", "page": 0, "kind": "paragraph",
                "text": "No box", "translated": "无框",
                "font_size": -3,
            },
        ]
        pdf, stats = render_plan_to_pdf(entries, page_sizes=PAGE_SIZES)
        self.assertEqual(stats["blocks"], 2)
        doc = pymupdf.open(stream=pdf, filetype="pdf")
        self.assertIn("回退", doc[0].get_text())
        self.assertIn("无框", doc[0].get_text())
        doc.close()

    def test_output_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "sub", "paper_mono.pdf")
            pdf, _ = render_plan_to_pdf(PLAN[:1], page_sizes=PAGE_SIZES,
                                        output_path=out)
            self.assertTrue(os.path.exists(out))
            with open(out, "rb") as fh:
                self.assertEqual(fh.read(), pdf)
            doc = pymupdf.open(out)
            self.assertEqual(doc.page_count, 1)
            doc.close()



class TestMagicPdfCliRenderIntegration(unittest.TestCase):
    SAMPLE_MIDDLE = {
        "pdf_info": [
            [
                {
                    "type": "text", "bbox": [0, 0, 600, 24],
                    "cls": "title",
                    "lines": [{
                        "bbox": [0, 0, 600, 24],
                        "spans": [
                            {"bbox": [0, 0, 600, 24], "content": "Hello MagicPDF",
                             "type": "text"},
                        ],
                    }],
                },
            ]
        ],
        "page_info": [{"page_no": 0, "width": 612, "height": 792}],
    }

    def _make_args(self, render=True, **kw):
        import argparse
        ns = argparse.Namespace(
            files=["paper.pdf"], output="", pages=None,
            lang_in="en", lang_out="zh", service="google", thread=4,
            no_parallel=False, parallel_workers=None, vfont="", vchar="",
            envs={}, prompt=None, ignore_cache=False, compatible=False,
            debug=False, dir=False, backend="auto", mode="fast",
            parse_engine="magicpdf", magicpdf_ocr=False,
            magicpdf_render=render,
        )
        for k, v in kw.items():
            setattr(ns, k, v)
        return ns

    def _run(self, render):
        from pdf2zh.magicpdf_adapter import MagicPdfAdapter
        from pdf2zh.magicpdf_cli import run_magicpdf_main

        fake_translator = Mock()
        fake_translator.translate = Mock(side_effect=lambda t: "T:" + t)
        results = MagicPdfAdapter.from_middle_json(self.SAMPLE_MIDDLE)
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = os.path.join(tmp, "paper.pdf")
            with open(pdf_path, "w", encoding="utf-8") as fh:
                fh.write("%PDF-1.4 placeholder")
            with patch(
                "pdf2zh.magicpdf_adapter.MagicPdfAdapter.is_available",
                return_value=True,
            ), patch(
                "pdf2zh.magicpdf_adapter.MagicPdfAdapter.parse",
                return_value=results,
            ), patch(
                "pdf2zh.translator.build_translator",
                return_value=fake_translator,
            ):
                code = run_magicpdf_main(
                    self._make_args(render=render, files=[pdf_path], output=tmp))
            self.assertEqual(code, 0)
            mono = os.path.join(tmp, "magicpdf", "paper_mono.pdf")
            # 在临时目录生命周期内完成 PDF 校验（TemporaryDirectory 退出即清理）。
            exists = os.path.exists(mono)
            text = ""
            page_count = 0
            if exists:
                doc = pymupdf.open(mono)
                page_count = doc.page_count
                text = "".join(doc[p].get_text() for p in range(page_count))
                doc.close()
            return exists, page_count, text

    def test_default_renders_mono_pdf(self):
        exists, page_count, text = self._run(render=True)
        self.assertTrue(exists, "mono PDF 应默认产出")
        self.assertEqual(page_count, 1)
        self.assertIn("T:Hello MagicPDF", text)

    def test_no_render_flag_keeps_json_only(self):
        exists, _, _ = self._run(render=False)
        self.assertFalse(exists, "--no-magicpdf-render 不应产出 mono PDF")


if __name__ == "__main__":
    unittest.main()
