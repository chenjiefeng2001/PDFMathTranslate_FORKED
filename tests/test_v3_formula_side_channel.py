"""Step 1.3 — 公式 LaTeX 侧通道（formula_side_channel）单元测试。

覆盖：
- FormulaLatexChannel：register/lookup/to_dict/from_dict/to_json 可序列化；
- collect_formula_latex：从 DocumentModel 收集公式块 LaTeX（含 latex 字段）；
- apply_formula_latex：把侧通道 LaTeX 回填到模型公式块 metadata；
- latex_channel_from_magicpdf_json：从 dump JSON 恢复通道。
"""
import json
import os
import tempfile
import unittest

from pdf2zh.magicpdf_adapter import MagicPdfAdapter
from pdf2zh.v3.formula_side_channel import (
    FORMULA_KINDS,
    FormulaLatexChannel,
    apply_formula_latex,
    collect_formula_latex,
    latex_channel_from_magicpdf_json,
)
from pdf2zh.v3.magicpdf_bridge import MagicPdfBridge

SAMPLE_MIDDLE = {
    "pdf_info": [
        [
            {
                "type": "text",
                "bbox": [0, 0, 300, 24],
                "cls": "interline_equation",
                "latex": "x = a + b",
                "lines": [
                    {
                        "bbox": [0, 0, 300, 24],
                        "spans": [
                            {"bbox": [0, 0, 300, 24],
                             "content": "x = a + b", "type": "text"},
                        ],
                    }
                ],
            },
            {
                "type": "text",
                "bbox": [0, 40, 300, 70],
                "cls": "body",
                "lines": [
                    {
                        "bbox": [0, 40, 300, 70],
                        "spans": [
                            {"bbox": [0, 40, 300, 70],
                             "content": "body text", "type": "text"},
                        ],
                    }
                ],
            },
            {
                "type": "text",
                "bbox": [0, 90, 200, 115],
                "cls": "interline_equation",
                "lines": [
                    {
                        "bbox": [0, 90, 200, 115],
                        "spans": [
                            {"bbox": [0, 90, 200, 115],
                             "content": "y = z", "type": "text"},
                        ],
                    }
                ],
            },
        ]
    ],
    "page_info": [{"page_no": 0, "width": 612, "height": 792}],
}

def make_model():
    results = MagicPdfAdapter.from_middle_json(SAMPLE_MIDDLE)
    bridge = MagicPdfBridge(default_font="Helvetica")
    return bridge.to_document_model(bridge.convert_all(results))


class TestChannelContainer(unittest.TestCase):
    def test_register_and_lookup(self):
        ch = FormulaLatexChannel()
        ch.register("p0_0", "x = a", confidence=0.9, kind="formula", page=0)
        entry = ch.lookup("p0_0")
        self.assertEqual(entry["latex"], "x = a")
        self.assertEqual(entry["confidence"], 0.9)
        self.assertIsNone(ch.lookup("missing"))

    def test_register_ignores_empty(self):
        ch = FormulaLatexChannel()
        ch.register("p0_0", "  ")
        ch.register("", "x = a")
        self.assertEqual(len(ch.entries), 0)

    def test_register_keeps_higher_confidence(self):
        ch = FormulaLatexChannel()
        ch.register("p0_0", "low", confidence=0.3)
        ch.register("p0_0", "high", confidence=0.9)
        self.assertEqual(ch.entries["p0_0"]["latex"], "high")
        ch.register("p0_0", "lower", confidence=0.5)
        self.assertEqual(ch.entries["p0_0"]["latex"], "high")

    def test_serialization(self):
        ch = FormulaLatexChannel()
        ch.register("p0_0", "x", confidence=0.8, page=1)
        d = ch.to_dict()
        self.assertEqual(d["formula_count"], 1)
        restored = FormulaLatexChannel.from_dict(d)
        self.assertEqual(restored.lookup("p0_0")["latex"], "x")
        payload = json.loads(ch.to_json())
        self.assertEqual(payload["entries"]["p0_0"]["page"], 1)

    def test_from_dict_none(self):
        self.assertEqual(FormulaLatexChannel.from_dict(None).entries, {})


class TestCollectAndApply(unittest.TestCase):
    def setUp(self):
        self.doc = make_model()

    def test_kinds_constant(self):
        self.assertIn("formula", FORMULA_KINDS)
        self.assertIn("interline_equation", FORMULA_KINDS)

    def test_collect_only_formulas_with_latex(self):
        channel = collect_formula_latex(self.doc)
        # 只有带 latex 的公式块进通道（第 0 块），无 latex 的第 2 块不进
        self.assertEqual(len(channel.entries), 1)
        entry = next(iter(channel.entries.values()))
        self.assertEqual(entry["latex"], "x = a + b")
        self.assertEqual(entry["kind"], "formula")

    def test_apply_backfills_metadata(self):
        channel = collect_formula_latex(self.doc)
        applied = apply_formula_latex(self.doc, channel)
        self.assertEqual(applied, 1)
        formula = self.doc.pages[0].blocks[0]
        self.assertEqual(formula.metadata["latex"], "x = a + b")
        self.assertEqual(
            formula.metadata["latex_source"], "magicpdf_side_channel")

    def test_apply_keeps_higher_confidence_existing(self):
        # 已有 LaTeX 且置信度不低于通道时，不回填（保持既有）
        channel = collect_formula_latex(self.doc)
        self.doc.pages[0].blocks[0].metadata["latex"] = "better"
        self.doc.pages[0].blocks[0].metadata["confidence"] = 0.99
        applied = apply_formula_latex(self.doc, channel)
        self.assertEqual(applied, 0)
        self.assertEqual(self.doc.pages[0].blocks[0].metadata["latex"],
                         "better")


class TestChannelFileIO(unittest.TestCase):
    def test_from_magicpdf_json(self):
        ch = FormulaLatexChannel()
        ch.register("p0_0", "x", confidence=0.5, kind="formula", page=0)
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "channel.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(ch.to_json())
            restored = latex_channel_from_magicpdf_json(path)
            self.assertEqual(restored.lookup("p0_0")["latex"], "x")

    def test_missing_file_returns_empty(self):
        ch = latex_channel_from_magicpdf_json("no_such_file.json")
        self.assertEqual(ch.entries, {})


if __name__ == "__main__":
    unittest.main()

