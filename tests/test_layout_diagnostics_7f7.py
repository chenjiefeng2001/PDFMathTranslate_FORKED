# -*- coding: utf-8 -*-
"""Commit 7F-7 — Layout Observability / Diagnostics.

Locks the diagnostics chain that turns *settled* layout results into one
machine-readable, JSON-safe record (7F-7a/7b), plus:

- per-stage recovery **trace** on ``LayoutResult`` (7F-7d) — kept OUT of
  ``recovery`` / ``to_dict()`` so the 7F-6a contract stays byte-identical;
- the ``--debug-layout`` CLI entry writing ``debug/layout.json`` (7F-7c);
- a golden-gate projection over **stable fields only** (7F-7f) — schema /
  primitive kind / decision ladder / overflow flag / channel anchors — so
  font / PyMuPDF / unrelated-field churn never breaks the baseline.

Hard rules enforced here:

- a diagnostic is built ONLY from the settled render plan (never re-layout,
  never re-derives geometry from level/index);
- diagnostics.py has no detector / parser / translator / renderer imports;
- trace never changes the 7F-6a ``recovery`` dict shape.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

import pymupdf

from pdf2zh.semantic.layout.adaptive import adaptive_layout
from pdf2zh.semantic.layout.diagnostics import (
    LayoutDiagnostic,
    collect_layout_diagnostics,
    diagnostic_from_plan_entry,
    stable_fields,
    summarize_diagnostics,
)
from pdf2zh.semantic.layout.overflow import LayoutResult
from pdf2zh.semantic.layout.primitives import FlowText


#: CJK-aware measurer for layout runs (same estimate as the layout layer).
def _measure(text, font_size):
    cjk = sum(1 for ch in text if ord(ch) > 0x2E80)
    return (len(text) - cjk) * font_size * 0.5 + cjk * font_size


def _flow_entry(
    text,
    translated,
    x0=60.0,
    y0=100.0,
    x1=160.0,
    y1=130.0,
    font_size=11.0,
    block_id="p1_3",
    page=1,
):
    """A settled flow plan entry (payload built through the real pipeline)."""
    from pdf2zh.v3.flow_sidechannel import build_block_flow_payload
    from pdf2zh.v3.canonical_page import BlockModel, LineModel, SpanModel

    line = LineModel(text=text, baseline=0.0, x0=x0, y0=y0, x1=x1, y1=y1)
    line.spans.append(SpanModel(size=font_size, text=text, x0=x0, y0=y0, x1=x1, y1=y1))
    block = BlockModel(
        text=text,
        kind="paragraph",
        x0=x0,
        y0=y0,
        x1=x1,
        y1=y1,
        lines=[line],
        metadata={"translated": translated},
    )
    payload = build_block_flow_payload(block)
    return {
        "block_id": block_id,
        "page": page,
        "kind": "flow",
        "text": text,
        "translated": translated,
        "src_box": [x0, y0, x1, y1],
        "dst_box": [x0, y0, x1, y1],
        "font_size": font_size,
        "render_payload": payload,
    }


def _list_entry():
    from pdf2zh.semantic.renderer.list import build_page_list_plan

    plan = build_page_list_plan(
        ["1. Alpha", "   a. Beta"],
        geom=[
            {"x0": 60.0, "x1": 200.0, "size": 11.0, "y0": 700.0},
            {"x0": 76.0, "x1": 200.0, "size": 11.0, "y0": 680.0},
        ],
        translate=lambda s: "译_" + s,
    )
    return {
        "block_id": "p1_4",
        "page": 1,
        "kind": "list",
        "text": "1. Alpha",
        "translated": "译_Alpha",
        "src_box": [60.0, 620.0, 300.0, 700.0],
        "dst_box": [60.0, 620.0, 300.0, 700.0],
        "font_size": 11.0,
        "render_payload": {"kind": "list", "commands": plan["commands"]},
        "list_items": plan,
    }


def _toc_entry():
    from pdf2zh.semantic.renderer.toc import TocRenderer

    entries = [
        {
            "title": "Introduction",
            "title_only": "Introduction",
            "number": "",
            "level": 0,
            "page_number": "42",
            "title_x": 72.0,
            "page_x": 500.0,
            "indent": 72.0,
            "dot_leader": "........",
            "leader_present": True,
            "continuation": [],
            "bbox": [72.0, 0.0, 500.0, 16.0],
        },
        {
            "title": "Method",
            "title_only": "Method",
            "number": "",
            "level": 0,
            "page_number": "12",
            "title_x": 72.0,
            "page_x": 500.0,
            "indent": 72.0,
            "dot_leader": "........",
            "leader_present": True,
            "continuation": [],
            "bbox": [72.0, 0.0, 500.0, 16.0],
        },
    ]
    cmds = TocRenderer(measure_width=None).render(
        entries,
        ys=[750.0, 730.0],
        size=10.0,
        translate=lambda s: "译_" + s,
    )
    return {
        "block_id": "p1_5",
        "page": 1,
        "kind": "toc",
        "text": "Introduction",
        "translated": "译_Introduction",
        "src_box": [72.0, 700.0, 500.0, 760.0],
        "dst_box": [72.0, 700.0, 500.0, 760.0],
        "font_size": 10.0,
        "render_payload": {"kind": "toc", "commands": [c.to_dict() for c in cmds]},
        "toc_entries": entries,
    }


def _code_entry():
    return {
        "block_id": "p1_6",
        "page": 1,
        "kind": "code",
        "text": "def f(): return 42",
        "translated": "def f(): return 42",
        "src_box": [60.0, 251.6, 157.2, 262.9],
        "dst_box": [60.0, 251.6, 157.2, 262.9],
        "font_size": 9.0,
        "render_payload": {"kind": "preserve", "commands": []},
    }


def _mixed_plan():
    """Flow + List + TOC + Code — all four paths in one settled plan."""
    return [
        _flow_entry(
            "This is a translated paragraph that wraps over lines",
            "ALPHAFLOW " * 12,
            x0=60.0,
            x1=140.0,
            block_id="p1_0",
            page=1,
        ),
        _flow_entry("short", "short", x0=60.0, x1=300.0, block_id="p1_1", page=1),
        _list_entry(),
        _toc_entry(),
        _code_entry(),
    ]


# ---------------------------------------------------------------------------
# 1. trace on LayoutResult (7F-7d) — optional, never breaks 7F-6a contract
# ---------------------------------------------------------------------------


class TestRecoveryTrace(unittest.TestCase):
    def test_trace_recorded_per_stage_for_full_ladder(self):
        r = adaptive_layout(
            FlowText(
                text="X" * 200, origin=(0.0, 0.0), max_width=20.0, max_height=10.0
            ),
            measure=_measure,
            avail_width=20.0,
            avail_height=10.0,
            font_size=11.0,
        )
        self.assertTrue(r.overflow)
        self.assertEqual(r.recovery["decision"], "clip")
        # "X"*200 is one unbreakable token: WRAP is skipped, ladder is
        # SHRINK -> CLIP (exactly as the 7F-6b contract locks).
        self.assertEqual([t["decision"] for t in r.recovery_trace], ["SHRINK", "CLIP"])
        for t in r.recovery_trace:
            self.assertEqual(
                set(t), {"decision", "overflow", "line_count", "font_size"}
            )
        self.assertIsInstance(r.recovery_trace[-1]["font_size"], float)

    def test_trace_full_ladder_for_breakable_token(self):
        # a breakable token runs the whole WRAP -> SHRINK -> CLIP ladder
        r = adaptive_layout(
            FlowText(
                text="alpha beta gamma " * 40,
                origin=(0.0, 0.0),
                max_width=30.0,
                max_height=10.0,
            ),
            measure=_measure,
            avail_width=30.0,
            avail_height=10.0,
            font_size=11.0,
        )
        self.assertTrue(r.overflow)
        self.assertEqual(r.recovery["decision"], "clip")
        self.assertEqual(
            [t["decision"] for t in r.recovery_trace], ["WRAP", "SHRINK", "CLIP"]
        )

    def test_trace_empty_when_no_recovery(self):
        r = adaptive_layout(
            FlowText(text="hi", origin=(0.0, 0.0), max_width=200.0),
            measure=_measure,
            avail_width=200.0,
            font_size=11.0,
        )
        self.assertFalse(r.overflow)
        self.assertIsNone(r.recovery)
        self.assertEqual(r.recovery_trace, [])

    def test_trace_wrap_only_when_shrink_disabled(self):
        from pdf2zh.semantic.layout.recovery import LayoutBudget

        budget = LayoutBudget(allow_wrap=True, allow_shrink=False, allow_clip=False)
        r = adaptive_layout(
            FlowText(
                text="X" * 200, origin=(0.0, 0.0), max_width=20.0, max_height=10.0
            ),
            measure=_measure,
            avail_width=20.0,
            avail_height=10.0,
            font_size=11.0,
            budget=budget,
        )
        self.assertTrue(r.overflow)
        self.assertEqual(r.recovery["decision"], "preserve_overflow")
        # unbreakable token skips WRAP too -> no trace stages
        self.assertEqual(r.recovery_trace, [])

    def test_trace_wrap_only_when_shrink_disabled_breakable(self):
        from pdf2zh.semantic.layout.recovery import LayoutBudget

        budget = LayoutBudget(allow_wrap=True, allow_shrink=False, allow_clip=False)
        r = adaptive_layout(
            FlowText(
                text="alpha beta gamma " * 40,
                origin=(0.0, 0.0),
                max_width=30.0,
                max_height=10.0,
            ),
            measure=_measure,
            avail_width=30.0,
            avail_height=10.0,
            font_size=11.0,
            budget=budget,
        )
        self.assertTrue(r.overflow)
        self.assertEqual(r.recovery["decision"], "preserve_overflow")
        self.assertEqual([t["decision"] for t in r.recovery_trace], ["WRAP"])

    def test_trace_does_not_change_recovery_dict_or_to_dict(self):
        """7F-7d must keep the 7F-6a contract byte-identical."""
        r = adaptive_layout(
            FlowText(
                text="X" * 200, origin=(0.0, 0.0), max_width=20.0, max_height=10.0
            ),
            measure=_measure,
            avail_width=20.0,
            avail_height=10.0,
            font_size=11.0,
        )
        self.assertEqual(
            set(r.recovery),
            {"reason", "decision", "steps", "original_font_size", "final_font_size"},
        )
        d = r.to_dict()
        self.assertEqual(d["recovery"], r.recovery)  # no trace inside
        self.assertNotIn("trace", d["recovery"])


# ---------------------------------------------------------------------------
# 2. diagnostics schema + collector (7F-7a / 7F-7b)
# ---------------------------------------------------------------------------


class TestDiagnosticsSchema(unittest.TestCase):
    def test_flow_diagnostic_carries_settled_result(self):
        entry = _flow_entry("ALPHAFLOW " * 12, "ALPHAFLOW " * 12, x0=60.0, x1=120.0)
        diag = diagnostic_from_plan_entry(entry)
        self.assertEqual(diag.page, 1)
        self.assertEqual(diag.block_index, 3)
        self.assertEqual(diag.kind, "flow")
        self.assertEqual(diag.primitive_kind, "flow")
        self.assertEqual(diag.source_text, "ALPHAFLOW " * 12)
        self.assertEqual(diag.translated_text, "ALPHAFLOW " * 12)
        self.assertEqual(diag.bbox, (60.0, 100.0, 120.0, 130.0))
        self.assertEqual(diag.resolved_bbox, (60.0, 100.0, 120.0, 130.0))
        # JSON-safe, stable key set
        d = diag.to_dict()
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
        json.dumps(d)  # serializable

    def test_overflowing_flow_diagnostic_exposes_recovery_and_trace(self):
        entry = _flow_entry(
            "alpha beta gamma " * 40,
            "alpha beta gamma " * 40,
            x0=60.0,
            x1=100.0,
            font_size=12.0,
            block_id="p2_0",
            page=2,
        )
        diag = diagnostic_from_plan_entry(entry)
        self.assertTrue(diag.overflow)
        self.assertIsNotNone(diag.recovery)
        self.assertEqual(diag.recovery["decision"], "clip")
        self.assertEqual(diag.recovery["steps"], ["WRAP", "SHRINK", "CLIP"])
        self.assertEqual(
            [t["decision"] for t in diag.trace], ["WRAP", "SHRINK", "CLIP"]
        )
        self.assertEqual(diag.primitive_kind, "flow")

    def test_clean_flow_diagnostic_recovery_none(self):
        entry = _flow_entry("short", "short", x0=60.0, x1=300.0)
        diag = diagnostic_from_plan_entry(entry)
        self.assertFalse(diag.overflow)
        self.assertIsNone(diag.recovery)
        self.assertEqual(diag.trace, [])

    def test_list_diagnostic_copies_channel_anchors(self):
        diag = diagnostic_from_plan_entry(_list_entry())
        self.assertEqual(diag.kind, "list")
        self.assertEqual(diag.target, "list_item")
        self.assertAlmostEqual(diag.anchors["marker_x"], 60.0, places=1)
        # content_x comes from the settled payload verbatim (marker 60 + width)
        self.assertAlmostEqual(diag.anchors["content_x"], 71.0, places=1)

    def test_toc_diagnostic_copies_page_x_invariant(self):
        diag = diagnostic_from_plan_entry(_toc_entry())
        self.assertEqual(diag.kind, "toc")
        self.assertEqual(diag.target, "toc_entry")
        self.assertAlmostEqual(diag.anchors["title_x"], 72.0, places=1)
        self.assertAlmostEqual(diag.anchors["page_x"], 500.0, places=1)

    def test_code_diagnostic_preserved_kind(self):
        diag = diagnostic_from_plan_entry(_code_entry())
        self.assertEqual(diag.kind, "code")
        self.assertEqual(diag.primitive_kind, "preserved")
        self.assertFalse(diag.overflow)
        self.assertEqual(diag.bbox, (60.0, 251.6, 157.2, 262.9))

    def test_collector_over_mixed_plan(self):
        diags = collect_layout_diagnostics(_mixed_plan())
        kinds = {d.kind for d in diags}
        self.assertEqual(kinds, {"flow", "list", "toc", "code"})
        self.assertEqual(len(diags), 5)

    def test_summary_counts(self):
        plan = _mixed_plan()
        # add one overflowing pathological block
        plan.append(
            _flow_entry(
                "A" * 500,
                "A" * 500,
                x0=60.0,
                x1=100.0,
                font_size=12.0,
                block_id="p2_9",
                page=2,
            )
        )
        diags = collect_layout_diagnostics(plan)
        s = summarize_diagnostics(diags)
        self.assertEqual(s["blocks"], 6)
        self.assertGreaterEqual(s["overflow"], 1)
        self.assertGreaterEqual(s["recovered"], 1)


# ---------------------------------------------------------------------------
# 3. stable fields — the golden-gate projection (7F-7f)
# ---------------------------------------------------------------------------


class TestStableFields(unittest.TestCase):
    def test_stable_fields_are_architectural_only(self):
        diag = diagnostic_from_plan_entry(_toc_entry())
        s = stable_fields(diag)
        self.assertEqual(s["schema_version"], 1)
        self.assertEqual(s["kind"], "toc")
        self.assertEqual(s["primitive_kind"], "toc")
        self.assertEqual(s["target"], "toc_entry")
        self.assertIn("page_x", s["anchors"])
        # noisy fields excluded
        for noisy in ("source_text", "translated_text", "bbox", "trace", "font_size"):
            self.assertNotIn(noisy, s)

    def test_stable_fields_ladder_for_overflow(self):
        entry = _flow_entry(
            "alpha beta gamma " * 40,
            "alpha beta gamma " * 40,
            x0=60.0,
            x1=100.0,
            font_size=12.0,
        )
        s = stable_fields(diagnostic_from_plan_entry(entry))
        self.assertTrue(s["overflow"])
        self.assertEqual(s["recovery_decision"], "clip")
        self.assertEqual(s["recovery_steps"], ["WRAP", "SHRINK", "CLIP"])


# ---------------------------------------------------------------------------
# 4. --debug-layout CLI entry (7F-7c)
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


class TestDebugLayoutCli(unittest.TestCase):
    def test_dump_layout_debug_writes_json(self):
        from pdf2zh.semantic.layout_debug import dump_layout_debug

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.pdf"
            _make_pdf(src)
            payload = dump_layout_debug(str(src), str(Path(tmp) / "out"))
            out = Path(tmp) / "out" / "layout.json"
            self.assertTrue(out.exists())
            data = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(data["schema_version"], 1)
            self.assertEqual(data["summary"]["blocks"], payload["summary"]["blocks"])
            self.assertIn("diagnostics", data)
            # every diagnostic is the stable schema
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

    def test_cli_flag_registered(self):
        from pdf2zh.pdf2zh import create_parser

        args = create_parser().parse_args(["--debug-layout", "x.pdf"])
        self.assertTrue(args.debug_layout)


# ---------------------------------------------------------------------------
# 4b. golden diagnostic gate (7F-7f) — stable fields only
# ---------------------------------------------------------------------------


class TestGoldenDiagnosticGate(unittest.TestCase):
    """7F-7f: lock the diagnostic chain against a committed baseline.

    Only ``stable_fields`` are compared — schema / primitive kind / decision
    ladder / overflow flag / channel anchors — so font, PyMuPDF version and
    unrelated-field churn cannot break the gate.
    """

    def _baseline_path(self):
        return Path(__file__).parent / "baselines" / "layout_diagnostics_7f7.json"

    def test_mixed_plan_matches_baseline(self):
        plan = _mixed_plan()
        plan.append(
            _flow_entry(
                "A" * 500,
                "A" * 500,
                x0=60.0,
                x1=100.0,
                font_size=12.0,
                block_id="p2_9",
                page=2,
            )
        )
        diags = collect_layout_diagnostics(plan)
        actual = {
            "schema_version": 1,
            "blocks": [stable_fields(d) for d in diags],
            "summary": {
                "blocks": len(diags),
                "overflow": sum(1 for d in diags if d.overflow),
                "recovered": sum(1 for d in diags if d.recovery),
            },
        }
        baseline = json.loads(self._baseline_path().read_text(encoding="utf-8"))
        self.assertEqual(actual, baseline)

    def test_baseline_locks_toc_page_x_and_ladder(self):
        baseline = json.loads(self._baseline_path().read_text(encoding="utf-8"))
        by_kind = {b["kind"]: b for b in baseline["blocks"]}
        self.assertEqual(by_kind["toc"]["anchors"]["page_x"], 500.0)
        self.assertEqual(by_kind["list"]["anchors"]["marker_x"], 60.0)
        # 7I-5C: a wrapable flow block now re-wraps to a fit (no CLIP), and a
        # genuinely unbreakable flow block stays a terminal CLIP.  Lock both.
        rewrapped = [
            b
            for b in baseline["blocks"]
            if b["kind"] == "flow"
            and b["overflow"] is False
            and b.get("recovery_steps")
        ]
        self.assertEqual(rewrapped[0]["recovery_steps"], ["WRAP", "SHRINK"])
        flow_overflow = [
            b for b in baseline["blocks"] if b["kind"] == "flow" and b["overflow"]
        ]
        self.assertEqual(flow_overflow[0]["recovery_steps"], ["SHRINK", "CLIP"])


# ---------------------------------------------------------------------------
# 5. architecture guards — no detector / parser / translator / renderer
# ---------------------------------------------------------------------------


class TestDiagnosticsArchitecture(unittest.TestCase):
    def test_diagnostics_module_is_pure(self):
        import ast
        import inspect
        import pdf2zh.semantic.layout.diagnostics as mod

        tree = ast.parse(inspect.getsource(mod))
        # strip the module docstring: only the code body is guarded
        body = tree.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
        ):
            body = body[1:]
        code = ast.unparse(ast.Module(body=body, type_ignores=[]))
        for banned in (
            "detector",
            "parser",
            "translator",
            "renderer",
            "wrap_lines",
            "shrink_to_fit",
            "clip_text",
            "lay_out(",
            "adaptive_layout(",
            "level *",
            "index *",
        ):
            self.assertNotIn(
                banned, code, f"diagnostics.py must not reference {banned!r}"
            )

    def test_layout_debug_module_never_renders(self):
        import inspect
        import pdf2zh.semantic.layout_debug as mod

        src = inspect.getsource(mod)
        self.assertNotIn("render_plan_to_pdf", src)
        self.assertNotIn("magicpdf", src)

    def test_collector_is_read_only_view_of_settled_payload(self):
        """Mutating a collected diagnostic must not touch the plan payload."""
        entry = _flow_entry("A" * 500, "A" * 500, x0=60.0, x1=100.0, font_size=12.0)
        diag = diagnostic_from_plan_entry(entry)
        self.assertTrue(diag.overflow)
        diag.overflow = False  # local view mutation
        self.assertTrue(entry["render_payload"]["overflow"])


if __name__ == "__main__":
    unittest.main()
