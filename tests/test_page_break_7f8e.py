# -*- coding: utf-8 -*-
"""Commit 7F-8e-1 — Page Break Contract (pure policy, before any executor).

Pins the four things the executor phase will build on:

1. **NEXT_PAGE semantics** — the settled placement does not fit its page →
   ``BREAK_TO_NEXT_PAGE``; 8c ``NEXT_PAGE`` decisions are consumed directly,
   never re-judged.
2. **Next-page start position** — a break changes ONLY ``page`` and ``y``;
   the resolved top lands at ``next_page_start_y``; x / width / height /
   source geometry stay byte-identical.
3. **List / TOC continuation invariants** — marker_x / content_x /
   continuation_x / title_x / page_x never change across a break; the TOC
   page-number run stays drawn exactly once and never leaks into a
   continuation line.
4. **Code / PRESERVED_REGION 禁止换页** — preserved blocks are never split,
   never moved (PRESERVE_OVERFLOW), even when they overflow.

Plus the page-chain semantics (A→0, B→1, C→2 — monotonic, bounded, no page
reuse) and the record shapes the 8e-2 executor will emit.

Architecture guards (same file, same discipline as 8a–8d): page_break.py is
pure — no detector / parser / renderer / translator imports, no
lay_out / adaptive_layout / wrap/shrink/clip, no level/index math, and it
never mutates a plan (the 8e-2 executor is the only writer).
"""

import ast
import copy
import json
import unittest
from pathlib import Path

from pdf2zh.semantic.layout.page_break import (
    PageBreakDecision,
    PageBreakExecution,
    PageBreakReport,
    assert_break_invariants,
    break_invariants,
    break_placement_to_page,
    decide_page_break,
    decide_page_breaks,
    next_free_page,
    next_page_start_y,
    page_break_execution,
    page_break_from_shift,
)
from pdf2zh.semantic.layout.page_flow import (
    placements_from_plan,
)
from pdf2zh.semantic.layout.page_recovery import (
    PageRecoveryDecision,
    decide_block_shift,
    keep_decision,
)

_HERE = Path(__file__).resolve().parent
_PAGE_BREAK_PATH = _HERE.parent / "pdf2zh" / "semantic" / "layout" / "page_break.py"


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


def _flow(block_id, page, x0, y0, x1, y1):
    return _entry(block_id, page, "flow", x0, y0, x1, y1)


def _list(block_id, page, x0, y0, x1, y1):
    items = [
        {
            "marker": "1.",
            "marker_x": 60.0,
            "content_x": 76.0,
            "continuation_x": 76.0,
            "continuation": ["wrapped line"],
        }
    ]
    return _entry(
        block_id,
        page,
        "list",
        x0,
        y0,
        x1,
        y1,
        list_items={"items": items},
        payload={"kind": "list", "commands": []},
    )


def _toc(block_id, page, x0, y0, x1, y1):
    entries = [
        {
            "title": "Intro",
            "title_x": 72.0,
            "page_x": 500.0,
            "page_number": "42",
            "continuation": [],
        }
    ]
    cmds = [
        {"kind": "title", "text": "Intro", "x": 72.0, "y": 700.0, "width": 100.0},
        {"kind": "page", "text": "42", "x": 500.0, "y": 700.0, "width": 20.0},
    ]
    return _entry(
        block_id,
        page,
        "toc",
        x0,
        y0,
        x1,
        y1,
        toc_entries=entries,
        toc_commands={"commands": cmds},
        payload={"kind": "toc", "commands": cmds},
    )


def _code_entry(block_id, page, x0, y0, x1, y1):
    return _entry(block_id, page, "code", x0, y0, x1, y1)


def _moved_entry(source, target_page=1, page_start_y=10.0):
    """A would-be whole-block break: page+1, top re-anchored at page_start_y,
    everything else identical (what 8e-2 will produce)."""
    target = copy.deepcopy(source)
    target["page"] = target_page
    dst = list(target["dst_box"])
    h = dst[3] - dst[1]
    target["dst_box"] = [
        dst[0],
        round(page_start_y - h, 2),
        dst[2],
        round(page_start_y, 2),
    ]
    return target


# ---------------------------------------------------------------------------
# 1. NEXT_PAGE semantics — capacity decision
# ---------------------------------------------------------------------------


