# -*- coding: utf-8 -*-
"""V9.0 收尾 — ImagePipeline / RenderTakeover / MainlineQA（P1/P2 闭环）。

覆盖：
- ImagePipeline：OCR 结果喂入决策链（wide→可翻译、narrow→keep）、
  端到端渲染（REGION_REPLACE 只改区域、PRESERVE 零改动）、失败降级；
- RenderTakeover：gate 判据 → 逐块渲染路由；apply 后 block 剔除 / shift 下移；
- MainlineQA：置信度路由 + Review 复检（未翻译 → retranslate 标记）。
"""
import unittest
from unittest.mock import Mock, patch

import numpy as np

from pdf2zh.v3.image_engine import (
    ImageClass, ImageObject, RenderMode, TextRegion,
    TranslationDecision, TranslationDecisionEngine,
)
from pdf2zh.v3.ocr_engine import DeterministicOCRBackend


def make_img(image_class=ImageClass.DIAGRAM, regions=None):
    obj = ImageObject(id="x", image_class=image_class,
                      features={"color_count": 8})
    obj.regions = regions or []
    return obj


class TestImagePipelineOCRFeed(unittest.TestCase):
    def test_no_ocr_empty_text_stays_keep(self):
        img = make_img(regions=[TextRegion(bbox=(0, 0, 0.5, 0.3))])
        d = TranslationDecisionEngine().decide(img)
        self.assertFalse(any(rd.translate for rd in d.region_decisions))

    def test_ocr_backfill_feeds_decision(self):
        from pdf2zh.v3.image_pipeline import decide_with_ocr
        img = make_img(regions=[
            TextRegion(bbox=(0, 0, 0.5, 0.15)),   # wide → 可翻译
            TextRegion(bbox=(0, 0.5, 0.1, 0.9)),  # narrow → keep
        ])
        # _region_is_empty 需要像素：给足够暗的像素避免判空
        pixels = np.zeros((40, 100, 3), dtype=np.uint8) + 60
        img._pixels = pixels
        d = decide_with_ocr(img, ocr_backend=DeterministicOCRBackend(
            narrow_text="42"))
        by_bbox = {tuple(round(v, 2) for v in r.region.bbox): r
                   for r in d.region_decisions}
        wide = by_bbox[(0.0, 0.0, 0.5, 0.15)]
        narrow = by_bbox[(0.0, 0.5, 0.1, 0.9)]
        self.assertTrue(wide.translate)
        self.assertEqual(wide.region.text, "Sample label text")
        self.assertFalse(narrow.translate)
        self.assertEqual(narrow.region.text, "42")

    def test_pipeline_translate_region_replace_only_region(self):
        from pdf2zh.v3.image_pipeline import translate_image_pixels
        canvas = np.full((40, 100, 3), 255, dtype=np.uint8)
        img = make_img(regions=[
            TextRegion(bbox=(0.1, 0.1, 0.5, 0.3), text="Quarterly revenue",
                       ocr_confidence=0.9),
        ])
        d = TranslationDecisionEngine().decide(img)
        self.assertTrue(d.translate)
        out, summ = translate_image_pixels(
            canvas, object_id="t1", page_num=0,
            decision=d, translate_fn=lambda t: t.upper())
        self.assertEqual(summ.render_mode, RenderMode.REGION_REPLACE.value)
        self.assertGreaterEqual(summ.regions_translated, 1)
        arr = np.frombuffer(out, dtype=np.uint8).reshape(40, 100, 3)
        region = arr[4:12, 10:50]
        outside = arr[20:40, 60:100]
        # 区域被改写，区域外保持原像素（255）
        self.assertTrue((region != 255).any())
        self.assertTrue((outside == 255).all())

    def test_preserve_mode_returns_original_bytes(self):
        from pdf2zh.v3.image_pipeline import translate_image_pixels
        canvas = np.full((30, 60, 3), 200, dtype=np.uint8)
        out, summ = translate_image_pixels(
            canvas, object_id="p0",
            decision=TranslationDecision(
                translate=False, render_mode=RenderMode.PRESERVE))
        self.assertEqual(summ.render_mode, "preserve")
        self.assertEqual(out, canvas[..., :3].tobytes())

    def test_analyze_failure_degrades_to_preserve(self):
        from pdf2zh.v3.image_pipeline import translate_image_pixels
        canvas = np.full((20, 40, 3), 255, dtype=np.uint8)
        with patch("pdf2zh.v3.image_pipeline.analyze_image_bytes",
                   side_effect=RuntimeError("boom")):
            out, summ = translate_image_pixels(canvas, object_id="x")
        self.assertTrue(summ.ok)
        self.assertEqual(out, canvas[..., :3].tobytes())

    def test_solid_plate_deterministic(self):
        from pdf2zh.v3.image_pipeline import SolidPlateRenderer
        a = SolidPlateRenderer().render("hello", 8, 24)
        b = SolidPlateRenderer().render("hello", 8, 24)
        self.assertTrue(np.array_equal(a, b))
        self.assertEqual(a.shape, (8, 24, 3))


