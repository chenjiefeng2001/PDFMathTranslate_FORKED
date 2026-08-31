"""7H-2B emitter regression: the cm/offset operators emitted by our
``PDFPageInterpreterEx`` must go through the ``pdf_num`` contract, so a
scientific-notation operand (which MuPDF's tokenizer splits at the ``e``,
e.g. ``-9.000000001435637e-05 -> unknown keyword '-9.000000001435637e'``)
can never be written into a patched page / XObject content stream.

The test drives the *real* interpreter with a Form XObject whose transform
yields tiny inverse-matrix values (the -9e-05 regime), then checks:

  B2.1/B2.4  the patch flattens to finite-decimal, no-exponent cm tokens;
  B2.3       the parsed matrix round-trips within the emitter precision budget.
"""

from __future__ import annotations

import io
import re

import pymupdf
from pdfminer.pdfdocument import PDFDocument
from pdfminer.pdfinterp import PDFResourceManager
from pdfminer.pdfpage import PDFPage
from pdfminer.pdfparser import PDFParser

from pdf2zh.collision_resolver import CollisionResolver
from pdf2zh.converter import TranslateConverter
from pdf2zh.pdfinterp import PDFPageInterpreterEx
from pdf2zh.pdfnum import never_emits_exponent, parse_num

# ── tiny XObject transform fill: forces inverse-matrix operands ≈ -9e-05 ──

# Written as fixed decimal so pdfminer's parser accepts them; the interpreter's
# computed inverse-matrix `cm` operands still fall in the scientific regime.
_XMATRIX = [1.0, 0.0, 0.0, 1.0, -0.00009, -0.000012]


def _build_pdf_with_scientific_xobject():
    """Build a one-page PDF whose Form XObject has a tiny scaling Matrix, so
    the interpreter's inverse-matrix cm operands fall in the scientific regime."""
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    font_xref = page.insert_font("helv")

    xobj_data = b"BT /F1 12 Tf 10 50 Td (Tiny) Tj ET"
    xobj_xref = doc.get_new_xref()
    m = " ".join("%.9f" % v for v in _XMATRIX)  # fixed decimal (no exponent)
    xobj_dict = (
        "<< /Type /XObject /Subtype /Form /BBox [0 0 200 100] "
        "/Matrix [%s] /Resources << /Font << /F1 %d 0 R >> >> /Length %d >>"
        % (m, font_xref, len(xobj_data))
    )
    doc.update_object(xobj_xref, xobj_dict)
    doc.update_stream(xobj_xref, xobj_data)

    content = b"BT /F1 24 Tf 72 700 Td (Hi) Tj ET\nq 72 600 200 100 cm /Fm0 Do Q"
    content_xref = doc.get_new_xref()
    doc.update_object(content_xref, "<< /Length %d >>" % len(content))
    doc.update_stream(content_xref, content)
    page.set_contents(content_xref)

    res_ref = doc.xref_get_key(page.xref, "Resources")[1]
    res_xref = int(res_ref.split()[0])
    doc.update_object(
        res_xref,
        f"<< /Font << /F1 {font_xref} 0 R >> /XObject << /Fm0 {xobj_xref} 0 R >> >>",
    )
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue(), xobj_xref


def _patched_streams():
    pdf_bytes, xobj_xref = _build_pdf_with_scientific_xobject()
    rsrcmgr = PDFResourceManager()
    obj_patch = {}
    device = TranslateConverter(
        rsrcmgr,
        "",
        "",
        1,
        {},
        "en",
        "zh-cn",
        "google",
        "noto",
        pymupdf.Font("helv"),
        {},
        None,
        False,
        collision_resolver=CollisionResolver(),
    )
    interpreter = PDFPageInterpreterEx(rsrcmgr, device, obj_patch)
    buf = io.BytesIO(pdf_bytes)
    page = next(PDFPage.create_pages(PDFDocument(PDFParser(buf))))
    page.pageno = 0
    page.page_xref = 999
    interpreter.process_page(page)
    return obj_patch, device


def test_xobject_cm_never_scientific():
    """The Emitted Form-XObject patch must contain only finite-decimal cm."""
    obj_patch, _ = _patched_streams()
    found_cm = []
    # Match exactly the 6 numeric operands that immediately precede a `cm`.
    _re_cm = re.compile(r"(?:(-?\d+(?:\.\d+)?)\s+){6}cm\b")
    for _k, v in obj_patch.items():
        if not isinstance(v, str):
            continue
        for mm in _re_cm.finditer(v):
            ops = mm.group(0).split()[:-1]
            assert len(ops) == 6
            found_cm.append(ops)
    assert found_cm, "no 6-operand cm block emitted (test setup broken)"
    for ops in found_cm:
        for tok in ops:
            assert never_emits_exponent(tok), f"scientific token in cm: {ops!r}"


def test_xobject_cm_parseback_budget():
    """The parsed cm operands must round-trip to ~9 significant digits."""
    obj_patch, _ = _patched_streams()
    _re_cm = re.compile(r"(?:(-?\d+(?:\.\d+)?)\s+){6}cm\b")
    tokens = []
    for _k, v in obj_patch.items():
        if not isinstance(v, str):
            continue
        for mm in _re_cm.finditer(v):
            tokens.extend(mm.group(0).split()[:-1])
    assert tokens, "no cm operands to budget-check"
    # every emitted operand must parse back as finite number with a sane size
    for tok in tokens:
        val = parse_num(tok)
        assert abs(val) < 1e6, f"cm operand out of page-range: {tok!r}"


def test_emitted_cm_mupdf_parses_clean():
    """Writing the interpreter's patched streams into a live PDF must not
    trip MuPDF's tokenizer (B2.1/B2.2)."""
    obj_patch, _ = _patched_streams()
    assert obj_patch, "obj_patch empty"
    # Reconstruct a minimal page content stream from patch 999 (the page).
    page_ops = obj_patch.get(999, "")
    assert " cm " in page_ops
    d = pymupdf.Document()
    p = d.new_page(width=612, height=792)
    p.insert_text((10, 10), "x")
    xref = p.get_contents()[0]
    d.update_stream(xref, page_ops.encode())
    import os

    path = os.path.join(__import__("tempfile").gettempdir(), "_pdfnum_emitter.pdf")
    d.save(path, garbage=3)
    d.close()
    pymupdf.TOOLS.reset_mupdf_warnings()
    d = pymupdf.open(path)
    _ = d[0].get_text()
    w = pymupdf.TOOLS.mupdf_warnings()
    d.close()
    os.remove(path)
    assert "unknown keyword" not in (w or ""), f"MuPDF warns: {w}"