class TestDecidePageBreak(unittest.TestCase):
    def test_enum_values(self):
        self.assertEqual(
            [d.value for d in PageBreakDecision],
            ["keep", "break_to_next_page", "preserve_overflow"],
        )

    def test_block_that_fits_is_keep(self):
        p = placements_from_plan([_flow("p0_0", 0, 60.0, 500.0, 260.0, 700.0)])[0]
        self.assertEqual(
            decide_page_break(p, page_height=792.0), PageBreakDecision.KEEP
        )

    def test_block_below_page_bottom_breaks(self):
        p = placements_from_plan([_flow("p0_0", 0, 60.0, -20.0, 260.0, 50.0)])[0]
        self.assertEqual(
            decide_page_break(p, page_height=792.0),
            PageBreakDecision.BREAK_TO_NEXT_PAGE,
        )

    def test_block_above_page_top_breaks(self):
        p = placements_from_plan([_flow("p0_0", 0, 60.0, 700.0, 260.0, 810.0)])[0]
        self.assertEqual(
            decide_page_break(p, page_height=792.0),
            PageBreakDecision.BREAK_TO_NEXT_PAGE,
        )

    def test_code_overflow_is_preserve_not_break(self):
        # code below the page bottom must NOT break — immovable geometry
        p = placements_from_plan([_code_entry("p0_0", 0, 60.0, -20.0, 260.0, 50.0)])[0]
        self.assertEqual(
            decide_page_break(p, page_height=792.0), PageBreakDecision.PRESERVE_OVERFLOW
        )

    def test_decide_page_breaks_over_plan(self):
        plan = [
            _flow("p0_0", 0, 60.0, 500.0, 260.0, 700.0),
            _flow("p0_1", 0, 60.0, -20.0, 260.0, 50.0),
            _code_entry("p0_2", 0, 60.0, -30.0, 260.0, 10.0),
        ]
        decisions = decide_page_breaks(
            placements_from_plan(plan), page_sizes={0: 792.0}
        )
        self.assertEqual(
            [d for _, d in decisions],
            [
                PageBreakDecision.KEEP,
                PageBreakDecision.BREAK_TO_NEXT_PAGE,
                PageBreakDecision.PRESERVE_OVERFLOW,
            ],
        )

    def test_missing_page_height_defaults_keep(self):
        p = placements_from_plan([_flow("p0_0", 0, 60.0, -20.0, 260.0, 50.0)])[0]
        self.assertEqual(
            decide_page_breaks([p], page_sizes={})[0][1], PageBreakDecision.KEEP
        )


# ---------------------------------------------------------------------------
# 2. NEXT_PAGE semantics — consume 8c without re-judging
# ---------------------------------------------------------------------------


