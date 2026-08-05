# -*- coding: utf-8 -*-
"""V9.0 — Node Processors：单一核心 IR 上的 AST-Pass 层单元测试。

覆盖：
- 每类 Processor 只改写 node.metadata / node_type（不建平行 IR）
- 领域引擎复用：TOC（toc_semantics）/ 图片（image_engine）/ 统一策略（content_preservation）
- 注册表按阶段/类型调度、题注连边
"""
import unittest

import numpy as np

from pdf2zh.v3.graph import (
    DocumentGraph, DocumentNode, Edge, EdgeType, NodeType,
)
from pdf2zh.v3.processors import (
    NodeStage, POLICY_KEY, SEMANTIC_KEY, get_semantic,
    ProcessorRegistry, TOCSemanticProcessor, FormulaNodeProcessor,
    CodeNodeProcessor, ImageTranslationProcessor, ContentPolicyProcessor,
    CaptionNodeProcessor,
)


def make_node(nid, node_type, text="", page=0, bbox=(0, 0, 100, 20)):
    return DocumentNode(id=nid, node_type=node_type, bbox=bbox, text=text, page_num=page)


class TestTOCSemanticProcessor(unittest.TestCase):
    def test_chapter_local_only(self):
        node = make_node("n1", NodeType.PARAGRAPH, text="Chapter 3")
        TOCSemanticProcessor().process(node, DocumentGraph())
        self.assertEqual(node.node_type, NodeType.TOC_ENTRY)
        self.assertEqual(node.metadata[SEMANTIC_KEY]["toc"]["kind"], "chapter")
        self.assertEqual(node.metadata[SEMANTIC_KEY]["toc"]["number"], "3")
        self.assertEqual(node.metadata[POLICY_KEY], "template_local")

    def test_section_remainder_goes_to_translator(self):
        node = make_node("n1", NodeType.PARAGRAPH, text="Section 2 Results")
        TOCSemanticProcessor().process(node, DocumentGraph())
        self.assertEqual(node.node_type, NodeType.TOC_ENTRY)
        self.assertEqual(node.metadata[POLICY_KEY], "translate_title_remainder")

    def test_plain_untouched(self):
        node = make_node("n1", NodeType.PARAGRAPH, text="Intro")
        TOCSemanticProcessor().process(node, DocumentGraph())
        self.assertEqual(node.node_type, NodeType.PARAGRAPH)
        self.assertNotIn(POLICY_KEY, node.metadata)


class TestFormulaNodeProcessor(unittest.TestCase):
    def test_marker_detected_as_formula(self):
        node = make_node("n1", NodeType.PARAGRAPH, text="The energy {v1} is large")
        FormulaNodeProcessor().process(node, DocumentGraph())
        self.assertEqual(node.node_type, NodeType.FORMULA)
        self.assertEqual(node.metadata[SEMANTIC_KEY]["formula"]["marker"], "{v1}")
        self.assertEqual(node.metadata[POLICY_KEY], "preserve")

    def test_plain_text_untouched(self):
        node = make_node("n1", NodeType.PARAGRAPH, text="Plain sentence")
        FormulaNodeProcessor().process(node, DocumentGraph())
        self.assertEqual(node.node_type, NodeType.PARAGRAPH)


class TestCodeNodeProcessor(unittest.TestCase):
    def test_code_annotation(self):
        node = make_node("n1", NodeType.CODE, text="print(1)")
        CodeNodeProcessor().process(node, DocumentGraph())
        self.assertEqual(node.metadata[SEMANTIC_KEY]["code"]["language"], "unknown")
        self.assertEqual(node.metadata[POLICY_KEY], "preserve")

    def test_language_from_metadata(self):
        node = make_node("n1", NodeType.CODE, text="int main() {}")
        node.metadata["language"] = "cpp"
        CodeNodeProcessor().process(node, DocumentGraph())
        self.assertEqual(node.metadata[SEMANTIC_KEY]["code"]["language"], "cpp")


class TestImageTranslationProcessor(unittest.TestCase):
    def _checkerboard(self):
        px = np.zeros((32, 32, 3), dtype=np.uint8)
        px[::2, ::2] = (255, 255, 255)
        return px

    def test_with_pixels_runs_full_chain(self):
        node = make_node("img1", NodeType.IMAGE, page=1, bbox=(0, 0, 64, 64))
        node.metadata["pixels"] = self._checkerboard()
        ImageTranslationProcessor().process(node, DocumentGraph())
        detail = node.metadata[SEMANTIC_KEY]["image"]
        self.assertIn("class", detail)
        self.assertIn("decision", detail)
        # checkerboard → 未知类型默认保护原图像素
        self.assertEqual(node.metadata[POLICY_KEY], "preserve")

    def test_no_pixels_is_graceful(self):
        node = make_node("img2", NodeType.IMAGE)
        ImageTranslationProcessor().process(node, DocumentGraph())
        detail = node.metadata[SEMANTIC_KEY]["image"]
        self.assertEqual(detail["status"], "no_pixels")
        self.assertNotIn(POLICY_KEY, node.metadata)


