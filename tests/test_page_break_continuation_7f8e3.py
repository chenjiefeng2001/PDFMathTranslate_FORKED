# -*- coding: utf-8 -*-
"""Commit 7F-8e-3 — List / TOC Continuation Break.

Where 8e-2 moved a block whole, 8e-3 splits a list / TOC block at its
*settled line boundary* — the fitted lines stay, the overflow tail continues
on the next page:::

    before overflow = 1
        ↓ split continuation at the page bottom edge
    kept (page 0, marker once)  +  cont (page 1, re-anchored at next_page_start_y)
    after overflow = 0

Locked guarantees (the 8e-3 DoD):

1. **list 2+ lines split correctly** — tail lands on the next page, re-anchored
   by page + Y only; no dropped line, no duplicated marker;
2. **marker exactly once** — never regenerated on the continuation page;
3. **TOC title continuation** — wrapped tail continues, page number drawn
   exactly once, ``page_x`` byte-identical;
4. **X / source 100% immutable** — every X (marker_x / content_x /
   continuation_x / title_x / page_x), width, font, text and ``src_box``
   verbatim on BOTH splits;
5. **overflow 1 → 0** — re-running 8b shows the page overflow cleared and
   ``source_collision_count`` unchanged;
6. **Code / preserved region → PRESERVE_OVERFLOW** — never split, never moved;
7. **an already-settled (post-split) kept block is not re-split** — no
   duplicated continuation;
8. **bounded budget** — ``max_splits`` stops immediately;
9. **pure geometry** — splits only already-settled commands, never calls
   adaptive / wrap / shrink / clip, never mutates the source plan.
"""

import ast
import copy
import json
import unittest
from pathlib import Path

from pdf2zh.semantic.layout.page_break import (
    PageBreakDecision,
    break_invariants,
)
from pdf2zh.semantic.layout.page_break_continuation import (
    ContinuationBreakReport,
    execute_continuation_breaks,
    split_continuation_break,
)
from pdf2zh.semantic.layout.page_flow import (
    build_page_flow_report,
    placements_from_plan,
)

_HERE = Path(__file__).resolve().parent
_MOD_PATH = (
    _HERE.parent / "pdf2zh" / "semantic" / "layout" / "page_break_continuation.py"
)


def _entry(
    block_id,
    page,
    kind,
    x0,
    y0,
    x1,
    y1,
    payload=None,
    list_items=None,
    toc_entries=None,
    toc_commands=None,
):
    box = [float(x0), float(y0), float(x1), float(y1)]
    return {
        "block_id": block_id,
        "page": page,
        "kind": kind,
        "text": "t",
        "translated": "t",
        "src_box": list(box),
        "dst_box": list(box),
        "font_size": 11.0,
        "render_payload": (
            payload if payload is not None else {"kind": kind, "commands": []}
        ),
        "list_items": list_items,
        "toc_entries": toc_entries,
        "toc_commands": toc_commands,
    }


def _list_block(block_id, page=0):
    """A list whose settled command run dips below the page bottom (y=-20)."""
    commands = [
        {"kind": "marker", "text": "1.", "x": 60.0, "y": 40.0, "width": 11.0},
        {"kind": "text", "text": "A", "x": 76.0, "y": 40.0, "width": 40.0},
        {"kind": "text", "text": "B", "x": 76.0, "y": 28.0, "width": 40.0},
        {"kind": "text", "text": "C", "x": 76.0, "y": 16.0, "width": 40.0},
        {"kind": "text", "text": "D", "x": 76.0, "y": 4.0, "width": 40.0},
        {"kind": "text", "text": "E", "x": 76.0, "y": -8.0, "width": 40.0},
        {"kind": "text", "text": "F", "x": 76.0, "y": -20.0, "width": 40.0},
    ]
    items = [
        {
            "marker": "1.",
            "marker_x": 60.0,
            "content_x": 76.0,
            "continuation_x": 76.0,
            "continuation": ["B", "C", "D", "E", "F"],
        }
    ]
    return _entry(
        block_id,
        page,
        "list",
        60.0,
        -30.0,
        260.0,
        50.0,
        list_items={"commands": commands, "items": items},
        payload={"kind": "list", "commands": commands},
    )


