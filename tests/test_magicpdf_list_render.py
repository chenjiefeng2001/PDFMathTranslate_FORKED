"""Commit 4 渲染链集成：List block → render_plan → PDF。

验证 ``render_plan_to_pdf`` 对带 ``list_items`` 载荷的 List 块：
- marker 原样落位（不在译者载荷内、未翻译）；
- content 翻译后落位并保留 content_x 几何；
- 无 ``list_items`` 的普通/保留块走原路径（既有行为不变）。
"""

import unittest

import pymupdf

from pdf2zh.semantic.renderer.list import build_page_list_plan
from pdf2zh.v3.magicpdf_renderer import render_plan_to_pdf


def _list_entry(page=0):
    payload = build_page_list_plan(
        ["1. First item", "2. Second item"],
        translate=lambda s: f"T[{s}]",
    )
    return {
        "block_id": "p0_list",
        "page": page,
        "kind": "list",
        "text": "1. First item\n2. Second item",
        "translated": "1. First item\n2. Second item",
        "render_path": "translate_refit",
        "src_box": [40, 700, 560, 740],
        "dst_box": [40, 700, 560, 740],
        "font_size": 12.0,
        "list_items": payload,
    }


class TestMagicPdfListRender(unittest.TestCase):
    def test_list_block_marker_preserved_and_content_translated(self):
        pdf, stats = render_plan_to_pdf([_list_entry()], page_sizes={0: [612.0, 792.0]})
        doc = pymupdf.open(stream=pdf, filetype="pdf")
        text = doc[0].get_text()
        # marker 原样（未翻译、未改变文本）
        self.assertIn("1.", text)
        self.assertIn("2.", text)
        # content 已翻译
        self.assertIn("T[First item]", text)
        self.assertIn("T[Second item]", text)
        # 原始 content 行（marker+空格+未翻译文本）不再出现
        self.assertNotIn("1. First item", text)
        self.assertNotIn("2. Second item", text)
        doc.close()

    def test_list_and_paragraph_mix(self):
        """列表块 + 普通段同页：普通段仍走原翻译路径，两路径不互相污染。"""
        entries = [
            _list_entry(),
            {
                "block_id": "p0_para",
                "page": 0,
                "kind": "paragraph",
                "text": "A normal paragraph",
                "translated": "普通段落",
                "render_path": "translate_refit",
                "src_box": [40, 600, 560, 620],
                "dst_box": [40, 600, 560, 620],
                "font_size": 12.0,
            },
        ]
        pdf, _stats = render_plan_to_pdf(entries, page_sizes={0: [612.0, 792.0]})
        doc = pymupdf.open(stream=pdf, filetype="pdf")
        text = doc[0].get_text()
        self.assertIn("T[First item]", text)
        self.assertIn("普通段落", text)
        self.assertIn("2.", text)  # marker 保留
        doc.close()

    # ── Commit 4.1：真实 PDF 几何验证 ─────────────────────────
    def test_nested_list_content_x_differ_in_pdf(self):
        """Commit 4.1：嵌套列表真正渲染到 PDF，三个层级的 content_x 逐级右移。

        不是只看 JSON level —— 而是打开最终 PDF，用 ``get_text("words")``
        度量每个列表内容的 x 起点，证明渲染器把 content 落在逐级增大的列上。
        """
        paras = ["1. Intro", "   a. Background", "      i. deep"]
        geom = [
            {"x0": 40.0, "x1": 200.0, "size": 12.0, "y0": 700.0},
            {"x0": 52.0, "x1": 200.0, "size": 12.0, "y0": 680.0},
            {"x0": 64.0, "x1": 200.0, "size": 12.0, "y0": 660.0},
        ]
        payload = build_page_list_plan(
            paras,
            geom=geom,
            translate=lambda s: f"C[{s}]",
        )
        self.assertEqual([it["level"] for it in payload["items"]], [0, 1, 2])
        entry = {
            "block_id": "p0_nested",
            "page": 0,
            "kind": "list",
            "text": "\n".join(paras),
            "translated": "\n".join(paras),
            "render_path": "translate_refit",
            "src_box": [30, 620, 560, 740],
            "dst_box": [30, 620, 560, 740],
            "font_size": 12.0,
            "list_items": payload,
        }
        pdf, _stats = render_plan_to_pdf([entry], page_sizes={0: [612.0, 792.0]})
        doc = pymupdf.open(stream=pdf, filetype="pdf")
        words = {w[4]: float(w[0]) for w in doc[0].get_text("words")}
        # 每个 content 落位：x 必须逐级递增且不等于对应 marker 列
        self.assertIn("C[Intro]", words)
        self.assertIn("C[Background]", words)
        self.assertIn("C[deep]", words)
        depths = {}
        for lvl, key in enumerate(("C[Intro]", "C[Background]", "C[deep]")):
            depths[lvl] = words[key]
            # content 必须位于 page 左缘起的合理位置
            self.assertGreater(words[key], 0.0)
        # 三个 content_x 真实不同且严格递增（不是 JSON 里的 level 假装不同）
        self.assertGreater(depths[1], depths[0])
        self.assertGreater(depths[2], depths[1])
        doc.close()

    def test_translator_garbage_status_in_pdf(self):
        """Commit 4.1：翻译器返回垃圾串，最终 PDF 仍是 ``1. TRANSLATED``。

        打开 PDF 断言 marker 原样（"1."/"2."）与垃圾译文（"TRANSLATED"）
        同时存在，且不存在被改写的 marker（"1.1"）—— marker 与翻译通道解耦。
        """
        payload = build_page_list_plan(
            ["1. This is an item", "2. Second item"],
            translate=lambda s: "TRANSLATED",
        )
        entry = {
            "block_id": "p0_list",
            "page": 0,
            "kind": "list",
            "text": "1. This is an item\n2. Second item",
            "translated": "1. This is an item\n2. Second item",
            "render_path": "translate_refit",
            "src_box": [40, 700, 560, 740],
            "dst_box": [40, 700, 560, 740],
            "font_size": 12.0,
            "list_items": payload,
        }
        pdf, _stats = render_plan_to_pdf([entry], page_sizes={0: [612.0, 792.0]})
        doc = pymupdf.open(stream=pdf, filetype="pdf")
        text = doc[0].get_text()
        self.assertIn("1.", text)
        self.assertIn("2.", text)
        self.assertEqual(text.count("TRANSLATED"), 2)
        # marker 未被翻译器改写 —— 绝不出现 "1.1" 这类被吞并的 marker
        self.assertNotIn("1.1", text)
        self.assertNotIn("2.2", text)
        # 原文 content（"This is an item"）已被替换
        self.assertNotIn("This is an item", text)
        doc.close()


if __name__ == "__main__":
    unittest.main()
