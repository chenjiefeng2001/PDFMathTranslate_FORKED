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
        "block_id": "p0_0",
        "page": 0,
        "kind": "paragraph",
        "text": "Hello",
        "translated": "你好",
        "render_path": "translate_refit",
        "src_box": [50, 700, 550, 720],
        "dst_box": [50, 700, 550, 720],
        "font_size": 12.0,
    },
    {
        "block_id": "p0_1",
        "page": 0,
        "kind": "formula",
        "text": "x = a + b",
        "translated": "x = a + b",
        "render_path": "preserve_float",
        "src_box": [200, 600, 400, 620],
        "dst_box": [200, 600, 400, 620],
        "font_size": 14.0,
    },
    {
        "block_id": "p0_2",
        "page": 0,
        "kind": "code",
        "text": "def f():",
        "translated": "def f():",
        "render_path": "preserve_float",
        "src_box": [50, 550, 300, 570],
        "dst_box": [50, 550, 300, 570],
        "font_size": 10.0,
    },
    # 空文本块：应被安全跳过。
    {
        "block_id": "p0_3",
        "page": 0,
        "kind": "paragraph",
        "text": "",
        "translated": "",
        "render_path": "translate_refit",
        "src_box": [0, 0, 0, 0],
        "dst_box": [0, 0, 0, 0],
        "font_size": 12.0,
    },
    {
        "block_id": "p1_0",
        "page": 1,
        "kind": "heading",
        "text": "Title",
        "translated": "标题",
        "render_path": "translate_refit",
        "src_box": [50, 750, 550, 770],
        "dst_box": [50, 750, 550, 770],
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
        self.assertEqual(
            stats["glyphs"],
            len("你好") + len("x = a + b") + len("def f():") + len("标题"),
        )

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
        # CJK 字形 ascent（约 1.04em）会略超出 rect 顶（基线 = y0 + 0.85em），
        # 实测 y0 ≈ 69.7，下界取 65 留出字体度量余量，仍能验证翻转正确落位。
        self.assertGreaterEqual(hit[0][1], 65)
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
                "block_id": "p0_0",
                "page": 0,
                "kind": "paragraph",
                "text": "Falls back",
                "translated": "回退",
                "src_box": [10, 100, 200, 120],
                "font_size": "bad",
            },
            {
                "block_id": "p0_1",
                "page": 0,
                "kind": "paragraph",
                "text": "No box",
                "translated": "无框",
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
            pdf, _ = render_plan_to_pdf(
                PLAN[:1], page_sizes=PAGE_SIZES, output_path=out
            )
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
                    "type": "text",
                    "bbox": [0, 0, 600, 24],
                    "cls": "title",
                    "lines": [
                        {
                            "bbox": [0, 0, 600, 24],
                            "spans": [
                                {
                                    "bbox": [0, 0, 600, 24],
                                    "content": "Hello MagicPDF",
                                    "type": "text",
                                },
                            ],
                        }
                    ],
                },
            ]
        ],
        "page_info": [{"page_no": 0, "width": 612, "height": 792}],
    }

    def _make_args(self, render=True, **kw):
        import argparse

        ns = argparse.Namespace(
            files=["paper.pdf"],
            output="",
            pages=None,
            lang_in="en",
            lang_out="zh",
            service="google",
            thread=4,
            no_parallel=False,
            parallel_workers=None,
            vfont="",
            vchar="",
            envs={},
            prompt=None,
            ignore_cache=False,
            compatible=False,
            debug=False,
            dir=False,
            backend="auto",
            mode="fast",
            parse_engine="magicpdf",
            magicpdf_ocr=False,
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
            with (
                patch(
                    "pdf2zh.magicpdf_adapter.MagicPdfAdapter.is_available",
                    return_value=True,
                ),
                patch(
                    "pdf2zh.magicpdf_adapter.MagicPdfAdapter.parse",
                    return_value=results,
                ),
                patch(
                    "pdf2zh.translator.build_translator",
                    return_value=fake_translator,
                ),
            ):
                code = run_magicpdf_main(
                    self._make_args(render=render, files=[pdf_path], output=tmp)
                )
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


class TestFlowPayloadRendering(unittest.TestCase):
    """Commit 7E-1：render_payload.kind == "flow" 的已定版行命令直接绘制。

    - 带 commands 的 flow 块 → 逐行绘制，统计 flow_layout_used；
    - 无 commands 的 flow 块（layout_ok=False）→ 可观测降级 legacy 换行路径。
    """

    FLOW_ENTRY = {
        "block_id": "p0_flow",
        "page": 0,
        "kind": "paragraph",
        "text": "Source paragraph text",
        "translated": "译后段落文本内容",
        "render_path": "translate_refit",
        "src_box": [72.0, 700.0, 540.0, 722.0],
        "dst_box": [72.0, 700.0, 540.0, 722.0],
        "font_size": 12.0,
        "render_payload": {
            "kind": "flow",
            "commands": [
                {
                    "kind": "flow-text",
                    "text": "译后段落",
                    "x": 72.0,
                    "y": 722.0,
                    "width": 48.0,
                    "line": 0,
                    "is_last": False,
                    "overflow": False,
                },
                {
                    "kind": "flow-text",
                    "text": "文本内容",
                    "x": 72.0,
                    "y": 705.2,
                    "width": 48.0,
                    "line": 1,
                    "is_last": True,
                    "overflow": False,
                },
            ],
            "entries": [],
        },
    }

    def test_flow_commands_drawn_without_relayout(self):
        pdf, stats = render_plan_to_pdf(
            [self.FLOW_ENTRY], page_sizes=PAGE_SIZES, cjk_font=True
        )
        self.assertEqual(stats["flow_layout_used"], 1)
        self.assertNotIn("flow_legacy_fallback", stats)
        doc = pymupdf.open(stream=pdf, filetype="pdf")
        text = doc[0].get_text()
        self.assertIn("译后段落", text)
        self.assertIn("文本内容", text)
        # 已定版行命令不重新换行：两行都在输出里
        self.assertIn("\n", text)
        doc.close()

    def test_flow_legacy_fallback_when_no_commands(self):
        """layout_ok=False（无 commands）→ 降级 legacy 换行路径，且可观测。"""
        entry = dict(self.FLOW_ENTRY)
        entry["render_payload"] = {
            "kind": "flow",
            "commands": [],
            "entries": [],
        }
        pdf, stats = render_plan_to_pdf([entry], page_sizes=PAGE_SIZES, cjk_font=True)
        self.assertEqual(stats["flow_legacy_fallback"], 1)
        self.assertNotIn("flow_layout_used", stats)
        doc = pymupdf.open(stream=pdf, filetype="pdf")
        self.assertIn("译后段落文本内容", doc[0].get_text())
        doc.close()


class TestSourcePdfNoFileLock(unittest.TestCase):
    """回归：source_pdf 打不开时不得锁住源文件（Windows）。

    pymupdf 打开失败抛出的 ``FileDataError`` 的 traceback 会持有 C 层文件
    句柄 —— 若异常对象被日志记录（pytest 捕获 / 长驻 handler）保留，源 PDF
    会一直被锁，导致临时目录无法清理。修复后异常只字符串化、立即丢弃。
    """

    def test_invalid_source_pdf_is_released_after_render(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "paper.pdf")
            with open(src, "w", encoding="utf-8") as fh:
                fh.write("%PDF-1.4 placeholder")  # 非法 PDF → open 失败

            pdf, stats = render_plan_to_pdf(
                PLAN[:1], page_sizes=PAGE_SIZES, source_pdf=src
            )
            self.assertTrue(pdf.startswith(b"%PDF"))
            self.assertEqual(stats["blocks"], 1)

            # 渲染完成后源文件必须可删除（Windows 下此前被异常句柄锁住）
            os.unlink(src)

    def test_valid_source_pdf_closed_after_render(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "src.pdf")
            sdoc = pymupdf.Document()
            sp = sdoc.new_page(width=612, height=792)
            sp.insert_text((100, 100), "original")
            sdoc.save(src)
            sdoc.close()

            pdf, stats = render_plan_to_pdf(
                PLAN[:1], page_sizes=PAGE_SIZES, source_pdf=src
            )
            self.assertTrue(pdf.startswith(b"%PDF"))
            # src_doc 已 close：文件不再被锁，可删除
            os.unlink(src)


class TestRenderWithBackground(unittest.TestCase):
    """修复：``render_plan_to_pdf(source_pdf=...)`` 保留原 PDF 为背景层。

    - 只对「真正翻译」的块（translated != text）覆盖原文 + 写译文；
    - formula/code 保留块（translated == text）不重画（避免 LaTeX/原文叠影）；
    - 背景图形（有色方块等）不再被整页白底吞噬。
    """

    def _make_plan(self):
        return [
            {
                "block_id": "p0_t",
                "page": 0,
                "kind": "paragraph",
                "text": "Original colored text",
                "translated": "彩色原文的译文",
                "render_path": "translate_refit",
                "src_box": [200, 62, 400, 92],
                "dst_box": [200, 62, 400, 92],
                "font_size": 11.0,
            },
            {
                "block_id": "p0_f",
                "page": 0,
                "kind": "formula",
                "text": "x = a + b",
                "translated": "x = a + b",  # 保留块：translated == text
                "render_path": "preserve_float",
                "src_box": [200, 100, 400, 120],
                "dst_box": [200, 100, 400, 120],
                "font_size": 12.0,
            },
        ]

    def test_source_pdf_background_preserves_graphics_and_skips_kept_blocks(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "src.pdf")
            sdoc = pymupdf.Document()
            sp = sdoc.new_page(width=612, height=792)
            # 黄色背景块：PDF 左上原点 (200,700)-(400,730) ⇔ v3 y 向上 [200,62,400,92]。
            sp.draw_rect(
                pymupdf.Rect(200, 700, 400, 730), color=None, fill=(1, 0.85, 0.2)
            )
            sp.insert_text((210, 712), "Original colored text")
            sdoc.save(src)
            sdoc.close()

            pdf, stats = render_plan_to_pdf(
                self._make_plan(), page_sizes={0: [612, 792]}, source_pdf=src
            )
            doc = pymupdf.open(stream=pdf, filetype="pdf")
            page = doc[0]
            text = page.get_text()
            self.assertIn("彩色原文的译文", text)  # 翻译块写入
            self.assertNotIn("x = a + b", text)  # 保留块不重画（背景原文 + 不叠影）
            # 背景图形保留：黄色方块 + 翻译块覆盖白矩形都在 drawings 里。
            self.assertTrue(page.get_drawings(), "原 PDF 背景图形应被保留")
            doc.close()

    def test_without_source_pdf_keeps_plain_text_layer(self):
        """未传 source_pdf（测试/纯文本层）保持历史行为：所有块都写文本。"""
        pdf, stats = render_plan_to_pdf(self._make_plan(), page_sizes={0: [612, 792]})
        doc = pymupdf.open(stream=pdf, filetype="pdf")
        text = doc[0].get_text()
        self.assertIn("彩色原文的译文", text)
        self.assertIn("x = a + b", text)  # 纯文本层：保留块原文也绘制
        doc.close()


if __name__ == "__main__":
    unittest.main()
