# -*- coding: utf-8 -*-
"""V9.0 — DocumentPipeline：单一核心 IR 生命周期编排测试。

核心断言：RAW → SEMANTIC 全程只有一份 DocumentGraph（节点 id 集合
不变、不复制数据到第二份 IR）；处理器错误被容错记录；DocumentIR
只是同图的可序列化视图。
"""

import unittest

import numpy as np

from pdf2zh.v3.graph import (
    DocumentGraph,
    DocumentNode,
    NodeType,
)
from pdf2zh.v3.processors import (
    NodeProcessor,
    NodeStage,
    ProcessorRegistry,
    STAGE_KEY,
)
from pdf2zh.v3.document_pipeline import (
    DocumentPipeline,
    PipelineReport,
    run_semantic_pipeline,
    view_as_ir,
)
from pdf2zh.v3.document_ir import TranslationRole, SemanticRole


def make_node(nid, node_type, text="", page=0, bbox=(0, 0, 100, 20)):
    return DocumentNode(
        id=nid, node_type=node_type, bbox=bbox, text=text, page_num=page
    )


def build_synthetic_graph():
    graph = DocumentGraph()
    graph.add_node(make_node("p_toc", NodeType.PARAGRAPH, text="Chapter 3", page=0))
    graph.add_node(make_node("p_plain", NodeType.PARAGRAPH, text="Intro", page=0))
    graph.add_node(
        make_node("p_formula", NodeType.PARAGRAPH, text="E = {v12} mc2", page=1)
    )
    graph.add_node(make_node("c_code", NodeType.CODE, text="print(1)", page=1))
    img = make_node("img1", NodeType.IMAGE, page=1, bbox=(0, 0, 64, 64))
    px = np.zeros((32, 32, 3), dtype=np.uint8)
    px[::2, ::2] = (255, 255, 255)
    img.metadata["pixels"] = px
    graph.add_node(img)
    graph.add_node(make_node("fig1", NodeType.FIGURE, page=2, bbox=(0, 0, 100, 40)))
    graph.add_node(make_node("cap1", NodeType.CAPTION, page=2, bbox=(0, 45, 100, 60)))
    return graph


class TestDocumentPipeline(unittest.TestCase):
    def test_single_ir_semantics(self):
        graph = build_synthetic_graph()
        before = {n.id for n in graph.nodes}
        n_nodes = len(graph.nodes)
        report = DocumentPipeline().run(graph, (NodeStage.RAW, NodeStage.SEMANTIC))
        self.assertEqual({n.id for n in graph.nodes}, before)  # 不增不删不改建
        self.assertEqual(len(graph.nodes), n_nodes)
        self.assertTrue(report.ok())

    def test_stages_and_applied_counts(self):
        graph = build_synthetic_graph()
        report = DocumentPipeline().run(graph, (NodeStage.RAW, NodeStage.SEMANTIC))
        self.assertEqual(report.stages, ["raw", "semantic"])
        self.assertGreaterEqual(report.applied.get("raw:toc_semantic", 0), 1)
        self.assertGreaterEqual(report.applied.get("semantic:image_translation", 0), 1)
        self.assertGreaterEqual(report.applied.get("semantic:content_policy", 0), 1)

    def test_stage_stamp_written(self):
        graph = build_synthetic_graph()
        DocumentPipeline().run(graph, (NodeStage.RAW, NodeStage.SEMANTIC))
        self.assertTrue(
            all(n.metadata.get(STAGE_KEY) == "semantic" for n in graph.nodes)
        )

    def test_caption_linked_via_finalize(self):
        graph = build_synthetic_graph()
        DocumentPipeline().run(graph, (NodeStage.RAW, NodeStage.SEMANTIC))
        edges = graph.get_edges(source_id="cap1", edge_type=None)
        self.assertTrue(any(e.edge_type.value == "caption_of" for e in edges))

    def test_processor_error_is_captured_not_fatal(self):
        class Boom(NodeProcessor):
            name = "boom"
            stages = (NodeStage.SEMANTIC,)
            target_types = (NodeType.PARAGRAPH,)

            def process(self, node, graph):
                raise RuntimeError("kaboom")

        reg = ProcessorRegistry(
            [
                Boom(),
            ]
        )
        graph = build_synthetic_graph()
        report = DocumentPipeline(reg).run(graph, (NodeStage.SEMANTIC,))
        self.assertFalse(report.ok())
        self.assertEqual(len(report.errors), 3)  # 三个 PARAGRAPH 节点各一
        self.assertEqual(len(graph.nodes), len(build_synthetic_graph().nodes))

    def test_report_to_dict(self):
        graph = build_synthetic_graph()
        report = DocumentPipeline().run(graph, (NodeStage.RAW,))
        d = report.to_dict()
        self.assertEqual(d["stages"], ["raw"])
        self.assertTrue(d["ok"])
        self.assertEqual(d["node_count"], len(graph.nodes))


class TestRunSemanticPipeline(unittest.TestCase):
    def test_convenience(self):
        graph = build_synthetic_graph()
        report = run_semantic_pipeline(graph)
        self.assertEqual(report.stages, ["raw", "semantic"])
        toc_node = graph["p_toc"]
        self.assertEqual(toc_node.node_type, NodeType.TOC_ENTRY)
        self.assertEqual(toc_node.metadata["policy"], "template_local")


class TestViewAsIR(unittest.TestCase):
    def test_ir_is_view_of_same_graph(self):
        graph = build_synthetic_graph()
        report = run_semantic_pipeline(graph)
        iron = view_as_ir(graph, title="t", source_lang="en", target_lang="zh-CN")
        # IR 视图 = 同图内容节点 + 视图补充的 Page Section 容器
        self.assertGreaterEqual(iron.node_count, len(graph.nodes))
        for n in graph.nodes:
            self.assertIsNotNone(iron.get_node(n.id))
        self.assertEqual(iron.title, "t")
        self.assertTrue(report.ok())

    def test_roles_for_new_node_types(self):
        graph = build_synthetic_graph()
        run_semantic_pipeline(graph)
        ir = view_as_ir(graph, target_lang="zh-CN")
        toc = ir.get_node("p_toc")
        self.assertEqual(toc.semantic, SemanticRole.TOC_ENTRY)
        self.assertEqual(toc.translation, TranslationRole.TRANSLATE)
        img = ir.get_node("img1")
        self.assertEqual(img.semantic, SemanticRole.IMAGE)
        self.assertEqual(img.translation, TranslationRole.SKIP)
        self.assertEqual(img.metadata["node_type"], "image")

    def test_stage_metadata_merged(self):
        graph = build_synthetic_graph()
        run_semantic_pipeline(graph)
        ir = view_as_ir(graph)
        toc = ir.get_node("p_toc")
        self.assertEqual(toc.metadata.get("v3.stage"), "semantic")
        self.assertEqual(toc.metadata.get("policy"), "template_local")
        self.assertEqual(toc.metadata["semantic"]["toc"]["kind"], "chapter")


if __name__ == "__main__":
    unittest.main()
