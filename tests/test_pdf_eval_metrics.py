# -*- coding: utf-8 -*-
"""Commit 7D — eval.metrics tests (through ``evaluate``).

Verifies the structural-fidelity report on small real PDFs, the sensitivity of
list / TOC / code metrics to a real geometric regression, the overflow counter,
and end-to-end integration over five PDF categories.
"""

from pdf2zh.semantic.eval import evaluate

from tests.pdf_eval_build import (
    add_page,
    build_cjk,
    build_code,
    build_list,
    build_nested_list,
    build_prose,
    build_toc,
    new_doc,
    write,
)


def test_identical_document_is_perfect(tmp_path):
    src = str(tmp_path / "a.pdf")
    out = str(tmp_path / "b.pdf")
    build_prose(src)
    build_prose(out)
    r = evaluate(src, out)["metrics"]
    assert r["text_exactness"] == 1.0
    assert r["font_match_rate"] == 1.0
    assert r["bold_accuracy"] == 1.0
    assert r["italic_accuracy"] == 1.0
    assert r["bbox_mean_delta"] <= 0.1
    assert r["overflow_count"] == 0


def test_list_indentation_regression(tmp_path):
    """Moving list content/continuation indentation drops the list metrics."""
    src = str(tmp_path / "list_src.pdf")
    ok = str(tmp_path / "list_ok.pdf")
    build_list(src)
    build_list(ok)
    good = evaluate(src, ok)["metrics"]
    assert good["list_content_x_accuracy"] == 1.0
    assert good["list_continuation_x_accuracy"] == 1.0

    # broken output: continuation lines shifted +25pt indentation
    bad = str(tmp_path / "list_bad.pdf")
    doc = new_doc()
    add_page(doc, [
        (60, 100, "1. Algorithm design", "body", 12),
        (120, 115, "a wrapped continuation line", "body", 12),
        (60, 150, "2. Evaluation on the test set", "body", 12),
        (120, 165, "second continuation line", "body", 12),
    ])
    write(doc, bad)
    broke = evaluate(src, bad)["metrics"]
    assert broke["list_continuation_x_accuracy"] < 0.5


def test_toc_column_regression(tmp_path):
    """Moving the TOC right page column drops toc_page_x_accuracy."""
    src = str(tmp_path / "toc_src.pdf")
    ok = str(tmp_path / "toc_ok.pdf")
    build_toc(src)
    build_toc(ok)
    good = evaluate(src, ok)["metrics"]
    assert good["toc_title_x_accuracy"] == 1.0
    assert good["toc_page_x_accuracy"] == 1.0
    assert good["toc_level_accuracy"] == 1.0
    assert good["toc_page_number_accuracy"] == 1.0
    assert good["outline_destination_accuracy"] == 1.0

    # broken output: second entry's page number column drawn further right
    bad = str(tmp_path / "toc_bad.pdf")
    doc = new_doc()
    add_page(doc, [
        (72, 80, "Contents", "bold", 14),
        (72, 110, "Introduction ................. 1", "body", 12),
        (96, 135, "Background ........................ 3", "body", 12),
    ])
    doc.set_toc([[1, "Introduction", 1], [2, "Background", 1]])
    write(doc, bad)
    broke = evaluate(src, bad)["metrics"]
    assert broke["toc_page_x_accuracy"] < 0.5
    # page numbers themselves still match
    assert broke["toc_page_number_accuracy"] == 1.0


def test_code_preserved_bbox_regression(tmp_path):
    """Code lines kept verbatim at the same bbox => 1.0; shifted => drops."""
    src = str(tmp_path / "code_src.pdf")
    ok = str(tmp_path / "code_ok.pdf")
    build_code(src)
    build_code(ok)
    good = evaluate(src, ok)["metrics"]
    assert good["code_preserved_bbox"] == 1.0
    assert good["text_exactness"] == 1.0

    bad = str(tmp_path / "code_bad.pdf")
    doc = new_doc()
    add_page(doc, [
        (120, 90, "def f(x, y):", "mono", 10),
        (132, 110, "return x + long_identifier_name(y)", "mono", 10),
    ])
    write(doc, bad)
    broke = evaluate(src, bad)["metrics"]
    assert broke["code_preserved_bbox"] < 1.0


