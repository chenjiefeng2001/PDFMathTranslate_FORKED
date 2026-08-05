"""V8.6 Content Preservation Engine 测试。

覆盖：统一动作收敛（TRANSLATE/PRESERVE/OVERLAY）、语义角色默认策略、
图片委托决策、apply_to_ir 角色写回、serialization。
"""
import unittest

import numpy as np

from pdf2zh.v3.content_preservation import (
    ACTION_TO_RENDER, PreservationAction, PreservationDecision,
    ContentPreservationEngine, classify_node,
)
from pdf2zh.v3.document_ir import (
    DocumentIR, IRNode, RenderingRole, SemanticRole, TranslationRole,
)
from pdf2zh.v3.image_engine import ImageClass, ImageObject, TextRegion


def _build_ir():
    ir = DocumentIR(title="t", source_lang="en", target_lang="zh-cn")
    page = ir.add_node("page_0", semantic=SemanticRole.SECTION, page_num=0)
    nodes = {
        "body": ir.add_node("body", semantic=SemanticRole.BODY_TEXT,
                            parent_id="page_0", text="hello world"),
        "heading": ir.add_node("h1", semantic=SemanticRole.HEADING,
                               parent_id="page_0", text="Overview"),
        "figure": ir.add_node("fig", semantic=SemanticRole.FIGURE,
                              parent_id="page_0", text="figure"),
        "formula": ir.add_node("fml", semantic=SemanticRole.FORMULA,
                               parent_id="page_0", text="E=mc^2"),
        "table": ir.add_node("tbl", semantic=SemanticRole.TABLE,
                             parent_id="page_0", text="table"),
        "caption": ir.add_node("cap", semantic=SemanticRole.CAPTION,
                               parent_id="page_0", text="Fig 1: results"),
        "code": ir.add_node("code", semantic=SemanticRole.CODE,
                            parent_id="page_0", text="print(1)"),
        "header": ir.add_node("hdr", semantic=SemanticRole.HEADER,
                              parent_id="page_0", text="company"),
    }
    return ir, nodes


class TestRoleDefaults(unittest.TestCase):
    def setUp(self):
        self.engine = ContentPreservationEngine()

    def test_body_translates(self):
        ir, n = _build_ir()
        d = self.engine.decide_ir_node(n["body"])
        self.assertEqual(d.action, PreservationAction.TRANSLATE)
        self.assertEqual(d.translation_role, TranslationRole.TRANSLATE)

    def test_figure_preserved(self):
        ir, n = _build_ir()
        d = self.engine.decide_ir_node(n["figure"])
        self.assertEqual(d.action, PreservationAction.PRESERVE)
        self.assertEqual(d.render_mode.value, "preserve")

    def test_table_preserved(self):
        ir, n = _build_ir()
        self.assertEqual(self.engine.decide_ir_node(n["table"]).action,
                         PreservationAction.PRESERVE)

    def test_formula_preserved(self):
        ir, n = _build_ir()
        d = self.engine.decide_ir_node(n["formula"])
        self.assertEqual(d.action, PreservationAction.PRESERVE)
        self.assertEqual(d.translation_role, TranslationRole.SKIP)

    def test_caption_translate_with_context(self):
        ir, n = _build_ir()
        d = self.engine.decide_ir_node(n["caption"])
        self.assertEqual(d.action, PreservationAction.TRANSLATE)
        self.assertEqual(d.translation_role, TranslationRole.NEED_CONTEXT)

    def test_header_preserved(self):
        ir, n = _build_ir()
        self.assertEqual(self.engine.decide_ir_node(n["header"]).action,
                         PreservationAction.PRESERVE)

    def test_code_preserved(self):
        ir, n = _build_ir()
        self.assertEqual(self.engine.decide_ir_node(n["code"]).action,
                         PreservationAction.PRESERVE)


class TestDecideWholeIR(unittest.TestCase):
    def test_ir_walk_produces_decision_per_node(self):
        ir, _ = _build_ir()
        decisions = ContentPreservationEngine().decide_ir(ir)
        ids = {d.object_id for d in decisions}
        self.assertEqual(ids, {"page_0", "body", "h1", "fig", "fml", "tbl",
                               "cap", "code", "hdr"})

    def test_decisions_serializable(self):
        ir, _ = _build_ir()
        d = ContentPreservationEngine().decide_ir(ir)[0].to_dict()
        self.assertIn("action", d)
        self.assertIn("confidence", d)
        self.assertIn("reasons", d)


