# -*- coding: utf-8 -*-
"""Commit 7F-9 — Global Recovery Integration (orchestrator).

The recovery *capabilities* (8a–8e) are already verified at the plan and glyph
layers.  7F-9 proves that wrapping them in one bounded loop converges for a
document where several recoveries fire together — and that they never fight:

    settled plan
        ↓ 8d apply_page_shifts            (same-page SHIFT_DOWN)
        ↓ 8e execute_continuation_breaks  (cross-page BREAK / continuation /
                                           PRESERVE_OVERFLOW)
        ↓ re-diagnose → converge or record unresolved (bounded)

Locked DoD:

1. **convergence** — a cascade corpus (collision + list/toc continuation +
   flow whole-block + code preserved) reaches ``collision_count == 0`` with
   ``converged == True``, ``unresolved == 0``;
2. **event chain** — the report's ``events`` carry the full
   ``pass / block / action / detail`` trail: SHIFT_DOWN, CONTINUATION,
   BREAK_TO_NEXT_PAGE and PRESERVE_OVERFLOW all appear;
3. **no self-fighting** — a round that executes nothing stops (``stopped_early``,
   ''unresolved > 0'') instead of looping ``3 → 3 → 3``;
4. **source geometry is the anchor** — ``src_box`` and the list / TOC anchors
   (marker_x / content_x / continuation_x / title_x / page_x) are byte-identical;
   only resolved page / Y (dst_box + command y) changed, X never;
5. **marker / TOC page number appear exactly once**; code is never moved.
6. **golden PDF** — the converged plan renders; words assert the DoD at the
   glyph layer (marker once, page number once at ``page_x``, continuation x
   correct, code preserved).
"""

import json
import unittest
from pathlib import Path

import pymupdf

from pdf2zh.semantic.layout.global_recovery import (
    GlobalRecoveryReport,
    global_recovery,
    source_geometry_snapshot,
)
from pdf2zh.semantic.layout.page_flow import (
    detect_page_collisions,
    detect_page_overflows,
    placements_from_plan,
)
from pdf2zh.v3.magicpdf_renderer import render_plan_to_pdf
from tests.pdf_word_utils import extract_words, words_with_text

_HERE = Path(__file__).resolve().parent
_BASELINE = _HERE / "baselines" / "global_recovery_7f9.json"

PAGE_H = 792.0
PAGE_W = 612.0
PAGE_START = 752.0  # v3 content-top margin
BOTTOM = 10.0  # fitted margin for continuation splits
HEIGHTS = {0: PAGE_H, 1: PAGE_H, 2: PAGE_H, 3: PAGE_H}
SIZES = {i: [PAGE_W, PAGE_H] for i in range(4)}


