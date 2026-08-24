"""Step 2.2 — magic-pdf → v3 规范页面模型桥接器（MagicPdfBridge）单元测试。

覆盖：
- interpolate_char_bboxes：均匀内插字符级坐标（ASCII/CJK/空文本/零宽）；
- flip_bbox：左上角原点 → 左下角原点坐标系翻转；
- map_magicpdf_cls：布局类别 → v3 kind（含未知类别回退）；
- convert：MagicPdfParseResult → PageModel（块/行/span/字形 + metadata）；
- to_document_model / build_document_from_results：标注 Pass 全链路可用。
"""

import unittest

from pdf2zh.magicpdf_adapter import MagicPdfAdapter
from pdf2zh.v3.magicpdf_bridge import (
    MagicPdfBridge,
    build_document_from_results,
    flip_bbox,
    interpolate_char_bboxes,
    map_magicpdf_cls,
)

SAMPLE_MIDDLE = {
    "pdf_info": [
        [
            {
                "type": "text",
                "bbox": [0, 0, 300, 24],
                "cls": "title",
                "lines": [
                    {
                        "bbox": [0, 0, 300, 24],
                        "spans": [
                            {
                                "bbox": [0, 0, 300, 24],
                                "content": "Attention",
                                "type": "text",
                            },
                        ],
                    }
                ],
            },
            {
                "type": "text",
                "bbox": [0, 40, 520, 70],
                "cls": "body",
                "lines": [
                    {
                        "bbox": [0, 40, 520, 70],
                        "spans": [
                            {
                                "bbox": [0, 40, 520, 70],
                                "content": "We propose a new architecture.",
                                "type": "text",
                            }
                        ],
                    }
                ],
            },
            {
                "type": "text",
                "bbox": [0, 90, 400, 120],
                "cls": "interline_equation",
                "lines": [
                    {
                        "bbox": [0, 90, 400, 120],
                        "spans": [
                            {
                                "bbox": [0, 90, 400, 120],
                                "content": "x = a + b",
                                "type": "text",
                            },
                        ],
                    }
                ],
            },
            {
                "type": "text",
                "bbox": [0, 140, 200, 165],
                "cls": "code",
                "lines": [
                    {
                        "bbox": [0, 140, 200, 165],
                        "spans": [
                            {
                                "bbox": [0, 140, 200, 165],
                                "content": "def f(): pass",
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

SAMPLE_RESULTS = MagicPdfAdapter.from_middle_json(SAMPLE_MIDDLE)


class TestInterpolation(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(interpolate_char_bboxes([0, 0, 6, 10], ""), [])

    def test_ascii_uniform(self):
        glyphs = interpolate_char_bboxes([0, 0, 6, 10], "abc")
        self.assertEqual(len(glyphs), 3)
        self.assertEqual(glyphs[0]["char"], "a")
        self.assertAlmostEqual(glyphs[0]["bbox"][2] - glyphs[0]["bbox"][0], 2.0)
        # 覆盖完整 span 宽度
        self.assertAlmostEqual(glyphs[-1]["bbox"][2], 6.0)

    def test_cjk(self):
        glyphs = interpolate_char_bboxes([0, 0, 20, 10], "中文")
        self.assertEqual(len(glyphs), 2)
        self.assertAlmostEqual(glyphs[0]["bbox"][2] - glyphs[0]["bbox"][0], 10.0)

    def test_zero_width(self):
        glyphs = interpolate_char_bboxes([0, 0, 0, 10], "ab")
        self.assertEqual(len(glyphs), 2)
        self.assertEqual(glyphs[0]["bbox"][2] - glyphs[0]["bbox"][0], 0.0)


class TestCoordinateFlip(unittest.TestCase):
    def test_flip(self):
        bbox = flip_bbox([10, 700, 600, 780], 792)
        self.assertEqual(bbox, [10.0, 12.0, 600.0, 92.0])


class TestClassMapping(unittest.TestCase):
    def test_known(self):
        self.assertEqual(map_magicpdf_cls("Title"), "heading")
        self.assertEqual(map_magicpdf_cls("interline_equation"), "formula")
        self.assertEqual(map_magicpdf_cls("code"), "code")
        self.assertEqual(map_magicpdf_cls("figure"), "figure")

    def test_unknown_default(self):
        self.assertEqual(map_magicpdf_cls("unknown_cls"), "paragraph")
        self.assertEqual(map_magicpdf_cls(""), "paragraph")


class TestConvert(unittest.TestCase):
    def setUp(self):
        self.bridge = MagicPdfBridge(default_font="Helvetica")
        self.page = self.bridge.convert(SAMPLE_RESULTS[0])

    def test_page_model(self):
        self.assertEqual(self.page.page_num, 0)
        self.assertEqual(self.page.width, 612.0)
        self.assertEqual(self.page.height, 792.0)
        self.assertEqual(len(self.page.blocks), 4)

    def test_kind_mapping(self):
        kinds = [b.kind for b in self.page.blocks]
        self.assertEqual(kinds, ["heading", "paragraph", "formula", "code"])

    def test_glyphs(self):
        block = self.page.blocks[0]
        span = block.lines[0].spans[0]
        self.assertEqual(len(span.glyphs), len(span.text))
        g = span.glyphs[0]
        self.assertEqual(g.char, "A")
        self.assertEqual(g.font, "Helvetica")
        self.assertGreater(g.x1, g.x0)

    def test_coordinate_flip(self):
        # body 块原始 bbox [0,40,520,70]（PDF 点、左上角原点）
        block = self.page.blocks[1]
        self.assertEqual(round(block.y0, 1), 722.0)
        self.assertEqual(round(block.y1, 1), 752.0)


class TestDocumentModel(unittest.TestCase):
    def setUp(self):
        self.bridge = MagicPdfBridge(default_font="Helvetica")
        self.doc = self.bridge.to_document_model(
            self.bridge.convert_all(SAMPLE_RESULTS)
        )

    def test_document_model(self):
        self.assertEqual(len(self.doc.pages), 1)
        self.assertEqual(self.doc.pages[0].blocks[0].kind, "heading")

    def test_render_annotations(self):
        page = self.doc.pages[0]
        self.assertEqual(page.blocks[0].metadata["render_path"], "translate_refit")
        formula = next(b for b in page.blocks if b.kind == "formula")
        self.assertEqual(formula.metadata["render_path"], "preserve_float")

    def test_build_document_from_results(self):
        doc = build_document_from_results(SAMPLE_RESULTS, default_font="Times")
        self.assertEqual(len(doc.pages), 1)
        self.assertEqual(doc.metadata["page_order"], [0])

    def test_to_dict_serializable(self):
        d = self.doc.to_dict()
        self.assertEqual(len(d["pages"][0]["blocks"]), 4)
        self.assertIsInstance(d["pages"][0]["blocks"][0]["x0"], float)


if __name__ == "__main__":
    unittest.main()