class TestPageBreakFromShift(unittest.TestCase):
    def _collision_plan(self, page=0):
        return [
            _flow("p%d_0" % page, page, 60.0, 700.0, 260.0, 728.0),
            _flow("p%d_1" % page, page, 60.0, 660.0, 260.0, 720.0),
        ]

    def test_8c_next_page_becomes_break(self):
        plan = [
            _flow("p0_0", 0, 60.0, 8.0, 260.0, 18.0),
            _flow("p0_1", 0, 60.0, 0.0, 260.0, 10.0),
        ]
        d = decide_block_shift(
            __import__(
                "pdf2zh.semantic.layout.page_flow", fromlist=["detect_page_collisions"]
            ).detect_page_collisions(plan)[0],
            page_height=18.0,
        )
        self.assertEqual(d.decision, PageRecoveryDecision.NEXT_PAGE)
        self.assertEqual(page_break_from_shift(d), PageBreakDecision.BREAK_TO_NEXT_PAGE)

    def test_8c_shift_down_is_not_a_break(self):
        from pdf2zh.semantic.layout.page_flow import detect_page_collisions

        d = decide_block_shift(
            detect_page_collisions(self._collision_plan())[0], page_height=792.0
        )
        self.assertEqual(d.decision, PageRecoveryDecision.SHIFT_DOWN)
        self.assertEqual(page_break_from_shift(d), PageBreakDecision.KEEP)

    def test_8c_preserve_is_preserve(self):
        from pdf2zh.semantic.layout.page_flow import detect_page_collisions

        plan = [
            _flow("p0_0", 0, 60.0, 700.0, 260.0, 728.0),
            _code_entry("p0_1", 0, 60.0, 660.0, 260.0, 720.0),
        ]
        d = decide_block_shift(detect_page_collisions(plan)[0], page_height=792.0)
        self.assertEqual(d.decision, PageRecoveryDecision.PRESERVE_OVERFLOW)
        self.assertEqual(page_break_from_shift(d), PageBreakDecision.PRESERVE_OVERFLOW)

    def test_8c_next_page_on_preserved_placement_is_preserve(self):
        # code wins over ANY NEXT_PAGE decision — never broken.  (Through the
        # real 8c path a preserved collision short-circuits to PRESERVE_OVERFLOW
        # before NEXT_PAGE is even considered; this unit directly feeds a
        # NEXT_PAGE decision to page_break_from_shift to prove the guard.)
        from pdf2zh.semantic.layout.page_recovery import BlockShiftDecision

        d = BlockShiftDecision(
            block_index=1,
            page=0,
            decision=PageRecoveryDecision.NEXT_PAGE,
            shift_y=2.0,
            reason="overlap",
            source_bbox=(60.0, 0.0, 260.0, 10.0),
            resolved_bbox=(60.0, 0.0, 260.0, 10.0),
        )
        placement = placements_from_plan(
            [_code_entry("p0_1", 0, 60.0, 0.0, 260.0, 10.0)]
        )[0]
        self.assertTrue(placement.preserved)
        self.assertEqual(
            page_break_from_shift(d, placement=placement),
            PageBreakDecision.PRESERVE_OVERFLOW,
        )

    def test_keep_decision_maps_to_keep(self):
        p = placements_from_plan([_flow("p0_0", 0, 60.0, 500.0, 260.0, 700.0)])[0]
        self.assertEqual(
            page_break_from_shift(keep_decision(p)), PageBreakDecision.KEEP
        )


# ---------------------------------------------------------------------------
# 3. next-page start position — only page and y change
# ---------------------------------------------------------------------------


class TestNextPagePosition(unittest.TestCase):
    def test_next_page_start_y_is_the_content_start(self):
        self.assertEqual(next_page_start_y(), 0.0)
        self.assertEqual(next_page_start_y(20.0), 20.0)

    def test_break_maps_page_plus_one_and_y_only(self):
        plan = [
            _flow("p0_0", 0, 60.0, 700.0, 260.0, 728.0),
            _flow("p0_1", 0, 60.0, 660.0, 260.0, 720.0),
        ]
        p = placements_from_plan(plan)[1]  # [660,720], height 60
        mapped = break_placement_to_page(p, target_page=1, page_start_y=10.0)
        self.assertEqual(mapped.page, 1)
        # top lands at page_start_y; x / width / height preserved
        self.assertEqual(mapped.resolved_bbox[3], 10.0)
        self.assertEqual(mapped.resolved_bbox[1], -50.0)  # 10 - 60
        self.assertEqual(mapped.resolved_bbox[0], p.resolved_bbox[0])
        self.assertEqual(mapped.resolved_bbox[2], p.resolved_bbox[2])
        self.assertEqual(mapped.height, p.height)
        # source bbox untouched
        self.assertEqual(mapped.bbox, p.bbox)
        # kind / flags preserved
        self.assertEqual(mapped.kind, p.kind)
        self.assertEqual(mapped.preserved, p.preserved)

    def test_break_maps_preserved_unchanged(self):
        p = placements_from_plan([_code_entry("p0_0", 0, 60.0, 660.0, 260.0, 720.0)])[0]
        self.assertIs(break_placement_to_page(p, target_page=1), p)

    def test_page_break_execution_record_shape(self):
        plan = [_flow("p0_1", 0, 60.0, 660.0, 260.0, 720.0)]
        p = placements_from_plan(plan)[0]
        record, mapped = page_break_execution(p, page_start_y=10.0)
        self.assertIsInstance(record, PageBreakExecution)
        self.assertEqual(record.source_page, 0)
        self.assertEqual(record.target_page, 1)
        self.assertEqual(record.decision, PageBreakDecision.BREAK_TO_NEXT_PAGE)
        self.assertEqual(record.kind, "flow")
        self.assertEqual(mapped.page, 1)
        d = record.to_dict()
        self.assertEqual(
            set(d),
            {
                "block_index",
                "source_page",
                "target_page",
                "kind",
                "decision",
                "reason",
                "next_start_y",
                "source_bbox",
                "resolved_bbox",
                "target",
            },
        )
        json.dumps(d)

    def test_page_break_report_summary(self):
        plan = [
            _flow("p0_1", 0, 60.0, 660.0, 260.0, 720.0),
            _code_entry("p0_2", 0, 60.0, 500.0, 260.0, 600.0),
        ]
        report = PageBreakReport()
        for p in placements_from_plan(plan):
            record, _ = page_break_execution(p)
            report.executions.append(record)
        s = report.summary()
        self.assertEqual(s["total"], 2)
        self.assertEqual(
            s["by_decision"], {"break_to_next_page": 1, "preserve_overflow": 1}
        )
        d = report.to_dict()
        self.assertEqual(set(d), {"executions", "summary"})
        json.dumps(d)