def _toc_block(block_id, page=0):
    """A TOC whose wrapped-title tail dips below the page bottom."""
    commands = [
        {"kind": "number", "text": "1", "x": 72.0, "y": 40.0, "width": 8.0},
        {"kind": "title", "text": "Intro", "x": 82.0, "y": 40.0, "width": 60.0},
        {"kind": "title", "text": "wrap1", "x": 82.0, "y": 28.0, "width": 60.0},
        {"kind": "title", "text": "wrap2", "x": 82.0, "y": 16.0, "width": 60.0},
        {"kind": "leader", "text": "...", "x": 150.0, "y": 40.0, "width": 320.0},
        {"kind": "page", "text": "42", "x": 500.0, "y": 40.0, "width": 20.0},
        {"kind": "title", "text": "cont1", "x": 82.0, "y": 4.0, "width": 60.0},
        {"kind": "title", "text": "cont2", "x": 82.0, "y": -8.0, "width": 60.0},
        {"kind": "title", "text": "cont3", "x": 82.0, "y": -20.0, "width": 60.0},
    ]
    entries = [
        {
            "title": "Intro",
            "title_x": 82.0,
            "page_x": 500.0,
            "page_number": "42",
            "continuation": [],
        }
    ]
    return _entry(
        block_id,
        page,
        "toc",
        60.0,
        -30.0,
        260.0,
        50.0,
        toc_entries=entries,
        toc_commands={"commands": commands},
        payload={"kind": "toc", "commands": commands},
    )


def _code(block_id, page=0):
    box = [60.0, -30.0, 260.0, 50.0]
    return _entry(
        block_id,
        page,
        "code",
        60.0,
        -30.0,
        260.0,
        50.0,
        payload={"kind": "code", "commands": []},
    )


def _flow(block_id, page=0, x0=60.0, y0=-20.0, x1=260.0, y1=50.0):
    return _entry(
        block_id, page, "flow", x0, y0, x1, y1, payload={"kind": "flow", "commands": []}
    )


# 7G-2.1 P0: a break may only land on a page that exists (max page_sizes key).
# The mixed corpus needs THREE target pages (1, 2, 3) — the document must
# declare page 3 or the third break is correctly deferred as out-of-document.
_PAGES = {0: 792.0, 1: 792.0, 2: 792.0, 3: 792.0}
_PAGE_START = 792.0
_BOTTOM = 10.0  # fitted margin: lines below y=10 are the continuation tail


def _overflow(plan, pages=_PAGES):
    return build_page_flow_report(plan, page_sizes=pages).summary()[
        "page_overflow_count"
    ]


def _marker_count(plan):
    return sum(
        1
        for e in plan
        for c in (e.get("render_payload") or {}).get("commands") or []
        if c.get("kind") == "marker"
    )


def _page_cmd_count(plan):
    return sum(
        1
        for e in plan
        for c in (e.get("render_payload") or {}).get("commands") or []
        if c.get("kind") == "page"
    )


def _src_box_list(plan):
    return [e["src_box"] for e in plan]


# ---------------------------------------------------------------------------
# 1. list — 2+ lines, tail lands next page, no dropped line, marker once
# ---------------------------------------------------------------------------