class TestRenderTakeover(unittest.TestCase):
    def _blocks(self):
        from pdf2zh.v3.render_takeover import WritebackBlock
        return [
            WritebackBlock(node_id="p0_0", x=50, y=700, width=300,
                           height=20, page=0, node_type="paragraph"),
            WritebackBlock(node_id="p0_1", x=50, y=660, width=300,
                           height=20, page=0, node_type="paragraph"),
        ]

    def test_plan_routes_overflow_to_block_when_rejected(self):
        from pdf2zh.v3.render_takeover import plan_writeback_takeover
        verdict = {"writeback_allowed": False,
                   "issues": ["blocks overflow the page: [p0_0]"],
                   "overlap_rate": 0.2, "page_height": 792.0}
        plan = plan_writeback_takeover(self._blocks(), verdict=verdict)
        self.assertFalse(plan["admissible"])
        self.assertEqual(plan["routing"]["p0_0"]["render_path"], "block")
        self.assertEqual(plan["routing"]["p0_1"]["render_path"],
                         "translate_refit")

    def test_apply_plan_blocks_dropped_and_shifted(self):
        from pdf2zh.v3.render_takeover import (
            apply_render_plan, plan_writeback_takeover,
        )
        blocks = self._blocks()
        verdict = {"writeback_allowed": True,
                   "issues": ["blocks overflow the page: [p0_1]"],
                   "overlap_rate": 0.05, "page_height": 792.0}
        plan = plan_writeback_takeover(blocks, verdict=verdict)
        # allowed=True + overflow → shift_down（非 block）
        applied = apply_render_plan(plan, blocks)
        self.assertEqual(len(applied), 2)
        shifted = [b for b in applied if b["node_id"] == "p0_1"][0]
        self.assertEqual(shifted["render_path"], "shift_down")
        self.assertGreater(shifted["y"], 660)
        # rejected + overflow → block 剔除
        verdict2 = {"writeback_allowed": False,
                    "issues": ["blocks overflow the page: [p0_1]"],
                    "overlap_rate": 0.4, "page_height": 792.0}
        plan2 = plan_writeback_takeover(blocks, verdict=verdict2)
        applied2 = apply_render_plan(plan2, blocks)
        self.assertEqual(len(applied2), 1)
        self.assertEqual(applied2[0]["node_id"], "p0_0")

    def test_no_verdict_defaults_refit(self):
        from pdf2zh.v3.render_takeover import (
            apply_render_plan, plan_writeback_takeover,
        )
        blocks = self._blocks()
        plan = plan_writeback_takeover(blocks, verdict=None)
        self.assertTrue(plan["admissible"])
        applied = apply_render_plan(plan, blocks)
        self.assertEqual(len(applied), 2)
        self.assertEqual(applied[0]["render_path"], "translate_refit")


