# -*- coding: utf-8 -*-
"""Commit 7F-8a — Cross-block / Cross-page Vertical Collision Detection.

Locks the first step of the cross-block recovery phase: pure **detection** of
spatial relationships between already-settled blocks, with **zero** movement,
**zero** re-layout, and **zero** PDF-output change.

    settled render plan
        ↓ page_flow.py (7F-8a)
    BlockPlacement / PageCollision / PageOverflow / PageFlowReport

Locked guarantees:

1. **DoD answerable** — a collision record states page / upper block / lower
   block / overlap (pt) / required_shift (pt); page overflow records state the
   direction and amount.
2. **Five cases distinguished** — normal adjacency (no record), real overlap
   (``overlap``), preserved-region collision (``preserved_region`` — immovable),
   continuation collision (``continuation``), and page-boundary overflow
   (``PageOverflow``).
3. **Coordinate-agnostic** — boxes are normalized bottom/top, so the same
   geometry in y-up and y-down produces identical collisions.
4. **Read-only** — detection never mutates the plan, never re-lays-out, never
   touches a renderer, never changes any PDF output.
5. **Wiring** — ``--debug-layout`` gains a ``page_flow`` section in
   ``debug/layout.json`` without changing the 7F-7 diagnostics schema.
"""

import ast
import copy
import json
import tempfile
import unittest
from pathlib import Path

import pymupdf

from pdf2zh.semantic.layout.page_flow import (
    BlockPlacement,
    PageCollision,
    PageOverflow,
    PageFlowReport,
    build_page_flow_report,
    detect_page_collisions,
    detect_page_overflows,
    placements_from_plan,
)

_HERE = Path(__file__).resolve().parent
_PAGE_FLOW_PATH = _HERE.parent / "pdf2zh" / "semantic" / "layout" / "page_flow.py"


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
):
    """One settled render-plan entry (v3 y-up boxes: y0 bottom, y1 top)."""
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
    }


def _flow(block_id, page, x0, y0, x1, y1):
    return _entry(block_id, page, "flow", x0, y0, x1, y1)


# ---------------------------------------------------------------------------
# 1. BlockPlacement — pure read of the settled plan
# ---------------------------------------------------------------------------


