"""Tests for FontResolver (Phase 1)."""

import unittest
from pdf2zh.font_resolver import FontResolver, FontStyle


class TestFontResolver(unittest.TestCase):
    """Test font style analysis and mapping."""

    def setUp(self):
        self.resolver_zh = FontResolver("zh-cn")
        self.resolver_ja = FontResolver("ja")
        self.resolver_ko = FontResolver("ko")

    # ── Style Analysis Tests ──────────────────────────────────

    def test_serif_keyword_matching(self):
        """Font names with 'Times' or 'Roman' should map to SERIF."""
        style = self.resolver_zh._analyze_style("TimesNewRoman", 0)
        self.assertEqual(style, FontStyle.SERIF)

    def test_sans_keyword_matching(self):
        """Font names with 'Arial' or 'Helvetica' should map to SANS_SERIF."""
        style = self.resolver_zh._analyze_style("Arial", 0)
        self.assertEqual(style, FontStyle.SANS_SERIF)

        style = self.resolver_zh._analyze_style("Helvetica", 0)
        self.assertEqual(style, FontStyle.SANS_SERIF)

    def test_mono_keyword_matching(self):
        """Font names with 'Courier' or 'Mono' should map to MONOSPACE."""
        style = self.resolver_zh._analyze_style("CourierNew", 0)
        self.assertEqual(style, FontStyle.MONOSPACE)

        style = self.resolver_zh._analyze_style("Consolas", 32)
        # NonSymbolic flag (0x10) + no keyword match
        self.assertEqual(style, FontStyle.SYMBOL)  # 0x20 is Symbolic

    def test_flag_based_serif(self):
        """Font flags with Serif bit (0x02) should map to SERIF."""
        style = self.resolver_zh._analyze_style("UnknownFont", 0x02)
        self.assertEqual(style, FontStyle.SERIF)

    def test_flag_based_monospace(self):
        """Font flags with FixedPitch bit (0x01) should map to MONOSPACE."""
        style = self.resolver_zh._analyze_style("UnknownFont", 0x01)
        self.assertEqual(style, FontStyle.MONOSPACE)

    def test_flag_based_script(self):
        """Font flags with Script bit (0x08) should map to SCRIPT."""
        style = self.resolver_zh._analyze_style("UnknownFont", 0x08)
        self.assertEqual(style, FontStyle.SCRIPT)

    # ── Font Resolution Tests ────────────────────────────────

    def test_match_serif_zh(self):
        """Serif font should resolve to SourceHanSerifCN for zh-cn."""
        result = self.resolver_zh.match("TimesNewRoman", 0)
        self.assertEqual(result, "SourceHanSerifCN-Regular.otf")

    def test_match_sans_zh(self):
        """Sans-serif font should resolve to SourceHanSansCN for zh-cn."""
        result = self.resolver_zh.match("Arial", 0)
        self.assertEqual(result, "SourceHanSansCN-Regular.otf")

    def test_match_mono_zh(self):
        """Monospace font should resolve to NotoSansMonoCJKsc for zh-cn."""
        result = self.resolver_zh.match("CourierNew", 0)
        self.assertEqual(result, "NotoSansMonoCJKsc-Regular.otf")

    def test_match_serif_ja(self):
        """Serif font should resolve to NotoSerifJP for ja."""
        result = self.resolver_ja.match("TimesNewRoman", 0)
        self.assertEqual(result, "NotoSerifJP-Regular.otf")

    def test_match_serif_ko(self):
        """Serif font should resolve to NotoSerifKR for ko."""
        result = self.resolver_ko.match("TimesNewRoman", 0)
        self.assertEqual(result, "NotoSerifKR-Regular.otf")

    def test_match_unknown_font_serif_default(self):
        """Unknown font without flags should fallback to serif."""
        result = self.resolver_zh.match("SomeRandomFont", 0)
        self.assertIn(
            result,
            [
                "SourceHanSerifCN-Regular.otf",
                "SourceHanSansCN-Regular.otf",
                "NotoSansMonoCJKsc-Regular.otf",
            ],
        )

    # ── Fallback Chain Tests ─────────────────────────────────

    def test_fallback_chain_contains_three_styles(self):
        """Fallback chain should contain SERIF, SANS_SERIF, and MONOSPACE."""
        self.assertIn(FontStyle.SERIF, FontResolver.FALLBACK_CHAIN)
        self.assertIn(FontStyle.SANS_SERIF, FontResolver.FALLBACK_CHAIN)
        self.assertIn(FontStyle.MONOSPACE, FontResolver.FALLBACK_CHAIN)

    def test_symbol_font_falls_back_to_serif(self):
        """Symbol font should resolve to a valid font via fallback."""
        result = self.resolver_zh.match("ZapfDingbats", 0x20)
        # Symbolic flag (0x20) triggers FontStyle.SYMBOL
        self.assertIsNotNone(result)
        self.assertIn("Regular.otf", result)


if __name__ == "__main__":
    unittest.main()
