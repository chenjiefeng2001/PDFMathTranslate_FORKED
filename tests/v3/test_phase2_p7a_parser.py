# -*- coding: utf-8 -*-
"""Parser module tests."""
import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
try:
    from pdf2zh.v3.parser import PDFParser, RawBlock, RawBlockType, RawSpan
    _HAS = True
except ImportError as e:
    print(f"Parser import error: {e}")
    _HAS = False

@unittest.skipIf(not _HAS, "V3 parser not importable")
class TestRawBlock(unittest.TestCase):
    def test_defaults(self):
        rb = RawBlock(block_type=RawBlockType.TEXT)
        self.assertEqual(rb.block_type, RawBlockType.TEXT)
        self.assertEqual(rb.spans, [])

    def test_with_spans(self):
        span = RawSpan(text="hello", font_name="Times", font_size=12.0)
        rb = RawBlock(block_type=RawBlockType.TEXT, spans=[span])
        self.assertEqual(len(rb.spans), 1)
        self.assertEqual(rb.spans[0].text, "hello")

    def test_raw_block_type_values(self):
        self.assertEqual(RawBlockType.TEXT.value, "text")
        self.assertEqual(RawBlockType.IMAGE.value, "image")

    def test_raw_span_defaults(self):
        rs = RawSpan(text="test")
        self.assertEqual(rs.font_size, 0.0)
        self.assertEqual(rs.confidence, 1.0)

    def test_pdf_parser_instantiation(self):
        parser = PDFParser()
        self.assertIsNotNone(parser)

    def test_parse_raises_on_nonexistent(self):
        parser = PDFParser()
        with self.assertRaises(Exception):
            parser.parse("/nonexistent/file.pdf")

    def test_parse_raises_on_empty(self):
        parser = PDFParser()
        with self.assertRaises(Exception):
            parser.parse("")

    def test_parse_raises_on_none(self):
        parser = PDFParser()
        with self.assertRaises(Exception):
            parser.parse(None)

if __name__ == "__main__":
    unittest.main(verbosity=2)