class TestBlockPlacement(unittest.TestCase):
    def test_placements_from_plan_shape(self):
        plan = [_flow("p1_0", 1, 60.0, 100.0, 160.0, 130.0)]
        placements = placements_from_plan(plan)
        self.assertEqual(len(placements), 1)
        p = placements[0]
        self.assertIsInstance(p, BlockPlacement)
        self.assertEqual(p.page, 1)
        # 7F-9.1: block_index is the per-page reading-order ordinal (0 here),
        # never parsed from the block_id string — for canonical p{page}_{i}
        # ids the two agree, but the ordinal is the structured source.
        self.assertEqual(p.block_index, 0)
        self.assertEqual(p.kind, "flow")
        self.assertEqual(p.bbox, (60.0, 100.0, 160.0, 130.0))
        self.assertEqual(p.resolved_bbox, (60.0, 100.0, 160.0, 130.0))
        self.assertEqual(p.height, 30.0)
        self.assertFalse(p.preserved)
        self.assertFalse(p.has_continuation)
        d = p.to_dict()
        self.assertEqual(
            set(d),
            {
                "page",
                "block_index",
                "kind",
                "bbox",
                "resolved_bbox",
                "height",
                "preserved",
                "has_continuation",
            },
        )
        json.dumps(d)  # serializable

    def test_preserved_flag_for_code_formula(self):
        plan = [
            _entry("p1_0", 1, "code", 60.0, 100.0, 160.0, 120.0),
            _entry("p1_1", 1, "formula", 60.0, 80.0, 160.0, 95.0),
            _entry("p1_2", 1, "toc", 60.0, 60.0, 160.0, 75.0),
        ]
        flags = [p.preserved for p in placements_from_plan(plan)]
        self.assertEqual(flags, [True, True, False])

    def test_continuation_flag_from_list_and_toc_payload(self):
        plan = [
            _entry(
                "p1_0",
                1,
                "list",
                60.0,
                620.0,
                300.0,
                700.0,
                list_items={"items": [{"continuation": ["wrapped line"]}]},
            ),
            _entry(
                "p1_1",
                1,
                "toc",
                60.0,
                600.0,
                300.0,
                616.0,
                toc_entries=[{"title": "T", "continuation": ["cont"]}],
            ),
            _entry(
                "p1_2",
                1,
                "list",
                60.0,
                580.0,
                300.0,
                596.0,
                list_items={"items": [{"continuation": []}]},
            ),
        ]
        flags = [p.has_continuation for p in placements_from_plan(plan)]
        self.assertEqual(flags, [True, True, False])

    def test_unparseable_block_id_gets_structured_ordinal(self):
        # 7F-9.1 regression: block_index comes from the plan's per-page
        # reading order, never from the block_id string — so non-`pX_<number>`
        # ids get distinct ordinals instead of silently collapsing to 0.
        plan = [
            _entry("weird", 1, "flow", 0.0, 0.0, 10.0, 10.0),
            _entry("block_abc", 1, "flow", 0.0, -20.0, 10.0, -10.0),
            _entry("p2_x", 2, "flow", 0.0, 0.0, 10.0, 10.0),
        ]
        indices = [p.block_index for p in placements_from_plan(plan)]
        self.assertEqual(indices, [0, 1, 0])  # per-page ordinals, never a 0-collapse
        # (page, block_index) keys are unique even though all three ids are
        # unparseable as `pX_<number>`
        keys = [(p.page, p.block_index) for p in placements_from_plan(plan)]
        self.assertEqual(len(keys), len(set(keys)))

    def test_empty_plan(self):
        self.assertEqual(placements_from_plan([]), [])
        report = build_page_flow_report([])
        self.assertEqual(report.summary()["collision_count"], 0)


# ---------------------------------------------------------------------------
# 2. adjacency cases — normal / overlap / page overflow / preserved / continuation
# ---------------------------------------------------------------------------


