# -*- coding: utf-8 -*-
"""Commit 7F-8b — Resolved Geometry Cross-block Collision.

8a validated the geometric detector against *declared* boxes.  8b feeds the
**settled drawn extent** (the LayoutResult's own geometry, carried verbatim in
the render payload's positioned commands) into the cross-block analysis, so a
translation that wraps below its source box is finally visible as a collision::

    source:  A ─────          resolved:  A ─────────────
                                        ─────────────
             B ─────                      B ─────

Locked guarantees:

1. **Resolved bbox** — ``BlockPlacement.resolved_bbox`` is ``dst_box``
   extended **downward** by the drawn lines; ``bbox`` stays the pure source
   geometry; the plan entries are never mutated.
2. **Dual-mode detection** — ``bbox_mode="resolved"`` (default) vs
   ``bbox_mode="source"``; every :class:`PageCollision` records its mode.
3. **Four golden cases** — normal (0 / 0), source collision (1 / 1),
   **translation-inflated** (0 / 1 — the case 8b exists for), cross-page (0 / 0).
4. **Summary provenance** — ``source_collision_count`` vs
   ``resolved_collision_count`` answer "这个碰撞是源文档就有，还是译文
   layout 后产生的" without re-analysis later.
5. **8b still does nothing** — no movement, no writeback, no re-layout, no
   renderer / converter / ONNX changes; the architecture guards from 8a keep
   holding (page_flow.py stays a pure read).
"""

import json
import unittest

from pdf2zh.semantic.layout.page_flow import (
    build_page_flow_report,
    detect_page_collisions,
    placements_from_plan,
)


def _entry(block_id, page, kind, x0, y0, x1, y1, payload=None,
           list_items=None, toc_entries=None):
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
    }


def _flow(block_id, page, x0, y0, x1, y1, payload=None):
    return _entry(block_id, page, "flow", x0, y0, x1, y1, payload=payload)


def _wrapped_payload(font_size=10.0, baselines=(714.0, 700.0, 686.0, 672.0)):
    """A settled flow payload whose lines step downward past the box bottom.

    Mirrors what ``render_flow_text`` produces when a translation wraps below
    its source box (e.g. a no-clip / PRESERVE_OVERFLOW run).
    """
    return {
        "kind": "flow",
        "font_size": font_size,
        "commands": [
            {"kind": "flow-text", "text": "l", "x": 60.0, "y": y,
             "width": 100.0, "line": i, "is_last": i == len(baselines) - 1,
             "overflow": True, "font_size": font_size}
            for i, y in enumerate(baselines)
        ],
    }


def _real_wrapped_payload(text, x0, y0, x1, y1, font_size=10.0):
    """Run the real flow pipeline (wrap, no clip) → settled payload.

    Uses ``allow_shrink=False`` so the budget forbids SHRINK/CLIP: the
    translation stays fully wrapped and genuinely extends below the source
    box — the honest translation-inflated geometry 8b must see.
    """
    from pdf2zh.semantic.renderer.flow import render_flow_text

    return render_flow_text(
        text, origin=(x0, y1), max_width=max(1.0, x1 - x0),
        max_height=max(1.0, y1 - y0), font_size=font_size,
        allow_shrink=False, line_step=-(font_size * 1.4),
    )


# ---------------------------------------------------------------------------
# 1. resolved bbox — settled drawn extent, only ever extends downward
# ---------------------------------------------------------------------------