class TestListContinuation(unittest.TestCase):
    def _exec(self, plan=None):
        plan = plan if plan is not None else [_list_block("p0_0")]
        return execute_continuation_breaks(
            plan, page_sizes=_PAGES, page_start_y=_PAGE_START, page_bottom_y=_BOTTOM
        )

    def test_list_two_plus_lines_split(self):
        self.assertEqual(_overflow([_list_block("p0_0")]), 1)  # before 1
        new_plan, report = self._exec()
        # 2 entries: kept (page 0) + cont (page 1)
        kept = [e for e in new_plan if e["page"] == 0][0]
        cont = [e for e in new_plan if e["page"] == 1][0]
        self.assertEqual(len(report.applied), 1)
        self.assertEqual(report.applied[0].mode, "split")
        self.assertEqual(report.applied[0].fitted_lines, 4)  # marker+A+B+C
        self.assertEqual(report.applied[0].moved_lines, 3)  # D+E+F
        # no dropped line: text union identical
        origin = [
            c["text"] for c in (_list_block("p0_0")["render_payload"]["commands"])
        ]
        after = [
            c["text"]
            for c in kept["render_payload"]["commands"]
            + cont["render_payload"]["commands"]
        ]
        self.assertEqual(sorted(after), sorted(origin))
        # cont top re-anchored at page start; marker stays once
        self.assertEqual(_marker_count(new_plan), 1)
        cont_ys = [c["y"] for c in cont["render_payload"]["commands"]]
        self.assertEqual(cont_ys, [792.0, 780.0, 768.0])
        self.assertEqual(_overflow(new_plan), 0)  # after 0
        self.assertEqual(report.applied[0].target_page, 1)

    def test_list_x_and_source_immutable(self):
        plan = [_list_block("p0_0")]
        src_before = copy.deepcopy(plan)[0]["src_box"]
        new_plan, _ = self._exec()
        for e in new_plan:
            self.assertEqual(e["src_box"], src_before)
            self.assertEqual(
                break_invariants(e),
                {"marker_x": 60.0, "content_x": 76.0, "continuation_x": 76.0},
            )
            for c in e["render_payload"]["commands"]:
                self.assertIn(c["x"], (60.0, 76.0))
        # the whole command run's x / width unchanged (only y moved)
        kept = [e for e in new_plan if e["page"] == 0][0]
        cont = [e for e in new_plan if e["page"] == 1][0]
        for e in (kept, cont):
            for c in e["render_payload"]["commands"]:
                self.assertIn(c["width"], (11.0, 40.0))

    def test_list_marker_not_duplicated_on_continuation(self):
        new_plan, _ = self._exec()
        cont = [e for e in new_plan if e["page"] == 1][0]
        kinds = [c["kind"] for c in cont["render_payload"]["commands"]]
        self.assertNotIn("marker", kinds)

    def test_empty_and_all_overflow_degrade_to_whole_block(self):
        # a block whose ENTIRE run is below the fold can't split — it must not
        # be silently mis-handled. Build one and pass decisions; the tail is all
        # overflow → split returns None → whole-block move on a fresh plan.
        low = _entry(
            "p0_0",
            0,
            "list",
            60.0,
            -60.0,
            260.0,
            -30.0,
            list_items={
                "commands": [
                    {
                        "kind": "marker",
                        "text": "1.",
                        "x": 60.0,
                        "y": -20.0,
                        "width": 11.0,
                    },
                    {"kind": "text", "text": "a", "x": 76.0, "y": -30.0, "width": 40.0},
                ],
                "items": [],
            },
            payload={"kind": "list", "commands": []},
        )
        new_plan, report = execute_continuation_breaks(
            [low], page_sizes=_PAGES, page_start_y=_PAGE_START, page_bottom_y=_BOTTOM
        )
        self.assertEqual(report.applied[0].mode, "whole_block")
        self.assertEqual(new_plan[0]["page"], 1)


# ---------------------------------------------------------------------------
# 2. continuation already settled — never re-split / never duplicate
# ---------------------------------------------------------------------------


