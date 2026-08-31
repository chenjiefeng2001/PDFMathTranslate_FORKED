# -*- coding: utf-8 -*-
"""Commit 7F-8d — Block Shift Executor.

The first phase of 7F-8 that actually changes layout geometry — and the
strictest one: 8d executes the 8c ``BlockShiftDecision`` (only SHIFT_DOWN),
then re-validates the page flow::

    before collision = 1
        ↓ apply shift
    after collision = 0

Locked guarantees (the 8d DoD):

1. **SHIFT_DOWN is executed**; KEEP / PRESERVE_OVERFLOW leave the block
   untouched; NEXT_PAGE is **deferred** to 7F-8e, never executed here.
2. **X geometry is 100% immutable** — x0 / x1 / width never change; only Y of
   the resolved placement (and the draw commands) moves.
3. **Source geometry is 100% immutable** — ``BlockPlacement.bbox`` / plan
   ``src_box`` stay forever; only ``resolved_bbox`` / ``dst_box`` move.
4. **shift_y contract** — positive = toward the page bottom; the executor
   converts to the coordinate space in exactly one place
   (:func:`shift_box_down`, v3 y-up: subtract).
5. **cascade is bounded** — detect → decide → shift → detect again, at most
   ``max_passes`` (default ``blocks + 1``); a pass that cannot progress stops
   early; leftovers are recorded unresolved (final stance PRESERVE_OVERFLOW).
6. **before / after observable** — ``apply_page_shifts`` returns a NEW plan;
   re-running 8b shows the resolved collisions cleared.
7. **read-only input** — the source plan is never mutated; no renderer /
   converter / ONNX / detector / parser touched; no re-layout.
"""

import ast
import copy
import unittest
from pathlib import Path

from pdf2zh.semantic.layout.page_flow import (
    BlockPlacement,
    build_page_flow_report,
    detect_page_collisions,
    placements_from_plan,
)
from pdf2zh.semantic.layout.page_recovery import (
    PageRecoveryDecision,
    decide_block_shift,
)
from pdf2zh.semantic.layout.page_shift import (
    ShiftExecutionReport,
    apply_block_shift,
    apply_page_shifts,
    block_deltas,
    resolve_page_shifts,
    shift_box_down,
)

_HERE = Path(__file__).resolve().parent
_PAGE_SHIFT_PATH = _HERE.parent / "pdf2zh" / "semantic" / "layout" / "page_shift.py"


def _entry(block_id, page, kind, x0, y0, x1, y1, payload=None,
           list_items=None, toc_entries=None, toc_commands=None):
    """One settled render-plan entry (v3 y-up boxes: y0 bottom, y1 top)."""
    box = [float(x0), float(y0), float(x1), float(y1)]
    return {
        "block_id": block_id, "page": page, "kind": kind,
        "text": "t", "translated": "t",
        "src_box": list(box), "dst_box": list(box),
        "font_size": 11.0,
        "render_payload": payload if payload is not None
        else {"kind": kind, "commands": []},
        "list_items": list_items,
        "toc_entries": toc_entries,
        "toc_commands": toc_commands,
    }


def _flow(block_id, page, x0, y0, x1, y1):
    """Flow entry with a single settled command line inside its box.

    7G-5 (recovery-side draw-extent parity): the cascade floor is the
    receiver's REAL glyph top (settled baseline + ascent).  A well-formed
    block's baseline sits INSIDE its box, so the command baseline is placed
    at ``top - 9`` (ascent 0.8*10 = 8 → glyph top = top - 1 < top → zero
    excess, the 7G-2.1 convention).  Tiny boxes (height < 12, the page-edge
    regression) center the line so the drawn bottom never pokes below the
    box.  Blocks whose lines genuinely poke ABOVE their box are the 7G-5 P0
    case, tested separately in tests/test_page_shift_7g5.py.
    """
    box_h = float(y1) - float(y0)
    cmd_y = float(y1) - 9.0 if box_h >= 12.0 else (float(y0) + float(y1)) / 2.0
    payload = {
        "kind": "flow", "font_size": 10.0,
        "commands": [{"kind": "flow-text", "text": "t", "x": float(x0),
                      "y": cmd_y, "width": 100.0, "line": 0,
                      "is_last": True, "overflow": False}],
    }
    return _entry(block_id, page, "flow", x0, y0, x1, y1, payload=payload)


def _code_entry(block_id, page, x0, y0, x1, y1):
    return _entry(block_id, page, "code", x0, y0, x1, y1)