class TestResolvedBbox(unittest.TestCase):
    def test_resolved_extends_down_when_drawn_lines_spill(self):
        # box [60,700,260,728]; drawn lines step to 672 -> drawn bottom 669.5
        plan = [_flow("p1_0", 1, 60.0, 700.0, 260.0, 728.0,
                      payload=_wrapped_payload(font_size=10.0))]
        p = placements_from_plan(plan)[0]
        # bbox stays pure source
        self.assertEqual(p.bbox, (60.0, 700.0, 260.0, 728.0))
        # resolved extends ONLY downward: x and top verbatim
        self.assertEqual(p.resolved_bbox[0], 60.0)
        self.assertEqual(p.resolved_bbox[2], 260.0)
        self.assertEqual(p.resolved_bbox[3], 728.0)
        self.assertAlmostEqual(p.resolved_bbox[1], 669.5, places=1)
        self.assertAlmostEqual(p.height, 728.0 - 669.5, places=1)

    def test_no_refinement_when_no_commands(self):
        plan = [_flow("p1_0", 1, 60.0, 700.0, 260.0, 728.0)]
        p = placements_from_plan(plan)[0]
        self.assertEqual(p.resolved_bbox, (60.0, 700.0, 260.0, 728.0))

    def test_no_refinement_when_drawn_lines_fit(self):
        # lines at 728, 714 -> drawn bottom 711.5, still above box bottom 700
        plan = [_flow("p1_0", 1, 60.0, 700.0, 260.0, 728.0,
                      payload=_wrapped_payload(font_size=10.0,
                                               baselines=(728.0, 714.0)))]
        p = placements_from_plan(plan)[0]
        self.assertEqual(p.resolved_bbox, (60.0, 700.0, 260.0, 728.0))

    def test_font_size_falls_back_to_entry(self):
        # payload without font_size -> entry font_size 11 -> descent 2.75
        payload = {
            "kind": "flow",
            "commands": [{"kind": "flow-text", "y": 690.0}],
        }
        plan = [_flow("p1_0", 1, 60.0, 700.0, 260.0, 728.0, payload=payload)]
        p = placements_from_plan(plan)[0]
        self.assertAlmostEqual(p.resolved_bbox[1], 690.0 - 11.0 * 0.25,
                               places=1)

    def test_resolved_never_shrinks_declared_box(self):
        # drawn lines above the box bottom must not move the bottom edge up
        plan = [_flow("p1_0", 1, 60.0, 700.0, 260.0, 728.0,
                      payload=_wrapped_payload(font_size=10.0,
                                               baselines=(720.0, 715.0)))]
        p = placements_from_plan(plan)[0]
        self.assertEqual(p.resolved_bbox[1], 700.0)


# ---------------------------------------------------------------------------
# 2. bbox_mode — resolved (default) vs source; recorded on every collision
# ---------------------------------------------------------------------------


class TestBboxMode(unittest.TestCase):
    def test_default_mode_is_resolved(self):
        plan = [
            _flow("p1_0", 1, 60.0, 700.0, 260.0, 728.0),
            _flow("p1_1", 1, 60.0, 660.0, 260.0, 720.0),
        ]
        c = detect_page_collisions(plan)[0]
        self.assertEqual(c.bbox_mode, "resolved")

    def test_source_mode_uses_source_geometry(self):
        # drawn spill only; source boxes have a clean gap -> no source collision
        plan = [
            _flow("p1_0", 1, 60.0, 700.0, 260.0, 728.0,
                  payload=_wrapped_payload(font_size=10.0)),
            _flow("p1_1", 1, 60.0, 660.0, 260.0, 688.0),
        ]
        self.assertEqual(detect_page_collisions(plan, bbox_mode="source"), [])
        resolved = detect_page_collisions(plan)
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0].bbox_mode, "resolved")

    def test_bbox_mode_recorded_in_to_dict(self):
        plan = [
            _flow("p1_0", 1, 60.0, 700.0, 260.0, 728.0),
            _flow("p1_1", 1, 60.0, 660.0, 260.0, 720.0),
        ]
        d = detect_page_collisions(plan)[0].to_dict()
        self.assertEqual(set(d),
                         {"page", "upper", "lower", "overlap",
                          "required_shift", "reason", "bbox_mode"})
        json.dumps(d)

    def test_invalid_mode_rejected(self):
        plan = []
        with self.assertRaises(ValueError):
            detect_page_collisions(plan, bbox_mode="nope")


# ---------------------------------------------------------------------------
# 3. four golden cases (source_collision_count / resolved_collision_count)
# ---------------------------------------------------------------------------


