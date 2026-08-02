"""Tests for Form XObject text stripping (Background Stream Issue) and
collision-obstacle injection."""
import io
import unittest

import pymupdf
from pdfminer.pdfdocument import PDFDocument
from pdfminer.pdfinterp import PDFResourceManager
from pdfminer.pdfpage import PDFPage
from pdfminer.pdfparser import PDFParser

from pdf2zh.collision_resolver import CollisionResolver
from pdf2zh.converter import TranslateConverter
from pdf2zh.pdfinterp import PDFPageInterpreterEx


class FakeTranslator:
    lang_in = "en"
    lang_out = "zh-cn"

    def translate(self, s: str) -> str:
        return "中文翻译测试内容"


def build_pdf_with_xobject():
    """Build a one-page PDF with a Form XObject containing English text.

    Returns (pdf_bytes, xobj_xref).
    """
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    font_xref = page.insert_font("helv")

    # Form XObject whose content stream contains original text
    xobj_data = b"BT /F1 12 Tf 10 50 Td (Original English) Tj ET"
    xobj_xref = doc.get_new_xref()
    xobj_dict = (
        "<< /Type /XObject /Subtype /Form /BBox [0 0 200 100] "
        "/Resources << /Font << /F1 %d 0 R >> >> /Length %d >>"
        % (font_xref, len(xobj_data))
    )
    doc.update_object(xobj_xref, xobj_dict)
    doc.update_stream(xobj_xref, xobj_data)

    # Page content: line text + invoke the XObject
    content = (
        b"BT /F1 24 Tf 72 700 Td (Hello World) Tj ET\n"
        b"q 72 600 200 100 cm /Fm0 Do Q"
    )
    content_xref = doc.get_new_xref()
    doc.update_object(content_xref, "<< /Length %d >>" % len(content))
    doc.update_stream(content_xref, content)
    page.set_contents(content_xref)

    # Page resources: font + xobject (Resources is an indirect ref)
    res_ref = doc.xref_get_key(page.xref, "Resources")[1]
    res_xref = int(res_ref.split()[0])
    doc.update_object(
        res_xref,
        f"<< /Font << /F1 {font_xref} 0 R >> /XObject << /Fm0 {xobj_xref} 0 R >> >>",
    )
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue(), xobj_xref


class TestXObjectStrip(unittest.TestCase):
    """Verify Form XObject content streams get their original text stripped
    (no more translucent original text overlaying the translation)."""

    def setUp(self):
        pdf_bytes, self.xobj_xref = build_pdf_with_xobject()
        self.rsrcmgr = PDFResourceManager()
        self.obj_patch = {}
        self.device = TranslateConverter(
            self.rsrcmgr, "", "", 1, {}, "en", "zh-cn", "google",
            "noto", pymupdf.Font("helv"), {}, None, False,
            collision_resolver=CollisionResolver(),
        )
        self.device.translator = FakeTranslator()
        self.interpreter = PDFPageInterpreterEx(
            self.rsrcmgr, self.device, self.obj_patch
        )
        buf = io.BytesIO(pdf_bytes)
        self.page = next(PDFPage.create_pages(PDFDocument(PDFParser(buf))))
        self.page.pageno = 0
        self.page.page_xref = 999  # dummy new content xref

    def test_xobject_text_stripped(self):
        """XObject patch must drop the original Tj operators and keep BT/ET."""
        self.interpreter.process_page(self.page)
        xobj_ops = self.obj_patch.get(self.xobj_xref, "")
        self.assertIn("ET", xobj_ops)                      # text block kept
        self.assertNotIn("Original English", xobj_ops)     # original text gone
        self.assertNotIn("Tj", xobj_ops.replace("TJ", ""))

    def test_page_patch_keeps_do(self):
        """Page patch must keep /Do so the (now stripped) XObject still draws."""
        self.interpreter.process_page(self.page)
        page_ops = self.obj_patch.get(999, "")
        self.assertIn("/Fm0 Do", page_ops)

    def test_collision_obstacles_recorded(self):
        """Figure bboxes are injected into collision obstacles."""
        self.interpreter.process_page(self.page)
        self.assertGreater(len(self.device._rendered_paragraphs), 0)
        self.assertGreater(len(self.device._rendered_obstacles), 0)
        first = self.device._rendered_obstacles[0]
        self.assertGreater(first.width, 0)
        self.assertGreater(first.height, 0)


if __name__ == "__main__":
    unittest.main()