# ---------------------------------------------------------------------------
# 4. page chain — monotonic, bounded, no reuse
# ---------------------------------------------------------------------------


class TestPageChain(unittest.TestCase):
    def test_chain_a_b_c_lands_on_distinct_pages(self):
        # A stays on 0; a break from 0 lands on 1; a second break from 0 on 2
        self.assertEqual(next_free_page(0, occupied=[0]), 1)
        self.assertEqual(next_free_page(0, occupied=[0, 1]), 2)
        self.assertEqual(next_free_page(0, occupied=[0, 1, 2]), 3)

    def test_chain_from_broken_source_page_skips_taken(self):
        # B broke to 1; C breaking from 1 must not reuse 2 if 2 is taken
        self.assertEqual(next_free_page(1, occupied=[0, 1, 2]), 3)

    def test_chain_never_reuses_and_is_bounded(self):
        taken = list(range(0, 50))
        self.assertEqual(next_free_page(0, occupied=taken), 50)


# ---------------------------------------------------------------------------
# 5. continuation invariants — X anchors byte-identical across a break
# ---------------------------------------------------------------------------


class TestContinuationInvariants(unittest.TestCase):
    def test_break_invariants_read_list_anchors(self):
        entry = _list("p0_0", 0, 60.0, 660.0, 260.0, 720.0)
        inv = break_invariants(entry)
        self.assertEqual(
            inv, {"marker_x": 60.0, "content_x": 76.0, "continuation_x": 76.0}
        )

    def test_break_invariants_read_toc_anchors(self):
        inv = break_invariants(_toc("p0_0", 0, 60.0, 660.0, 260.0, 720.0))
        self.assertEqual(inv, {"title_x": 72.0, "page_x": 500.0})

    def test_break_invariants_flow_empty(self):
        self.assertEqual(
            break_invariants(_flow("p0_0", 0, 60.0, 500.0, 260.0, 700.0)), {}
        )

    def test_good_list_break_passes(self):
        src = _list("p0_0", 0, 60.0, 660.0, 260.0, 720.0)
        target = _moved_entry(src, target_page=1, page_start_y=10.0)
        self.assertEqual(assert_break_invariants(src, target), [])

    def test_good_toc_break_passes(self):
        src = _toc("p0_0", 0, 60.0, 660.0, 260.0, 720.0)
        target = _moved_entry(src, target_page=1, page_start_y=10.0)
        self.assertEqual(assert_break_invariants(src, target), [])

    def test_list_x_change_is_flagged(self):
        src = _list("p0_0", 0, 60.0, 660.0, 260.0, 720.0)
        target = _moved_entry(src)
        target["list_items"]["items"][0]["marker_x"] = 61.0
        v = assert_break_invariants(src, target)
        self.assertTrue(any("marker_x changed" in x for x in v))

    def test_toc_page_x_change_is_flagged(self):
        src = _toc("p0_0", 0, 60.0, 660.0, 260.0, 720.0)
        target = _moved_entry(src)
        target["toc_entries"][0]["page_x"] = 501.0
        v = assert_break_invariants(src, target)
        self.assertTrue(any("page_x changed" in x for x in v))

    def test_src_box_change_is_flagged(self):
        src = _flow("p0_0", 0, 60.0, 660.0, 260.0, 720.0)
        target = _moved_entry(src)
        target["src_box"] = [61.0, 660.0, 260.0, 720.0]
        v = assert_break_invariants(src, target)
        self.assertTrue(any("src_box changed" in x for x in v))

    def test_dst_x_change_is_flagged(self):
        src = _flow("p0_0", 0, 60.0, 660.0, 260.0, 720.0)
        target = _moved_entry(src)
        target["dst_box"][0] = 61.0
        v = assert_break_invariants(src, target)
        self.assertTrue(any("dst_box x changed" in x for x in v))

    def test_break_that_did_not_move_is_flagged(self):
        src = _flow("p0_0", 0, 60.0, 660.0, 260.0, 720.0)
        target = copy.deepcopy(src)  # page/y unchanged
        v = assert_break_invariants(src, target)
        self.assertTrue(any("did not move" in x for x in v))

    def test_preserved_never_breaks(self):
        src = _code_entry("p0_0", 0, 60.0, 660.0, 260.0, 720.0)
        target = _moved_entry(src, target_page=1, page_start_y=10.0)
        v = assert_break_invariants(src, target)
        self.assertTrue(any("preserved block must never break" in x for x in v))

    def test_toc_page_number_not_duplicated(self):
        src = _toc("p0_0", 0, 60.0, 660.0, 260.0, 720.0)
        target = _moved_entry(src)
        target["toc_entries"][0]["continuation"] = ["continues 42 ..."]
        v = assert_break_invariants(src, target)
        self.assertTrue(
            any("page number became part of a continuation" in x for x in v)
        )

    def test_toc_page_run_count_unchanged(self):
        src = _toc("p0_0", 0, 60.0, 660.0, 260.0, 720.0)
        target = _moved_entry(src)
        target["render_payload"]["commands"].append(
            {"kind": "page", "text": "42", "x": 500.0, "y": 680.0, "width": 20.0}
        )
        v = assert_break_invariants(src, target)
        self.assertTrue(any("page-number run count changed" in x for x in v))


