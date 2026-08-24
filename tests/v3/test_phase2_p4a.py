"""Headless tests for Phase 2 - P4a: CollisionEngine + SVG/DOCX Renderer."""

from __future__ import annotations
import os, sys, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from pdf2zh.v3.graph import DocumentNode, NodeType
from pdf2zh.v3.visual_tree import (
    VisualTree,
    BoundingBox,
    Page,
    Paragraph,
    Line,
    TextRun,
)


class TestCollisionEngine(unittest.TestCase):
    def test_no_collisions(self):
        from pdf2zh.v3.layout import CollisionEngine

        nodes = [
            DocumentNode(
                id="a", node_type=NodeType.PARAGRAPH, bbox=(0, 0, 50, 50), text="x"
            ),
            DocumentNode(
                id="b", node_type=NodeType.PARAGRAPH, bbox=(100, 0, 150, 50), text="y"
            ),
        ]
        self.assertEqual(len(CollisionEngine.detect(nodes)), 0)

    def test_overlap_detected(self):
        from pdf2zh.v3.layout import CollisionEngine

        nodes = [
            DocumentNode(
                id="a", node_type=NodeType.PARAGRAPH, bbox=(0, 0, 100, 100), text="x"
            ),
            DocumentNode(
                id="b", node_type=NodeType.PARAGRAPH, bbox=(50, 50, 150, 150), text="y"
            ),
        ]
        self.assertGreater(len(CollisionEngine.detect(nodes)), 0)

    def test_has_collisions(self):
        from pdf2zh.v3.layout import CollisionEngine

        nodes = [
            DocumentNode(
                id="a", node_type=NodeType.PARAGRAPH, bbox=(0, 0, 100, 100), text="x"
            ),
            DocumentNode(
                id="b", node_type=NodeType.PARAGRAPH, bbox=(50, 50, 150, 150), text="y"
            ),
        ]
        self.assertTrue(CollisionEngine.has_collisions(nodes))

    def test_no_collisions_false(self):
        from pdf2zh.v3.layout import CollisionEngine

        nodes = [
            DocumentNode(
                id="a", node_type=NodeType.PARAGRAPH, bbox=(0, 0, 50, 50), text="x"
            ),
            DocumentNode(
                id="b", node_type=NodeType.PARAGRAPH, bbox=(100, 0, 150, 50), text="y"
            ),
        ]
        self.assertFalse(CollisionEngine.has_collisions(nodes))

    def test_count_critical(self):
        from pdf2zh.v3.layout import CollisionEngine

        nodes = [
            DocumentNode(
                id="a", node_type=NodeType.PARAGRAPH, bbox=(0, 0, 100, 100), text="x"
            ),
            DocumentNode(
                id="b", node_type=NodeType.PARAGRAPH, bbox=(50, 50, 150, 150), text="y"
            ),
        ]
        self.assertGreater(CollisionEngine.count_critical(nodes), 0)

    def test_empty(self):
        from pdf2zh.v3.layout import CollisionEngine

        self.assertEqual(len(CollisionEngine.detect([])), 0)


class TestSVGRenderer(unittest.TestCase):
    def setUp(self):
        from pdf2zh.v3.renderer import SVGRenderer

        self.renderer = SVGRenderer()
        self.tree = VisualTree()
        page = Page(id="p1", width=612, height=792, page_num=0)
        para = Paragraph(id="para1", bbox=BoundingBox(50, 50, 500, 20))
        line = Line(id="l1", baseline=52)
        run = TextRun(id="r1", text="Hello SVG", font="sans-serif")
        line.add_run(run)
        para.add_line(line)
        page.add_child(para)
        self.tree.add_page(page)

    def test_render_bytes(self):
        self.assertIsInstance(self.renderer.render(self.tree), bytes)

    def test_render_contains_text(self):
        self.assertIn("Hello SVG", self.renderer.render(self.tree).decode("utf-8"))

    def test_render_page_bytes(self):
        self.assertIsInstance(self.renderer.render_page(self.tree.pages[0]), bytes)


class TestDOCXRenderer(unittest.TestCase):
    def setUp(self):
        from pdf2zh.v3.renderer import DOCXRenderer

        self.renderer = DOCXRenderer()
        self.tree = VisualTree()
        page = Page(id="p1", width=612, height=792, page_num=0)
        para = Paragraph(id="para1", bbox=BoundingBox(50, 50, 500, 20))
        line = Line(id="l1", baseline=52)
        run = TextRun(id="r1", text="Hello DOCX", font="sans-serif")
        line.add_run(run)
        para.add_line(line)
        page.add_child(para)
        self.tree.add_page(page)

    def test_render_bytes(self):
        self.assertIsInstance(self.renderer.render(self.tree), bytes)

    def test_render_page_bytes(self):
        self.assertIsInstance(self.renderer.render_page(self.tree.pages[0]), bytes)


class TestRendererFactory(unittest.TestCase):
    def test_svg(self):
        from pdf2zh.v3.renderer import RendererFactory, SVGRenderer

        self.assertIsInstance(RendererFactory.create("svg"), SVGRenderer)

    def test_docx(self):
        from pdf2zh.v3.renderer import RendererFactory, DOCXRenderer

        self.assertIsInstance(RendererFactory.create("docx"), DOCXRenderer)

    def test_all_formats(self):
        from pdf2zh.v3.renderer import RendererFactory

        fmts = RendererFactory.supported_formats()
        for f in ("pdf", "html", "markdown", "svg", "docx"):
            self.assertIn(f, fmts)


if __name__ == "__main__":
    unittest.main(verbosity=2)
