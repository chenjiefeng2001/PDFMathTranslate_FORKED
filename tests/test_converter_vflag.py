"""Tests for the vflag formula font matching in converter.py"""

import re

# Copy of the regex from converter.py's vflag function for testing
FONT_PATTERN = re.compile(
    r"(CM[^R]|MS[BM]|XY|MT|BL|RM|EU[FM]|LA|RS|LINE|LCIRCLE|"
    r"TeX-|rsfs|txsy|wasy|stmary|"
    r".*Mono|.*Code|.*Ital|.*Sym|.*Math|"
    r"EUFM|MSBM|MSAM|CMSY|CMEX|CMMI|S[0-9]|"
    r"STIX.*|XITS.*|Cambria\s*Math|Asana\s*Math|LMMath|MnSymbol|"
    r"bb[0-9]?|bbold|cal[0-9]?|frak[0-9]?|mathscr)"
)


def _extract_font_name(font):
    """Copy of the _extract_font_name from converter.py"""
    if isinstance(font, bytes):
        try:
            font = font.decode("utf-8")
        except UnicodeDecodeError:
            return ""
    if "+" in font:
        font = font.split("+")[-1]
    return font


class TestExtractFontName:
    def test_simple_font_name(self):
        assert _extract_font_name("CMMI10") == "CMMI10"

    def test_font_with_prefix(self):
        assert _extract_font_name("ABCDEF+CMMI10") == "CMMI10"

    def test_multiple_plus(self):
        assert _extract_font_name("A+B+CMMI10") == "CMMI10"

    def test_bytes_input(self):
        assert _extract_font_name(b"CMMI10") == "CMMI10"

    def test_bytes_with_prefix(self):
        assert _extract_font_name(b"/ABCDEF+CMMI10") == "CMMI10"

    def test_empty_string(self):
        assert _extract_font_name("") == ""

    def test_no_plus(self):
        assert _extract_font_name("CMR10") == "CMR10"


class TestFormulaFontMatching:
    """Test that the regex correctly identifies formula/math fonts"""

    def test_cm_fonts(self):
        """LaTeX Computer Modern fonts (except CMR)"""
        assert FONT_PATTERN.match("CMMI10"), "CMMI10 should match"
        assert FONT_PATTERN.match("CMSY7"), "CMSY7 should match"
        assert FONT_PATTERN.match("CMEX10"), "CMEX10 should match"
        assert FONT_PATTERN.match("CMBX12"), "CMBX12 should match"

    def test_cmr_excluded(self):
        """CMR (regular text) should NOT match"""
        assert not FONT_PATTERN.match("CMR10"), "CMR10 should not match"

    def test_euler_fraktur(self):
        """Euler Fraktur fonts"""
        assert FONT_PATTERN.match("EUFM10"), "EUFM10 should match"

    def test_ms_fonts(self):
        """AMS mathematical fonts"""
        assert FONT_PATTERN.match("MSBM10"), "MSBM10 should match"
        assert FONT_PATTERN.match("MSAM10"), "MSAM10 should match"

    def test_stix_fonts(self):
        """STIX math fonts"""
        assert FONT_PATTERN.match("STIXGeneral"), "STIXGeneral should match"
        assert FONT_PATTERN.match("STIXMath"), "STIXMath should match"
        assert FONT_PATTERN.match("STIXSize1"), "STIXSize1 should match"

    def test_xits_fonts(self):
        """XITS math fonts"""
        assert FONT_PATTERN.match("XITSMath-Regular"), "should match"
        assert FONT_PATTERN.match("XITSMath-Bold"), "should match"

    def test_cambria_math(self):
        assert FONT_PATTERN.match("Cambria Math"), "should match"

    def test_lm_math(self):
        assert FONT_PATTERN.match("LMMathItalic10"), "should match"

    def test_mnsymbol(self):
        assert FONT_PATTERN.match("MnSymbol10"), "should match"

    def test_latex_script(self):
        assert FONT_PATTERN.match("rsfs10"), "rsfs10 should match"
        assert FONT_PATTERN.match("txsy"), "txsy should match"
        assert FONT_PATTERN.match("wasy10"), "wasy10 should match"
        assert FONT_PATTERN.match("stmary10"), "stmary10 should match"

    def test_symbol_mt(self):
        assert FONT_PATTERN.match("SymbolMT"), "should match"

    def test_mt_extra(self):
        assert FONT_PATTERN.match("MTExtra"), "should match"

    def test_bb_and_cal(self):
        assert FONT_PATTERN.match("bb7"), "bb7 should match"
        assert FONT_PATTERN.match("bbold"), "bbold should match"
        assert FONT_PATTERN.match("cal10"), "cal10 should match"

    def test_frak_and_mathscr(self):
        assert FONT_PATTERN.match("frak10"), "frak10 should match"
        assert FONT_PATTERN.match("mathscr"), "mathscr should match"

    def test_s_series(self):
        assert FONT_PATTERN.match("S1"), "S1 should match"
        assert FONT_PATTERN.match("S2"), "S2 should match"

    def test_suffix_patterns(self):
        assert FONT_PATTERN.match("TeX-123"), "should match"
        assert FONT_PATTERN.match("SomeCustomMath"), "should match"
        assert FONT_PATTERN.match("SomeSym"), "should match"
        assert FONT_PATTERN.match("SomeItal"), "should match"
        assert FONT_PATTERN.match("SomeCode"), "should match"

    def test_text_fonts_not_matched(self):
        assert not FONT_PATTERN.match("Helvetica")
        assert not FONT_PATTERN.match("TimesNewRoman")
        assert not FONT_PATTERN.match("ArialMT")
        assert not FONT_PATTERN.match("Courier")
        assert not FONT_PATTERN.match("NotoSans")
        assert not FONT_PATTERN.match("SourceHanSerif")


class TestIntegratedExtractAndMatch:
    """Test full pipeline: extract font name then match"""

    def _match(self, raw_font_name):
        font = _extract_font_name(raw_font_name)
        return bool(FONT_PATTERN.match(font))

    def test_prefixed_euler(self):
        assert self._match("/ABC123+EUFM10")

    def test_prefixed_cmsy(self):
        assert self._match("/XYZ789+CMSY7")

    def test_prefixed_cmr_not_matched(self):
        assert not self._match("/ABC+CMR10")

    def test_bytes_prefixed(self):
        assert self._match(b"/ABCDEF+CMMI10")

    def test_springer_book_fonts(self):
        """Simulate real Springer PDF font naming"""
        assert self._match("EUFM10")
        assert self._match("CMSY7")
        assert self._match("CMEX10")
        assert self._match("CMMI10")
        assert self._match("MSBM10")