# ---------------------------------------------------------------------------
# 6. architecture purity — contract only, no detection / layout / rendering
# ---------------------------------------------------------------------------


def _code(path: Path) -> str:
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


class TestPageBreakArchitecture(unittest.TestCase):
    def test_page_break_is_contract_only(self):
        src = _code(_PAGE_BREAK_PATH)
        for banned in (
            "lay_out(",
            "adaptive_layout(",
            "wrap_lines(",
            "shrink_to_fit(",
            "clip_text(",
            "detect_page_collisions(",
            "detect_page_overflows(",
            "build_page_flow_report(",
            "resolve_page_shifts(",
            "apply_page_shifts(",
        ):
            self.assertNotIn(banned, src, f"page_break.py 不得执行/复用: {banned}")
        for banned in (
            "list_detector",
            "list_parser",
            "toc_parser",
            "code_detector",
            "style_detector",
            "looks_like_",
            "semantic.renderer",
            "translator",
            "magicpdf",
        ):
            self.assertNotIn(banned, src, f"page_break.py 不得引用: {banned}")

    def test_page_break_never_derives_geometry_from_level_index(self):
        tree = ast.parse(_code(_PAGE_BREAK_PATH))
        for node in ast.walk(tree):
            if not isinstance(node, ast.BinOp):
                continue
            names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            if names & {"level", "index"}:
                raise AssertionError(
                    f"page_break.py 用 {type(node.op).__name__} 重建几何"
                )

    def test_page_break_defines_no_adaptive_executor(self):
        tree = ast.parse(_PAGE_BREAK_PATH.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name.startswith("adaptive"):
                raise AssertionError(
                    f"page_break.py 定义了第二个 executor: {node.name}"
                )

    def test_page_break_never_mutates_a_plan(self):
        # no plan write: the module never assigns entry geometry fields
        src = _code(_PAGE_BREAK_PATH)
        for banned in ('"dst_box"] =', '"src_box"] =', '"page"] =', "entry["):
            self.assertNotIn(banned, src, f"page_break.py 不得写 plan: {banned}")

    def test_page_break_consumes_settled_geometry_only(self):
        src = _code(_PAGE_BREAK_PATH)
        self.assertIn("resolved_bbox", src)  # consumes settled extents
        self.assertIn("BlockShiftDecision", src)  # consumes 8c decisions


if __name__ == "__main__":
    unittest.main()