class TestNoResplit(unittest.TestCase):
    def test_kept_block_is_not_resplit(self):
        # run once → take the page-0 kept entry → re-decide → it fits (KEEP)
        first_plan, _ = execute_continuation_breaks(
            [_list_block("p0_0")],
            page_sizes=_PAGES,
            page_start_y=_PAGE_START,
            page_bottom_y=_BOTTOM,
        )
        kept = [e for e in first_plan if e["page"] == 0][0]
        new_plan, report = execute_continuation_breaks(
            [kept], page_sizes=_PAGES, page_start_y=_PAGE_START, page_bottom_y=_BOTTOM
        )
        self.assertEqual(report.applied, [])
        self.assertEqual(_marker_count(new_plan), 1)
        # same command count — no duplicated continuation
        self.assertEqual(
            len(new_plan[0]["render_payload"]["commands"]),
            len(kept["render_payload"]["commands"]),
        )


# ---------------------------------------------------------------------------
# 3. TOC — title continuation, page number once, page_x verbatim
# ---------------------------------------------------------------------------


class TestTocContinuation(unittest.TestCase):
    def _exec(self):
        return execute_continuation_breaks(
            [_toc_block("p0_0")],
            page_sizes=_PAGES,
            page_start_y=_PAGE_START,
            page_bottom_y=_BOTTOM,
        )

    def test_title_continuation_splits(self):
        self.assertEqual(_overflow([_toc_block("p0_0")]), 1)
        new_plan, report = self._exec()
        cont = [e for e in new_plan if e["page"] == 1][0]
        self.assertEqual(report.applied[0].mode, "split")
        self.assertEqual(
            [c["text"] for c in cont["render_payload"]["commands"]],
            ["cont1", "cont2", "cont3"],
        )
        self.assertEqual(_overflow(new_plan), 0)

    def test_page_number_once_and_page_x_verbatim(self):
        new_plan, _ = self._exec()
        self.assertEqual(_page_cmd_count(new_plan), 1)
        for e in new_plan:
            inv = break_invariants(e)
            self.assertEqual(inv.get("page_x"), 500.0)
            self.assertEqual(inv.get("title_x"), 82.0)
        page_cmd = [
            c
            for e in new_plan
            for c in e["render_payload"]["commands"]
            if c.get("kind") == "page"
        ][0]
        self.assertEqual(page_cmd["x"], 500.0)


# ---------------------------------------------------------------------------
# 4. code / preserved — never break, never split
# ---------------------------------------------------------------------------


class TestPreserve(unittest.TestCase):
    def test_code_overflow_preserved(self):
        plan = [_code("p0_0"), _flow("p0_1")]
        new_plan, report = execute_continuation_breaks(
            plan, page_sizes=_PAGES, page_start_y=_PAGE_START, page_bottom_y=_BOTTOM
        )
        code = [e for e in new_plan if e["kind"] == "code"][0]
        self.assertEqual(code["page"], 0)
        self.assertEqual(code["dst_box"], _code("p0_0")["dst_box"])
        self.assertEqual(report.deferred[0].mode, "preserve")

    def test_break_decision_on_preserved_is_preserve(self):
        plan = [_code("p0_0")]
        decisions = [
            (p, PageBreakDecision.BREAK_TO_NEXT_PAGE)
            for p in placements_from_plan(plan)
        ]
        new_plan, report = execute_continuation_breaks(
            plan,
            page_sizes=_PAGES,
            decisions=decisions,
            page_start_y=_PAGE_START,
            page_bottom_y=_BOTTOM,
        )
        self.assertEqual(report.applied, [])
        self.assertEqual(report.deferred[0].mode, "preserve")
        self.assertEqual(new_plan[0]["dst_box"], plan[0]["dst_box"])


# ---------------------------------------------------------------------------
# 5. A→B→C — monotonic bounded page chain
# ---------------------------------------------------------------------------


