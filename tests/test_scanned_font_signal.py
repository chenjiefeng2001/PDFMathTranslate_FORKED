"""font_to_unicode 信号的根因修复回归测试。

背景（2026-08-22 根因分析）：pdfminer.six 的 LTChar 不暴露字体对象、
``PDFFont.get_toUnicode()`` API 不存在，旧实现异常被吞后所有字形恒计为
「不可信」——该信号对**任何** PDF 恒输出 1.000，预检恒触发自动 OCR
（此前仅在合成样张上被注意到，实际影响所有文档）。

修复后语义：
- 简单字体（Type1/TrueType/Type3）缺 ToUnicode 属常态 ⇒ 计为可信；
- 复合字体（Type0/CIDFontType*）缺 ToUnicode ⇒ 解码不可信（真损坏信号）；
- 字体无法定位时保持 None（保守计数）。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from pdf2zh.scanned_detection import (
    TO_UNICODE_MISSING_THRESHOLD,
    _page_font_table,
    _ps_name,
    analyze_glyph_signals,
    preflight_scan_check,
)

fitz = pytest.importorskip("fitz")
PSLiteral = pytest.importorskip("pdfminer.psparser").PSLiteral


def _make_pdf(path: Path, text: str, fontname: str = "helv") -> None:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text, fontname=fontname, fontsize=14)
    doc.save(str(path))
    doc.close()


class TestPsName(unittest.TestCase):
    def test_bytes_psliteral_str(self):
        self.assertEqual(_ps_name(b"Song"), "Song")
        self.assertEqual(_ps_name(PSLiteral("Type0")), "Type0")
        self.assertEqual(_ps_name("Helvetica"), "Helvetica")
        self.assertEqual(_ps_name("/LeadingSlash"), "LeadingSlash")
        self.assertEqual(_ps_name(None), "")


class TestPageFontTable(unittest.TestCase):
    def _fake_page(self, fonts):
        return type("FakePage", (), {"resources": {"Font": fonts}})()

    def test_str_and_bytes_keys_both_resolved(self):
        """资源字典的键可能是 str 或 bytes（pdfminer 版本差异），都要能读。"""
        for keyfmt in (lambda k: k, lambda k: k.encode("latin-1")):
            with self.subTest(keyfmt=keyfmt):
                page = self._fake_page(
                    {
                        keyfmt("F1"): {
                            "Subtype": PSLiteral("Type0"),
                            "BaseFont": b"ABCDEF+SimSun",
                            # ToUnicode 缺失
                        },
                        keyfmt("F2"): {
                            "Subtype": PSLiteral("Type1"),
                            "BaseFont": b"Helvetica",
                            "ToUnicode": "<stream>",  # 存在即可
                        },
                    }
                )
                table = _page_font_table(page)
                self.assertEqual(table["F1"], {"tounicode": False, "cid": True})
                self.assertEqual(
                    table["ABCDEF+SimSun"], {"tounicode": False, "cid": True}
                )
                self.assertEqual(table["F2"], {"tounicode": True, "cid": False})
                self.assertEqual(table["Helvetica"], {"tounicode": True, "cid": False})

    def test_malformed_resources_returns_empty(self):
        self.assertEqual(_page_font_table(type("P", (), {"resources": None})()), {})
        self.assertEqual(_page_font_table(type("P", (), {"resources": {}})()), {})


class TestFontSignalSemantics(unittest.TestCase):
    def _records(self, hu_list):
        return [
            {"char": "x", "has_to_unicode": hu, "decode": "ok", "is_replacement": False}
            for hu in hu_list
        ]

    def test_analyze_counts_false_and_none_as_untrusted(self):
        sig = analyze_glyph_signals(self._records([True, False, None]))
        self.assertEqual((sig.total_glyphs, sig.no_to_unicode), (3, 2))

    def test_threshold_constant_unchanged(self):
        self.assertEqual(TO_UNICODE_MISSING_THRESHOLD, 0.60)

    def test_all_none_records_ratio_one(self):
        sig = analyze_glyph_signals(self._records([None] * 4))
        self.assertEqual(sig.to_unicode_missing_ratio, 1.0)


@pytest.mark.parametrize("fontname,expected_tu", [("helv", True), ("china-ss", False)])
def test_extraction_font_signal_by_font_class(fontname, expected_tu):
    """真实 pdfminer 提取路径：简单字体可信 / Type0-CJK 无 ToUnicode 判不可信。"""
    try:
        with TemporaryDirectory() as td:
            path = Path(td) / f"sample_{fontname}.pdf"
            _make_pdf(path, "Sample 中文 Text 123", fontname=fontname)
            _, glyphs = __import__(
                "pdf2zh.scanned_detection",
                fromlist=["_extract_pdf_samples"],
            )._extract_pdf_samples(str(path))
            assert glyphs, "未提取到任何字形"
            values = {g["has_to_unicode"] for g in glyphs}
            assert values == {
                expected_tu
            }, f"font={fontname}: 期望全部 {expected_tu}，实际 {values}"
    except RuntimeError as exc:  # fitz 内置字体缺失等环境差异
        pytest.skip(f"环境不支持字体 {fontname}: {exc}")


def test_preflight_not_scanned_on_healthy_synthetic():
    """修复目标回归：合成健康样张不得再因 font_to_unicode 恒触发 OCR。"""
    with TemporaryDirectory() as td:
        path = Path(td) / "healthy.pdf"
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), "Sample Heading", fontsize=16)
        page.insert_text((72, 120), "First paragraph of English text.", fontsize=11)
        doc.save(str(path))
        doc.close()
        decision = preflight_scan_check(str(path))
        font_sig = next(s for s in decision.signals if s.name == "font_to_unicode")
        assert font_sig.value == 0.0, font_sig.detail
        assert not font_sig.triggered


if __name__ == "__main__":
    unittest.main()
