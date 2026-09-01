# -*- coding: utf-8 -*-
"""Commit 7D — eval.normalize tests.

Normalization removes unimportant noise (font subset prefixes, family aliases,
whitespace, float rounding) while preserving real drift.
"""

from pdf2zh.semantic.eval.normalize import (
    canon,
    normalize_doc,
    normalize_font,
    normalize_pdf,
)

from tests.pdf_eval_build import build_prose


def test_normalize_font_strips_subset_prefix():
    assert normalize_font("ABCDEF+TimesNewRomanPSMT") == "times"


def test_normalize_font_family_aliases():
    assert normalize_font("TimesNewRoman") == "times"
    assert normalize_font("Helvetica") == "helv"
    assert normalize_font("ArialMT") == "helv"
    assert normalize_font("CourierNewPSMT") == "cour"
    assert normalize_font("SimSun") == "cjk"
    assert normalize_font("SimHei") == "cjk"


def test_normalize_font_unknown_and_empty():
    assert normalize_font("MyCustomFamily") == "mycustomfamily"
    assert normalize_font("") == "unknown"


def test_canon_collapses_whitespace():
    assert canon("  a \t b\n  c ") == "a b c"
    assert canon("") == ""


def test_normalize_doc_shape(tmp_path):
    src = str(tmp_path / "prose.pdf")
    build_prose(src)
    doc = normalize_pdf(src)
    assert {"meta", "pages", "outline"} <= set(doc)
    line = doc["pages"][0]["lines"][0]
    assert line["font"] == normalize_font(line["font"])  # already canonical
    assert all(isinstance(b, float) for b in line["bbox"])
    # text canonicalized (single-spaced)
    assert "  " not in line["text"]
    # normalize_doc is idempotent
    again = normalize_doc(doc)
    assert again == doc
