"""Commit 7F-5b — TOC multi-line height / extra-line budget semantics.

Locks ``pdf2zh.semantic.layout.toc_layout`` height behavior:

1. 1-line title → ``line_count == 1``, ``total_height == line_height``,
   no recovery (NO_ACTION).
2. Long title within the extra-line budget → WRAP into multiple lines,
   ``overflow=False``, ``recovery.steps == ["WRAP"]``.
3. Original 2-line entry (title + 1 continuation) + 2 extra lines → 4 lines
   total, still no overflow (``original_lines + max_extra_lines``, NOT a hard
   cap of 2).
4. Extra-line budget exhausted → SHRINK; if still over → explicit overflow,
   ``decision == "preserve_overflow"``, never silent.
5. SHRINK reduces line count (font drops, lines fit the budget).
6. SHRINK still insufficient → PRESERVE_OVERFLOW (overflow stays True).
7. ``continuation_x`` verbatim from the entry (or established fallback), never
   derived from ``level`` / ``index``.
8. ``page_x`` preserved even when the title grows / wraps.
9. page number emitted **exactly once** (entry-level), never per wrapped line.
10. leader shrinks when the title grows; ``page_x`` unchanged.
11. no-leader stays no-leader (dots never forced).
12. CJK long title wraps correctly via the unified measurer.

Geometry contract (7E-3 + 7F-5b): ``title_x`` / ``page_x`` / ``bbox`` /
``continuation_x`` come verbatim from the entry; nothing is re-derived from
``level`` / entry ``index``.
"""

import unittest

from pdf2zh.semantic.layout.overflow import OverflowPolicy
from pdf2zh.semantic.layout.recovery import LayoutBudget
from pdf2zh.semantic.layout.toc_layout import layout_toc_entry, toc_layout_commands

_SIZE = 10.0  # latin 5pt, CJK 10pt, dot 3pt


def _measure(text, size=_SIZE):
    w = 0.0
    for ch in text or "":
        if ch == ".":
            w += size * 0.3
        elif ord(ch) >= 0x2E80:
            w += size * 1.0
        else:
            w += size * 0.5
    return w


def _entry(
    number="",
    title_only="Introduction",
    level=0,
    page_number="1",
    title_x=72.0,
    page_x=540.0,
    indent=72.0,
    dot_leader="...................",
    leader_present=True,
    continuation=None,
    continuation_x=None,
    bbox=None,
):
    d = {
        "title": (f"{number} {title_only}").strip(),
        "number": number,
        "title_only": title_only,
        "level": level,
        "page_number": page_number,
        "title_x": title_x,
        "page_x": page_x,
        "indent": indent,
        "dot_leader": dot_leader,
        "leader_present": leader_present,
        "continuation": list(continuation or []),
        "bbox": list(bbox or (title_x, 0.0, page_x, 16.0)),
    }
    if continuation_x is not None:
        d["continuation_x"] = float(continuation_x)
    return d


def _layout(entry, translated=None, **kw):
    return layout_toc_entry(
        entry, measure=_measure, size=10.0, translated_title=translated, **kw
    )


# ── 1. 1-line title ──────────────────────────────────────────────────────


class TestSingleLineTitle(unittest.TestCase):
    def test_one_line_no_recovery(self):
        r = _layout(_entry(title_only="Introduction", page_x=500.0),
                    translated="译_Introduction")
        self.assertEqual(r.line_count, 1)
        self.assertEqual(r.total_height, 14.0)  # 1 * line_height
        self.assertEqual(r.original_lines, 1)
        self.assertFalse(r.overflow)
        self.assertIsNone(r.recovery)  # NO_ACTION — nothing to recover


# ── 2. WRAP within the extra-line budget ─────────────────────────────────


class TestWrapWithinBudget(unittest.TestCase):
    def test_long_title_wraps_not_overflow(self):
        r = _layout(_entry(title_only="A", page_x=300.0),
                    translated="A much longer translated title that wraps over lines")
        self.assertGreaterEqual(r.line_count, 2)
        self.assertFalse(r.overflow)
        self.assertIsNotNone(r.recovery)
        self.assertEqual(r.recovery["decision"], "wrap")
        self.assertIn("WRAP", r.recovery["steps"])
        # wrapped lines step DOWN in v3 y-up
        ys = [c["y"] for c in toc_layout_commands(r) if c["kind"] == "title"]
        self.assertEqual(ys, sorted(ys, reverse=True))

    def test_every_wrapped_line_emitted_as_command(self):
        r = _layout(_entry(title_only="A", page_x=300.0),
                    translated="A much longer translated title that wraps over lines")
        title_cmds = [c for c in toc_layout_commands(r) if c["kind"] == "title"]
        self.assertEqual(len(title_cmds), r.line_count)
        # all title lines keep the title_x anchor column
        xs = {c["x"] for c in title_cmds}
        self.assertEqual(xs, {round(r.title.bbox[0], 2)})