class TestPageChain(unittest.TestCase):
    def test_three_splits_land_on_distinct_pages(self):
        plan = [
            _flow("p0_keep", 0, 60.0, 700.0, 260.0, 720.0),
            _list_block("p0_1"),
            _list_block("p0_2"),
        ]
        new_plan, report = execute_continuation_breaks(
            plan, page_sizes=_PAGES, page_start_y=_PAGE_START, page_bottom_y=_BOTTOM
        )
        kept_pages = sorted(e["page"] for e in new_plan if e["page"] == 0)
        cont_pages = sorted(e["page"] for e in new_plan if e["page"] != 0)
        self.assertEqual(report.applied[0].target_page, 1)
        self.assertEqual(report.applied[1].target_page, 2)
        self.assertEqual(cont_pages, [1, 2])
        self.assertEqual(len(kept_pages), 3)  # keep + 2 fitted parts


# ---------------------------------------------------------------------------
# 6. budget — max_splits stops immediately
# ---------------------------------------------------------------------------


class TestBudget(unittest.TestCase):
    def test_budget_zero_touches_nothing(self):
        new_plan, report = execute_continuation_breaks(
            [_list_block("p0_0")],
            page_sizes=_PAGES,
            max_splits=0,
            page_start_y=_PAGE_START,
            page_bottom_y=_BOTTOM,
        )
        self.assertEqual(report.applied, [])
        self.assertTrue(report.stopped_early)
        self.assertEqual(len(report.unresolved), 1)
        self.assertEqual(new_plan[0]["page"], 0)

    def test_budget_less_than_overflowing(self):
        plan = [
            _flow("p0_keep", 0, 60.0, 700.0, 260.0, 720.0),
            _list_block("p0_1"),
            _list_block("p0_2"),
        ]
        new_plan, report = execute_continuation_breaks(
            plan,
            page_sizes=_PAGES,
            max_splits=1,
            page_start_y=_PAGE_START,
            page_bottom_y=_BOTTOM,
        )
        self.assertEqual(len(report.applied), 1)
        self.assertEqual(len(report.deferred), 1)
        self.assertTrue(report.stopped_early)


# ---------------------------------------------------------------------------
# 7. source geometry invariant — overflow 1→0, source_collision_count same
# ---------------------------------------------------------------------------


class TestSourceInvariant(unittest.TestCase):
    def test_overflow_clears_and_source_collisions_unchanged(self):
        plan = [_list_block("p0_0"), _flow("p0_1", 0, 60.0, 700.0, 260.0, 720.0)]
        before = build_page_flow_report(plan, page_sizes=_PAGES)
        src_before = before.source_collision_count
        new_plan, _ = execute_continuation_breaks(
            plan, page_sizes=_PAGES, page_start_y=_PAGE_START, page_bottom_y=_BOTTOM
        )
        after = build_page_flow_report(new_plan, page_sizes=_PAGES)
        self.assertEqual(after.source_collision_count, src_before)
        self.assertEqual(after.summary()["page_overflow_count"], 0)
        # every src_box is an original value (verbatim; cont duplicates the list
        # source box, never invents one)
        origin_srcs = {tuple(e["src_box"]) for e in plan}
        self.assertTrue({tuple(e["src_box"]) for e in new_plan} <= origin_srcs)
        self.assertIn(
            tuple(_flow("p0_1", 0, 60.0, 700.0, 260.0, 720.0)["src_box"]), origin_srcs
        )


# ---------------------------------------------------------------------------
# 8. mixed PDF — TOC + Flow + List + Code in one run
# ---------------------------------------------------------------------------


class TestMixedPdf(unittest.TestCase):
    def test_all_kinds_once(self):
        plan = [
            _toc_block("p0_toc"),
            _flow("p0_flow"),
            _list_block("p0_list"),
            _code("p0_code"),
        ]
        new_plan, report = execute_continuation_breaks(
            plan, page_sizes=_PAGES, page_start_y=_PAGE_START, page_bottom_y=_BOTTOM
        )
        # three breakable blocks recovered (toc/flow/list); code preserved
        self.assertEqual(len(report.applied), 3)
        self.assertEqual(report.deferred[0].mode, "preserve")
        self.assertIsInstance(report, ContinuationBreakReport)
        # marker once, page number once, code untouched
        self.assertEqual(_marker_count(new_plan), 1)
        self.assertEqual(_page_cmd_count(new_plan), 1)
        code = [e for e in new_plan if e["kind"] == "code"][0]
        self.assertEqual(code["page"], 0)
        # json round-trip of the report
        json.dumps(report.to_dict())


