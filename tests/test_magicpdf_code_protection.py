"""Step 1.1/1.2 — code/伪代码块保护链路测试。

覆盖：
- magicpdf_bridge.convert：code 块 metadata 写 translate=False /
  pseudocode_protected=True；
- document_model._KEEP_KINDS 含 code；
- translate_document：code 块不送翻译器（原文保留），heading/paragraph 正常翻译。
"""

import unittest

from pdf2zh.magicpdf_adapter import MagicPdfAdapter
from pdf2zh.v3.document_model import _KEEP_KINDS, translate_document
from pdf2zh.v3.magicpdf_bridge import MagicPdfBridge

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
                                "content": "Title",
                                "type": "text",
                            },
                        ],
                    }
                ],
            },
            {
                "type": "text",
                "bbox": [0, 40, 300, 70],
                "cls": "code",
                "lines": [
                    {
                        "bbox": [0, 40, 300, 70],
                        "spans": [
                            {
                                "bbox": [0, 40, 300, 70],
                                "content": "def f(): return 1",
                                "type": "text",
                            },
                        ],
                    }
                ],
            },
            {
                "type": "text",
                "bbox": [0, 90, 300, 115],
                "cls": "body",
                "lines": [
                    {
                        "bbox": [0, 90, 300, 115],
                        "spans": [
                            {
                                "bbox": [0, 90, 300, 115],
                                "content": "A normal paragraph.",
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


class TestCodeProtection(unittest.TestCase):
    def setUp(self):
        results = MagicPdfAdapter.from_middle_json(SAMPLE_MIDDLE)
        bridge = MagicPdfBridge(default_font="Helvetica")
        self.doc = bridge.to_document_model(bridge.convert_all(results))
        self.code = next(b for b in self.doc.pages[0].blocks if b.kind == "code")

    def test_keep_kinds_contains_code(self):
        self.assertIn("code", _KEEP_KINDS)

    def test_bridge_marks_code_metadata(self):
        self.assertIs(self.code.metadata["translate"], False)
        self.assertIs(self.code.metadata["pseudocode_protected"], True)

    def test_translate_document_preserves_code(self):
        calls = []

        def fake_translate(text):
            calls.append(text)
            return f"T:{text}"

        stats = translate_document(self.doc, fake_translate, lang_out="zh")
        self.assertEqual(self.code.metadata["translated"], "def f(): return 1")
        self.assertIs(self.code.metadata["translated_same"], True)
        # 翻译器只被标题/正文调用（2 次），绝不触达 code 块
        self.assertEqual(len(calls), 2)
        self.assertNotIn("def f(): return 1", calls[0] + calls[1])
        self.assertEqual(stats["preserved"], 1)
        self.assertEqual(stats["translated"], 2)


if __name__ == "__main__":
    unittest.main()