# ── 3. original_lines + max_extra_lines (NOT a hard cap of 2) ────────────


class TestOriginalPlusExtraBudget(unittest.TestCase):
    def test_original_two_lines_plus_two_extra_allowed(self):
        """original 2-line entry (title + 1 continuation) + max_extra_lines=2
        → up to 4 lines total; a 3-line wrapped title + 1 continuation = 4
        lines stays legal (no overflow)."""
        r = _layout(
            _entry(title_only="A", page_x=300.0, continuation=["cont line"]),
            translated=("word " * 25).strip(),
            translated_continuation=["cont line"],
        )
        self.assertEqual(r.original_lines, 2)  # title + continuation
        self.assertEqual(r.max_extra_lines, 2)
        # 3 title lines + 1 continuation = 4 = original(2) + extra(2)
        self.assertEqual(r.line_count, 4)
        self.assertFalse(r.overflow)

    def test_budget_is_original_plus_extra_not_absolute(self):
        """A 3-line original (title + 2 continuations) may grow to 5 lines."""
        r = _layout(
            _entry(title_only="A", page_x=300.0,
                   continuation=["c1", "c2"]),
            translated=("word " * 25).strip(),
            translated_continuation=["c1", "c2"],
        )
        self.assertEqual(r.original_lines, 3)
        self.assertEqual(r.line_count, 5)  # 3 + 2 = original(3) + extra(2)
        self.assertFalse(r.overflow)


# ── 4/5/6. budget exhausted → SHRINK → PRESERVE_OVERFLOW ─────────────────


class TestBudgetExhaustedRecovery(unittest.TestCase):
    def test_shrink_reduces_line_count(self):
        """SHRINK lowers the font so the wrapped title fits the line budget."""
        r = _layout(
            _entry(title_only="A", page_x=300.0),
            translated=("word " * 30).strip(),
        )
        # without shrink this would exceed 1+2=3 lines; SHRINK engaged
        self.assertIn("SHRINK", r.recovery["steps"])
        self.assertEqual(r.recovery["decision"], "shrink")
        self.assertFalse(r.overflow)
        self.assertLess(r.title.font_size, 10.0)
        self.assertLessEqual(r.line_count, 3)
        self.assertEqual(r.recovery["final_font_size"], round(r.title.font_size, 2))

    def test_shrink_insufficient_preserve_overflow(self):
        """Even after SHRINK to the floor the budget is exceeded → explicit
        PRESERVE_OVERFLOW, never silent, page_x never moves."""
        r = _layout(
            _entry(title_only="A", page_x=100.0),
            translated=("word " * 120).strip(),
        )
        self.assertTrue(r.overflow)
        self.assertEqual(r.recovery["decision"], "preserve_overflow")
        self.assertEqual(r.recovery["steps"][0], "WRAP")
        self.assertIn("SHRINK", r.recovery["steps"])
        # page column preserved
        self.assertEqual(r.page.bbox[0], 100.0)
        self.assertIsNone(r.leader)

    def test_no_shrink_budget_preserve_directly(self):
        b = LayoutBudget(allow_wrap=True, allow_shrink=False, allow_clip=False,
                         max_extra_lines=2)
        r = layout_toc_entry(
            _entry(title_only="A", page_x=100.0),
            measure=_measure, size=10.0,
            translated_title=("word " * 120).strip(),
            budget=b,
        )
        self.assertTrue(r.overflow)
        self.assertEqual(r.recovery["decision"], "preserve_overflow")
        self.assertNotIn("SHRINK", r.recovery["steps"])


# ── 7. continuation_x verbatim, never f(level) ───────────────────────────


class TestContinuationGeometry(unittest.TestCase):
    def test_continuation_x_verbatim_from_entry(self):
        r = _layout(
            _entry(title_only="Title", page_x=500.0, continuation=["cont"],
                   continuation_x=120.0),
            translated="译_Title", translated_continuation=["cont"],
        )
        self.assertEqual(r.continuation_x, 120.0)
        self.assertEqual(r.continuation[0].bbox[0], 120.0)

    def test_continuation_x_fallback_is_title_x_plus_size(self):
        r = _layout(
            _entry(title_only="Title", page_x=500.0, title_x=72.0,
                   continuation=["cont"]),
            translated="译_Title", translated_continuation=["cont"],
        )
        # established fallback: title_x + size (not level-derived)
        self.assertEqual(r.continuation_x, 72.0 + 10.0)

    def test_continuation_x_not_function_of_level(self):
        """Same level with different entry continuation_x → different x."""
        r1 = _layout(_entry(title_only="A", page_x=500.0, level=2,
                            continuation=["c"], continuation_x=100.0),
                     translated="A", translated_continuation=["c"])
        r2 = _layout(_entry(title_only="B", page_x=500.0, level=2,
                            continuation=["c"], continuation_x=140.0),
                     translated="B", translated_continuation=["c"])
        self.assertNotEqual(r1.continuation_x, r2.continuation_x)
        # and the fallback is independent of level too
        r3 = _layout(_entry(title_only="C", page_x=500.0, level=2,
                            continuation=["c"]),
                     translated="C", translated_continuation=["c"])
        r4 = _layout(_entry(title_only="D", page_x=500.0, level=3,
                            continuation=["c"]),
                     translated="D", translated_continuation=["c"])
        self.assertEqual(r3.continuation_x, r4.continuation_x)