class TestCollisionDetection(unittest.TestCase):
    def test_normal_adjacency_gap_no_collision(self):
        # upper [700,750], lower [620,690] — 10pt gap (v3 y-up)
        plan = [
            _flow("p1_0", 1, 60.0, 700.0, 300.0, 750.0),
            _flow("p1_1", 1, 60.0, 620.0, 300.0, 690.0),
        ]
        self.assertEqual(detect_page_collisions(plan), [])

    def test_normal_adjacency_touching_no_collision(self):
        # upper [700,750], lower [650,700] — exactly touching, not overlapping
        plan = [
            _flow("p1_0", 1, 60.0, 700.0, 300.0, 750.0),
            _flow("p1_1", 1, 60.0, 650.0, 300.0, 700.0),
        ]
        self.assertEqual(detect_page_collisions(plan), [])

    def test_real_overlap_reported_with_exact_numbers(self):
        # upper [700,750], lower [660,720] — 20pt overlap
        plan = [
            _flow("p1_0", 1, 60.0, 700.0, 300.0, 750.0),
            _flow("p1_1", 1, 60.0, 660.0, 300.0, 720.0),
        ]
        collisions = detect_page_collisions(plan)
        self.assertEqual(len(collisions), 1)
        c = collisions[0]
        self.assertIsInstance(c, PageCollision)
        self.assertEqual(c.page, 1)
        self.assertEqual(c.upper.block_index, 0)
        self.assertEqual(c.lower.block_index, 1)
        self.assertEqual(c.overlap, 20.0)
        self.assertEqual(c.required_shift, 20.0)
        self.assertEqual(c.reason, "overlap")
        d = c.to_dict()
        self.assertEqual(
            set(d),
            {
                "page",
                "upper",
                "lower",
                "overlap",
                "required_shift",
                "reason",
                "bbox_mode",
            },
        )
        json.dumps(d)

    def test_required_shift_is_full_clearance_distance(self):
        # lower is contained inside upper's span [700,750]: overlap is only
        # the intersection (35pt) but clearing requires moving lower down past
        # upper's bottom edge entirely (45pt) — overlap and shift differ
        plan = [
            _flow("p1_0", 1, 60.0, 700.0, 300.0, 750.0),
            _flow("p1_1", 1, 60.0, 710.0, 300.0, 745.0),
        ]
        c = detect_page_collisions(plan)[0]
        self.assertEqual(c.overlap, 35.0)
        self.assertEqual(c.required_shift, 45.0)

    def test_side_by_side_blocks_never_collide(self):
        # different columns: horizontal overlap is zero
        plan = [
            _flow("p1_0", 1, 60.0, 700.0, 150.0, 750.0),
            _flow("p1_1", 1, 200.0, 650.0, 350.0, 700.0),
        ]
        self.assertEqual(detect_page_collisions(plan), [])

    def test_two_column_interleave_is_skipped(self):
        # reading order interleaves columns; the earlier block sits below the
        # later one, so the pair is not a stacked upper/lower relationship
        plan = [
            _flow("p1_0", 1, 60.0, 600.0, 150.0, 650.0),
            _flow("p1_1", 1, 200.0, 700.0, 350.0, 750.0),
        ]
        self.assertEqual(detect_page_collisions(plan), [])

    def test_cross_page_blocks_never_collide(self):
        plan = [
            _flow("p1_0", 1, 60.0, 700.0, 300.0, 750.0),
            _flow("p2_0", 2, 60.0, 660.0, 300.0, 720.0),
        ]
        self.assertEqual(detect_page_collisions(plan), [])

    def test_y_down_boxes_produce_identical_collisions(self):
        # same geometry written y-down (top edge = larger y0); normalization
        # must make the detection direction-agnostic
        plan = [
            _entry("p1_0", 1, "flow", 60.0, 750.0, 300.0, 700.0),
            _entry("p1_1", 1, "flow", 60.0, 720.0, 300.0, 660.0),
        ]
        c = detect_page_collisions(plan)[0]
        self.assertEqual(c.overlap, 20.0)
        self.assertEqual(c.required_shift, 20.0)

    def test_preserved_region_collision_reason(self):
        # a code block (immovable) overlapping a flow block
        plan = [
            _flow("p1_0", 1, 60.0, 700.0, 300.0, 750.0),
            _entry("p1_1", 1, "code", 60.0, 660.0, 300.0, 720.0),
        ]
        c = detect_page_collisions(plan)[0]
        self.assertEqual(c.reason, "preserved_region")

    def test_continuation_collision_reason(self):
        # a list block whose payload draws continuation lines (drawn extent
        # extends below its declared bbox) colliding with the next block
        plan = [
            _entry(
                "p1_0",
                1,
                "list",
                60.0,
                700.0,
                300.0,
                750.0,
                list_items={"items": [{"continuation": ["wrapped"]}]},
            ),
            _flow("p1_1", 1, 60.0, 660.0, 300.0, 720.0),
        ]
        c = detect_page_collisions(plan)[0]
        self.assertEqual(c.reason, "continuation")

    def test_reason_prefers_preserved_over_continuation(self):
        plan = [
            _entry(
                "p1_0",
                1,
                "list",
                60.0,
                700.0,
                300.0,
                750.0,
                list_items={"items": [{"continuation": ["wrapped"]}]},
            ),
            _entry("p1_1", 1, "formula", 60.0, 660.0, 300.0, 720.0),
        ]
        c = detect_page_collisions(plan)[0]
        self.assertEqual(c.reason, "preserved_region")


# ---------------------------------------------------------------------------
# 3. page-boundary overflow (7F-8c trigger, detected today)
# ---------------------------------------------------------------------------


