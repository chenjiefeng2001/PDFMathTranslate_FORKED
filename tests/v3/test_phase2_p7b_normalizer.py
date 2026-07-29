# -*- coding: utf-8 -*-
"""Normalizer module tests."""
import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
try:
    from pdf2zh.v3.normalizer import Normalizer, NormalizerConfig, NormalizedBlock
    from pdf2zh.v3.parser import RawBlock, RawBlockType, RawSpan
    from pdf2zh.font_resolver import FontStyle
    _HAS = True
except ImportError as e:
    _HAS = False
    print(f"Normalizer import error: {e}")

def NB(**kw):
    defaults = dict(page_num=0, font_size_avg=12.0,
                    font_style=FontStyle.SERIF, font_name_original="Times")
    defaults.update(kw)
    return NormalizedBlock(**defaults)

@unittest.skipIf(not _HAS, "not importable")
class TestNormalizerConfig(unittest.TestCase):
    def test_defaults(self):
        cfg = NormalizerConfig()
        self.assertEqual(cfg.lang_in, "auto")
        self.assertTrue(cfg.normalize_unicode)

    def test_custom(self):
        cfg = NormalizerConfig(lang_in="en", normalize_unicode=False)
        self.assertEqual(cfg.lang_in, "en")
        self.assertFalse(cfg.normalize_unicode)

@unittest.skipIf(not _HAS, "not importable")
class TestNormalizedBlock(unittest.TestCase):
    def test_full_construction(self):
        nb = NB(text="hello", bbox=(0, 0, 100, 20))
        self.assertEqual(nb.text, "hello")
        self.assertEqual(nb.bbox, (0, 0, 100, 20))

    def test_raw_type_text(self):
        nb = NB(text="hello", bbox=(0, 0, 100, 20), raw_type="text")
        self.assertEqual(nb.raw_type, "text")

    def test_confidence_default(self):
        nb = NB(text="test", bbox=(0, 0, 100, 20))
        self.assertAlmostEqual(nb.confidence, 1.0)

    def test_custom_font_style(self):
        nb = NB(text="hello", bbox=(0, 0, 100, 20), font_style=FontStyle.SANS_SERIF)
        self.assertEqual(nb.font_style, FontStyle.SANS_SERIF)

    def test_raw_type_image(self):
        nb = NB(text="", bbox=(0, 0, 100, 20), raw_type="image")
        self.assertEqual(nb.raw_type, "image")

    def test_page_num_custom(self):
        nb = NB(text="test", bbox=(0, 0, 100, 20), page_num=2)
        self.assertEqual(nb.page_num, 2)

@unittest.skipIf(not _HAS, "not importable")
class TestNormalizer(unittest.TestCase):
    def test_instantiation(self):
        n = Normalizer()
        self.assertIsNotNone(n)

    def test_with_config(self):
        cfg = NormalizerConfig(lang_in="en")
        n = Normalizer(cfg)
        self.assertEqual(n.config.lang_in, "en")

    def test_normalize_empty(self):
        n = Normalizer()
        self.assertEqual(n.normalize([]), [])

    def test_text_block(self):
        n = Normalizer()
        rb = RawBlock(block_type=RawBlockType.TEXT,
                      spans=[RawSpan(text="Hello", font_name="Times", font_size=12.0)])
        result = n.normalize([rb])
        self.assertEqual(len(result), 1)

    def test_vector_block_skipped(self):
        n = Normalizer()
        rb = RawBlock(block_type=RawBlockType.VECTOR)
        result = n.normalize([rb])
        self.assertEqual(len(result), 0)

    def test_unknown_block_skipped(self):
        n = Normalizer()
        rb = RawBlock(block_type=RawBlockType.UNKNOWN)
        result = n.normalize([rb])
        self.assertEqual(len(result), 0)

    def test_multi_block(self):
        n = Normalizer()
        rbs = [
            RawBlock(block_type=RawBlockType.TEXT,
                     spans=[RawSpan(text="Line 1", font_name="Times", font_size=12.0)]),
            RawBlock(block_type=RawBlockType.TEXT,
                     spans=[RawSpan(text="Line 2", font_name="Times", font_size=10.0)]),
        ]
        result = n.normalize(rbs)
        self.assertEqual(len(result), 2)

if __name__ == "__main__":
    unittest.main(verbosity=2)