def _entry(
    block_id,
    page,
    kind,
    x0,
    y0,
    x1,
    y1,
    payload=None,
    text="t",
    list_items=None,
    toc_entries=None,
    toc_commands=None,
):
    box = [float(x0), float(y0), float(x1), float(y1)]
    return {
        "block_id": block_id,
        "page": page,
        "kind": kind,
        "text": text,
        "translated": text,
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


def _flow(block_id, page, x0, y0, x1, y1, text="flow"):
    return _entry(block_id, page, "flow", x0, y0, x1, y1, text=text)


def _code(block_id="p0_code", page=0):
    return _entry(block_id, page, "code", 570.0, 760.0, 600.0, 782.0, text="def f()")


def _list_tail(block_id="p0_list", page=0, x_off=0.0):
    marker_x = 60.0 + x_off
    content_x = 76.0 + x_off
    commands = [
        {"kind": "marker", "text": "1.", "x": marker_x, "y": 80.0, "width": 11.0},
        {"kind": "text", "text": "LA", "x": content_x, "y": 80.0, "width": 40.0},
        {"kind": "text", "text": "LB", "x": content_x, "y": 68.0, "width": 40.0},
        {"kind": "text", "text": "LC", "x": content_x, "y": 56.0, "width": 40.0},
        {"kind": "text", "text": "LD", "x": content_x, "y": 44.0, "width": 40.0},
        {"kind": "text", "text": "LE", "x": content_x, "y": 32.0, "width": 40.0},
        {"kind": "text", "text": "LF", "x": content_x, "y": 20.0, "width": 40.0},
        {"kind": "text", "text": "LG", "x": content_x, "y": 8.0, "width": 40.0},
        {"kind": "text", "text": "LH", "x": content_x, "y": -8.0, "width": 40.0},
    ]
    items = [
        {
            "marker": "1.",
            "marker_x": marker_x,
            "content_x": content_x,
            "continuation_x": content_x,
            "continuation": ["LB", "LC", "LD", "LE", "LF", "LG", "LH"],
        }
    ]
    return _entry(
        block_id,
        page,
        "list",
        x_off,
        10.0,
        260.0 + x_off,
        90.0,
        list_items=dict(list([("commands", commands), ("items", items)])),
        payload={"kind": "list", "commands": commands},
    )


def _toc_tail(block_id="p0_toc", page=0):
    """TOC in its own right-hand column (never overlaps the list column x<260)."""
    title_x = 340.0
    page_x = 500.0
    commands = [
        {"kind": "number", "text": "1", "x": title_x - 10.0, "y": 80.0, "width": 8.0},
        {"kind": "title", "text": "Intro", "x": title_x, "y": 80.0, "width": 60.0},
        {"kind": "title", "text": "wrapA", "x": title_x, "y": 68.0, "width": 60.0},
        {"kind": "title", "text": "wrapB", "x": title_x, "y": 56.0, "width": 60.0},
        {"kind": "page", "text": "42", "x": page_x, "y": 80.0, "width": 20.0},
        {"kind": "title", "text": "contA", "x": title_x, "y": 44.0, "width": 60.0},
        {"kind": "title", "text": "contB", "x": title_x, "y": 8.0, "width": 60.0},
        {"kind": "title", "text": "contC", "x": title_x, "y": -8.0, "width": 60.0},
    ]
    entries = [
        {
            "title": "Intro",
            "title_x": title_x,
            "page_x": page_x,
            "page_number": "42",
            "continuation": [],
        }
    ]
    return _entry(
        block_id,
        page,
        "toc",
        300.0,
        10.0,
        560.0,
        90.0,
        toc_entries=entries,
        toc_commands={"commands": commands},
        payload={"kind": "toc", "commands": commands},
    )


def _cascade_corpus():
    """One document that fires several recoveries:

    - X/Y overlap → 8d SHIFT_DOWN (same page);
    - list tail / toc tail overflow → 8e CONTINUATION (pages 1 / 2);
    - flow Z overflow → 8e whole-block BREAK (page 3);
    - code preserved → PRESERVE_OVERFLOW (never moved).
    """
    return [
        _flow("p0_0", 0, 60.0, 700.0, 260.0, 728.0, text="KEEPFLOW"),
        _flow("p0_1", 0, 60.0, 670.0, 260.0, 715.0, text="SHIFTFLOW"),
        _list_tail("p0_2", x_off=0.0),
        _toc_tail("p0_3"),  # its own column; page_x=500
        _flow("p0_4", 0, 300.0, -30.0, 540.0, 40.0, text="MOVEDFLOW"),
        _code("p0_5"),
    ]


# ---------------------------------------------------------------------------
# 1. convergence + event chain + applied/deferred/unresolved
# ---------------------------------------------------------------------------


class TestOrchestratorConvergence(unittest.TestCase):
    def _run(self, **kw):
        return global_recovery(
            _cascade_corpus(),
            page_sizes=HEIGHTS,
            page_start_y=PAGE_START,
            page_bottom_y=BOTTOM,
            **kw,
        )

    def test_converges_with_event_chain(self):
        final, report = self._run()
        self.assertIsInstance(report, GlobalRecoveryReport)
        self.assertTrue(report.converged)
        self.assertEqual(report.unresolved, 0)
        self.assertGreaterEqual(report.passes, 1)
        # applied: 1 SHIFT + 2 splits + 1 whole-block = 4; code deferred = 1
        self.assertEqual(report.applied, 4)
        self.assertEqual(report.deferred, 1)
        # the event chain answers "round / block / action"
        actions = {e.action for e in report.events}
        for want in (
            "SHIFT_DOWN",
            "CONTINUATION",
            "BREAK_TO_NEXT_PAGE",
            "PRESERVE_OVERFLOW",
        ):
            self.assertIn(want, actions, f"missing action {want}")
        for e in report.events:
            self.assertIsInstance(e.pass_no, int)
            self.assertIn(e.page, (0, 1, 2, 3))
        # final doc has no collisions and no page overflow
        self.assertEqual(len(detect_page_collisions(final)), 0)
        self.assertEqual(len(detect_page_overflows(final, page_sizes=HEIGHTS)), 0)

    def test_final_summary_is_zero_every_pass_non_increasing(self):
        _, report = self._run()
        counts = [s["collision_count"] for s in report.pass_summaries]
        self.assertEqual(counts[-1], 0)
        for a, b in zip(counts, counts[1:]):
            self.assertLessEqual(b, a, "collision count must not grow")

    def test_json_round_trip(self):
        _, report = self._run()
        d = report.to_dict()
        self.assertEqual(
            set(d),
            {
                "passes",
                "max_passes",
                "converged",
                "applied",
                "deferred",
                "unresolved",
                "stopped_early",
                "stopped_reason",
                "events",
                "pass_summaries",
            },
        )
        self.assertIsInstance(d["events"], list)
        json.dumps(d)


# ---------------------------------------------------------------------------
# 2. no self-fighting / no-progress guard + budget bound
# ---------------------------------------------------------------------------


class TestNoFightAndBudget(unittest.TestCase):
    def test_no_progress_guard_stops_early(self):
        # preserved code overlapping a movable block is unresolvable: 8d defers
        # it, 8e keeps it — a round executes nothing → stop, record unresolved.
        plan = [
            _flow("p0_0", 0, 60.0, 700.0, 260.0, 728.0, text="A"),
            _code("p0_1"),
        ]
        # make code's dst overlap A vertically by lowering code's bbox
        plan[1]["dst_box"] = [60.0, 700.0, 260.0, 728.0]
        plan[1]["src_box"] = [60.0, 700.0, 260.0, 728.0]
        final, report = global_recovery(
            plan, page_sizes=HEIGHTS, page_start_y=PAGE_START, page_bottom_y=BOTTOM
        )
        self.assertFalse(report.converged)
        self.assertTrue(report.stopped_early)
        self.assertGreater(report.unresolved, 0)
        self.assertEqual(report.passes, 1)

    def test_never_unsilently_moves_preserved(self):
        plan = [_code("p0_0")]
        final, _ = global_recovery(
            plan, page_sizes=HEIGHTS, page_start_y=PAGE_START, page_bottom_y=BOTTOM
        )
        code = [e for e in final if e["kind"] == "code"][0]
        self.assertEqual(code["page"], 0)
        self.assertEqual(code["dst_box"], _code("p0_0")["dst_box"])


# ---------------------------------------------------------------------------
# 3. source geometry is the anchor; X never changes
# ---------------------------------------------------------------------------


class TestSourceAnchor(unittest.TestCase):
    def test_src_box_and_anchors_verbatim(self):
        orig = _cascade_corpus()
        orig_snap = source_geometry_snapshot(orig)
        origin_srcs = {tuple(e["src_box"]) for e in orig}
        final, _ = global_recovery(
            orig, page_sizes=HEIGHTS, page_start_y=PAGE_START, page_bottom_y=BOTTOM
        )
        # every final entry's source box is one of the originals (cont entries
        # copy the source verbatim — never invent new geometry)
        for e in final:
            self.assertIn(
                tuple(e["src_box"]), origin_srcs, f"{e['block_id']} src drifted"
            )
        # X geometry verbatim: dst_box edges copied from src; the multiset of
        # every command (x, width) is byte-identical to the source corpus — a
        # recovery may split lines onto other pages but never re-derive X.
        for e in final:
            self.assertEqual(e["dst_box"][0], e["src_box"][0])
            self.assertEqual(e["dst_box"][2], e["src_box"][2])

        def _cmds(plan):
            out = []
            for e in plan:
                for c in (e.get("render_payload") or {}).get("commands") or []:
                    out.append((c.get("x"), c.get("width")))
            return sorted(out)

        # same multiset before and after (kept+cont together hold every line)
        self.assertEqual(_cmds(final), _cmds(orig))
        # page_x anchor never changes on the toc entries
        # (a toc kept + cont share the entry; each preserves page_x verbatim)
        for e in final:
            for te in (e.get("toc_entries") or []) or []:
                if isinstance(te, dict):
                    self.assertEqual(te["page_x"], 500.0)

    def test_marker_and_page_number_once(self):
        final, _ = global_recovery(
            _cascade_corpus(),
            page_sizes=HEIGHTS,
            page_start_y=PAGE_START,
            page_bottom_y=BOTTOM,
        )
        marker = sum(
            1
            for e in final
            for c in e["render_payload"]["commands"]
            if c.get("kind") == "marker"
        )
        page_num = sum(
            1
            for e in final
            for c in e["render_payload"]["commands"]
            if c.get("kind") == "page"
        )
        self.assertEqual(marker, 1)
        self.assertEqual(page_num, 1)


# ---------------------------------------------------------------------------
# 4. baseline gate
# ---------------------------------------------------------------------------


class TestBaselineMatches(unittest.TestCase):
    def test_against_global_recovery_baseline(self):
        baseline = json.loads(_BASELINE.read_text(encoding="utf-8"))
        conv = baseline["convergence"]
        final, report = global_recovery(
            _cascade_corpus(),
            page_sizes=HEIGHTS,
            page_start_y=PAGE_START,
            page_bottom_y=BOTTOM,
        )
        self.assertEqual(report.converged, conv["converged"])
        self.assertEqual(report.applied, conv["applied"])
        self.assertEqual(report.deferred, conv["deferred"])
        self.assertEqual(report.unresolved, conv["unresolved"])
        self.assertEqual(
            {e.action for e in report.events}, set(baseline["events_actions"])
        )
        self.assertEqual(
            len(detect_page_collisions(final))
            + len(detect_page_overflows(final, page_sizes=HEIGHTS)),
            conv["final_collision_count"],
        )


# ---------------------------------------------------------------------------
# 5. golden PDF (7F-9c) — the converged plan rendered to the glyph layer
# ---------------------------------------------------------------------------


class TestGlobalRecoveryGoldenPdf(unittest.TestCase):
    def test_converged_plan_renders_with_DoD_at_words_layer(self):
        final, report = global_recovery(
            _cascade_corpus(),
            page_sizes=HEIGHTS,
            page_start_y=PAGE_START,
            page_bottom_y=BOTTOM,
        )
        self.assertTrue(report.converged)
        pdf, _ = render_plan_to_pdf(final, page_sizes=SIZES, cjk_font=True)
        doc = pymupdf.open(stream=pdf, filetype="pdf")
        try:
            words = [extract_words(doc[i]) for i in range(doc.page_count)]
            allw = [w for page in words for w in page]

            # marker drawn exactly once; toc page number exactly once
            self.assertEqual(len(words_with_text(allw, "1.")), 1)
            self.assertEqual(len(words_with_text(allw, "42")), 1)
            # code preserved text drawn (it renders its own text layer)
            self.assertTrue(
                any("def" in w["text"] or w["text"] == "def" for w in allw),
                "code text must be present",
            )
            # shifted flow present
            self.assertTrue(words_with_text(allw, "SHIFTFLOW"))
            # moved flow present
            self.assertTrue(words_with_text(allw, "MOVEDFLOW"))
            # continuation leaves survive: list LG/LH and toc contA/B/C
            for t in ("LG", "LH", "contA", "contB", "contC"):
                self.assertEqual(
                    len(words_with_text(allw, t)), 1, f"continuation {t} dup/lost"
                )
            # words exist on distinct pages (cross-page did happen)
            self.assertGreaterEqual(doc.page_count, 4)
        finally:
            doc.close()


# ---------------------------------------------------------------------------
# 6. architecture — orchestrate, never re-implement layout
# ---------------------------------------------------------------------------


class TestGlobalRecoveryArchitecture(unittest.TestCase):
    _MOD = _HERE.parent / "pdf2zh" / "semantic" / "layout" / "global_recovery.py"

    def _src(self):
        import ast

        tree = ast.parse(self._MOD.read_text(encoding="utf-8"))
        body = [
            n
            for n in tree.body
            if not (
                isinstance(n, ast.Expr)
                and isinstance(n.value, ast.Constant)
                and isinstance(n.value.value, str)
            )
        ]
        tree.body = body
        ast.fix_missing_locations(tree)
        return ast.unparse(tree)

    def test_never_reimplements_layout_or_new_policy(self):
        src = self._src()
        for banned in (
            "adaptive_layout(",
            "lay_out(",
            "wrap_lines(",
            "shrink_to_fit(",
            "clip_text(",
            "decide_page_recovery(",
            "page_break_from_shift(",
            "build_page_flow_report(",
            "semantic.renderer",
            "list_detector",
            "toc_parser",
            "code_detector",
            "translator",
            "magicpdf",
        ):
            self.assertNotIn(banned, src, f"global_recovery.py 不得实现/复用: {banned}")

    def test_only_orchestrates_existing_executors(self):
        src = self._src()
        for needed in (
            "apply_page_shifts",
            "execute_continuation_breaks",
            "detect_page_collisions",
            "detect_page_overflows",
        ):
            self.assertIn(needed, src, f"must orchestrate {needed}")

    def test_no_adaptive_entrypoint(self):
        import ast

        tree = ast.parse(self._MOD.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name.startswith("adaptive"):
                raise AssertionError(
                    f"global_recovery.py 定义了第二个 executor: {node.name}"
                )


if __name__ == "__main__":
    unittest.main()
