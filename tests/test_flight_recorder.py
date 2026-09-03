"""Flight recorder + invariant rules + trace audit tests.

Covers the production runtime trace (``pdf2zh.v3.flight_recorder`` /
``trace_rules`` / ``trace_audit``):

1. recorder: JSONL round-trip, semantic coordinates, index, disabled no-op;
2. rules: every MECH-4 / FIX-3 invariant fires on the OLD (buggy) event
   shapes and stays silent on the FIXED shapes;
3. end-to-end: plan → fixup → renderer with a recorder, then trace-audit
   produces summary / ledger / pages / index / qualification (+ crops).
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from pdf2zh.v3.flight_recorder import (
    Coord,
    FlightRecorder,
    TraceContext,
    build_trace_index,
    read_events,
)
from pdf2zh.v3.trace_rules import grade_pages, group_by_block, run_rules


def _event(event, page, block, stage, payload, y_meaning=None):
    return {
        "event": event,
        "run_id": "test-run",
        "book_id": "test-book",
        "page": page,
        "block_id": block,
        "trace_id": f"{page}/{block}",
        "stage": stage,
        "payload": payload,
    }


def _plan_event(page, block, **over):
    p = {
        "kind": "paragraph",
        "text": "source text",
        "translated": "译文文本内容",
        "render_path": "translate_refit",
        "src_box": [50.0, 586.0, 400.0, 600.0],
        "dst_box": [50.0, 586.0, 400.0, 600.0],
        "font_size": 12.0,
        "overflow": False,
        "layout_ok": True,
        "lines": ["译文文本内容"],
        "recovery": None,
        "commands": [
            {"x": 50.0, "y": 600.0, "y_meaning": "box_top", "font_size": 12.0,
             "text": "译文文本内容", "is_last": True, "overflow": False}
        ],
    }
    p.update(over)
    return _event("plan.flow", page, block, "plan", p)


def _shift_event(page, block, src, dst, fixup="shift_down", first_cmd_y=None):
    return _event(
        "plan.shift_down",
        page,
        block,
        "plan",
        {
            "kind": "paragraph",
            "fixup": fixup,
            "src_box": src,
            "dst_box": dst,
            "delta_y": round(dst[3] - src[3], 2),
            "delta_y_meaning": "v3_y_up_shift",
            "first_cmd_y": first_cmd_y,
            "overflowed": False,
        },
    )


class TestRecorder(unittest.TestCase):
    def test_disabled_recorder_is_noop(self):
        rec = FlightRecorder(None)
        rec.emit("run.begin", rec.ctx(0, "*", "run"), {})
        self.assertFalse(rec.enabled)
        self.assertEqual(rec.count, 0)
        rec.close()

    def test_jsonl_roundtrip_with_semantic_coords(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.jsonl")
            rec = FlightRecorder(path, book_id="bookA")
            rec.emit(
                "render.flow",
                rec.ctx(3, "p3_1", "render"),
                {
                    "commands": [
                        {
                            "x": 10.0,
                            "y": Coord(366.58, space="v3", origin="lower-left",
                                       meaning="box_top"),
                            "actual_baseline": 370.83,
                            "expected_baseline": 370.83,
                        }
                    ]
                },
            )
            rec.close()
            evs = list(read_events(path))
            self.assertEqual(evs[0]["event"], "run.begin")
            self.assertEqual(evs[-1]["event"], "run.end")
            flow = next(e for e in evs if e["event"] == "render.flow")
            self.assertEqual(flow["trace_id"], "3/p3_1")
            self.assertEqual(flow["stage"], "render")
            y = flow["payload"]["commands"][0]["y"]
            self.assertEqual(y["space"], "v3")
            self.assertEqual(y["meaning"], "box_top")

    def test_trace_index(self):
        evs = [
            _event("plan.flow", 1, "p1_0", "plan", {"kind": "paragraph"}),
            _event("render.flow", 1, "p1_0", "render", {}),
            _event("plan.flow", 2, "p2_0", "plan", {"kind": "formula"}),
        ]
        idx = build_trace_index(evs)
        self.assertEqual(idx["total_events"], 3)
        self.assertEqual(idx["page_events"], {"1": 2, "2": 1})
        t = next(x for x in idx["traces"] if x["trace_id"] == "1/p1_0")
        self.assertIn("plan", t["stages"])
        self.assertIn("render", t["stages"])


def _fix_render_event(page, block, baseline=370.83, expected=370.83, y_meaning="box_top",
                      erase_rect=None, src=None, dst=None, shifted=False):
    p = {
        "kind": "paragraph",
        "text": "source",
        "translated": "译文",
        "src_box": src or [50.0, 586.0, 400.0, 600.0],
        "dst_box": dst or ([50.0, 546.0, 400.0, 560.0] if shifted else [50.0, 586.0, 400.0, 600.0]),
        "page_height": 792.0,
        "font_size": 12.0,
        "commands": [
            {
                "x": 50.0, "y": 600.0, "y_space": "v3", "y_meaning": y_meaning,
                "font_size": 12.0, "text": "译文",
                "actual_baseline": baseline, "expected_baseline": expected,
                "baseline_delta": round(baseline - expected, 2),
            }
        ],
    }
    if erase_rect is not None:
        p["erase_rect"] = erase_rect
    return _event("render.flow", page, block, "render", p)


class TestRules(unittest.TestCase):
    def test_flow_baseline_ok(self):
        evs = [
            _plan_event(1, "p1_0"),
            _fix_render_event(1, "p1_0", baseline=370.83, expected=370.83),
        ]
        res = run_rules(evs)
        self.assertFalse([r for r in res if r.rule.startswith("FLOW_BASELINE")])

    def test_flow_baseline_semantic_mismatch(self):
        """MECH-4: renderer treated the box-top y as a baseline."""
        evs = [
            _plan_event(1, "p1_0"),
            _fix_render_event(1, "p1_0", y_meaning="baseline"),
        ]
        res = run_rules(evs)
        hit = next(r for r in res if r.rule == "FLOW_BASELINE_SEMANTICS")
        self.assertEqual(hit.severity, "HIGH")
        self.assertEqual(hit.action, "FIX-3")

    def test_flow_baseline_mismatch_regression(self):
        """Renderer drops the 0.85em offset → actual != expected."""
        evs = [
            _plan_event(1, "p1_0"),
            _fix_render_event(1, "p1_0", baseline=366.58, expected=370.83),
        ]
        res = run_rules(evs)
        hit = next(r for r in res if r.rule == "FLOW_BASELINE_MISMATCH")
        self.assertEqual(hit.severity, "HIGH")

    def test_erase_geometry_src_ok(self):
        src = [50.0, 586.0, 400.0, 600.0]
        dst = [50.0, 546.0, 400.0, 560.0]  # shifted down
        evs = [
            _plan_event(1, "p1_0", src_box=src, dst_box=src),
            _shift_event(1, "p1_0", src, dst),
            _fix_render_event(1, "p1_0", src=src, dst=dst, erase_rect=list(src), shifted=True),
        ]
        res = run_rules(evs)
        self.assertFalse([r for r in res if r.rule == "ERASE_GEOMETRY"])

    def test_erase_geometry_dst_bad(self):
        """MECH-4: the white rect covered the shifted dst_box (wipes neighbours)."""
        src = [50.0, 586.0, 400.0, 600.0]
        dst = [50.0, 546.0, 400.0, 560.0]
        evs = [
            _plan_event(1, "p1_0", src_box=src, dst_box=src),
            _shift_event(1, "p1_0", src, dst),
            _fix_render_event(1, "p1_0", src=src, dst=dst, erase_rect=list(dst), shifted=True),
        ]
        res = run_rules(evs)
        hit = next(r for r in res if r.rule == "ERASE_GEOMETRY")
        self.assertEqual(hit.severity, "HIGH")
        self.assertEqual(hit.evidence["shifted"], True)

    def test_shift_direction(self):
        src = [50.0, 586.0, 400.0, 600.0]
        # +Δy (old bug): box moved UP in v3
        bad = run_rules([_shift_event(1, "p1_0", src, [50.0, 608.0, 400.0, 622.0])])
        self.assertTrue(any(r.rule == "SHIFT_DIRECTION" and r.severity == "HIGH" for r in bad))
        # −Δy (FIX-3): down
        good = run_rules([_shift_event(1, "p1_0", src, [50.0, 564.0, 400.0, 578.0])])
        self.assertFalse([r for r in good if r.rule == "SHIFT_DIRECTION"])

    def test_decoupled(self):
        src = [50.0, 586.0, 400.0, 600.0]
        dst = [50.0, 564.0, 400.0, 578.0]
        evs = [
            _plan_event(1, "p1_0", src_box=src, dst_box=src),
            _shift_event(1, "p1_0", src, dst),
        ]
        # co-shifted command → fixup event declares post-fixup y == dst.y1 → PASS
        evs[1]["payload"]["first_cmd_y"] = dst[3]
        ok = run_rules(evs)
        self.assertFalse([r for r in ok if r.rule == "DECOUPLED"])
        # stale command y → FAIL
        evs2 = [
            _plan_event(1, "p1_0", src_box=src, dst_box=src,
                        commands=[{"x": 50.0, "y": 600.0, "y_meaning": "box_top",
                                   "font_size": 12.0, "text": "t", "is_last": True,
                                   "overflow": False}]),
            _shift_event(1, "p1_0", src, dst),
        ]
        res = run_rules(evs2)
        hit = next(r for r in res if r.rule == "DECOUPLED")
        self.assertEqual(hit.severity, "HIGH")
        self.assertEqual(hit.action, "FIX-2")

    def test_clip_rule(self):
        evs = [
            _plan_event(1, "p1_0", overflow=True, layout_ok=False,
                        recovery={"decision": "clip", "steps": ["WRAP", "SHRINK", "CLIP"],
                                  "final_font_size": 5.0}),
        ]
        res = run_rules(evs)
        hit = next(r for r in res if r.rule == "CLIP_READABILITY")
        self.assertEqual(hit.severity, "MEDIUM")
        self.assertEqual(hit.action, "FIX-1")

    def test_group_and_grade(self):
        evs = [
            _plan_event(1, "p1_0"),
            _fix_render_event(1, "p1_0", y_meaning="baseline"),  # HIGH → D
            _plan_event(2, "p2_0", recovery={"decision": "clip", "steps": ["CLIP"]}),  # MEDIUM → C
        ]
        res = run_rules(evs)
        grades = grade_pages(res)
        self.assertEqual(grades.get(1), "D")
        self.assertEqual(grades.get(2), "C")
        self.assertEqual(len(group_by_block(evs)), 2)


class TestFirstDivergence(unittest.TestCase):
    """first_divergence：每个 FAIL 块的最早 FAIL 阶段是根因，其后是下游症状。"""

    def _bad_events(self, page=1, block="p1_0"):
        """render 层基线语义 FAIL + raster 层 INK_OVERLAP FAIL 的事件集。"""
        return [
            _plan_event(page, block),
            _fix_render_event(page, block, y_meaning="baseline"),  # render FAIL
            _event(
                "raster.ink", page, block, "raster",
                {"foreign_overlap_pct": 55.0, "ink_bbox": [10.0, 10.0, 50.0, 20.0],
                 "found": True},
            ),
        ]

    def test_annotate_first_divergence(self):
        from pdf2zh.v3.trace_rules import annotate_first_divergence

        res = run_rules(self._bad_events())
        first_map = annotate_first_divergence(res)
        self.assertEqual(first_map["1/p1_0"], "render")
        baseline = next(r for r in res if r.rule == "FLOW_BASELINE_SEMANTICS")
        self.assertEqual(baseline.first_divergence, "render")
        self.assertFalse(baseline.downstream)
        ink = next(r for r in res if r.rule == "INK_OVERLAP")
        self.assertEqual(ink.first_divergence, "render")
        self.assertTrue(ink.downstream)

    def test_first_divergence_plan_wins_over_raster(self):
        """plan 层 FAIL（无译文的 shift 块）比 raster 层 FAIL 更早 → plan 是根因。"""
        from pdf2zh.v3.trace_rules import annotate_first_divergence

        src = [50.0, 586.0, 400.0, 600.0]
        dst = [50.0, 564.0, 400.0, 578.0]
        evs = [
            _plan_event(1, "p1_0", translated="", text="needs translation",
                        render_path="shift_down"),
            _shift_event(1, "p1_0", src, dst),
            _event(
                "raster.ink", 1, "p1_0", "raster",
                {"foreign_overlap_pct": 55.0, "ink_bbox": [10.0, 10.0, 50.0, 20.0],
                 "found": True},
            ),
        ]
        res = run_rules(evs)
        self.assertTrue(any(r.rule == "EMPTY_TRANSLATION" for r in res))
        first_map = annotate_first_divergence(res)
        self.assertEqual(first_map["1/p1_0"], "plan")
        ink = next(r for r in res if r.rule == "INK_OVERLAP")
        self.assertTrue(ink.downstream)

    def test_audit_outputs_first_divergence_tree(self):
        from pdf2zh.v3.trace_audit import _run_audit

        with tempfile.TemporaryDirectory() as d:
            trace_path = os.path.join(d, "bad_events.jsonl")
            with open(trace_path, "w", encoding="utf-8") as fh:
                for ev in self._bad_events():
                    fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
            out = os.path.join(d, "audit")
            rc = _run_audit(trace_path, out=out)
            self.assertEqual(rc, 0)

            summary = json.load(open(os.path.join(out, "summary.json"), encoding="utf-8"))
            self.assertEqual(summary["qualification"], "FAIL")
            self.assertEqual(summary["first_divergence_by_stage"], {"render": 1})
            self.assertEqual(summary["first_divergence_blocks"], 1)
            self.assertEqual(summary["downstream_symptoms"], 1)
            base = next(r for r in summary["rules"] if r["rule"] == "FLOW_BASELINE_SEMANTICS")
            self.assertEqual(base["first_divergence"], "render")
            self.assertFalse(base["downstream"])
            ink = next(r for r in summary["rules"] if r["rule"] == "INK_OVERLAP")
            self.assertEqual(ink["first_divergence"], "render")
            self.assertTrue(ink["downstream"])

            md = open(os.path.join(out, "qualification.md"), encoding="utf-8").read()
            self.assertIn("## First divergence", md)
            self.assertIn("├─ render", md)
            self.assertIn("← first divergence (FLOW_BASELINE_SEMANTICS)", md)
            self.assertIn("← downstream symptom (INK_OVERLAP)", md)

            with open(os.path.join(out, "defect-ledger.csv"), encoding="utf-8") as fh:
                header = fh.readline().strip()
                rows = fh.readlines()
            self.assertIn("first_divergence", header)
            self.assertTrue(any(",render," in row for row in rows))


class TestExplain(unittest.TestCase):
    """trace_audit explain：单块一键诊断（根因 → 模块 → 修复 → 下游 → 证据）。"""

    def _bad_events(self):
        return [
            _plan_event(1, "p1_0"),
            _fix_render_event(1, "p1_0", y_meaning="baseline"),
            _event(
                "raster.ink", 1, "p1_0", "raster",
                {"foreign_overlap_pct": 55.0, "ink_bbox": [10.0, 10.0, 50.0, 20.0],
                 "found": True},
            ),
        ]

    def _write(self, d, evs):
        path = os.path.join(d, "events.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            for ev in evs:
                fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
        return path

    def test_explain_fail_block(self):
        from pdf2zh.v3.trace_audit import _run_explain

        with tempfile.TemporaryDirectory() as d:
            trace_path = self._write(d, self._bad_events())
            rc = _run_explain(trace_path, "1/p1_0", out=os.path.join(d, "audit"))
            self.assertEqual(rc, 0)
            out = os.path.join(d, "audit", "crops")
            # 无 --pdf：不生成 crops，也不报错
            self.assertFalse(os.path.isdir(out))

            # 直接检查 explain_block 文本
            from pdf2zh.v3.trace_audit import explain_block
            from pdf2zh.v3.flight_recorder import read_events

            report = explain_block(list(read_events(trace_path)), "1/p1_0")
            self.assertIn("Trace:       1/p1_0", report)
            self.assertIn("Status:      FAIL", report)
            self.assertIn("First stage: render", report)
            self.assertIn("Module:      pdf2zh/v3/magicpdf_renderer.py", report)
            self.assertIn("Severity:    HIGH", report)
            self.assertIn("FLOW_BASELINE_SEMANTICS", report)
            self.assertIn("Fix:         FIX-3", report)
            self.assertIn("Downstream:  INK_OVERLAP (raster)", report)
            self.assertIn("├─ render", report)
            self.assertIn("Plan:", report)
            self.assertIn("dst y1 meaning", report)
            self.assertIn("Renderer:", report)
            self.assertIn("y_meaning", report)

    def test_explain_pass_and_missing(self):
        from pdf2zh.v3.trace_audit import explain_block
        from pdf2zh.v3.flight_recorder import read_events

        evs = [_plan_event(1, "p1_0"), _fix_render_event(1, "p1_0")]
        with tempfile.TemporaryDirectory() as d:
            trace_path = self._write(d, evs)
            events = list(read_events(trace_path))
            report = explain_block(events, "1/p1_0")
            self.assertIn("Status:      PASS", report)
            self.assertIn("├─ plan", report)
            missing = explain_block(events, "99/nope")
            self.assertIn("not found", missing)
            self.assertIn("1 block", missing)

    def test_explain_crops_evidence(self):
        """audit/ 目录里已有的 crops 会被列为 evidence（无需重跑 --pdf）。"""
        from pdf2zh.v3.trace_audit import explain_block
        from pdf2zh.v3.flight_recorder import read_events

        with tempfile.TemporaryDirectory() as d:
            trace_path = self._write(d, self._bad_events())
            crop_dir = os.path.join(d, "audit", "crops")
            os.makedirs(crop_dir)
            with open(os.path.join(crop_dir, "p1_p1_0_mono.png"), "wb") as fh:
                fh.write(b"png")
            report = explain_block(
                list(read_events(trace_path)), "1/p1_0", out=os.path.join(d, "audit")
            )
            self.assertIn("Evidence:", report)
            self.assertIn("crop        = ", report)
            self.assertIn("p1_p1_0_mono.png", report)


class TestEndToEndAudit(unittest.TestCase):
    """plan → fixup → renderer with recorder → trace-audit outputs."""

    def _render_with_trace(self, trace_path, pdf_path):
        from pdf2zh.v3.canonical_page import BlockModel, PageModel, SpanModel
        from pdf2zh.v3.document_model import DocumentModel, render_plan_from_model
        from pdf2zh.v3.magicpdf_renderer import render_plan_to_pdf
        from pdf2zh.v3.render_takeover import fixup_render_plan

        def blk(text, translated, x0, y0, x1, y1, size=12.0, kind="paragraph"):
            from pdf2zh.v3.canonical_page import LineModel

            ln = LineModel(text=text, baseline=0.0, x0=x0, y0=y0, x1=x1, y1=y1)
            ln.spans.append(SpanModel(size=size, text=text, x0=x0, y0=y0, x1=x1, y1=y1))
            return BlockModel(
                text=text, kind=kind, x0=x0, y0=y0, x1=x1, y1=y1,
                lines=[ln], metadata={"translated": translated},
            )

        page = PageModel(page_num=0)
        page.blocks.append(blk("A short paragraph.", "一个简短的段落。", 72, 700, 540, 722))
        model = DocumentModel()
        model.pages = [page]

        rec = FlightRecorder(trace_path, book_id="tiny")
        plan = render_plan_from_model(model, trace=rec)
        fixed, _ = fixup_render_plan(plan, trace=rec)
        render_plan_to_pdf(fixed, page_sizes={0: [612, 792]}, output_path=pdf_path,
                           cjk_font=True, trace=rec)
        rec.close()

    def test_audit_outputs(self):
        with tempfile.TemporaryDirectory() as d:
            trace_path = os.path.join(d, "trace", "tiny_events.jsonl")
            pdf_path = os.path.join(d, "tiny.pdf")
            self._render_with_trace(trace_path, pdf_path)
            self.assertTrue(os.path.exists(trace_path))
            evs = list(read_events(trace_path))
            names = {e["event"] for e in evs}
            self.assertIn("plan.flow", names)
            self.assertIn("render.flow", names)
            # render.erase 只在传入 src_doc（真实覆盖源文）时发射 —— 合成
            # 渲染路径没有源 PDF，擦除几何由 ERASE_GEOMETRY 单测覆盖。
            self.assertNotIn("render.erase", names)
            flow = next(e for e in evs if e["event"] == "render.flow")
            cmd = flow["payload"]["commands"][0]
            self.assertEqual(cmd["y_meaning"], "box_top")
            self.assertAlmostEqual(cmd["actual_baseline"], cmd["expected_baseline"], places=1)

            from pdf2zh.v3.trace_audit import _run_audit

            out = os.path.join(d, "audit")
            rc = _run_audit(trace_path, pdf=pdf_path, out=out)
            self.assertEqual(rc, 0)
            for name in ("summary.json", "pages.json", "trace-index.json",
                         "defect-ledger.csv", "qualification.md"):
                self.assertTrue(os.path.exists(os.path.join(out, name)), name)
            summary = json.load(open(os.path.join(out, "summary.json"), encoding="utf-8"))
            self.assertEqual(summary["qualification"], "PASS")
            # ledger header present
            with open(os.path.join(out, "defect-ledger.csv"), encoding="utf-8") as fh:
                header = fh.readline().strip()
            self.assertTrue(header.startswith("page,block,defect"))


if __name__ == "__main__":
    unittest.main()