"""Commit 7F-5d — adaptive-TOC evaluator metrics tests.

Five machine-checkable metrics quantify the 7F-5a/5b TOC recovery contract
on real extracted PDFs (source vs output, index-aligned, no detector):

- ``toc_page_column_stability``   — ``page_x`` never moves (strict epsilon).
- ``toc_adaptive_wrap_integrity`` — every wrapped line keeps the entry's
  title column, no wrapped line is a duplicate, no same-language title word
  is lost.
- ``toc_adaptive_font_size``      — font size is stable when the entry does
  not grow, never increases, and genuinely shrinks when SHRINK was required
  (a naive font-size-only drop overflows the page column).
- ``toc_adaptive_overflow``       — an entry that cannot fit is honest:
  explicit overflow evidence or every same-language title word present;
  a silently truncated title (CLIP) drops it.
- ``toc_continuation_x_accuracy`` — continuation column preserved.

Each metric scores 1.0 on a faithful corpus pair and drops on exactly the
intentional break it exists to catch.
"""

import pytest

from pdf2zh.semantic.eval import evaluate
from tests.pdf_eval_build import (
    build_toc_adaptive_cjk_output,
    build_toc_adaptive_cjk_source,
    build_toc_adaptive_cont_shifted,
    build_toc_adaptive_extreme_clip,
    build_toc_adaptive_extreme_output,
    build_toc_adaptive_page_column_shifted,
    build_toc_adaptive_shrink_output,
    build_toc_adaptive_shrink_undone,
    build_toc_adaptive_short,
    build_toc_adaptive_wrap_missing_word,
    build_toc_adaptive_wrap_output,
    build_toc_multiline,
)

ADAPTIVE = [
    "toc_adaptive_wrap_integrity",
    "toc_adaptive_font_size",
    "toc_adaptive_overflow",
    "toc_page_column_stability",
    "toc_continuation_x_accuracy",
]


def _metrics(tmp_path, sb, ob):
    src = str(tmp_path / "src.pdf")
    out = str(tmp_path / "out.pdf")
    sb(src)
    ob(out)
    return evaluate(src, out)["metrics"]


def _assert_all_one(r):
    for m in ADAPTIVE:
        assert r[m] == 1.0, f"{m} should be 1.0, got {r[m]}"


class TestCorpusPairs:
    """The six golden cases: every adaptive metric is perfect."""

    def test_short_title(self, tmp_path):
        _assert_all_one(
            _metrics(tmp_path, build_toc_adaptive_short, build_toc_adaptive_short)
        )

    def test_long_title_wrap(self, tmp_path):
        _assert_all_one(
            _metrics(tmp_path, build_toc_adaptive_short, build_toc_adaptive_wrap_output)
        )

    def test_long_title_shrink(self, tmp_path):
        _assert_all_one(
            _metrics(
                tmp_path, build_toc_adaptive_short, build_toc_adaptive_shrink_output
            )
        )

    def test_extreme_preserve_overflow(self, tmp_path):
        _assert_all_one(
            _metrics(
                tmp_path, build_toc_adaptive_short, build_toc_adaptive_extreme_output
            )
        )

    def test_cjk_wrap(self, tmp_path):
        _assert_all_one(
            _metrics(
                tmp_path, build_toc_adaptive_cjk_source, build_toc_adaptive_cjk_output
            )
        )

    def test_multiline_continuation(self, tmp_path):
        _assert_all_one(_metrics(tmp_path, build_toc_multiline, build_toc_multiline))


class TestSensitivity:
    """Each intentional break drops exactly the metric it targets."""

    def test_page_column_shifted(self, tmp_path):
        r = _metrics(
            tmp_path, build_toc_adaptive_short, build_toc_adaptive_page_column_shifted
        )
        assert r["toc_page_column_stability"] < 1.0
        assert r["toc_page_x_accuracy"] < 1.0  # graded twin also drops
        # nothing else about the entry is harmed
        assert r["toc_adaptive_font_size"] == 1.0
        assert r["toc_adaptive_overflow"] == 1.0

    def test_continuation_shifted(self, tmp_path):
        r = _metrics(tmp_path, build_toc_multiline, build_toc_adaptive_cont_shifted)
        assert r["toc_continuation_x_accuracy"] < 1.0
        assert r["toc_page_column_stability"] == 1.0

    def test_wrapped_word_deleted(self, tmp_path):
        r = _metrics(
            tmp_path,
            build_toc_adaptive_wrap_output,
            build_toc_adaptive_wrap_missing_word,
        )
        assert r["toc_adaptive_wrap_integrity"] < 1.0
        # geometry unaffected — only the same-language text loss is flagged
        assert r["toc_page_column_stability"] == 1.0
        assert r["toc_adaptive_font_size"] == 1.0

    def test_shrink_undone(self, tmp_path):
        r = _metrics(
            tmp_path, build_toc_adaptive_short, build_toc_adaptive_shrink_undone
        )
        assert r["toc_adaptive_font_size"] < 1.0
        assert r["toc_page_column_stability"] == 1.0

    def test_extreme_clip(self, tmp_path):
        r = _metrics(
            tmp_path, build_toc_adaptive_extreme_output, build_toc_adaptive_extreme_clip
        )
        assert r["toc_adaptive_overflow"] < 1.0
        # the page column is not what broke
        assert r["toc_page_column_stability"] == 1.0
