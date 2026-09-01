# -*- coding: utf-8 -*-
"""Commit 7F-8c — Cross-block Recovery Decision (Page Recovery Matrix).

Turns the 8b collision *fact layer* into a *decision contract* — and nothing
more.  The Recovery Matrix is the golden contract of this commit::

    | primitive         | overlap | page overflow | preserved | decision          |
    | Flow              |   ✓    |      no       |    no     | SHIFT_DOWN        |
    | Flow              |   ✓    |     yes       |    no     | NEXT_PAGE         |
    | List content      |   ✓    |      no       |    no     | SHIFT_DOWN        |
    | List continuation |   ✓    |      no       |    no     | SHIFT_DOWN        |
    | TOC title         |   ✓    |      no       |    no     | SHIFT_DOWN        |
    | Code              |   ✓    |      any      |   yes     | PRESERVE_OVERFLOW |
    | TOC page column   |   ✓    |      any      |  fixed    | PRESERVE_OVERFLOW |
    | no collision      |   —    |       —       |    —      | KEEP              |

Locked guarantees (the 8c DoD):

1. **decision-only** — nothing moves, no geometry is modified, the plan is
   never mutated, no renderer / converter / ONNX is touched.
2. **single geometry authority** — ``shift_y`` is consumed verbatim from the
   8b ``PageCollision.required_shift``; ``lower.top - upper.bottom`` is never
   recomputed in the recovery layer.
3. **only Y recovery** — a decision carries only ``shift_y``; List
   marker_x/content_x/continuation_x and TOC title_x/page_x/continuation_x are
   untouched by construction (a block moves down as a whole).
4. **immovable hard boundary** — Code / PreservedRegion and the TOC page
   column → PRESERVE_OVERFLOW, never SHIFT_DOWN.
5. **explicit page overflow** — SHIFT_DOWN that would cross the page bottom
   becomes NEXT_PAGE, never a silent off-page shift.
6. **trace** — every decision carries the 8b collision snapshot, so JSON
   diagnostics answer "第 1 页 block 4 因为与 block 3 重叠 18.5pt，被建议向下
   移动 18.5pt" without re-running the PDF.
"""

import ast
import json
import tempfile
import unittest
from pathlib import Path

from pdf2zh.semantic.layout.page_flow import (
    PageCollision,
    detect_page_collisions,
)
from pdf2zh.semantic.layout.page_recovery import (
    BlockShiftDecision,
    PageRecoveryDecision,
    decide_block_shift,
    decide_page_recovery,
    decision_summary,
    keep_decision,
)

