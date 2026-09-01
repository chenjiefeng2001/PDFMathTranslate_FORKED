# -*- coding: utf-8 -*-
"""7I-7 — XObject/unicode compatibility regression tests.

Locks the root cause of ``Xobj id must be provided when unicode is provided``:

- BabelDOC's ``TypesettingUnit.__init__`` asserts that a unicode typesetting
  unit has a non-None ``xobj_id`` (the XObject container that anchors the text
  on the original page).
- The value flows ``paragraph.xobj_id = chars[0].xobj_id``; ``None`` means the
  paragraph's text was never attributed to any XObject container.
- Trigger: a paragraph carrying translated ``pdf_same_style_unicode_characters``
  (the normal output of any real translation) whose ``xobj_id`` is ``None``.

Minimal reproducer (no PDF, no full pipeline) is frozen here so the invariant
and its boundary (-1/0 are valid page-level sentinels) stay pinned.
"""

from __future__ import annotations

import pymupdf
import pytest

from babeldoc.format.pdf.document_il import il_version_1
from babeldoc.format.pdf.document_il.midend.typesetting import Typesetting

_ERR = "Xobj id must be provided when unicode is provided"


class _FontMapper:
    """Minimal FontMapper stub: identity mapping."""

    def map(self, font, unicode_char):
        return font


def _paragraph_with_unicode(xobj_id):
    """A translated paragraph carrying a unicode composition."""
    gs = il_version_1.GraphicState()
    style = il_version_1.PdfStyle(font_id="F1", font_size=12.0, graphic_state=gs)
    return il_version_1.PdfParagraph(
        box=il_version_1.Box(x=0, y=0, x2=100, y2=20),
        pdf_style=style,
        xobj_id=xobj_id,
        pdf_paragraph_composition=[
            il_version_1.PdfParagraphComposition(
                pdf_same_style_unicode_characters=(
                    il_version_1.PdfSameStyleUnicodeCharacters(
                        unicode="测",
                        pdf_style=style,
                    )
                ),
            ),
        ],
    )


def _create_units(xobj_id):
    eng = Typesetting.__new__(Typesetting)
    eng.font_mapper = _FontMapper()
    font = pymupdf.Font("china-s")
    para = _paragraph_with_unicode(xobj_id)
    return eng.create_typesetting_units(para, {"F1": font})


def test_unicode_unit_with_none_xobj_id_asserts():
    """The exact failure: unicode unit + xobj_id=None must raise the error."""
    with pytest.raises(AssertionError, match=_ERR):
        _create_units(None)


def test_page_level_sentinels_do_not_assert():
    """-1/0 (page-level XObject sentinels) must NOT trigger the assertion."""
    for sentinel in (-1, 0, 1, 7):
        units = _create_units(sentinel)
        assert units, f"expected units for xobj_id={sentinel}"
        assert all(u.xobj_id == sentinel for u in units)


def test_error_string_origin_is_babeldoc_typesetting():
    """The message must live in BabelDOC's TypesettingUnit (not pdf2zh)."""
    import inspect

    from babeldoc.format.pdf.document_il.midend import typesetting as ts

    init_src = inspect.getsource(ts.TypesettingUnit.__init__)
    assert _ERR in init_src
    assert "xobj_id is not None" in init_src


def test_paragraph_xobj_id_flows_from_first_char():
    """paragraph.xobj_id is assigned from chars[0].xobj_id (root cause wire)."""
    import inspect

    from babeldoc.format.pdf.document_il.midend import paragraph_finder as pf

    src = inspect.getsource(pf)
    assert "paragraph.xobj_id = chars[0].xobj_id" in src


def test_shim_normalizes_none_to_minus1_for_unicode_units():
    """7I-7C fix semantics: the shim maps xobj_id=None → -1 for unicode units,
    so the production pipeline can never trip BabelDOC's assertion, while all
    real xobj_id values (0/positive/-1) pass through unchanged.

    This is the production layer of the 7I-7 fix — upstream workaround until
    BabelDOC itself treats None as its -1 "no XObject" sentinel.
    """
    from pdf2zh.babeldoc_xobj_shim import (
        _NO_XOBJ_SENTINEL,
        apply_babeldoc_xobj_shim,
        get_babeldoc_xobj_shim_enabled,
    )

    assert _NO_XOBJ_SENTINEL == -1
    assert get_babeldoc_xobj_shim_enabled()  # default on

    from babeldoc.format.pdf.document_il.midend.typesetting import (
        TypesettingUnit,
    )

    apply_babeldoc_xobj_shim()
    try:
        # None + unicode → normalized to -1 → no assertion, unit built.
        style = il_version_1.PdfStyle(
            font_id="F1", font_size=12.0, graphic_state=il_version_1.GraphicState()
        )
        unit = TypesettingUnit(unicode="测", xobj_id=None, font_size=12.0, style=style)
        assert unit.xobj_id == -1

        # Real xobj_ids untouched.
        for real in (0, 1, 7, -1):
            u = TypesettingUnit(unicode="测", xobj_id=real, font_size=12.0, style=style)
            assert u.xobj_id == real
    finally:
        # Restore pristine class for other tests.
        from pdf2zh.babeldoc_xobj_shim import _ORIGINAL_INIT

        if _ORIGINAL_INIT is not None:
            TypesettingUnit.__init__ = _ORIGINAL_INIT
            from pdf2zh.babeldoc_xobj_shim import _PATCH_LOCK

            with _PATCH_LOCK:
                import pdf2zh.babeldoc_xobj_shim as mod

                mod._ORIGINAL_INIT = None


def test_shim_off_keeps_native_assert():
    """With the env gate off, native BabelDOC behavior is preserved (None still
    asserts) — the shim is strictly opt-in defense, never semantic override.
    """
    import os

    import pdf2zh.babeldoc_xobj_shim as mod
    from pdf2zh.babeldoc_xobj_shim import get_babeldoc_xobj_shim_enabled

    os.environ["PDF2ZH_BABELDOC_XOBJ_SHIM"] = "0"
    try:
        assert not get_babeldoc_xobj_shim_enabled()
        mod.apply_babeldoc_xobj_shim()  # must be a silent no-op
    finally:
        os.environ.pop("PDF2ZH_BABELDOC_XOBJ_SHIM", None)
    assert get_babeldoc_xobj_shim_enabled()


def test_books_are_page_level_text_high_risk():
    """Both reported books place nearly all text at page level (0 XObjects).

    Page-level text is exactly the class where ``xobj_id`` may end up None
    after parsing, which is why both books hit the same assertion.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    books = [
        "tests/file/Matrix Algebra (Abadir K.M., Magnus J.R.) "
        "(z-library.sk, 1lib.sk, z-lib.sk).pdf",
        "tests/file/Groups and Symmetries From Finite Groups to Lie Groups, "
        "Second Edition (Yvette Kosmann-Schwarzbach) "
        "(z-library.sk, 1lib.sk, z-lib.sk).pdf",
    ]
    # 大 PDF fixture 不入库（.gitignore 排除 *.pdf）；CI clean checkout 没有
    # 这些文件时优雅 skip，本地 corpus 取证时照常执行。
    missing = [b for b in books if not (root / b).exists()]
    if missing:
        pytest.skip(f"fixtures not present in this checkout: {missing}")
    for b in books:
        doc = pymupdf.open(str(root / b))
        page = doc[2]  # a body-ish page
        assert len(page.get_xobjects()) <= 1
        doc.close()
