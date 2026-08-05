"""V8.6 Image Translation Engine 测试。

覆盖：ImageObject 结构、统计特征提取、规则分类器、文字区域检测、
TranslationDecisionEngine 决策、Router keep 词典、图片级端到端分析。
"""
import unittest

import numpy as np

from pdf2zh.v3.image_engine import (
    IMAGE_POLICY, ImageClass, ImageObject, RenderMode, TextRegion,
    TranslationDecision, TranslationDecisionEngine,
    analyze_image_bytes, classify_image, compute_image_features,
    detect_text_regions, is_probably_brand_or_technical,
    router_should_translate,
)


def _white(img, h, w):
    img.fill(255)
    return img


class TestImageFeatures(unittest.TestCase):
    def test_white_image_low_edge(self):
        arr = _white(np.full((64, 64, 3), 255, dtype=np.uint8), 64, 64)
        f = compute_image_features(arr)
        self.assertEqual(f.width, 64)
        self.assertEqual(f.height, 64)
        self.assertLess(f.edge_density, 0.1)
        self.assertGreater(f.white_ratio, 0.9)

    def test_photo_like_high_color_count(self):
        rng = np.random.default_rng(7)
        arr = rng.integers(0, 256, size=(128, 128, 3), dtype=np.uint8)
        f = compute_image_features(arr)
        self.assertGreater(f.color_count, 64)

    def test_alpha_flag_passthrough(self):
        arr = _white(np.full((8, 8, 3), 255, dtype=np.uint8), 8, 8)
        f = compute_image_features(arr, has_alpha=True)
        self.assertTrue(f.has_alpha)

    def test_features_serializable(self):
        arr = _white(np.full((16, 16, 3), 255, dtype=np.uint8), 16, 16)
        d = compute_image_features(arr).to_dict()
        self.assertIn("color_count", d)
        self.assertIn("edge_density", d)


class TestClassifier(unittest.TestCase):
    def test_qr_square_mono(self):
        rng = np.random.default_rng(0)
        arr = np.zeros((100, 100, 3), dtype=np.uint8)
        # QR 数据区：黑白随机 module，高对比 + 高边缘密度
        mod = rng.integers(0, 2, size=(20, 20))
        for y in range(20):
            for x in range(20):
                if mod[y, x] == 1:
                    arr[y * 5:(y + 1) * 5, x * 5:(x + 1) * 5] = 255
        f = compute_image_features(arr)
        cls, conf = classify_image(f)
        self.assertEqual(cls, ImageClass.QR_CODE)
        self.assertGreater(conf, 0.5)

    def test_cad_monochrome_line_art(self):
        arr = np.full((60, 100, 3), 255, dtype=np.uint8)
        # 密集工程图线网：横向 + 纵向细线（非方形，规避 QR 规则）
        for x in range(0, 100, 4):
            arr[:, x:x + 1] = 0
        for y in range(0, 60, 4):
            arr[y:y + 1, :] = 0
        f = compute_image_features(arr)
        cls, _ = classify_image(f)
        self.assertEqual(cls, ImageClass.CAD)

    def test_logo_small_few_colors(self):
        arr = np.zeros((32, 32, 3), dtype=np.uint8)
        arr[:16, :16] = (200, 30, 30)   # red block
        arr[16:, 16:] = (30, 30, 200)   # blue block
        f = compute_image_features(arr)
        cls, _ = classify_image(f)
        # 绝不允许翻译成可翻译类型（低于阈值进入 preserve 保护链）
        self.assertIn(cls, {ImageClass.LOGO, ImageClass.UNKNOWN})

    def test_photo_not_translatable(self):
        rng = np.random.default_rng(3)
        arr = rng.integers(0, 256, size=(96, 96, 3), dtype=np.uint8)
        f = compute_image_features(arr)
        cls, _ = classify_image(f)
        self.assertTrue(IMAGE_POLICY[cls].render_mode in (RenderMode.PRESERVE, RenderMode.OVERLAY))

    def test_unknown_defaults_preserve(self):
        f = compute_image_features(_white(np.full((5, 5, 3), 255, dtype=np.uint8), 5, 5))
        cls, _ = classify_image(f)
        self.assertEqual(cls, ImageClass.UNKNOWN)
        self.assertEqual(IMAGE_POLICY[ImageClass.UNKNOWN].render_mode, RenderMode.PRESERVE)


