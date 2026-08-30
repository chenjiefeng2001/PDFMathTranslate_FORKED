# -*- coding: utf-8 -*-
"""Commit 7F-8e-2 — Page Break Executor.

The first commit that actually changes cross-page ownership — it converts the
8e-1 :class:`PageBreakDecision` into geometry, and only ``BREAK_TO_NEXT_PAGE``
is executed:::

    before overflow = 1
        ↓ execute NEXT_PAGE
    after  overflow = 0        (page = N+1, top = next_page_start_y)

Locked guarantees (the 8e-2 DoD):

1. **single-flow NEXT_PAGE** — the whole block moves N → N+1;
2. **top lands at ``next_page_start_y``** — the overflow clears (1 → 0);
3. **X is 100% immutable** — every dst_box x, command x / width, list
   marker_x / content_x / continuation_x, toc title_x / page_x verbatim;
4. **source geometry 100% immutable** — ``src_box`` / ``BlockPlacement.bbox``
   stay forever; only ``page`` and ``dst_box.y`` / command ``page`` / ``y`` move;
5. **KEEP / PRESERVE_OVERFLOW untouched** — code never breaks, never moves;
6. **page chain is monotonic and bounded** — A→0, B→1, C→2; a block already on
   the last page correctly creates / claims the next page;
7. **budget** — ``max_page_breaks`` stops immediately; over-budget breaks are
   recorded deferred / unresolved, never silently applied;
8. **pure geometry** — consumes only 8e-1 decisions; never re-detects a
   collision, never re-lays-out, never mutates the source plan.

Plus the invariant: re-running 8b shows ``source_collision_count`` unchanged,
proving the executor moves only resolved geometry, never source geometry.
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
from pdf2zh.semantic.layout.page_break_executor import (
    PageBreakExecutionReport,
    execute_page_breaks,
    move_entry_to_page,
    shift_command_fields,
)
from pdf2zh.semantic.layout.page_flow import (
    build_page_flow_report,
    placements_from_plan,
)

_HERE = Path(__file__).resolve().parent
_EXEC_PATH = _HERE.parent / "pdf2zh" / "semantic" / "layout" / "page_break_executor.py"


def _entry(block_id, page, kind, x0, y0, x1, y1, payload=None,
           list_items=None, toc_entries=None, toc_commands=None, src_box=None):
    box = [float(x0), float(y0), float(x1), float(y1)]
    return {
        "block_id": block_id, "page": page, "kind": kind,
        "text": "t", "translated": "t",
        "src_box": list(src_box) if src_box is not None else list(box),
        "dst_box": list(box),
        "font_size": 11.0,
        "render_payload": payload if payload is not None
        else {"kind": kind, "commands": []},
        "list_items": list_items,
        "toc_entries": toc_entries,
        "toc_commands": toc_commands,
    }


def _flow(block_id, page, x0, y0, x1, y1):
    """Flow entry with one settled command line at its top (v3 y-up)."""
    payload = {
        "kind": "flow", "font_size": 10.0,
        "commands": [{"kind": "flow-text", "text": "t", "x": float(x0),
                      "y": float(y1), "width": 100.0, "line": 0,
                      "is_last": True, "overflow": False}],
    }
    return _entry(block_id, page, "flow", x0, y0, x1, y1, payload=payload)


def _flow_simple(block_id, page, x0, y0, x1, y1):
    """Flow entry with NO command geometry (resolved == dst_box)."""
    return _entry(block_id, page, "flow", x0, y0, x1, y1)


def _code_entry(block_id, page, x0, y0, x1, y1):
    return _entry(block_id, page, "code", x0, y0, x1, y1)


def _list(block_id, page, x0, y0, x1, y1):
    items = [{
        "marker": "1.", "marker_x": 60.0, "content_x": 76.0,
        "continuation_x": 76.0, "continuation": ["cont"],
    }]
    cmds = [
        {"kind": "marker", "text": "1.", "x": 60.0, "y": float(y1), "width": 11.0},
        {"kind": "text", "text": "c", "x": 76.0, "y": float(y1), "width": 40.0},
        {"kind": "text", "text": "cont", "x": 76.0, "y": float(y1) - 12.0,
         "width": 40.0},
    ]
    return _entry(block_id, page, "list", x0, y0, x1, y1,
                  list_items={"commands": cmds, "items": items},
                  payload={"kind": "list", "commands": cmds})


def _toc(block_id, page, x0, y0, x1, y1):
    entries = [{"title": "Intro", "title_x": 72.0, "page_x": 500.0,
                "page_number": "42", "continuation": []}]
    cmds = [
        {"kind": "title", "text": "Intro", "x": 72.0, "y": float(y1),
         "width": 100.0},
        {"kind": "page", "text": "42", "x": 500.0, "y": float(y1), "width": 20.0},
    ]
    return _entry(block_id, page, "toc", x0, y0, x1, y1,
                  toc_entries=entries, toc_commands={"commands": cmds},
                  payload={"kind": "toc", "commands": cmds})


def _overflow_counts(plan, page_sizes):
    return build_page_flow_report(plan, page_sizes=page_sizes).summary()[ 
        "page_overflow_count"
    ]


# ---------------------------------------------------------------------------
# 1. single flow NEXT_PAGE — page N → N+1, top lands at next_page_start_y
# ---------------------------------------------------------------------------


class TestSingleFlowBreak(unittest.TestCase):
    PAGES = {0: 792.0, 1: 792.0}
    PAGE_START = 792.0  # v3 y-up: content top edge == page height

    def test_block_below_page_bottom_breaks_to_next_page(self):
        plan = [_flow_simple("p0_0", 0, 60.0, -20.0, 260.0, 50.0)]
        self.assertEqual(_overflow_counts(plan, self.PAGES), 1)  # before 1
        new_plan, report = execute_page_breaks(
            plan, page_sizes=self.PAGES, page_start_y=self.PAGE_START)
        entry = new_plan[0]
        self.assertEqual(entry["page"], 1)              # N → N+1
        self.assertEqual(len(report.applied), 1)
        self.assertEqual(report.applied[0].source_page, 0)
        self.assertEqual(report.applied[0].target_page, 1)
        # top re-anchored at next_page_start_y; height preserved
        dst = entry["dst_box"]
        self.assertEqual(dst[3], self.PAGE_START)
        self.assertEqual(dst[3] - dst[1], 70.0)         # 50 - (-20)
        self.assertEqual(_overflow_counts(new_plan, self.PAGES), 0)  # after 0

    def test_decision_unpacked_from_8e1_pairs(self):
        plan = [_flow_simple("p0_0", 0, 60.0, -20.0, 260.0, 50.0)]
        p = placements_from_plan(plan)[0]
        decisions = [(p, PageBreakDecision.BREAK_TO_NEXT_PAGE)]
        new_plan, report = execute_page_breaks(
            plan, page_sizes=self.PAGES, decisions=decisions,
            page_start_y=self.PAGE_START)
        self.assertEqual(new_plan[0]["page"], 1)
        self.assertEqual(len(report.applied), 1)

    def test_block_with_command_geometry_moves_page_and_commands_y(self):
        plan = [_flow("p0_0", 0, 60.0, -20.0, 260.0, 50.0)]
        new_plan, report = execute_page_breaks(
            plan, page_sizes=self.PAGES, page_start_y=self.PAGE_START)
        new = new_plan[0]
        self.assertEqual(new["page"], 1)
        cmd = new["render_payload"]["commands"][0]
        old_cmd = plan[0]["render_payload"]["commands"][0]
        # y moved so the top lands at page_start_y; x / width verbatim
        self.assertEqual(cmd["x"], old_cmd["x"])
        self.assertEqual(cmd["width"], old_cmd["width"])
        self.assertEqual(cmd["y"], self.PAGE_START)  # top re-anchored
        self.assertEqual(len(report.applied), 1)

    def test_input_plan_never_mutated(self):
        plan = [_flow("p0_0", 0, 60.0, -20.0, 260.0, 50.0)]
        snapshot = copy.deepcopy(plan)
        new_plan, _ = execute_page_breaks(
            plan, page_sizes=self.PAGES, page_start_y=self.PAGE_START)
        self.assertIsNot(new_plan, plan)
        self.assertEqual(plan, snapshot)


# ---------------------------------------------------------------------------
# 2. X immutability — every x / width byte-identical, only page / y move
# ---------------------------------------------------------------------------


class TestXImmutability(unittest.TestCase):
    PAGES = {0: 792.0, 1: 792.0}
    PAGE_START = 792.0

    def _x_snapshot(self, plan):
        xs = {}
        for i, e in enumerate(plan):
            xs[("dst_x", i)] = (e["dst_box"][0], e["dst_box"][2])
            xs[("src_x", i)] = (e["src_box"][0], e["src_box"][2])
            payload = e.get("render_payload") or {}
            for j, c in enumerate(payload.get("commands") or []):
                xs[("cmd_x", i, j)] = (c.get("x"), c.get("width"))
            for sep in ("list_items", "toc_commands"):
                obj = e.get(sep)
                if isinstance(obj, dict):
                    for j, c in enumerate(obj.get("commands") or []):
                        xs[("cmd_x", sep, i, j)] = (c.get("x"), c.get("width"))
        return xs

    def _y_snapshot(self, plan):
        ys = {}
        for i, e in enumerate(plan):
            ys[("dst_y", i)] = (e["dst_box"][1], e["dst_box"][3])
        return ys

    def test_flow_x_verbatim_across_break(self):
        plan = [_flow("p0_0", 0, 60.0, -20.0, 260.0, 50.0)]
        new_plan, _ = execute_page_breaks(
            plan, page_sizes=self.PAGES, page_start_y=self.PAGE_START)
        self.assertEqual(self._x_snapshot(new_plan), self._x_snapshot(plan))
        # y moved, source box untouched
        new = new_plan[0]
        self.assertEqual(new["dst_box"][1:4:2], [722.0, 792.0])
        self.assertEqual(new["src_box"], plan[0]["src_box"])

    def test_list_x_anchors_verbatim(self):
        plan = [_list("p0_0", 0, 60.0, -20.0, 260.0, 50.0)]  # overflows bottom
        new_plan, report = execute_page_breaks(
            plan, page_sizes=self.PAGES,  # page 0 height 792: bottom -20 < 0
            page_start_y=self.PAGE_START)
        new_list = new_plan[0]
        self.assertEqual(new_list["page"], 1)
        inv = break_invariants(new_list)
        self.assertEqual(inv, {"marker_x": 60.0, "content_x": 76.0,
                               "continuation_x": 76.0})
        self.assertEqual(self._x_snapshot(new_plan), self._x_snapshot(plan))
        self.assertEqual(len(report.applied), 1)

    def test_toc_x_anchor_and_page_column_verbatim(self):
        plan = [_toc("p0_0", 0, 60.0, -20.0, 260.0, 50.0)]  # overflows bottom
        new_plan, report = execute_page_breaks(
            plan, page_sizes=self.PAGES, page_start_y=self.PAGE_START)
        new_toc = new_plan[0]
        self.assertEqual(new_toc["page"], 1)
        inv = break_invariants(new_toc)
        self.assertEqual(inv, {"title_x": 72.0, "page_x": 500.0})
        self.assertEqual(self._x_snapshot(new_plan), self._x_snapshot(plan))
        self.assertEqual(len(report.applied), 1)

    def test_only_y_moved_not_page_unmoved_block(self):
        plan = [_flow("p0_0", 0, 60.0, 700.0, 260.0, 728.0),
                _flow_simple("p0_1", 0, 60.0, -30.0, 260.0, 40.0)]
        new_plan, _ = execute_page_breaks(
            plan, page_sizes=self.PAGES, page_start_y=self.PAGE_START)
        self.assertEqual(new_plan[0]["page"], 0)
        self.assertEqual(new_plan[0]["dst_box"], plan[0]["dst_box"])
        self.assertEqual(new_plan[0]["src_box"], plan[0]["src_box"])


# ---------------------------------------------------------------------------
# 3. KEEP / PRESERVE untouched — code never breaks
# ---------------------------------------------------------------------------


class TestKeepAndPreserve(unittest.TestCase):
    PAGES = {0: 792.0}
    PAGE_START = 792.0

    def test_keep_block_untouched(self):
        plan = [_flow_simple("p0_0", 0, 60.0, 700.0, 260.0, 728.0)]
        new_plan, report = execute_page_breaks(
            plan, page_sizes=self.PAGES, page_start_y=self.PAGE_START)
        self.assertEqual(report.applied, [])
        self.assertEqual(new_plan[0], plan[0])
        self.assertEqual(report.passes, 0)

    def test_code_overflow_never_breaks(self):
        plan = [_code_entry("p0_0", 0, 60.0, -30.0, 260.0, 40.0),
                _flow_simple("p0_1", 0, 60.0, 700.0, 260.0, 728.0)]
        new_plan, report = execute_page_breaks(
            plan, page_sizes=self.PAGES, page_start_y=self.PAGE_START)
        self.assertEqual(report.applied, [])
        # code stays on page 0, geometry unchanged
        self.assertEqual(new_plan[0]["page"], 0)
        self.assertEqual(new_plan[0]["dst_box"], plan[0]["dst_box"])
        self.assertEqual(new_plan[0]["src_box"], plan[0]["src_box"])
        self.assertEqual(len(report.deferred), 1)
        self.assertEqual(report.deferred[0].decision.value, "preserve_overflow")

    def test_break_decision_on_preserved_becomes_preserve(self):
        # feed a BREAK decision directly onto the code; code wins — never broken
        plan = [_flow_simple("p0_0", 0, 60.0, 700.0, 260.0, 728.0),
                _code_entry("p0_1", 0, 60.0, 660.0, 260.0, 720.0)]
        flow_p, code_p = placements_from_plan(plan)
        decisions = [(flow_p, PageBreakDecision.KEEP),
                     (code_p, PageBreakDecision.BREAK_TO_NEXT_PAGE)]
        new_plan, report = execute_page_breaks(
            plan, page_sizes=self.PAGES, decisions=decisions,
            page_start_y=self.PAGE_START)
        code = [e for e in new_plan if e["kind"] == "code"][0]
        self.assertEqual(code["page"], 0)
        self.assertEqual(code["dst_box"], plan[1]["dst_box"])
        # flow untouched, nothing applied, the demoted preserve recorded
        self.assertEqual(new_plan[0]["dst_box"], plan[0]["dst_box"])
        self.assertEqual(report.applied, [])
        self.assertEqual(len(report.deferred), 1)
        self.assertEqual(report.deferred[0].decision.value, "preserve_overflow")


# ---------------------------------------------------------------------------
# 4. page chain — monotonic, bounded, creates next page
# ---------------------------------------------------------------------------


class TestPageChain(unittest.TestCase):
    PAGES = {0: 792.0, 1: 792.0, 2: 792.0, 3: 792.0}
    PAGE_START = 792.0

    def test_a_b_c_breaks_land_on_distinct_pages(self):
        plan = [
            _flow_simple("p0_0", 0, 60.0, 700.0, 260.0, 728.0),
            _flow_simple("p0_1", 0, 60.0, -30.0, 260.0, 30.0),   # overflow → 1
            _flow_simple("p0_2", 0, 60.0, -60.0, 260.0, 0.0),    # overflow → 2
        ]
        new_plan, report = execute_page_breaks(
            plan, page_sizes=self.PAGES, page_start_y=self.PAGE_START)
        pages = [e["page"] for e in new_plan]
        self.assertEqual(pages, [0, 1, 2])   # A→0, B→1, C→2 monotonic
        self.assertEqual(len(report.applied), 2)
        self.assertEqual(report.applied[0].target_page, 1)
        self.assertEqual(report.applied[1].target_page, 2)
        # pages are distinct — never all on one, never reused
        self.assertEqual(len(set(pages)), 3)

    def test_block_on_last_page_creates_next_page(self):
        # a source block already claims page 1; the overflow from 0 must not
        # reuse page 1 → it creates page 2
        plan = [
            _flow_simple("p0_0", 0, 60.0, 700.0, 260.0, 728.0),
            _flow_simple("p0_1", 0, 60.0, -30.0, 260.0, 30.0),   # overflow
            _flow_simple("p1_0", 1, 60.0, 700.0, 260.0, 728.0),  # occupies 1
        ]
        new_plan, report = execute_page_breaks(
            plan, page_sizes=self.PAGES, page_start_y=self.PAGE_START)
        mover = [e for e in new_plan if e["block_id"] == "p0_1"][0]
        self.assertEqual(mover["page"], 2)   # skipped taken page 1
        self.assertEqual(report.applied[0].target_page, 2)

    def test_chain_bounded_no_unbounded_pages(self):
        # every overflowed block breaks monotonic A's keep — created pages are
        # exactly the overflowed blocks, never an unbounded page stream
        plan = [
            _flow_simple("p0_keep", 0, 60.0, 700.0, 260.0, 720.0),
            _flow_simple("p0_1", 0, 60.0, -30.0, 260.0, 0.0),
            _flow_simple("p0_2", 0, 60.0, -60.0, 260.0, -30.0),
            _flow_simple("p0_3", 0, 60.0, -90.0, 260.0, -60.0),
        ]
        new_plan, report = execute_page_breaks(
            plan, page_sizes=self.PAGES, page_start_y=self.PAGE_START)
        pages = [e["page"] for e in new_plan]
        self.assertEqual(pages, [0, 1, 2, 3])   # keep on 0; three breaks
        self.assertEqual(len(report.applied), 3)
        self.assertEqual([e.target_page for e in report.applied], [1, 2, 3])


# ---------------------------------------------------------------------------
# 5. budget — max_page_breaks stops immediately
# ---------------------------------------------------------------------------


class TestBudget(unittest.TestCase):
    PAGES = {0: 792.0, 1: 792.0, 2: 792.0}
    PAGE_START = 792.0

    def test_budget_zero_applies_nothing(self):
        plan = [_flow_simple("p0_0", 0, 60.0, -20.0, 260.0, 50.0)]
        new_plan, report = execute_page_breaks(
            plan, page_sizes=self.PAGES, max_page_breaks=0,
            page_start_y=self.PAGE_START)
        self.assertEqual(report.applied, [])
        self.assertTrue(report.stopped_early)
        self.assertEqual(len(report.deferred), 1)
        self.assertEqual(len(report.unresolved), 1)
        self.assertEqual(new_plan[0]["page"], 0)  # untouched

    def test_budget_less_than_overflowed_defers_rest(self):
        plan = [
            _flow_simple("p0_0", 0, 60.0, 700.0, 260.0, 728.0),
            _flow_simple("p0_1", 0, 60.0, -30.0, 260.0, 30.0),
            _flow_simple("p0_2", 0, 60.0, -60.0, 260.0, 0.0),
        ]
        new_plan, report = execute_page_breaks(
            plan, page_sizes=self.PAGES, max_page_breaks=1,
            page_start_y=self.PAGE_START)
        self.assertEqual(len(report.applied), 1)
        self.assertEqual(len(report.deferred), 1)
        self.assertEqual(len(report.unresolved), 1)
        self.assertTrue(report.stopped_early)
        pages = [e["page"] for e in new_plan]
        self.assertEqual(pages, [0, 1, 0])  # third block deferred
        self.assertEqual(report.applied[0].target_page, 1)
        self.assertEqual(report.deferred[0].target_page, 1)  # would-have-been

    def test_budget_defaults_to_blocks(self):
        plan = [_flow_simple("p0_0", 0, 60.0, -20.0, 260.0, 50.0)]
        _, report = execute_page_breaks(plan, page_sizes=self.PAGES,
                                        page_start_y=self.PAGE_START)
        self.assertEqual(report.max_page_breaks, 1)


# ---------------------------------------------------------------------------
# 6. source geometry immutable — source_collision_count unchanged
# ---------------------------------------------------------------------------


class TestSourceImmutability(unittest.TestCase):
    PAGES = {0: 792.0, 1: 792.0}
    PAGE_START = 792.0

    def test_source_collision_count_identical_before_and_after(self):
        # B overflows the page bottom (resolved geometry) but does not overlap
        # any other block's SOURCE geometry.  The break moves only resolved
        # geometry; the source collision count is invariant and src_box stays.
        plan = [
            _flow_simple("p0_0", 0, 60.0, 700.0, 260.0, 720.0),
            _flow_simple("p0_1", 0, 60.0, -20.0, 260.0, 50.0),  # overflow only
        ]
        before = build_page_flow_report(plan, page_sizes=self.PAGES)
        src_before = before.source_collision_count
        self.assertEqual(src_before, 0)  # no source overlap anywhere
        new_plan, report = execute_page_breaks(
            plan, page_sizes=self.PAGES, page_start_y=self.PAGE_START)
        after = build_page_flow_report(new_plan, page_sizes=self.PAGES)
        self.assertEqual(after.source_collision_count, src_before)  # invariant
        # resolved overflow on page 0 cleared
        self.assertEqual(after.summary()["page_overflow_count"], 0)
        self.assertEqual(len(report.applied), 1)
        # source boxes byte-identical across the whole plan
        self.assertEqual([e["src_box"] for e in new_plan],
                         [e["src_box"] for e in plan])

    def test_src_box_never_changes(self):
        plan = [_flow("p0_0", 0, 60.0, -20.0, 260.0, 50.0)]
        src_before = copy.deepcopy(plan[0]["src_box"])
        new_plan, _ = execute_page_breaks(
            plan, page_sizes=self.PAGES, page_start_y=self.PAGE_START)
        self.assertEqual(new_plan[0]["src_box"], src_before)


# ---------------------------------------------------------------------------
# 7. isolated command writers (units)
# ---------------------------------------------------------------------------


class TestCommandWriters(unittest.TestCase):
    def test_shift_command_fields_only_y_and_page(self):
        cmds = [{"kind": "t", "x": 10.0, "y": 700.0, "width": 20.0},
                {"kind": "p", "x": 500.0, "y": 700.0, "page": 0}]
        shift_command_fields(cmds, -700.0, 3)
        self.assertEqual(cmds[0]["y"], 0.0)
        self.assertEqual(cmds[0]["x"], 10.0)
        self.assertEqual(cmds[0]["width"], 20.0)
        # carries a page → updated; non-paged command untouched in that regard
        self.assertEqual(cmds[1]["page"], 3)
        self.assertEqual(cmds[1]["y"], 0.0)

    def test_move_entry_to_page_only_page_and_y(self):
        entry = _flow("p0_0", 0, 60.0, 660.0, 260.0, 720.0)
        src = copy.deepcopy(entry)
        move_entry_to_page(entry, -700.0, 2)
        self.assertEqual(entry["page"], 2)
        self.assertEqual(entry["dst_box"], [60.0, -40.0, 260.0, 20.0])
        self.assertEqual(entry["src_box"], src["src_box"])
        self.assertEqual(entry["dst_box"][0], src["dst_box"][0])
        # commands y moved, x / text verbatim
        cmd = entry["render_payload"]["commands"][0]
        self.assertEqual(cmd["y"], 20.0)
        self.assertEqual(cmd["x"], 60.0)


# ---------------------------------------------------------------------------
# 8. report shape / json round-trip
# ---------------------------------------------------------------------------


class TestReportShape(unittest.TestCase):
    PAGES = {0: 792.0, 1: 792.0}
    PAGE_START = 792.0

    def test_report_to_dict_json(self):
        plan = [_flow_simple("p0_0", 0, 60.0, -20.0, 260.0, 50.0),
                _code_entry("p0_1", 0, 60.0, 700.0, 260.0, 728.0)]
        _, report = execute_page_breaks(plan, page_sizes=self.PAGES,
                                        page_start_y=self.PAGE_START)
        self.assertIsInstance(report, PageBreakExecutionReport)
        d = report.to_dict()
        self.assertEqual(set(d),
                         {"passes", "max_page_breaks", "stopped_early",
                          "applied_count", "deferred_count", "unresolved_count",
                          "applied", "deferred", "unresolved"})
        json.dumps(d)


# ---------------------------------------------------------------------------
# 9. architecture purity — executor consumes decisions, never re-decides
# ---------------------------------------------------------------------------


def _code(path: Path) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"))

    def _clean(body):
        return [
            n for n in body
            if not (isinstance(n, ast.Expr)
                    and isinstance(n.value, ast.Constant)
                    and isinstance(n.value.value, str))
        ]

    tree.body = _clean(tree.body)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            node.body = _clean(node.body)
    ast.fix_missing_locations(tree)
    return ast.unparse(tree)


class TestExecutorArchitecture(unittest.TestCase):
    def test_executor_never_redecides_or_relays_out(self):
        src = _code(_EXEC_PATH)
        for banned in ("lay_out(", "adaptive_layout(", "wrap_lines(",
                       "shrink_to_fit(", "clip_text(", "decide_page_recovery(",
                       "build_page_flow_report(", "detect_page_collisions(",
                       "detect_page_overflows(", "resolve_page_shifts(",
                       "apply_page_shifts("):
            self.assertNotIn(banned, src,
                             f"page_break_executor.py 不得决策/重排: {banned}")
        for banned in ("list_detector", "list_parser", "toc_parser",
                       "code_detector", "style_detector",
                       "semantic.renderer", "translator", "magicpdf"):
            self.assertNotIn(banned, src,
                             f"page_break_executor.py 不得引用: {banned}")

    def test_executor_uses_the_8e1_landing_contract(self):
        src = _code(_EXEC_PATH)
        self.assertIn("break_placement_to_page", src)
        self.assertIn("next_free_page", src)
        self.assertIn("next_page_start_y", src)

    def test_executor_defines_no_adaptive_entry_point(self):
        tree = ast.parse(_EXEC_PATH.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name.startswith("adaptive"):
                raise AssertionError(
                    f"page_break_executor.py 定义了第二个 executor: {node.name}"
                )


if __name__ == "__main__":
    unittest.main()