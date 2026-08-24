# -*- coding: utf-8 -*-
"""V1.17 — Phase 6：Document Compiler Runtime。

覆盖：
- 6.1 DOM/版本系统：DocumentRuntime.open/edit/undo/diff/version history；
- 6.2 ResourceManager：字体/图片注册 + from_model；
- 6.3 Query API：kind/page/translated/confidence_below/ids 组合；
- 6.4 Cache：五层缓存、translate 复用、invalidate_page 级联；
- 6.5 BuildSystem：依赖闭包 + 增量计划（rebuilt/cached）；
- 6.6 Plugins：PassPlugin/TranslatePlugin/ExportPlugin + 注册表容错；
- 6.8 Runtime API：translate(缓存)/render_page/export(markdown/html/text)；
- 6.9 Inspector：节点视图含版本/缓存/资源。
"""

import unittest
from unittest.mock import Mock

from pdfminer.layout import LTChar, LTPage

from pdf2zh.v3.document_model import build_document_model


def make_char(x, y, text="A", size=10.0, fontname="Helvetica"):
    font = Mock()
    font.fontname = fontname
    font.get_descent.return_value = -0.25
    ch = LTChar(
        (1, 0, 0, 1, x, y),
        font,
        size,
        1.0,
        0.0,
        text,
        textwidth=0.5,
        textdisp=(0.0, 0.0),
        ncs=Mock(),
        graphicstate=Mock(),
    )
    ch.cid = ord(text[0])
    ch.font = font
    return ch


def add_text(page, x0, y, text, adv=9.0, fontname="Helvetica", size=10.0):
    for i, t in enumerate(text):
        page.add(make_char(x0 + i * adv, y, t, fontname=fontname, size=size))


def build_model():
    page = LTPage(1, (0, 0, 600, 800))
    add_text(page, 50, 760, "5 Methodology", size=16)
    add_text(page, 50, 740, "5.1 Data Collection ...... 10")
    add_text(page, 50, 720, "The kernel scheduler runs threads.", fontname="Times")
    add_text(page, 50, 700, "x^2 + y^2 = z^2")
    return build_document_model([page])


class TestVersionManager(unittest.TestCase):
    def test_record_undo_diff(self):
        from pdf2zh.v3.runtime_doc import VersionManager

        vm = VersionManager()
        vm.record("p1_2", "text", "hello")
        vm.record("p1_2", "translated", "你好")
        self.assertEqual(vm.version("p1_2"), 2)
        self.assertEqual(len(vm.history_of("p1_2")), 2)
        d = vm.diff("p1_2", 1, 2)
        self.assertEqual(d["field"], "translated")
        rev = vm.undo("p1_2")
        self.assertEqual(rev.field, "translated")
        self.assertEqual(vm.version("p1_2"), 1)
        self.assertIsNone(vm.undo("p9_9"))


class TestRuntimeEditUndo(unittest.TestCase):
    def test_edit_and_undo(self):
        from pdf2zh.v3.runtime_doc import DocumentRuntime

        model = build_model()
        # V1.23：Lv2 段拆把标题/正文拆开，正文段落下标不再固定为 p1_1。
        tid = next(
            (
                "p1_%d" % i
                for i, b in enumerate(model.pages[0].blocks)
                if b.kind == "paragraph" and b.text
            ),
            None,
        )
        self.assertIsNotNone(tid)
        runtime = DocumentRuntime().open(model)
        res = runtime.edit(tid, text="The kernel scheduler runs threads here.")
        self.assertTrue(res["ok"])
        block = runtime._find_block(tid)
        self.assertEqual(block.text, "The kernel scheduler runs threads here.")
        # undo 恢复
        runtime.undo(tid)
        self.assertEqual(block.text, "The kernel scheduler runs threads.")
        # 编辑触发同页缓存失效
        self.assertEqual(runtime.cache.stats().hits, 0)


