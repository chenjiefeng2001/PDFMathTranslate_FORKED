# -*- coding: utf-8 -*-
"""V1.6 收尾 — 阶段三融合 / 题注编号 / TOC 变体。

覆盖：
- SemanticAnalyzer._apply_rule_classifier：规则流先行（高置信度采纳 +
  rule_role/rule_confidence 记录；BODY_TEXT 不覆盖；开关可关）；
- CaptionNodeProcessor：题注编号提取（Fig. 1 / Table 2: / 图 3 / 无编号）；
- TOC Grammar 变体：中文枚举、右括号编号、顿号编号。
"""

import unittest

from pdf2zh.v3.graph import DocumentGraph, DocumentNode, NodeType


class TestAnalyzerRuleFusion(unittest.TestCase):
    def _graph(self):
        g = DocumentGraph()
        g.add_node(
            DocumentNode(
                id="h",
                node_type=NodeType.PARAGRAPH,
                bbox=(50, 700, 550, 712),
                text="3. Results and Discussion",
                font_size=16,
                page_num=0,
            )
        )
        g.add_node(
            DocumentNode(
                id="c",
                node_type=NodeType.PARAGRAPH,
                bbox=(50, 650, 550, 665),
                text="Fig. 1. System architecture.",
                font_size=10,
                page_num=0,
            )
        )
        g.add_node(
            DocumentNode(
                id="f",
                node_type=NodeType.PARAGRAPH,
                bbox=(50, 600, 550, 615),
                text="x^2 + y^2 = z^2",
                font_size=10,
                page_num=0,
            )
        )
        g.add_node(
            DocumentNode(
                id="b",
                node_type=NodeType.PARAGRAPH,
                bbox=(50, 550, 550, 580),
                text="We discuss the experimental findings.",
                font_size=10,
                page_num=0,
            )
        )
        return g

    def _rules_only_config(self):
        from pdf2zh.v3.analyzer import AnalyzerConfig

        return AnalyzerConfig(
            use_rule_classifier=True,
            refine_heading_levels=False,
            detect_captions=False,
            detect_formulas=False,
            detect_footnotes=False,
            detect_headers_footers=False,
            detect_references=False,
            detect_sections=False,
            merge_fragments=False,
            detect_paragraph_boundaries=False,
        )

    def test_rule_classifier_adopts_high_confidence(self):
        from pdf2zh.v3.analyzer import SemanticAnalyzer

        g = self._graph()
        SemanticAnalyzer(self._rules_only_config()).analyze(g)
        types = {n.id: n.node_type for n in g.nodes}
        self.assertEqual(types["h"], NodeType.HEADING)
        self.assertEqual(types["f"], NodeType.FORMULA)
        meta = {n.id: n.metadata for n in g.nodes}
        self.assertGreaterEqual(meta["h"]["analysis.rule_confidence"], 0.65)
        # BODY_TEXT 默认 0.6 低于阈值 → 不覆盖
        self.assertEqual(types["b"], NodeType.PARAGRAPH)
        self.assertEqual(meta["b"]["analysis.rule_role"], "body_text")

    def test_rule_classifier_can_be_disabled(self):
        from pdf2zh.v3.analyzer import SemanticAnalyzer

        cfg = self._rules_only_config()
        cfg.use_rule_classifier = False
        g = self._graph()
        SemanticAnalyzer(cfg).analyze(g)
        types = {n.id: n.node_type for n in g.nodes}
        self.assertEqual(types["h"], NodeType.PARAGRAPH)
        self.assertNotIn("analysis.rule_role", {n.id: n.metadata for n in g.nodes}["h"])

    def test_rule_pass_with_full_analyzer_does_not_crash(self):
        from pdf2zh.v3.analyzer import SemanticAnalyzer

        g = self._graph()
        SemanticAnalyzer().analyze(g)  # 全 pass 下规则先行 + 图级兜底
        metas = {n.id: n.metadata for n in g.nodes}
        self.assertIn("analysis.rule_role", metas["b"])

    def test_existing_type_not_overridden(self):
        from pdf2zh.v3.analyzer import SemanticAnalyzer

        g = self._graph()
        g.nodes[1].node_type = NodeType.TABLE  # 已定型不覆盖
        SemanticAnalyzer().analyze(g)
        self.assertEqual(g.nodes[1].node_type, NodeType.TABLE)


class TestCaptionNumbering(unittest.TestCase):
    def _caption(self, text):
        from pdf2zh.v3.processors import CaptionNodeProcessor

        g = DocumentGraph()
        n = DocumentNode(
            id="c1",
            node_type=NodeType.CAPTION,
            bbox=(0, 0, 100, 10),
            text=text,
            page_num=0,
        )
        g.add_node(n)
        CaptionNodeProcessor().process(n, g)
        return n.metadata["semantic"]["caption"]

    def test_figure_number_extracted(self):
        cap = self._caption("Fig. 1. System architecture overview.")
        self.assertEqual(cap["number"], "1")
        self.assertTrue(cap["number_keep"])
        self.assertEqual(cap["title_remainder"], "System architecture overview.")

    def test_table_number_extracted(self):
        cap = self._caption("Table 2: Results summary")
        self.assertEqual(cap["number"], "2")

    def test_cn_number_extracted(self):
        cap = self._caption("图 3 系统总体架构")
        self.assertEqual(cap["number"], "3")

    def test_no_number_marks_false(self):
        cap = self._caption("Photo courtesy of the authors")
        self.assertFalse(cap["number_keep"])
        self.assertEqual(cap["number"], "")


class TestTOCVariants(unittest.TestCase):
    def test_zh_enum(self):
        from pdf2zh.v3.toc_semantics import parse_toc_entry

        e = parse_toc_entry("一、引言 .......... 3")
        self.assertEqual(e.kind.value, "section")
        self.assertEqual(e.number, "一")
        self.assertIn("引言", e.title)

    def test_close_paren(self):
        from pdf2zh.v3.toc_semantics import parse_toc_entry

        e = parse_toc_entry("1) Background")
        self.assertEqual(e.kind.value, "section")
        self.assertEqual(e.number, "1")
        self.assertEqual(e.title, "Background")

    def test_dunhao_numbered(self):
        from pdf2zh.v3.toc_semantics import parse_toc_entry

        e = parse_toc_entry("1、研究背景")
        self.assertEqual(e.kind.value, "section")
        self.assertEqual(e.number, "1")

    def test_plain_unchanged(self):
        from pdf2zh.v3.toc_semantics import parse_toc_entry

        e = parse_toc_entry("Introduction")
        self.assertEqual(e.kind.value, "plain")
        self.assertFalse(e.matched)


if __name__ == "__main__":
    unittest.main()