def _list_with_continuation(block_id, page, x0, y0, x1, y1):
    """List block with continuation lines (marker/content/continuation X)."""
    commands = [
        {"kind": "marker", "text": "1.", "x": 60.0, "y": 700.0, "width": 11.0},
        {"kind": "text", "text": "content", "x": 76.0, "y": 700.0,
         "width": 50.0},
        {"kind": "text", "text": "cont", "x": 76.0, "y": 688.0,
         "width": 40.0},
    ]
    items = [{
        "marker": "1.", "marker_x": 60.0, "content_x": 76.0,
        "continuation_x": 76.0, "continuation": ["cont"],
    }]
    return _entry(block_id, page, "list", x0, y0, x1, y1,
                  list_items={"commands": commands, "items": items},
                  payload={"kind": "list", "commands": commands})


def _toc(block_id, page, x0, y0, x1, y1):
    """TOC block with title + page-number runs (title_x / page_x fixed)."""
    commands = [
        {"kind": "title", "text": "Intro", "x": 72.0, "y": 700.0,
         "width": 100.0},
        {"kind": "page", "text": "42", "x": 500.0, "y": 700.0, "width": 20.0},
    ]
    entries = [{"title": "Intro", "title_x": 72.0, "page_x": 500.0,
                "continuation": []}]
    return _entry(block_id, page, "toc", x0, y0, x1, y1,
                  toc_entries=entries,
                  toc_commands={"commands": commands},
                  payload={"kind": "toc", "commands": commands})


# ---------------------------------------------------------------------------
# 1. 8d-1 — pure geometry shift (coordinate contract + X immutability)
# ---------------------------------------------------------------------------


class TestPureGeometryShift(unittest.TestCase):
    def test_shift_box_down_positive_moves_toward_page_bottom(self):
        # v3 y-up: "down" decreases y; x is untouched
        box = (60.0, 660.0, 260.0, 720.0)
        shifted = shift_box_down(box, 20.0)
        self.assertEqual(shifted, (60.0, 640.0, 260.0, 700.0))

    def test_shift_box_down_zero_is_identity(self):
        box = (60.0, 660.0, 260.0, 720.0)
        self.assertEqual(shift_box_down(box, 0.0), box)

    def test_apply_block_shift_shifts_resolved_only(self):
        plan = [_flow("p1_0", 1, 60.0, 700.0, 260.0, 728.0),
                _flow("p1_1", 1, 60.0, 660.0, 260.0, 720.0)]
        placements = placements_from_plan(plan)
        b = placements[1]
        collision = detect_page_collisions(plan)[0]
        decision = decide_block_shift(collision, page_height=792.0)
        shifted = apply_block_shift(b, decision)
        # resolved moved down; source bbox and x never touched
        self.assertEqual(shifted.resolved_bbox, (60.0, 640.0, 260.0, 700.0))
        self.assertEqual(shifted.bbox, b.bbox)
        self.assertEqual(shifted.resolved_bbox[0], b.resolved_bbox[0])
        self.assertEqual(shifted.resolved_bbox[2], b.resolved_bbox[2])
        self.assertEqual(shifted.height, b.height)  # height preserved
        # the input placement is unchanged (frozen, new object returned)
        self.assertEqual(b.resolved_bbox, (60.0, 660.0, 260.0, 720.0))

    def test_keep_preserve_next_page_never_move(self):
        plan = [_flow("p1_0", 1, 60.0, 700.0, 260.0, 728.0),
                _flow("p1_1", 1, 60.0, 660.0, 260.0, 720.0)]
        b = placements_from_plan(plan)[1]
        from pdf2zh.semantic.layout.page_recovery import keep_decision
        for decision in (
            keep_decision(b),
        ):
            self.assertIs(apply_block_shift(b, decision), b)
        collision = detect_page_collisions(plan)[0]
        # PRESERVE_OVERFLOW (code involved) and NEXT_PAGE (page too short)
        code_plan = [_flow("p1_0", 1, 60.0, 700.0, 260.0, 728.0),
                     _code_entry("p1_1", 1, 60.0, 660.0, 260.0, 720.0)]
        d_preserve = decide_block_shift(detect_page_collisions(code_plan)[0],
                                        page_height=792.0)
        self.assertIs(apply_block_shift(b, d_preserve), b)
        near_bottom = [_flow("p1_0", 1, 60.0, 8.0, 260.0, 18.0),
                       _flow("p1_1", 1, 60.0, 0.0, 260.0, 10.0)]
        d_next = decide_block_shift(detect_page_collisions(near_bottom)[0],
                                    page_height=18.0)
        self.assertEqual(d_next.decision, PageRecoveryDecision.NEXT_PAGE)
        nb = placements_from_plan(near_bottom)[1]
        self.assertIs(apply_block_shift(nb, d_next), nb)

    def test_apply_block_shift_guards_off_page_shift(self):
        # decided WITHOUT page context -> SHIFT_DOWN, but executing the shift
        # would push the block below the page bottom (v3 bottom edge 0): the
        # executor must refuse loudly, never silently shift off-page
        plan = [_flow("p1_0", 1, 60.0, 8.0, 260.0, 18.0),
                _flow("p1_1", 1, 60.0, 0.0, 260.0, 10.0)]
        b = placements_from_plan(plan)[1]
        c = detect_page_collisions(plan)[0]
        d = decide_block_shift(c)  # no page_height -> SHIFT_DOWN by default
        self.assertEqual(d.decision, PageRecoveryDecision.SHIFT_DOWN)
        self.assertEqual(d.shift_y, 2.0)
        with self.assertRaises(ValueError):
            apply_block_shift(b, d)