_HERE = Path(__file__).resolve().parent
_PAGE_RECOVERY_PATH = (
    _HERE.parent / "pdf2zh" / "semantic" / "layout" / "page_recovery.py"
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


def _list(block_id, page, x0, y0, x1, y1, continuation=False):
    items = [{"continuation": ["wrapped"]}] if continuation else [{"continuation": []}]
    return _entry(block_id, page, "list", x0, y0, x1, y1, list_items={"items": items})


def _toc(block_id, page, x0, y0, x1, y1):
    return _entry(
        block_id,
        page,
        "toc",
        x0,
        y0,
        x1,
        y1,
        toc_entries=[{"title": "T", "continuation": []}],
    )


def _overlap_pair(kind="flow", page=1, x0=60.0):
    """Two blocks that genuinely overlap (source geometry) on one page."""
    upper = _flow("p%d_0" % page, page, x0, 700.0, 300.0, 750.0)
    if kind == "list":
        lower = _list("p%d_1" % page, page, x0, 660.0, 300.0, 720.0)
    elif kind == "list_continuation":
        lower = _list("p%d_1" % page, page, x0, 660.0, 300.0, 720.0, continuation=True)
    elif kind == "toc":
        lower = _toc("p%d_1" % page, page, x0, 660.0, 300.0, 720.0)
    elif kind == "code":
        lower = _entry("p%d_1" % page, page, "code", x0, 660.0, 300.0, 720.0)
    elif kind == "column":
        lower = _entry("p%d_1" % page, page, "column", x0, 660.0, 300.0, 720.0)
    else:
        lower = _flow("p%d_1" % page, page, x0, 660.0, 300.0, 720.0)
    return [upper, lower]


# ---------------------------------------------------------------------------
# 1. decision contract — enum + BlockShiftDecision shape
# ---------------------------------------------------------------------------


class TestDecisionContract(unittest.TestCase):
    def test_enum_values(self):
        self.assertEqual(
            [d.value for d in PageRecoveryDecision],
            ["keep", "shift_down", "next_page", "preserve_overflow"],
        )

    def test_decision_to_dict_trace_shape(self):
        plan = _overlap_pair()
        collision = detect_page_collisions(plan)[0]
        d = decide_block_shift(collision, page_height=792.0)
        self.assertIsInstance(d, BlockShiftDecision)
        out = d.to_dict()
        # the user's recovery-trace JSON shape: collision (8b) + recovery (8c)
        self.assertEqual(
            set(out),
            {"page", "block_index", "target", "collision", "recovery"},
        )
        self.assertEqual(
            set(out["collision"]),
            {"upper", "lower", "overlap", "required_shift", "bbox_mode"},
        )
        self.assertEqual(set(out["recovery"]), {"decision", "shift_y", "reason"})
        self.assertEqual(out["block_index"], 1)
        self.assertEqual(out["target"], "lower")
        json.dumps(out)  # serializable

    def test_keep_decision_shape(self):
        from pdf2zh.semantic.layout.page_flow import placements_from_plan

        p = placements_from_plan(_overlap_pair())[0]
        k = keep_decision(p)
        self.assertEqual(k.decision, PageRecoveryDecision.KEEP)
        self.assertEqual(k.shift_y, 0.0)
        self.assertEqual(k.reason, "none")
        self.assertIsNone(k.collision)
        out = k.to_dict()
        self.assertEqual(out["recovery"]["decision"], "keep")


# ---------------------------------------------------------------------------
# 2. the Recovery Matrix — golden contract
# ---------------------------------------------------------------------------


class TestRecoveryMatrix(unittest.TestCase):
    def _one(self, plan, page_height=792.0):
        collision = detect_page_collisions(plan)[0]
        return decide_block_shift(collision, page_height=page_height)

    def test_flow_overlap_no_page_overflow_shift_down(self):
        d = self._one(_overlap_pair("flow"))
        self.assertEqual(d.decision, PageRecoveryDecision.SHIFT_DOWN)
        self.assertEqual(d.shift_y, 20.0)

    def test_flow_overlap_page_overflow_next_page(self):
        # lower block sits at the page bottom edge: any downward shift leaves
        # the page -> NEXT_PAGE (page height 18 fits the upper block only)
        plan = [
            _flow("p1_0", 1, 60.0, 8.0, 260.0, 18.0),
            _flow("p1_1", 1, 60.0, 0.0, 260.0, 10.0),
        ]
        d = self._one(plan, page_height=18.0)
        self.assertEqual(d.decision, PageRecoveryDecision.NEXT_PAGE)
        self.assertEqual(d.shift_y, 2.0)  # carries the required amount

    def test_list_content_shift_down(self):
        d = self._one(_overlap_pair("list"))
        self.assertEqual(d.decision, PageRecoveryDecision.SHIFT_DOWN)
        self.assertEqual(d.shift_y, 20.0)

    def test_list_continuation_shift_down(self):
        # movable, but only Y: the whole list block moves down as a unit
        d = self._one(_overlap_pair("list_continuation"))
        self.assertEqual(d.decision, PageRecoveryDecision.SHIFT_DOWN)
        self.assertEqual(d.reason, "continuation")
        self.assertEqual(d.shift_y, 20.0)

    def test_toc_title_shift_down(self):
        # page_x / title_x untouched by construction — only shift_y is decided
        d = self._one(_overlap_pair("toc"))
        self.assertEqual(d.decision, PageRecoveryDecision.SHIFT_DOWN)
        self.assertEqual(d.shift_y, 20.0)

    def test_code_preserve_overflow_never_shift(self):
        for ph in (None, 792.0, 18.0):  # page overflow irrelevant: immovable
            d = self._one(_overlap_pair("code"), page_height=ph)
            self.assertEqual(d.decision, PageRecoveryDecision.PRESERVE_OVERFLOW)
            self.assertEqual(d.shift_y, 0.0)
            self.assertEqual(d.reason, "preserved_region")

    def test_toc_page_column_preserve_overflow_never_shift(self):
        d = self._one(_overlap_pair("column"))
        self.assertEqual(d.decision, PageRecoveryDecision.PRESERVE_OVERFLOW)
        self.assertEqual(d.shift_y, 0.0)
        self.assertEqual(d.reason, "preserved_region")

    def test_no_collision_keep(self):
        plan = [
            _flow("p1_0", 1, 60.0, 700.0, 260.0, 728.0),
            _flow("p1_1", 1, 60.0, 620.0, 260.0, 648.0),
        ]
        self.assertEqual(detect_page_collisions(plan), [])
        # decide_page_recovery emits nothing (KEEP is the implicit absence)
        self.assertEqual(decide_page_recovery(plan), [])


# ---------------------------------------------------------------------------
# 3. single geometry authority — shift_y comes from 8b, never recomputed
# ---------------------------------------------------------------------------


class TestSingleAuthority(unittest.TestCase):
    def test_shift_y_consumes_8b_required_shift_not_overlap(self):
        # contained case: overlap 35 but required_shift 45 — the decision must
        # carry 45 (the 8b number), never re-derive 35 from the boxes
        plan = [
            _flow("p1_0", 1, 60.0, 700.0, 260.0, 750.0),
            _flow("p1_1", 1, 60.0, 710.0, 260.0, 745.0),
        ]
        collision = detect_page_collisions(plan)[0]
        self.assertEqual(collision.overlap, 35.0)
        self.assertEqual(collision.required_shift, 45.0)
        d = decide_block_shift(collision, page_height=792.0)
        self.assertEqual(d.decision, PageRecoveryDecision.SHIFT_DOWN)
        self.assertEqual(d.shift_y, 45.0)

    def test_recovery_layer_never_recomputes_geometry(self):
        src = _code(_PAGE_RECOVERY_PATH)
        # no lower.top - upper.bottom style arithmetic in the recovery layer
        for banned in (
            "required_shift =",
            "top - ",
            "bottom + ",
            "upper.bottom",
            "lower.top",
        ):
            self.assertNotIn(banned, src, f"page_recovery.py 不得重算几何: {banned}")

    def test_shift_y_is_zero_when_immovable(self):
        d = self._shift_for("code")
        self.assertEqual(d.shift_y, 0.0)

    @staticmethod
    def _shift_for(kind):
        plan = _overlap_pair(kind)
        collision = detect_page_collisions(plan)[0]
        return decide_block_shift(collision, page_height=792.0)


# ---------------------------------------------------------------------------
# 4. NEXT_PAGE boundary — exactly at the page bottom is still SHIFT_DOWN
# ---------------------------------------------------------------------------


class TestNextPageBoundary(unittest.TestCase):
    def test_touching_page_bottom_is_still_shift_down(self):
        # lower bottom 2 - shift 2 = 0 -> lands exactly on the edge: SHIFT_DOWN
        plan = [
            _flow("p1_0", 1, 60.0, 10.0, 260.0, 18.0),
            _flow("p1_1", 1, 60.0, 2.0, 260.0, 12.0),
        ]
        c = detect_page_collisions(plan)[0]
        self.assertEqual(c.required_shift, 2.0)
        d = decide_block_shift(c, page_height=18.0)
        self.assertEqual(d.decision, PageRecoveryDecision.SHIFT_DOWN)

    def test_crossing_page_bottom_is_next_page(self):
        plan = [
            _flow("p1_0", 1, 60.0, 8.0, 260.0, 18.0),
            _flow("p1_1", 1, 60.0, 0.0, 260.0, 10.0),
        ]
        c = detect_page_collisions(plan)[0]
        d = decide_block_shift(c, page_height=18.0)
        self.assertEqual(d.decision, PageRecoveryDecision.NEXT_PAGE)

    def test_missing_page_height_defaults_shift_down(self):
        plan = _overlap_pair()
        c = detect_page_collisions(plan)[0]
        d = decide_block_shift(c, page_height=None)
        self.assertEqual(d.decision, PageRecoveryDecision.SHIFT_DOWN)

    def test_page_overflow_uses_resolved_extent(self):
        # 8b resolved extent (translation-inflated) decides the boundary, not
        # the declared box: drawn bottom 669.5, shift 18.5 stays in a tall page
        from pdf2zh.semantic.layout.page_flow import placements_from_plan

        payload = {
            "kind": "flow",
            "font_size": 10.0,
            "commands": [
                {"kind": "flow-text", "y": 690.0},
                {"kind": "flow-text", "y": 676.0},
                {"kind": "flow-text", "y": 662.0},
                {"kind": "flow-text", "y": 648.0},
            ],
        }
        plan = [
            _entry("p1_0", 1, "flow", 60.0, 700.0, 260.0, 728.0, payload=payload),
            _flow("p1_1", 1, 60.0, 660.0, 260.0, 688.0),
        ]
        collision = detect_page_collisions(plan)[0]
        # drawn bottom = min y (648) - 0.25*10 descent = 645.5;
        # required_shift = B.top (688) - A resolved bottom (645.5) = 42.5
        self.assertEqual(collision.required_shift, 42.5)
        upper = placements_from_plan(plan)[0]
        self.assertAlmostEqual(upper.resolved_bbox[1], 645.5, places=1)
        # 645.5 - 42.5 = 603 >= 0 -> SHIFT_DOWN on a 792pt page
        d = decide_block_shift(collision, page_height=792.0)
        self.assertEqual(d.decision, PageRecoveryDecision.SHIFT_DOWN)


# ---------------------------------------------------------------------------
# 5. decide_page_recovery over a plan + summary
# ---------------------------------------------------------------------------


class TestDecidePageRecovery(unittest.TestCase):
    def test_one_decision_per_collision(self):
        plan = _overlap_pair() + [
            _flow("p1_2", 1, 60.0, 500.0, 260.0, 550.0),
            _flow("p1_3", 1, 60.0, 460.0, 260.0, 520.0),
        ]
        decisions = decide_page_recovery(plan, page_sizes={1: 792.0})
        self.assertEqual(len(decisions), 2)
        self.assertTrue(
            all(d.decision is PageRecoveryDecision.SHIFT_DOWN for d in decisions)
        )
        s = decision_summary(decisions)
        self.assertEqual(s["total"], 2)
        self.assertEqual(s["by_decision"], {"shift_down": 2})

    def test_cross_page_isolated(self):
        plan = [
            _flow("p1_0", 1, 60.0, 700.0, 260.0, 750.0),
            _flow("p1_1", 1, 60.0, 660.0, 260.0, 720.0),
            _flow("p2_0", 2, 60.0, 700.0, 260.0, 750.0),
            _flow("p2_1", 2, 60.0, 660.0, 260.0, 720.0),
        ]
        decisions = decide_page_recovery(plan, page_sizes={1: 792.0, 2: 792.0})
        self.assertEqual(len(decisions), 2)
        self.assertEqual({d.page for d in decisions}, {1, 2})

    def test_summary_by_decision_mixed(self):
        plan = [
            _flow("p1_0", 1, 60.0, 700.0, 260.0, 750.0),  # shift_down
            _flow("p1_1", 1, 60.0, 660.0, 260.0, 720.0),
            _flow("p1_2", 1, 60.0, 500.0, 260.0, 550.0),  # preserve (code)
            _entry("p1_3", 1, "code", 60.0, 460.0, 260.0, 520.0),
        ]
        decisions = decide_page_recovery(plan, page_sizes={1: 792.0})
        s = decision_summary(decisions)
        self.assertEqual(s["total"], 2)
        self.assertEqual(s["by_decision"], {"preserve_overflow": 1, "shift_down": 1})


# ---------------------------------------------------------------------------
# 6. debug/layout.json wiring — page_recovery section
# ---------------------------------------------------------------------------


def _make_pdf(path: Path) -> None:
    import pymupdf

    doc = pymupdf.Document()
    p1 = doc.new_page(width=612, height=792)
    p1.insert_text((60, 100), "This is a translated paragraph that wraps")
    p1.insert_text((60, 130), "1. Alpha", fontsize=11)
    p1.insert_text((60, 180), "def f(): return 42", fontsize=9, fontname="cour")
    doc.save(str(path))
    doc.close()


class TestDebugLayoutWiring(unittest.TestCase):
    def test_layout_json_gains_page_recovery_section(self):
        from pdf2zh.semantic.layout_debug import dump_layout_debug

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.pdf"
            _make_pdf(src)
            payload = dump_layout_debug(str(src), str(Path(tmp) / "out"))
            out = Path(tmp) / "out" / "layout.json"
            data = json.loads(out.read_text(encoding="utf-8"))
            pr = data["page_recovery"]
            self.assertEqual(set(pr), {"decisions", "summary"})
            self.assertEqual(set(pr["summary"]), {"total", "by_decision"})
            self.assertEqual(pr["summary"]["total"], len(pr["decisions"]))
            for d in pr["decisions"]:
                self.assertEqual(
                    set(d), {"page", "block_index", "target", "collision", "recovery"}
                )
            # 7F-7 diagnostics schema untouched
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

    def test_debug_layout_cli_flag_still_registered(self):
        from pdf2zh.pdf2zh import create_parser

        args = create_parser().parse_args(["--debug-layout", "x.pdf"])
        self.assertTrue(args.debug_layout)


# ---------------------------------------------------------------------------
# 7. architecture purity — decision only, no executor / detector / renderer
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


class TestPageRecoveryArchitecture(unittest.TestCase):
    def test_page_recovery_is_decision_only(self):
        src = _code(_PAGE_RECOVERY_PATH)
        for banned in (
            "lay_out(",
            "adaptive_layout(",
            "wrap_lines(",
            "shrink_to_fit(",
            "clip_text(",
            "dst_box[",
            "src_box[",
        ):
            self.assertNotIn(banned, src, f"page_recovery.py 不得执行/修改: {banned}")
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
            self.assertNotIn(banned, src, f"page_recovery.py 不得引用: {banned}")

    def test_page_recovery_never_derives_geometry_from_level_index(self):
        tree = ast.parse(_code(_PAGE_RECOVERY_PATH))
        for node in ast.walk(tree):
            if not isinstance(node, ast.BinOp):
                continue
            names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            if names & {"level", "index"}:
                raise AssertionError(
                    f"page_recovery.py 用 {type(node.op).__name__} 重建几何"
                )

    def test_page_recovery_defines_no_adaptive_executor(self):
        tree = ast.parse(_PAGE_RECOVERY_PATH.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name.startswith("adaptive"):
                raise AssertionError(
                    f"page_recovery.py 定义了第二个 executor: {node.name}"
                )

    def test_page_recovery_consumes_8b_collision(self):
        src = _code(_PAGE_RECOVERY_PATH)
        self.assertIn("required_shift", src)  # consumes the 8b field
        self.assertIn("PageCollision", src)  # typed against the 8b diagnosis


if __name__ == "__main__":
    unittest.main()
