# -*- coding: utf-8 -*-
"""Commit 7D — eval.extract tests.

Extract the structural model (lines/words/outline) from a real mini PDF.
"""

from pdf2zh.semantic.eval.extract import extract

from tests.pdf_eval_build import build_prose


def test_extract_prose_pages_lines_words(tmp_path):
    src = str(tmp_path / "prose.pdf")
    build_prose(src, with_outline=True)
    doc = extract(src)
    assert doc["meta"]["page_count"] == 1
    assert len(doc["pages"]) == 1

    page = doc["pages"][0]
    assert page["num"] == 1
    assert page["width"] == 612.0 and page["height"] == 792.0
    assert page["lines"], "expected extracted text lines"
    assert all(
        set(ln) >= {"text", "bbox", "size", "font", "bold", "italic"}
        for ln in page["lines"]
    )
    assert all(len(ln["bbox"]) == 4 for ln in page["lines"])
    assert page["words"] and all(
        set(w) >= {"text", "bbox"} for w in page["words"]
    )
    # outline captured
    assert doc["outline"] and doc["outline"][0]["title"] == "Introduction"
    assert doc["outline"][0]["page"] == 1
    assert doc["outline"][0]["level"] == 1


def test_extract_style_flags(tmp_path):
    """Bold/italic derived from pymupdf flags survive extraction."""
    src = str(tmp_path / "style.pdf")
    doc = extract(str(build_prose(src)))
    page = doc["pages"][0]
    by_text = {ln["text"]: ln for ln in page["lines"]}
    assert by_text["Title Here"]["bold"] is True
    assert by_text["Title Here"]["italic"] is False
    assert by_text["And an italicised emphasis line."]["italic"] is True


def test_extract_json_safe(tmp_path):
    import json

    src = str(tmp_path / "prose.pdf")
    build_prose(src)
    payload = extract(src)
    assert json.loads(json.dumps(payload)) == payload


def test_extract_missing_file_raises(tmp_path):
    import pytest

    from pdf2zh.semantic.eval.extract import extract

    with pytest.raises(Exception):
        extract(str(tmp_path / "nope.pdf"))