class TestPageOverflow(unittest.TestCase):
    def test_block_below_page_bottom(self):
        plan = [_flow("p1_0", 1, 60.0, -20.0, 300.0, 50.0)]
        overflows = detect_page_overflows(plan, page_sizes={1: 792.0})
        self.assertEqual(len(overflows), 1)
        o = overflows[0]
        self.assertIsInstance(o, PageOverflow)
        self.assertEqual(o.direction, "bottom")
        self.assertEqual(o.amount, 20.0)
        self.assertEqual(o.block.block_index, 0)

    def test_block_above_page_top(self):
        plan = [_flow("p1_0", 1, 60.0, 700.0, 300.0, 810.0)]
        overflows = detect_page_overflows(plan, page_sizes={1: 792.0})
        self.assertEqual(len(overflows), 1)
        self.assertEqual(overflows[0].direction, "top")
        self.assertEqual(overflows[0].amount, 18.0)

    def test_no_page_sizes_means_no_boundary_check(self):
        plan = [_flow("p1_0", 1, 60.0, -20.0, 300.0, 50.0)]
        self.assertEqual(detect_page_overflows(plan), [])

    def test_within_page_no_overflow(self):
        plan = [_flow("p1_0", 1, 60.0, 10.0, 300.0, 700.0)]
        self.assertEqual(detect_page_overflows(plan, page_sizes={1: 792.0}), [])

    def test_overflow_record_json_safe(self):
        plan = [_flow("p1_0", 1, 60.0, -20.0, 300.0, 50.0)]
        d = detect_page_overflows(plan, page_sizes={1: 792.0})[0].to_dict()
        self.assertEqual(set(d), {"page", "block", "direction", "amount"})
        json.dumps(d)


# ---------------------------------------------------------------------------
# 4. report — summary + JSON shape
# ---------------------------------------------------------------------------


class TestPageFlowReport(unittest.TestCase):
    def test_report_summary_counts_and_by_reason(self):
        plan = [
            _flow("p1_0", 1, 60.0, 700.0, 300.0, 750.0),  # overlap
            _flow("p1_1", 1, 60.0, 660.0, 300.0, 720.0),
            _flow("p1_2", 1, 60.0, 500.0, 300.0, 550.0),  # preserved
            _entry("p1_3", 1, "code", 60.0, 460.0, 300.0, 520.0),
            _flow("p1_4", 1, 60.0, 300.0, 300.0, 350.0),  # continuation
            _entry(
                "p1_5",
                1,
                "list",
                60.0,
                260.0,
                300.0,
                320.0,
                list_items={"items": [{"continuation": ["x"]}]},
            ),
            _flow("p1_6", 1, 60.0, -10.0, 300.0, 40.0),  # bottom overflow
        ]
        report = build_page_flow_report(plan, page_sizes={1: 792.0})
        self.assertIsInstance(report, PageFlowReport)
        s = report.summary()
        self.assertEqual(s["blocks"], 7)
        self.assertEqual(s["collision_count"], 3)
        self.assertEqual(s["page_overflow_count"], 1)
        self.assertEqual(
            s["by_reason"], {"continuation": 1, "overlap": 1, "preserved_region": 1}
        )
        d = report.to_dict()
        self.assertEqual(set(d), {"placements", "collisions", "overflows", "summary"})
        json.dumps(d)  # serializable

    def test_detection_is_read_only(self):
        plan = [
            _flow("p1_0", 1, 60.0, 700.0, 300.0, 750.0),
            _flow("p1_1", 1, 60.0, 660.0, 300.0, 720.0),
        ]
        snapshot = copy.deepcopy(plan)
        build_page_flow_report(plan, page_sizes={1: 792.0})
        self.assertEqual(plan, snapshot)  # nothing mutated


# ---------------------------------------------------------------------------
# 5. architecture purity — pure read, no executor / detector / renderer
# ---------------------------------------------------------------------------


def _code(path: Path) -> str:
    """Executable code with docstrings stripped (prose must not trip guards)."""
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