class TestGoldenCases(unittest.TestCase):
    def test_golden_normal_no_collision(self):
        # source: no overlap; resolved: no overlap -> 0 / 0
        plan = [
            _flow("p1_0", 1, 60.0, 700.0, 260.0, 728.0),
            _flow("p1_1", 1, 60.0, 620.0, 260.0, 648.0),
        ]
        s = build_page_flow_report(plan).summary()
        self.assertEqual(s["source_collision_count"], 0)
        self.assertEqual(s["resolved_collision_count"], 0)
        self.assertEqual(s["collision_count"], 0)

    def test_golden_source_collision_both_modes(self):
        # source already overlaps -> 1 / 1
        plan = [
            _flow("p1_0", 1, 60.0, 700.0, 260.0, 750.0),
            _flow("p1_1", 1, 60.0, 660.0, 260.0, 720.0),
        ]
        s = build_page_flow_report(plan).summary()
        self.assertEqual(s["source_collision_count"], 1)
        self.assertEqual(s["resolved_collision_count"], 1)

    def test_golden_translation_inflated_only_resolved(self):
        # THE 8b case: source has a clean 12pt gap, the wrapped translation
        # spills below the box -> 0 source / 1 resolved
        plan = [
            _flow("p1_0", 1, 60.0, 700.0, 260.0, 728.0,
                  payload=_wrapped_payload(font_size=10.0)),
            _flow("p1_1", 1, 60.0, 660.0, 260.0, 688.0),
        ]
        s = build_page_flow_report(plan).summary()
        self.assertEqual(s["source_collision_count"], 0)
        self.assertEqual(s["resolved_collision_count"], 1)
        c = build_page_flow_report(plan).collisions[0]
        self.assertEqual(c.upper.block_index, 0)
        self.assertEqual(c.lower.block_index, 1)
        self.assertEqual(c.bbox_mode, "resolved")
        self.assertGreater(c.required_shift, 0.0)

    def test_golden_cross_page_no_false_positive(self):
        # same geometry, different pages -> no collision in either mode
        plan = [
            _flow("p1_0", 1, 60.0, 700.0, 260.0, 728.0,
                  payload=_wrapped_payload(font_size=10.0)),
            _flow("p2_0", 2, 60.0, 660.0, 260.0, 688.0),
        ]
        s = build_page_flow_report(plan).summary()
        self.assertEqual(s["source_collision_count"], 0)
        self.assertEqual(s["resolved_collision_count"], 0)

    def test_golden_translation_inflated_through_real_pipeline(self):
        # build the inflated geometry with the actual flow pipeline (no-clip
        # budget): translation wraps to many lines below a 2-line source box
        text = "translated text that wraps over several lines " * 8
        payload = _real_wrapped_payload(text, 60.0, 700.0, 260.0, 728.0,
                                        font_size=10.0)
        self.assertTrue(payload["overflow"])       # PRESERVE_OVERFLOW run
        self.assertGreaterEqual(len(payload["commands"]), 2)
        plan = [
            _flow("p1_0", 1, 60.0, 700.0, 260.0, 728.0, payload=payload),
            _flow("p1_1", 1, 60.0, 660.0, 260.0, 688.0),
        ]
        s = build_page_flow_report(plan).summary()
        self.assertEqual(s["source_collision_count"], 0)
        self.assertEqual(s["resolved_collision_count"], 1)
        # the drawn extent is genuinely below the source box bottom
        p = placements_from_plan(plan)[0]
        self.assertLess(p.resolved_bbox[1], 700.0)


# ---------------------------------------------------------------------------
# 4. summary provenance + read-only
# ---------------------------------------------------------------------------


class TestSummaryAndReadOnly(unittest.TestCase):
    def test_summary_keys_including_provenance(self):
        plan = [
            _flow("p1_0", 1, 60.0, 700.0, 260.0, 728.0,
                  payload=_wrapped_payload(font_size=10.0)),
            _flow("p1_1", 1, 60.0, 660.0, 260.0, 688.0),
        ]
        s = build_page_flow_report(plan).summary()
        self.assertEqual(
            set(s),
            {"blocks", "collision_count", "resolved_collision_count",
             "source_collision_count", "page_overflow_count", "by_reason"},
        )
        self.assertEqual(s["collision_count"], s["resolved_collision_count"])
        self.assertEqual(s["by_reason"], {"overlap": 1})
        json.dumps(build_page_flow_report(plan).to_dict())

    def test_detection_is_still_read_only(self):
        import copy
        plan = [
            _flow("p1_0", 1, 60.0, 700.0, 260.0, 728.0,
                  payload=_wrapped_payload(font_size=10.0)),
            _flow("p1_1", 1, 60.0, 660.0, 260.0, 688.0),
        ]
        snapshot = copy.deepcopy(plan)
        build_page_flow_report(plan, page_sizes={1: 792.0})
        self.assertEqual(plan, snapshot)  # nothing mutated, no writeback

    def test_reason_classification_holds_in_resolved_mode(self):
        plan = [
            _flow("p1_0", 1, 60.0, 700.0, 260.0, 728.0,
                  payload=_wrapped_payload(font_size=10.0)),
            _entry("p1_1", 1, "code", 60.0, 660.0, 260.0, 688.0),
        ]
        c = detect_page_collisions(plan)[0]
        self.assertEqual(c.reason, "preserved_region")
        self.assertEqual(c.bbox_mode, "resolved")


if __name__ == "__main__":
    unittest.main()