# ── 8/9. page_x preserved, page number emitted once ──────────────────────


class TestPageChannel(unittest.TestCase):
    def test_page_x_never_moves_when_title_wraps(self):
        r = _layout(_entry(title_only="A", page_x=500.0, page_number="12"),
                    translated=("A very long translated title that wraps " * 3).strip())
        self.assertGreaterEqual(r.line_count, 2)
        self.assertEqual(r.page.bbox[0], 500.0)
        page_cmd = [c for c in toc_layout_commands(r) if c["kind"] == "page"]
        self.assertEqual(page_cmd[0]["x"], 500.0)

    def test_page_number_emitted_exactly_once(self):
        """Multi-line title still yields exactly ONE page command."""
        r = _layout(_entry(title_only="A", page_x=500.0, page_number="12"),
                    translated=("A very long translated title that wraps " * 3).strip())
        page_cmds = [c for c in toc_layout_commands(r) if c["kind"] == "page"]
        self.assertEqual(len(page_cmds), 1)
        self.assertEqual(page_cmds[0]["text"], "12")

    def test_page_number_never_duplicated_per_wrapped_line(self):
        cmds = toc_layout_commands(
            _layout(_entry(title_only="A", page_x=500.0, page_number="12"),
                    translated=("A very long translated title that wraps " * 3).strip())
        )
        texts = [c["text"] for c in cmds if c["kind"] == "page"]
        self.assertEqual(texts, ["12"])


# ── 10. leader shrinks, page_x unchanged ─────────────────────────────────


class TestLeaderBehavior(unittest.TestCase):
    def test_leader_shrinks_but_page_x_unchanged(self):
        short = _layout(_entry(page_x=500.0), translated="Intro")
        long = _layout(_entry(page_x=500.0),
                       translated="A much longer translated title here")
        s_len = len(short.leader.lines[0]) if short.leader else 0
        l_len = len(long.leader.lines[0]) if long.leader else 0
        self.assertLess(l_len, s_len)
        self.assertEqual(short.page.bbox[0], long.page.bbox[0])

    def test_no_leader_never_forces_dots(self):
        r = _layout(_entry(leader_present=False, dot_leader="", page_number="5"),
                    translated=("A long translated title " * 5).strip())
        self.assertIsNone(r.leader)
        self.assertFalse([c for c in toc_layout_commands(r) if c["kind"] == "leader"])


# ── 12. CJK multi-line ───────────────────────────────────────────────────


class TestCjkMultiline(unittest.TestCase):
    def test_cjk_title_wraps_correctly(self):
        r = _layout(_entry(title_only="A", page_x=200.0, leader_present=False),
                    translated="这是一个非常非常长的中文标题它需要换行成多行显示")
        self.assertGreaterEqual(r.line_count, 2)
        # measured by the unified measurer (CJK 1em), not char-count heuristic
        for c in toc_layout_commands(r):
            if c["kind"] == "title":
                self.assertGreater(c["width"], 0.0)
        self.assertFalse(r.overflow)


# ── misc: policy + JSON-safety ───────────────────────────────────────────


class TestMisc(unittest.TestCase):
    def test_title_policy_is_wrap_under_7f5b(self):
        r = _layout(_entry(title_only="A", page_x=300.0),
                    translated="A much longer translated title that wraps over lines")
        self.assertIs(r.title.policy, OverflowPolicy.WRAP)

    def test_result_json_safe(self):
        import json
        r = _layout(_entry(title_only="A", page_x=300.0, page_number="12"),
                    translated="A much longer translated title that wraps over lines")
        json.dumps(r.to_dict())
        json.dumps(toc_layout_commands(r))
        d = r.to_dict()
        self.assertEqual(d["line_count"], r.line_count)
        self.assertEqual(d["total_height"], r.total_height)
        self.assertEqual(d["page_x"], 300.0)


if __name__ == "__main__":
    import sys

    import pytest
    sys.exit(pytest.main([__file__]))