def test_list_marker_x_regression(tmp_path):
    """Marker column moved (+25pt) drops list_marker_x_accuracy."""
    src = str(tmp_path / "mk_src.pdf")
    ok = str(tmp_path / "mk_ok.pdf")
    build_list(src)
    build_list(ok)
    good = evaluate(src, ok)["metrics"]
    assert good["list_marker_x_accuracy"] == 1.0

    bad = str(tmp_path / "mk_bad.pdf")
    doc = new_doc()
    add_page(doc, [
        (85, 100, "1. Algorithm design", "body", 12),
        (95, 115, "a wrapped continuation line", "body", 12),
        (85, 150, "2. Evaluation on the test set", "body", 12),
        (95, 165, "second continuation line", "body", 12),
    ])
    write(doc, bad)
    broke = evaluate(src, bad)["metrics"]
    assert broke["list_marker_x_accuracy"] < 0.5


def test_list_wrap_integrity_marker_loss(tmp_path):
    """Markers merged away (two items on one line) drops wrap integrity."""
    src = str(tmp_path / "wi_src.pdf")
    ok = str(tmp_path / "wi_ok.pdf")
    build_list(src)
    build_list(ok)
    good = evaluate(src, ok)["metrics"]
    assert good["list_wrap_integrity"] == 1.0

    # broken output: the second item's marker is swallowed (item merged into
    # a marker-less line) — marker 2. never reaches the output
    bad = str(tmp_path / "wi_bad.pdf")
    doc = new_doc()
    add_page(doc, [
        (60, 100, "1. Algorithm design", "body", 12),
        (95, 115, "a wrapped continuation line", "body", 12),
        (60, 150, "Evaluation on the test set", "body", 12),
    ])
    write(doc, bad)
    broke = evaluate(src, bad)["metrics"]
    assert broke["list_wrap_integrity"] < 1.0


def test_list_nested_geometry_flattened(tmp_path):
    """A nested list flattened to one level drops nested geometry accuracy."""
    src = str(tmp_path / "nl_src.pdf")
    ok = str(tmp_path / "nl_ok.pdf")
    build_nested_list(src)
    build_nested_list(ok)
    good = evaluate(src, ok)["metrics"]
    assert good["list_nested_geometry_accuracy"] == 1.0
    assert good["list_wrap_integrity"] == 1.0

    # broken output: all items at the same column (nesting flattened)
    bad = str(tmp_path / "nl_bad.pdf")
    doc = new_doc()
    add_page(doc, [
        (40, 100, "1. Intro", "body", 12),
        (40, 120, "a. Background", "body", 12),
        (40, 140, "i. deep", "body", 12),
        (40, 180, "2. Method", "body", 12),
    ])
    write(doc, bad)
    broke = evaluate(src, bad)["metrics"]
    assert broke["list_nested_geometry_accuracy"] < 1.0


def test_overflow_count(tmp_path):
    """A line starting left of the page edge (negative x0) raises overflow_count.

    pymupdf clips right-edge overflow during extraction, but a negative x0
    survives, so the overflow metric keys off off-page geometry both ways.
    """
    src = str(tmp_path / "ov_src.pdf")
    out = str(tmp_path / "ov_bad.pdf")
    build_prose(src)
    doc = new_doc()
    add_page(doc, [(-20, 100, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "body", 30)])
    write(doc, out)
    r = evaluate(src, out)["metrics"]
    assert r["overflow_count"] >= 1


# -- 7D integration: real PDFs across five categories -----------------------

class TestRealPdfIntegration:
    """Evaluate faithful copies of code / list / toc / style / cjk PDFs."""

    _CASES = [
        ("code", build_code),
        ("list", build_list),
        ("toc", build_toc),
        ("style", build_prose),
        ("cjk", build_cjk),
    ]

    def test_all_categories_report_sane(self, tmp_path):
        for name, builder in self._CASES:
            src = str(tmp_path / f"{name}_src.pdf")
            out = str(tmp_path / f"{name}_out.pdf")
            builder(src)
            builder(out)
            r = evaluate(src, out)["metrics"]
            assert r["text_exactness"] == 1.0, name
            assert r["overflow_count"] == 0, name
            assert 0.0 <= r["bbox_mean_delta"] <= 5.0, name
            # preserves all structural metrics for a faithful copy
            for key in (
                "text_exactness",
                "font_match_rate",
                "list_content_x_accuracy",
                "list_continuation_x_accuracy",
                "toc_page_x_accuracy",
                "toc_level_accuracy",
                "outline_destination_accuracy",
                "code_preserved_bbox",
            ):
                assert key in r, f"{name}.{key}"

    def test_cjk_pdf_report(self, tmp_path):
        src = str(tmp_path / "cjk_src.pdf")
        out = str(tmp_path / "cjk_out.pdf")
        build_cjk(src)
        build_cjk(out)
        r = evaluate(src, out)["metrics"]
        assert r["text_exactness"] == 1.0
        assert r["font_match_rate"] == 1.0