class TestContentPolicyProcessor(unittest.TestCase):
    def _apply(self, node):
        engine_processor = ContentPolicyProcessor()
        graph = DocumentGraph()
        graph.add_node(node)
        engine_processor.process(node, graph)
        return node

    def test_body_translate(self):
        node = self._apply(make_node("n1", NodeType.PARAGRAPH, text="body"))
        self.assertEqual(node.metadata[POLICY_KEY], "translate")
        self.assertIn("preservation", node.metadata[SEMANTIC_KEY])

    def test_figure_preserve(self):
        node = self._apply(make_node("n1", NodeType.FIGURE))
        self.assertEqual(node.metadata[POLICY_KEY], "preserve")

    def test_caption_translate_with_context(self):
        node = self._apply(make_node("n1", NodeType.CAPTION, text="Fig 1: x"))
        self.assertEqual(node.metadata[POLICY_KEY], "translate")
        self.assertEqual(
            node.metadata[SEMANTIC_KEY]["preservation"]["translation_role"],
            "need_context",
        )

    def test_toc_entry_translate(self):
        node = self._apply(make_node("n1", NodeType.TOC_ENTRY, text="Chapter 3"))
        self.assertEqual(node.metadata[POLICY_KEY], "translate")

    def test_image_role_default_preserve(self):
        node = self._apply(make_node("n1", NodeType.IMAGE))
        self.assertEqual(node.metadata[POLICY_KEY], "preserve")

    def test_does_not_override_specialist_policy(self):
        node = make_node("n1", NodeType.PARAGRAPH, text="Chapter 3")
        TOCSemanticProcessor().process(node, DocumentGraph())
        graph = DocumentGraph()
        graph.add_node(node)
        ContentPolicyProcessor().process(node, graph)
        # TOC 语义处理器已定夺，通用策略不得覆盖
        self.assertEqual(node.metadata[POLICY_KEY], "template_local")


class TestCaptionNodeProcessor(unittest.TestCase):
    def _graph_with_caption_and_figure(self):
        graph = DocumentGraph()
        fig = make_node("fig1", NodeType.FIGURE, page=1, bbox=(0, 0, 100, 40))
        cap = make_node("cap1", NodeType.CAPTION, page=1, bbox=(0, 45, 100, 60))
        graph.add_node(fig)
        graph.add_node(cap)
        return graph, fig, cap

    def test_links_caption_to_figure_below(self):
        graph, fig, cap = self._graph_with_caption_and_figure()
        CaptionNodeProcessor().finalize(graph)
        edges = graph.get_edges(source_id="cap1", edge_type=EdgeType.CAPTION_OF)
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0].target_id, "fig1")

    def test_no_duplicate_edge(self):
        graph, fig, cap = self._graph_with_caption_and_figure()
        graph.add_edge(Edge("cap1", "fig1", EdgeType.CAPTION_OF))
        CaptionNodeProcessor().finalize(graph)
        edges = graph.get_edges(source_id="cap1", edge_type=EdgeType.CAPTION_OF)
        self.assertEqual(len(edges), 1)

    def test_caption_above_figure_not_linked(self):
        graph = DocumentGraph()
        fig = make_node("fig1", NodeType.FIGURE, page=1, bbox=(0, 50, 100, 90))
        cap = make_node("cap1", NodeType.CAPTION, page=1, bbox=(0, 0, 100, 15))
        graph.add_node(fig)
        graph.add_node(cap)
        CaptionNodeProcessor().finalize(graph)
        self.assertEqual(graph.get_edges(source_id="cap1"), [])


class TestProcessorRegistry(unittest.TestCase):
    def test_for_stage_and_matching(self):
        reg = ProcessorRegistry([
            TOCSemanticProcessor(),
            ImageTranslationProcessor(),
            ContentPolicyProcessor(),
        ])
        raw = reg.for_stage(NodeStage.RAW)
        semantic = reg.for_stage(NodeStage.SEMANTIC)
        self.assertEqual([p.name for p in raw], ["toc_semantic"])
        self.assertEqual(
            [p.name for p in semantic],
            ["image_translation", "content_policy"],
        )
        toc = raw[0]
        self.assertTrue(toc.matches(make_node("a", NodeType.PARAGRAPH)))
        self.assertFalse(toc.matches(make_node("a", NodeType.FIGURE)))
        # ContentPolicy 全类型匹配
        cp = semantic[1]
        self.assertTrue(cp.matches(make_node("a", NodeType.FIGURE)))


if __name__ == "__main__":
    unittest.main()