class TestMainlineQA(unittest.TestCase):
    def test_untranslated_flagged_retranslate(self):
        from pdf2zh.v3.mainline_qa import run_translation_qa
        report = run_translation_qa([
            {"node_id": "p0_0", "text": "This is a long sentence here",
             "translated": "This is a long sentence here"},
        ])
        self.assertEqual(report.total, 1)
        self.assertEqual(report.translate, 1)
        self.assertGreaterEqual(report.action_retranslate, 1)
        self.assertFalse(report.records[0].review_passed)
        codes = {i["code"] for i in report.records[0].issues}
        self.assertIn("UNTRANSLATED", codes)

    def test_keep_route_skips_review(self):
        from pdf2zh.v3.mainline_qa import run_translation_qa
        report = run_translation_qa([
            {"node_id": "p0_0", "text": "42.5%", "translated": "42.5%"},
        ])
        self.assertEqual(report.keep, 1)
        self.assertEqual(report.translate, 0)
        self.assertEqual(report.action_retranslate, 0)

    def test_good_translation_passes(self):
        from pdf2zh.v3.mainline_qa import run_translation_qa
        report = run_translation_qa([
            {"node_id": "p0_0",
             "text": "We describe the method in detail below.",
             "translated": "YI we describe the method in detail below."},
        ])
        self.assertEqual(report.translate, 1)
        self.assertTrue(report.records[0].review_passed)
        self.assertEqual(report.action_retranslate, 0)

    def test_numbers_missing_flagged_warning(self):
        from pdf2zh.v3.mainline_qa import run_translation_qa
        report = run_translation_qa([
            {"node_id": "p0_0", "text": "Table 3 shows results of 42 runs",
             "translated": "YI table shows results of runs"},
        ])
        # NUMBER_CHANGED 为 warning 级 → 记录 issue，但不触发 retranslate
        self.assertEqual(report.action_retranslate, 0)
        self.assertTrue(report.records[0].review_passed)
        codes = {i["code"] for i in report.records[0].issues}
        self.assertIn("NUMBER_CHANGED", codes)


class TestCalibrationCorpusDir(unittest.TestCase):
    def test_load_samples_and_calibrate(self):
        import json
        import os
        import tempfile
        from pdf2zh.v3.image_calibrate import (
            CalibrationReport, calibrate_corpus_dir, load_samples_from_dir,
        )
        d = tempfile.mkdtemp(prefix="cal_")
        samples = [
            {"features": {"width": 400, "height": 300, "color_count": 2000,
                          "edge_density": 0.05, "white_ratio": 0.2,
                          "brightness_var": 20.0, "is_gray": False,
                          "unique_colors": 1500},
             "label": "photo"},
            {"features": {"width": 400, "height": 300, "color_count": 40,
                          "edge_density": 0.6, "white_ratio": 0.7,
                          "brightness_var": 5.0, "is_gray": True,
                          "unique_colors": 30},
             "label": "chart"},
        ]
        for i, s in enumerate(samples):
            with open(os.path.join(d, f"s{i}.json"), "w",
                      encoding="utf-8") as f:
                json.dump(s, f)
        loaded = load_samples_from_dir(d)
        self.assertEqual(len(loaded), 2)
        out = os.path.join(d, "report.json")
        report = calibrate_corpus_dir(d, out_json=out)
        self.assertIsInstance(report, CalibrationReport)
        self.assertGreaterEqual(report.best_accuracy, 0.0)
        self.assertTrue(os.path.exists(out))

    def test_empty_dir_returns_none(self):
        import tempfile
        from pdf2zh.v3.image_calibrate import calibrate_corpus_dir
        d = tempfile.mkdtemp(prefix="cal_empty_")
        self.assertIsNone(calibrate_corpus_dir(d))

    def test_corrupted_sample_skipped(self):
        import os
        import tempfile
        from pdf2zh.v3.image_calibrate import load_samples_from_dir
        d = tempfile.mkdtemp(prefix="cal_bad_")
        with open(os.path.join(d, "bad.json"), "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertEqual(load_samples_from_dir(d), [])


if __name__ == "__main__":
    unittest.main()