# ---------------------------------------------------------------------------
# 2. 8d-2 — bounded cascade
# ---------------------------------------------------------------------------


class TestBoundedCascade(unittest.TestCase):
    def test_simple_collision_resolves_in_one_pass(self):
        plan = [_flow("p1_0", 1, 60.0, 700.0, 260.0, 728.0),
                _flow("p1_1", 1, 60.0, 660.0, 260.0, 720.0)]
        before = build_page_flow_report(plan, page_sizes={1: 792.0}).summary()
        self.assertEqual(before["resolved_collision_count"], 1)
        final, report = resolve_page_shifts(plan, page_sizes={1: 792.0})
        self.assertEqual(report.passes, 1)
        self.assertEqual(len(report.applied), 1)
        self.assertFalse(report.stopped_early)
        self.assertEqual(report.unresolved, [])
        # final placements are collision-free and B landed below A
        self.assertEqual(detect_collisions_of(final), 0)
        self.assertEqual(final[1].resolved_bbox, (60.0, 640.0, 260.0, 700.0))

    def test_cascade_a_b_c_converges(self):
        # B collides with A (shift 10); after B shifts it collides with C
        # (shift 5) — the cascade must clear both in two passes
        plan = [
            _flow("p1_0", 1, 60.0, 700.0, 260.0, 730.0),
            _flow("p1_1", 1, 60.0, 670.0, 260.0, 710.0),
            _flow("p1_2", 1, 60.0, 650.0, 260.0, 665.0),
        ]
        self.assertEqual(len(detect_page_collisions(plan)), 1)  # only A-B
        final, report = resolve_page_shifts(plan, page_sizes={1: 792.0})
        self.assertEqual(report.passes, 2)
        self.assertEqual(len(report.applied), 2)
        self.assertEqual(report.unresolved, [])
        self.assertEqual(final[1].resolved_bbox, (60.0, 660.0, 260.0, 700.0))
        self.assertEqual(final[2].resolved_bbox, (60.0, 645.0, 260.0, 660.0))
        # final state is collision-free (touching is not a collision)
        self.assertEqual(detect_collisions_of(final), 0)

    def test_budget_exhaustion_is_bounded_and_recorded(self):
        # a cascade that needs 2 passes, capped at 1 -> unresolved recorded
        plan = [
            _flow("p1_0", 1, 60.0, 700.0, 260.0, 730.0),
            _flow("p1_1", 1, 60.0, 670.0, 260.0, 710.0),
            _flow("p1_2", 1, 60.0, 650.0, 260.0, 665.0),
        ]
        final, report = resolve_page_shifts(plan, page_sizes={1: 792.0},
                                            max_passes=1)
        self.assertEqual(report.passes, 1)
        self.assertEqual(len(report.applied), 1)
        self.assertEqual(len(report.unresolved), 1)  # (B,C) remains
        self.assertEqual(len(report.deferred), 1)
        self.assertEqual(report.to_dict()["passes"], 1)

    def test_next_page_only_stops_early_without_progress(self):
        plan = [
            _flow("p1_0", 1, 60.0, 8.0, 260.0, 18.0),
            _flow("p1_1", 1, 60.0, 0.0, 260.0, 10.0),
        ]
        final, report = resolve_page_shifts(plan, page_sizes={1: 18.0})
        self.assertTrue(report.stopped_early)
        self.assertEqual(report.applied, [])
        self.assertEqual(len(report.unresolved), 1)
        self.assertEqual(report.deferred[0].decision,
                         PageRecoveryDecision.NEXT_PAGE)
        # geometry untouched — NEXT_PAGE is deferred to 7F-8e
        self.assertEqual(final[1].resolved_bbox, (60.0, 0.0, 260.0, 10.0))

    def test_default_max_passes_is_blocks_plus_one(self):
        plan = [_flow("p1_0", 1, 60.0, 700.0, 260.0, 728.0),
                _flow("p1_1", 1, 60.0, 660.0, 260.0, 720.0)]
        _, report = resolve_page_shifts(plan, page_sizes={1: 792.0})
        self.assertEqual(report.max_passes, 3)  # 2 blocks + 1

    def test_no_collision_plan_no_shifts(self):
        plan = [_flow("p1_0", 1, 60.0, 700.0, 260.0, 728.0),
                _flow("p1_1", 1, 60.0, 620.0, 260.0, 648.0)]
        final, report = resolve_page_shifts(plan, page_sizes={1: 792.0})
        self.assertEqual(report.passes, 0)
        self.assertEqual(report.applied, [])
        self.assertEqual(report.unresolved, [])
        self.assertEqual(final[1].resolved_bbox, (60.0, 620.0, 260.0, 648.0))

    def test_unparseable_block_ids_never_collapse_placement_keys(self):
        # 7F-9.1 regression: block_index must come from the structured per-page
        # reading order, never from parsing block_id.  With the old string-parse
        # all three ids below resolved to (page, 0) — the cascade's `current`
        # dict kept only the LAST block, collisions vanished, and the wrong
        # entries were shifted.  Now each block owns a distinct key.
        plan = [
            _flow("intro", 1, 60.0, 700.0, 260.0, 728.0),
            _flow("block_abc", 1, 60.0, 660.0, 260.0, 720.0),
            _flow("tail", 1, 60.0, 600.0, 260.0, 640.0),
        ]
        # distinct structured identities despite the unparseable ids
        self.assertEqual([p.block_index for p in placements_from_plan(plan)],
                         [0, 1, 2])
        # the A–B collision is resolved exactly once: B shifts down by its
        # required 20pt; C (touching B after the shift) is never touched
        final, report = resolve_page_shifts(plan, page_sizes={1: 792.0})
        self.assertEqual(len(report.applied), 1)
        self.assertEqual(report.applied[0].block_index, 1)
        self.assertEqual(final[0].resolved_bbox, (60.0, 700.0, 260.0, 728.0))
        self.assertEqual(final[1].resolved_bbox, (60.0, 640.0, 260.0, 700.0))
        self.assertEqual(final[2].resolved_bbox, (60.0, 600.0, 260.0, 640.0))
        # the plan wiring shifts exactly the entry that moved — never a
        # neighbor, never two entries with the same delta
        new_plan, _ = apply_page_shifts(plan, page_sizes={1: 792.0})
        self.assertEqual(new_plan[0]["dst_box"], [60.0, 700.0, 260.0, 728.0])
        self.assertEqual(new_plan[1]["dst_box"], [60.0, 640.0, 260.0, 700.0])
        self.assertEqual(new_plan[2]["dst_box"], [60.0, 600.0, 260.0, 640.0])