# ---------------------------------------------------------------------------
# 9. isolated splitter units + architecture purity
# ---------------------------------------------------------------------------


class TestSplitUnit(unittest.TestCase):
    def test_split_returns_kept_and_cont(self):
        res = split_continuation_break(
            _list_block("p0_0"),
            page_bottom_y=_BOTTOM,
            page_start_y=_PAGE_START,
            target_page=1,
        )
        self.assertIsNotNone(res)
        kept, cont, info = res
        self.assertEqual(kept["page"], 0)
        self.assertEqual(cont["page"], 1)
        self.assertEqual(info["fitted_lines"], 4)
        self.assertEqual(info["moved_lines"], 3)
        # only y changed: src_box identical on both
        self.assertEqual(kept["src_box"], cont["src_box"])
        self.assertEqual(kept["src_box"], _list_block("p0_0")["src_box"])

    def test_split_none_when_nothing_overflows(self):
        fitting = _entry(
            "p0_0",
            0,
            "list",
            60.0,
            100.0,
            260.0,
            200.0,
            payload={
                "kind": "list",
                "commands": [
                    {
                        "kind": "marker",
                        "text": "1.",
                        "x": 60.0,
                        "y": 180.0,
                        "width": 11.0,
                    },
                    {"kind": "text", "text": "a", "x": 76.0, "y": 168.0, "width": 40.0},
                ],
            },
        )
        self.assertIsNone(
            split_continuation_break(
                fitting, page_bottom_y=_BOTTOM, page_start_y=_PAGE_START
            )
        )

    def test_report_shape(self):
        _, report = execute_continuation_breaks(
            [_list_block("p0_0")],
            page_sizes=_PAGES,
            page_start_y=_PAGE_START,
            page_bottom_y=_BOTTOM,
        )
        d = report.to_dict()
        self.assertEqual(
            set(d),
            {
                "passes",
                "max_splits",
                "stopped_early",
                "applied_count",
                "deferred_count",
                "unresolved_count",
                "applied",
                "deferred",
                "unresolved",
            },
        )


def _read_module(path: Path) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"))

    def _clean(body):
        return [
            n
            for n in body
            if not (
                isinstance(n, ast.Expr)
                and isinstance(n.value, ast.Constant)
                and isinstance(n.value.value, str)
            )
        ]

    tree.body = _clean(tree.body)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            node.body = _clean(node.body)
    ast.fix_missing_locations(tree)
    return ast.unparse(tree)


class TestContinuationArchitecture(unittest.TestCase):
    def test_never_redecides_or_relays_out(self):
        src = _read_module(_MOD_PATH)
        for banned in (
            "lay_out(",
            "adaptive_layout(",
            "wrap_lines(",
            "shrink_to_fit(",
            "clip_text(",
            "decide_page_recovery(",
            "build_page_flow_report(",
            "detect_page_collisions(",
            "detect_page_overflows(",
            "resolve_page_shifts(",
            "apply_page_shifts(",
            "toc_parser",
            "list_parser",
            "code_detector",
            "semantic.renderer",
            "translator",
            "magicpdf",
        ):
            self.assertNotIn(banned, src, f"page_break_continuation.py 不得:{banned}")

    def test_uses_the_landing_contract(self):
        src = _read_module(_MOD_PATH)
        self.assertIn("next_free_page", src)
        self.assertIn("next_page_start_y", src)
        self.assertIn("break_placement_to_page", src)

    def test_no_second_layout_engine_entry(self):
        for probe in ("def wrap", "def shrink", "def clip", "def adaptive"):
            self.assertNotIn(probe, _read_module(_MOD_PATH))


if __name__ == "__main__":
    unittest.main()