class TestImageDelegate(unittest.TestCase):
    def test_preserve_image_protected(self):
        obj = ImageObject(id="i1", image_class=ImageClass.LOGO,
                          features={"color_count": 4})
        d = ContentPreservationEngine().decide_image(obj)
        self.assertEqual(d.action, PreservationAction.PRESERVE)
        self.assertEqual(d.object_type, "image:logo")

    def test_translate_image_whitelisted(self):
        obj = ImageObject(id="i2", image_class=ImageClass.DIAGRAM)
        obj.regions = [TextRegion(bbox=(0, 0, 0.5, 0.1), text="build a module",
                                  ocr_confidence=0.9)]
        d = ContentPreservationEngine().decide_image(obj)
        self.assertEqual(d.action, PreservationAction.TRANSLATE)
        self.assertEqual(d.render_mode.value, "region_replace")

    def test_overlay_image(self):
        obj = ImageObject(id="i3", image_class=ImageClass.SCREENSHOT)
        obj.regions = [TextRegion(bbox=(0, 0, 0.5, 0.1), text="click save button",
                                  ocr_confidence=0.95)]
        d = ContentPreservationEngine().decide_image(obj)
        self.assertTrue(d.image_decision is not None)
        self.assertIn(d.action, {PreservationAction.TRANSLATE, PreservationAction.OVERLAY})

    def test_image_decision_cached_on_object(self):
        obj = ImageObject(id="i4", image_class=ImageClass.PHOTO)
        eng = ContentPreservationEngine()
        eng.decide_image(obj)
        d2 = eng.decide_image(obj)
        # 底层图片决策只算一次并缓存到 obj.decision
        self.assertIsNotNone(obj.decision)
        self.assertIs(d2.image_decision, obj.decision)

    def test_decision_dict_roundtrip(self):
        obj = ImageObject(id="i5", image_class=ImageClass.CHART)
        obj.regions = [TextRegion(bbox=(0, 0, 0.5, 0.1), text="annual sales by region",
                                  ocr_confidence=0.9)]
        d = ContentPreservationEngine().decide_image(obj)
        out = d.to_dict()
        self.assertEqual(out["object_id"], "i5")
        self.assertIn("image_decision", out)


class TestApplyToIR(unittest.TestCase):
    def test_apply_sets_translation_roles(self):
        ir, n = _build_ir()
        ContentPreservationEngine().apply_to_ir(ir)
        self.assertEqual(ir.get_node("body").translation, TranslationRole.TRANSLATE)
        self.assertEqual(ir.get_node("fig").translation, TranslationRole.SKIP)
        self.assertEqual(ir.get_node("cap").translation, TranslationRole.NEED_CONTEXT)
        self.assertEqual(ir.get_node("hdr").translation, TranslationRole.SKIP)

    def test_apply_with_image_overrides_node(self):
        ir, n = _build_ir()
        img = ImageObject(id="i_img", image_class=ImageClass.LOGO)
        ContentPreservationEngine().apply_to_ir(ir, [img])
        # no matching ir node -> image decision still produced but node untouched
        self.assertEqual(ir.get_node("body").translation, TranslationRole.TRANSLATE)

    def test_classify_node_function(self):
        ir, n = _build_ir()
        d = classify_node(n["formula"])
        self.assertEqual(d.action, PreservationAction.PRESERVE)


class TestUnknownFallback(unittest.TestCase):
    def test_custom_node_preserve_hint(self):
        node = IRNode(id="z", semantic=SemanticRole.UNKNOWN,
                      translation=TranslationRole.KEEP_TERM)
        d = ContentPreservationEngine().decide_ir_node(node)
        self.assertEqual(d.action, PreservationAction.PRESERVE)
        self.assertEqual(d.translation_role, TranslationRole.KEEP_TERM)

    def test_unknown_translate_fallback(self):
        node = IRNode(id="y", semantic=SemanticRole.UNKNOWN)
        d = ContentPreservationEngine().decide_ir_node(node)
        self.assertEqual(d.action, PreservationAction.TRANSLATE)


class TestActionRenderMap(unittest.TestCase):
    def test_map_consistency(self):
        self.assertEqual(ACTION_TO_RENDER[PreservationAction.PRESERVE].value, "preserve")
        self.assertEqual(ACTION_TO_RENDER[PreservationAction.OVERLAY].value, "overlay")
        self.assertEqual(ACTION_TO_RENDER[PreservationAction.TRANSLATE].value, "region_replace")


if __name__ == "__main__":
    unittest.main()