def detect_collisions_of(placements):
    from pdf2zh.semantic.layout.page_flow import detect_collisions_from_placements
    return len(detect_collisions_from_placements(placements))


# ---------------------------------------------------------------------------
# 3. 8d-3 — render plan wiring (read-only + X/font/text immutability)
# ---------------------------------------------------------------------------


class TestApplyPageShifts(unittest.TestCase):
    def test_returns_new_plan_and_never_mutates_input(self):
        plan = [_flow("p1_0", 1, 60.0, 700.0, 260.0, 728.0),
                _flow("p1_1", 1, 60.0, 660.0, 260.0, 720.0)]
        snapshot = copy.deepcopy(plan)
        new_plan, report = apply_page_shifts(plan, page_sizes={1: 792.0})
        self.assertIsNot(new_plan, plan)
        self.assertEqual(plan, snapshot)          # input untouched
        self.assertEqual(len(report.applied), 1)
        self.assertEqual(new_plan[1]["dst_box"], [60.0, 640.0, 260.0, 700.0])
        # src_box never changes
        self.assertEqual(new_plan[1]["src_box"], [60.0, 660.0, 260.0, 720.0])
        self.assertEqual(new_plan[0]["dst_box"], [60.0, 700.0, 260.0, 728.0])

    def test_commands_y_shifted_x_and_width_untouched(self):
        plan = [_flow("p1_0", 1, 60.0, 700.0, 260.0, 728.0),
                _flow("p1_1", 1, 60.0, 660.0, 260.0, 720.0)]
        new_plan, _ = apply_page_shifts(plan, page_sizes={1: 792.0})
        old_cmd = plan[1]["render_payload"]["commands"][0]
        new_cmd = new_plan[1]["render_payload"]["commands"][0]
        self.assertEqual(new_cmd["y"], old_cmd["y"] - 20.0)
        self.assertEqual(new_cmd["x"], old_cmd["x"])
        self.assertEqual(new_cmd["width"], old_cmd["width"])
        # font / text unchanged
        self.assertEqual(new_plan[1]["font_size"], plan[1]["font_size"])
        self.assertEqual(new_plan[1]["text"], plan[1]["text"])

    def test_block_deltas_reports_only_movers(self):
        plan = [_flow("p1_0", 1, 60.0, 700.0, 260.0, 728.0),
                _flow("p1_1", 1, 60.0, 660.0, 260.0, 720.0)]
        initial = placements_from_plan(plan)
        final, _ = resolve_page_shifts(plan, page_sizes={1: 792.0})
        deltas = block_deltas(initial, final)
        self.assertEqual(deltas, {(1, 1): 20.0})

    def test_report_shape_and_json(self):
        import json
        plan = [_flow("p1_0", 1, 60.0, 700.0, 260.0, 728.0),
                _flow("p1_1", 1, 60.0, 660.0, 260.0, 720.0)]
        _, report = apply_page_shifts(plan, page_sizes={1: 792.0})
        self.assertIsInstance(report, ShiftExecutionReport)
        d = report.to_dict()
        self.assertEqual(set(d),
                         {"passes", "max_passes", "stopped_early",
                          "stopped_reason", "applied_count", "deferred_count",
                          "unresolved_count", "applied", "deferred",
                          "unresolved"})
        json.dumps(d)