class TestResourceManager(unittest.TestCase):
    def test_register_and_from_model(self):
        from pdf2zh.v3.resources import ResourceManager

        rm = ResourceManager()
        rm.register_font("NotoSerif", family="Noto", size=10.5)
        self.assertEqual(rm.get_font("NotoSerif").family, "Noto")
        rm2 = ResourceManager().from_model(build_model())
        self.assertGreaterEqual(len(rm2.fonts), 1)
        self.assertEqual(rm2.summary()["fonts"], len(rm2.fonts))
        self.assertIn("Times", rm2.fonts)


class TestQuery(unittest.TestCase):
    def test_query_filters(self):
        from pdf2zh.v3.query import query

        model = build_model()
        formulas = query(model).kind("formula").execute()
        self.assertGreaterEqual(len(formulas), 1)
        self.assertEqual(formulas[0]["kind"], "formula")
        # 未翻译（pending）
        pending = query(model).translated("pending").ids()
        self.assertTrue(pending)
        # 页码过滤
        p1 = query(model).page(1).count()
        self.assertGreaterEqual(p1, 1)
        # 组合
        combo = query(model).kind("paragraph", "heading").page(1).execute()
        self.assertTrue(combo)
        # 不存在的 kind
        self.assertEqual(query(model).kind("figure").count(), 0)

    def test_where_predicate(self):
        from pdf2zh.v3.query import query

        model = build_model()
        long_blocks = query(model).where(lambda b, bid: len(b.text or "") > 10).ids()
        self.assertTrue(long_blocks)


class TestCache(unittest.TestCase):
    def test_translate_cache(self):
        from pdf2zh.v3.cache import DocumentCache

        cache = DocumentCache()
        calls = []

        def fn(t):
            calls.append(t)
            return "译:" + t

        self.assertEqual(cache.translate("kernel", fn), "译:kernel")
        self.assertEqual(cache.translate("kernel", fn), "译:kernel")
        self.assertEqual(len(calls), 1)  # 第二次命中缓存

    def test_invalidate_page(self):
        from pdf2zh.v3.cache import DocumentCache

        cache = DocumentCache()
        cache.set("parse", "p1", {"ok": True})
        cache.set("render", "p1", {"ok": True})
        cache.set("render", "p2", {"ok": True})
        cleared = cache.invalidate_page(1)
        self.assertGreaterEqual(cleared["parse"], 1)
        self.assertGreaterEqual(cleared["render"], 1)
        # p2 不受影响
        self.assertIsNotNone(cache.get("render", "p2"))

    def test_lru_capacity(self):
        from pdf2zh.v3.cache import DocumentCache

        cache = DocumentCache(capacities={"translation": 2})
        cache.set("translation", "a", 1)
        cache.set("translation", "b", 2)
        cache.set("translation", "c", 3)
        self.assertIsNone(cache.get("translation", "a"))
        self.assertIsNotNone(cache.get("translation", "c"))

    def test_stats(self):
        from pdf2zh.v3.cache import DocumentCache

        cache = DocumentCache()
        cache.get("translation", "x")
        cache.set("translation", "y", 1)
        stats = cache.stats()
        self.assertGreaterEqual(stats.misses, 1)
        self.assertIn("translation", stats.layers)


class TestBuildSystem(unittest.TestCase):
    def test_dependency_closure(self):
        from pdf2zh.v3.build_system import DependencyGraph

        g = DependencyGraph()
        g.register_block("p1_2", is_translatable=True)
        affected = g.closure(["p1_2"])
        self.assertIn("p1_2", affected)

    def test_build_plan(self):
        from pdf2zh.v3.build_system import BuildSystem, DependencyGraph
        from pdf2zh.v3.incremental import IncrementalEngine

        model = build_model()
        graph = DependencyGraph().from_model(model)
        engine = IncrementalEngine()
        engine.register(model)
        model.pages[0].blocks[2].text = "changed text here"
        system = BuildSystem(graph=graph, incremental=engine)
        plan = system.build(model, changed_ids=["p1_2"])
        self.assertIn("p1_2", plan.stages["translation"]["rebuilt"])
        self.assertIn("p1_2", plan.stages["layout"]["rebuilt"])
        d = plan.to_dict()
        self.assertIn("render", d)
        self.assertTrue(plan.summary())