class TestTextRegionDetection(unittest.TestCase):
    def test_no_dark_pixels_none(self):
        arr = _white(np.full((96, 96, 3), 255, dtype=np.uint8), 96, 96)
        regs = detect_text_regions(arr)
        self.assertEqual(regs, [])

    def test_block_detected(self):
        arr = _white(np.full((120, 120, 3), 255, dtype=np.uint8), 120, 120)
        arr[20:40, 20:60] = 0   # a solid dark region
        regs = detect_text_regions(arr)
        self.assertGreaterEqual(len(regs), 1)
        for r in regs:
            x0, y0, x1, y1 = r.bbox
            self.assertGreaterEqual(x1 - x0, 0.0)
            self.assertGreaterEqual(y1 - y0, 0.0)

    def test_region_bbox_normalized(self):
        arr = _white(np.full((200, 100, 3), 255, dtype=np.uint8), 200, 100)
        arr[50:70, 10:30] = 0
        for r in detect_text_regions(arr):
            for v in r.bbox:
                self.assertGreaterEqual(v, 0.0)
                self.assertLessEqual(v, 1.0)


class TestRouter(unittest.TestCase):
    def test_technical_term_keep(self):
        self.assertFalse(router_should_translate("CPU")[0])
        self.assertFalse(router_should_translate("github.com")[0])
        self.assertTrue(is_probably_brand_or_technical("github"))

    def test_brand_keep(self):
        self.assertFalse(router_should_translate("Google")[0])

    def test_ui_keep(self):
        self.assertFalse(router_should_translate("Cancel")[0])

    def test_number_keep(self):
        self.assertFalse(router_should_translate("42")[0])

    def test_normal_text_translate(self):
        res = router_should_translate("This is a data pipeline")
        self.assertTrue(res[0])
        self.assertEqual(res[1], "translate")


class TestDecisionEngine(unittest.TestCase):
    def _img(self, cls=ImageClass.DIAGRAM):
        return ImageObject(id="x", image_class=cls, features={"color_count": 2})

    def test_photo_never_translate(self):
        d = TranslationDecisionEngine().decide(self._img(ImageClass.PHOTO))
        self.assertFalse(d.translate)
        self.assertEqual(d.render_mode, RenderMode.PRESERVE)

    def test_logo_never_translate(self):
        d = TranslationDecisionEngine().decide(self._img(ImageClass.LOGO))
        self.assertFalse(d.translate)

    def test_diagram_with_regions_translates_whitelisted(self):
        img = self._img(ImageClass.DIAGRAM)
        img.regions = [
            TextRegion(bbox=(0, 0, 0.2, 0.1), text="Start the process",
                       ocr_confidence=0.9),
        ]
        d = TranslationDecisionEngine().decide(img)
        self.assertTrue(d.translate)
        self.assertEqual(d.render_mode, RenderMode.REGION_REPLACE)
        self.assertEqual(len(d.region_decisions), 1)

    def test_diagram_regions_keep_technical(self):
        img = self._img(ImageClass.DIAGRAM)
        img.regions = [
            TextRegion(bbox=(0, 0, 0.2, 0.1), text="CPU", ocr_confidence=0.9),
        ]
        d = TranslationDecisionEngine().decide(img)
        self.assertFalse(d.translate)
        self.assertTrue(any("router_keep" in r for r in d.region_decisions[0].reasons))

    def test_axis_label_low_score(self):
        img = self._img(ImageClass.CHART)
        img.regions = [TextRegion(bbox=(0, 0, 0.1, 0.1), kind="axis_label", text="0")]
        d = TranslationDecisionEngine().decide(img)
        self.assertFalse(d.translate)

    def test_threshold_configurable(self):
        img = self._img(ImageClass.DIAGRAM)
        img.regions = [TextRegion(bbox=(0, 0, 0.2, 0.1), text="Process data here",
                                  ocr_confidence=0.9)]
        eng = TranslationDecisionEngine(translate_threshold=0.99)
        d = eng.decide(img)
        self.assertFalse(d.translate)

    def test_decision_schema_dict(self):
        img = self._img(ImageClass.CHART)
        img.regions = [TextRegion(bbox=(0, 0, 0.2, 0.1), text="Quarterly revenue",
                                  ocr_confidence=0.92)]
        d = TranslationDecisionEngine().decide(img)
        out = d.to_dict()
        self.assertIn("confidence", out)
        self.assertIn("render_mode", out)
        self.assertIn("regions", out)


class TestAnalyzeImageBytes(unittest.TestCase):
    def test_end_to_end(self):
        rng = np.random.default_rng(5)
        arr = rng.integers(0, 256, size=(80, 120, 3), dtype=np.uint8)
        obj = analyze_image_bytes(arr, object_id="i9", page_num=2)
        self.assertIsInstance(obj, ImageObject)
        self.assertIsInstance(obj.decision, TranslationDecision)
        self.assertTrue(obj.features.get("color_count", 0) > 0)

    def test_empty_pixels_no_crash(self):
        obj = analyze_image_bytes(None, object_id="e1")
        self.assertIsInstance(obj, ImageObject)
        self.assertEqual(obj.image_class, ImageClass.UNKNOWN)


if __name__ == "__main__":
    unittest.main()