# ---------------------------------------------------------------------------
# 4. golden gates — before / after PageFlow (the 8d-4 matrix, plan level)
# ---------------------------------------------------------------------------


class TestGoldenGates(unittest.TestCase):
    PAGE = {1: 792.0}

    def _counts(self, plan):
        s = build_page_flow_report(plan, page_sizes=self.PAGE).summary()
        return s["resolved_collision_count"], s["source_collision_count"]

    def test_case1_flow_collision_before_1_after_0(self):
        plan = [_flow("p1_0", 1, 60.0, 700.0, 260.0, 728.0),
                _flow("p1_1", 1, 60.0, 660.0, 260.0, 720.0)]
        self.assertEqual(self._counts(plan), (1, 1))
        new_plan, report = apply_page_shifts(plan, page_sizes=self.PAGE)
        self.assertEqual(self._counts(new_plan), (0, 1))  # source still 1
        self.assertEqual(len(report.applied), 1)

    def test_case2_list_continuation_x_unchanged(self):
        plan = [_flow("p1_0", 1, 60.0, 700.0, 260.0, 728.0),
                _list_with_continuation("p1_1", 1, 60.0, 660.0, 260.0, 720.0)]
        new_plan, report = apply_page_shifts(plan, page_sizes=self.PAGE)
        self.assertEqual(len(report.applied), 1)
        new = new_plan[1]
        # marker_x / content_x / continuation_x unchanged
        item = new["list_items"]["items"][0]
        self.assertEqual(item["marker_x"], 60.0)
        self.assertEqual(item["content_x"], 76.0)
        self.assertEqual(item["continuation_x"], 76.0)
        # commands: x unchanged, y shifted (whole block moved down as a unit)
        for cmd in new["render_payload"]["commands"]:
            self.assertEqual(cmd["x"], 60.0 if cmd["kind"] == "marker" else 76.0)
        ys = [c["y"] for c in new["render_payload"]["commands"]]
        self.assertEqual(ys, [680.0, 680.0, 668.0])

    def test_case3_toc_invariants_unchanged(self):
        plan = [_flow("p1_0", 1, 60.0, 700.0, 260.0, 728.0),
                _toc("p1_1", 1, 60.0, 660.0, 260.0, 720.0)]
        new_plan, report = apply_page_shifts(plan, page_sizes=self.PAGE)
        self.assertEqual(len(report.applied), 1)
        new = new_plan[1]
        entry = new["toc_entries"][0]
        self.assertEqual(entry["title_x"], 72.0)   # title_x unchanged
        self.assertEqual(entry["page_x"], 500.0)   # page_x unchanged
        # the page-number run stays exactly at page_x
        page_cmd = [c for c in new["render_payload"]["commands"]
                    if c["kind"] == "page"][0]
        self.assertEqual(page_cmd["x"], 500.0)
        title_cmd = [c for c in new["render_payload"]["commands"]
                     if c["kind"] == "title"][0]
        self.assertEqual(title_cmd["x"], 72.0)
        # only y moved
        self.assertEqual(page_cmd["y"], 680.0)

    def test_case4_code_preserve_never_moves(self):
        plan = [_flow("p1_0", 1, 60.0, 700.0, 260.0, 728.0),
                _code_entry("p1_1", 1, 60.0, 660.0, 260.0, 720.0)]
        new_plan, report = apply_page_shifts(plan, page_sizes=self.PAGE)
        self.assertEqual(report.applied, [])           # nothing shifted
        self.assertEqual(len(report.deferred), 1)      # PRESERVE_OVERFLOW
        self.assertEqual(report.deferred[0].decision,
                         PageRecoveryDecision.PRESERVE_OVERFLOW)
        self.assertEqual(report.deferred[0].shift_y, 0.0)
        # code entry untouched (dst == src, no commands)
        self.assertEqual(new_plan[1]["dst_box"], [60.0, 660.0, 260.0, 720.0])
        self.assertEqual(new_plan[1], plan[1])

    def test_case5_cascade_a_b_c_final_no_collision(self):
        plan = [
            _flow("p1_0", 1, 60.0, 700.0, 260.0, 730.0),
            _flow("p1_1", 1, 60.0, 670.0, 260.0, 710.0),
            _flow("p1_2", 1, 60.0, 650.0, 260.0, 665.0),
        ]
        self.assertEqual(self._counts(plan)[0], 1)
        new_plan, report = apply_page_shifts(plan, page_sizes=self.PAGE)
        self.assertEqual(report.passes, 2)
        self.assertEqual(len(report.applied), 2)
        self.assertEqual(self._counts(new_plan), (0, 1))
        # B and C both moved down; A untouched
        self.assertEqual(new_plan[0]["dst_box"], [60.0, 700.0, 260.0, 730.0])
        self.assertEqual(new_plan[1]["dst_box"], [60.0, 660.0, 260.0, 700.0])
        self.assertEqual(new_plan[2]["dst_box"], [60.0, 645.0, 260.0, 660.0])