class TestPlugins(unittest.TestCase):
    def test_registry_runs_plugins(self):
        from pdf2zh.v3.doc_passes import NormalizePass
        from pdf2zh.v3.plugins import (
            ExportPlugin,
            PassPlugin,
            PluginRegistry,
            TranslatePlugin,
        )

        model = build_model()
        registry = PluginRegistry()
        registry.register(PassPlugin(NormalizePass()))
        registry.register(TranslatePlugin(lambda t: "译:" + t))
        registry.register(ExportPlugin(lambda m: "exported", name="markdown"))
        self.assertIn("normalize", registry.available())
        results = registry.run(model)
        self.assertIn("translate", results)
        self.assertIn("markdown", results)
        self.assertEqual(registry.outputs["markdown"], "exported")
        # 翻译插件生效
        translated = [
            b for p in model.pages for b in p.blocks if b.metadata.get("translated")
        ]
        self.assertTrue(translated)

    def test_plugin_failure_tolerated(self):
        from pdf2zh.v3.plugins import DocumentPlugin, PluginRegistry

        class BadPlugin(DocumentPlugin):
            name = "bad"

            def process(self, doc):
                raise RuntimeError("boom")

        model = build_model()
        results = PluginRegistry().register(BadPlugin()).run(model)
        self.assertIn("error", results["bad"])


class TestExports(unittest.TestCase):
    def test_markdown_export(self):
        from pdf2zh.v3.exports import export_markdown
        from pdf2zh.v3.doc_passes import default_pass_manager

        model = build_model()
        default_pass_manager().run(model)
        md = export_markdown(model)
        self.assertIn("5 Methodology", md)  # 标题行
        self.assertIn("$$", md)  # formula
        self.assertIn("5.1", md)  # toc number

    def test_html_and_text_export(self):
        from pdf2zh.v3.exports import export_html, export_text

        model = build_model()
        html = export_html(model)
        self.assertIn("<section", html)
        self.assertIn("</html>", html)
        text = export_text(model)
        self.assertIn("Methodology", text)


class TestRuntimeAPI(unittest.TestCase):
    def test_runtime_translate_query_export(self):
        from pdf2zh.v3.runtime_doc import DocumentRuntime

        runtime = DocumentRuntime().open(build_model())
        stats = runtime.translate(lambda t: "译:" + t)
        self.assertGreaterEqual(stats["translated"], 1)
        done = runtime.query().translated("done").count()
        self.assertGreaterEqual(done, 1)
        md = runtime.export("markdown")
        self.assertTrue(md)
        html = runtime.export("html")
        self.assertIn("<!DOCTYPE html>", html)
        self.assertRaises(ValueError, runtime.export, "epub")

    def test_runtime_render_page_cache(self):
        from pdf2zh.v3.runtime_doc import DocumentRuntime

        runtime = DocumentRuntime().open(build_model())
        first = runtime.render_page(1)
        self.assertFalse(first["cached"])
        self.assertGreaterEqual(len(first["blocks"]), 1)
        second = runtime.render_page(1)
        self.assertTrue(second["cached"])

    def test_runtime_inspect(self):
        from pdf2zh.v3.runtime_doc import DocumentRuntime

        runtime = DocumentRuntime().open(build_model())
        view = runtime.inspect("p1_2")
        self.assertIsNotNone(view)
        self.assertEqual(view["block_id"], "p1_2")
        self.assertIn("version", view)
        self.assertIn("version_history", view)
        self.assertIn("cached_pages", view)
        self.assertIn("resource_fonts", view)
        self.assertIsNone(runtime.inspect("p9_9"))

    def test_runtime_summary(self):
        from pdf2zh.v3.runtime_doc import DocumentRuntime

        runtime = DocumentRuntime().open(build_model())
        s = runtime.summary()
        self.assertIn("pages=1", s)
        self.assertIn("versions=", s)


if __name__ == "__main__":
    unittest.main()