class TestPageFlowArchitecture(unittest.TestCase):
    def test_page_flow_is_pure_read_no_execution(self):
        src = _code(_PAGE_FLOW_PATH)
        for banned in (
            "lay_out(",
            "adaptive_layout(",
            "wrap_lines(",
            "shrink_to_fit(",
            "clip_text(",
        ):
            self.assertNotIn(banned, src, f"page_flow.py 不得执行 {banned}")
        for banned in (
            "list_detector",
            "list_parser",
            "toc_parser",
            "code_detector",
            "style_detector",
            "semantic.renderer",
            "translator",
            "magicpdf",
        ):
            self.assertNotIn(banned, src, f"page_flow.py 不得引用 {banned}")

    def test_page_flow_never_derives_geometry_from_level_index(self):
        tree = ast.parse(_code(_PAGE_FLOW_PATH))
        for node in ast.walk(tree):
            if not isinstance(node, ast.BinOp):
                continue
            names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            if names & {"level", "index"}:
                raise AssertionError(
                    f"page_flow.py 用 {type(node.op).__name__} 重建几何"
                )

    def test_page_flow_defines_no_adaptive_executor(self):
        tree = ast.parse(_PAGE_FLOW_PATH.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name.startswith("adaptive"):
                raise AssertionError(f"page_flow.py 定义了第二个 executor: {node.name}")


# ---------------------------------------------------------------------------
# 6. --debug-layout wiring — page_flow section without schema changes
# ---------------------------------------------------------------------------


def _make_pdf(path: Path) -> None:
    """A PDF with flow + list + code so the debug run has real blocks."""
    doc = pymupdf.Document()
    p1 = doc.new_page(width=612, height=792)
    p1.insert_text((60, 100), "This is a translated paragraph that wraps")
    p1.insert_text((60, 130), "1. Alpha", fontsize=11)
    p1.insert_text((76, 150), "a. Beta", fontsize=11)
    p1.insert_text((60, 180), "def f(): return 42", fontsize=9, fontname="cour")
    doc.save(str(path))
    doc.close()


class TestDebugLayoutWiring(unittest.TestCase):
    def test_layout_json_gains_page_flow_section(self):
        from pdf2zh.semantic.layout_debug import dump_layout_debug

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.pdf"
            _make_pdf(src)
            payload = dump_layout_debug(str(src), str(Path(tmp) / "out"))
            out = Path(tmp) / "out" / "layout.json"
            data = json.loads(out.read_text(encoding="utf-8"))
            # 7F-7 diagnostics schema untouched
            self.assertEqual(data["schema_version"], 1)
            for d in data["diagnostics"]:
                self.assertEqual(
                    set(d),
                    {
                        "page",
                        "block_index",
                        "kind",
                        "primitive_kind",
                        "target",
                        "source_text",
                        "translated_text",
                        "bbox",
                        "resolved_bbox",
                        "overflow",
                        "recovery",
                        "trace",
                        "anchors",
                        "font_size",
                    },
                )
            # 7F-8a page_flow section present with the full report shape
            pf = data["page_flow"]
            self.assertEqual(
                set(pf), {"placements", "collisions", "overflows", "summary"}
            )
            self.assertEqual(pf["summary"]["blocks"], payload["summary"]["blocks"])
            self.assertIn("collision_count", pf["summary"])
            self.assertIn("page_overflow_count", pf["summary"])
            for p in pf["placements"]:
                self.assertEqual(
                    set(p),
                    {
                        "page",
                        "block_index",
                        "kind",
                        "bbox",
                        "resolved_bbox",
                        "height",
                        "preserved",
                        "has_continuation",
                    },
                )

    def test_debug_layout_cli_flag_still_registered(self):
        from pdf2zh.pdf2zh import create_parser

        args = create_parser().parse_args(["--debug-layout", "x.pdf"])
        self.assertTrue(args.debug_layout)


if __name__ == "__main__":
    unittest.main()