# ---------------------------------------------------------------------------
# 5. architecture purity — the executor is pure geometry, no policy/rendering
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


class TestPageShiftArchitecture(unittest.TestCase):
    def test_executor_never_redecides_or_relays_out(self):
        src = _code(_PAGE_SHIFT_PATH)
        for banned in ("lay_out(", "adaptive_layout(", "wrap_lines(",
                       "shrink_to_fit(", "clip_text(", "decide_page_recovery(",
                       "build_page_flow_report("):
            self.assertNotIn(banned, src,
                             f"page_shift.py 不得决策/重排: {banned}")
        for banned in ("list_detector", "list_parser", "toc_parser",
                       "code_detector", "style_detector",
                       "semantic.renderer", "translator", "magicpdf"):
            self.assertNotIn(banned, src,
                             f"page_shift.py 不得引用: {banned}")

    def test_executor_never_derives_geometry_from_level_index(self):
        tree = ast.parse(_code(_PAGE_SHIFT_PATH))
        for node in ast.walk(tree):
            if not isinstance(node, ast.BinOp):
                continue
            names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            if names & {"level", "index"}:
                raise AssertionError(
                    f"page_shift.py 用 {type(node.op).__name__} 重建几何"
                )

    def test_executor_defines_no_adaptive_entry_point(self):
        tree = ast.parse(_PAGE_SHIFT_PATH.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name.startswith("adaptive"):
                raise AssertionError(
                    f"page_shift.py 定义了第二个 executor: {node.name}"
                )

    def test_shift_direction_contract_lives_in_one_place(self):
        # the v3 y-up conversion is centralized in shift_box_down
        src = _code(_PAGE_SHIFT_PATH)
        self.assertIn("shift_box_down", src)
        self.assertNotIn("- float(decision.shift_y)", src.replace(
            "shifted = shift_box_down(", ""))  # no inline re-derivation


if __name__ == "__main__":
    unittest.main()
