# -*- coding: utf-8 -*-
"""V1.18 — Phase D: Document Observability Framework（D0–D9）定向测试。

覆盖：
- D0 TraceContext：DocumentID/NodeID 层次、父链、子节点注册；
- D1 SnapshotSystem：PageModel/DocumentModel 快照、JSON/Binary 落盘往返、
  快照链、digest 确定性、stage diff；
- D2 PassDiff：节点增删 + 字段级 Before→After（含嵌套 metadata）；
- D3 Overlay：角色着色（Heading 绿/TOC 蓝/Formula 黄/Image 红/Caption 青）；
- D4 LayoutDebug：BBox/Baseline/LineHeight/Ascender/Descender 度量 + SVG；
- D5 DecisionLog：证据逐项 + fuse 置信度 + 按节点查询；
- D6 DiagnosticEngine：编译器式 warning/error 行 + 按 node_id 索引；
- D7 Replay：stage 输入存储 + 免重译回放（第二次全部 memo 命中）；
- D8 InspectorGUI：自包含 HTML（树/Overlay/生命周期/决策/诊断）+ 注入防护；
- D9 Regression：快照哈希基线 + 目录 diff + 回归报告。
"""

import json
import os
import tempfile
import unittest

from pdf2zh.v3.canonical_page import (
    BlockModel,
    GlyphModel,
    LineModel,
    PageModel,
    SpanModel,
)
from pdf2zh.v3.document_model import DocumentModel
from pdf2zh.v3.inspector_view import (
    build_inspector_html,
    build_inspector_html_from_bundle,
)
from pdf2zh.v3.layout_debug import (
    line_metrics_from_page,
    line_metrics_from_snapshot,
    metrics_json,
    render_svg as layout_svg,
)
from pdf2zh.v3.observability import (
    DecisionLog,
    DiagnosticEngine,
    DocumentID,
    NodeID,
    ObsSession,
    SnapshotStore,
    TraceContext,
    capture_snapshot,
    make_session,
    new_document_id,
)
from pdf2zh.v3.overlay_view import (
    overlay_for_page,
    overlay_from_snapshot,
    render_svg as overlay_svg,
)
from pdf2zh.v3.pass_diff import (
    diff_json,
    diff_snapshots,
    render_diff_report,
)
from pdf2zh.v3.regression import (
    build_baseline_dir,
    diff_baselines,
    diff_records,
    record_session,
    run_snapshot_regression,
    snapshot_hash,
)
from pdf2zh.v3.replay import (
    ReplaySystem,
    StageInputStore,
    TranslationMemo,
)

# ── 合成 fixtures（无 PDF） ─────────────────────────────────────────────


def make_glyph(text, x, y, size=10.0, decode="ok"):
    return GlyphModel(
        char=text,
        cid=ord(text[0]),
        font="Helvetica",
        size=size,
        x0=x,
        y0=y,
        x1=x + size * 0.6,
        y1=y + size,
        decode=decode,
    )


def make_span(text, x, y, size=10.0, decode="ok"):
    return SpanModel(
        font="Helvetica",
        size=size,
        text=text,
        x0=x,
        y0=y,
        x1=x + size * 0.6 * len(text),
        y1=y + size,
        glyphs=[
            make_glyph(c, x + i * size * 0.6, y, size, decode)
            for i, c in enumerate(text)
        ],
    )


def make_line(text, x0, y0, x1, y1, baseline, size=10.0, decode="ok"):
    return LineModel(
        text=text,
        baseline=baseline,
        x0=x0,
        y0=y0,
        x1=x1,
        y1=y1,
        spans=[make_span(text, x0, y0, size, decode)],
    )


def make_block(
    kind, text, x0=50.0, y0=100.0, x1=500.0, y1=120.0, lines=None, metadata=None
):
    lines = lines or [make_line(text, x0, y0, x1, y1, baseline=y0 + 8.0)]
    return BlockModel(
        text=text,
        kind=kind,
        x0=x0,
        y0=y0,
        x1=x1,
        y1=y1,
        lines=lines,
        metadata=dict(metadata or {}),
    )


def make_page(pno=1, blocks=None, w=600.0, h=800.0):
    return PageModel(
        page_num=pno,
        width=w,
        height=h,
        blocks=blocks or [make_block("paragraph", "hello world")],
    )


def make_document(pages=None):
    return DocumentModel(pages=pages or [make_page()])


def role_page():
    """每角色一块，验证 D3 着色。"""
    return make_page(
        pno=1,
        blocks=[
            make_block(
                "heading", "Chapter 1", y0=740.0, y1=760.0, metadata={"role": "heading"}
            ),
            make_block(
                "toc",
                "1.1 引言 ........ 3",
                y0=710.0,
                y1=725.0,
                metadata={"role": "toc"},
            ),
            make_block(
                "formula",
                "E = mc^2",
                y0=680.0,
                y1=700.0,
                metadata={"role": "formula", "formula_density": 0.9},
            ),
            make_block(
                "image",
                "",
                y0=600.0,
                y1=660.0,
                x0=400.0,
                x1=560.0,
                metadata={"role": "image"},
            ),
            make_block(
                "caption", "Fig 1", y0=590.0, y1=600.0, metadata={"role": "caption"}
            ),
        ],
    )


class TestD0TraceContext(unittest.TestCase):
    def test_document_id_unique(self):
        a, b = new_document_id(), new_document_id()
        self.assertNotEqual(a, b)
        self.assertTrue(str(DocumentID("DOC_x")).startswith("DOC_"))

    def test_node_id_hierarchy(self):
        tr = TraceContext("DOC_t")
        page = tr.node_id("P", 1)
        block = tr.node_id("B", 0, parent=page)
        line = tr.node_id("L", 2, parent=block)
        self.assertEqual(str(page), "DOC_t::P1")
        self.assertEqual(str(block), "DOC_t::P1::B0")
        self.assertEqual(str(line), "DOC_t::P1::B0::L2")
        self.assertEqual(line.parent, block)
        self.assertEqual(block.parent, page)
        self.assertEqual(page.parent.full, "DOC_t")  # 根 = 文档节点
        self.assertEqual(line.kind, "L")

    def test_ancestors_and_children(self):
        tr = TraceContext("DOC_t")
        page = tr.node_id("P", 1)
        b0 = tr.node_id("B", 0, parent=page)
        b1 = tr.node_id("B", 1, parent=page)
        chain = tr.ancestors(b0)
        self.assertEqual([str(n) for n in chain], ["DOC_t", "DOC_t::P1"])
        self.assertEqual([str(c) for c in tr.children_of(page)], [str(b0), str(b1)])


class TestD1SnapshotSystem(unittest.TestCase):
    def test_capture_page_snapshot(self):
        tr = TraceContext("DOC_s")
        snap = capture_snapshot(role_page(), "render", tr)
        self.assertEqual(snap["doc_id"], "DOC_s")
        self.assertEqual(snap["stage"], "render")
        self.assertEqual(snap["stats"]["blocks"], 5)
        self.assertIn("DOC_s::P1", snap["nodes"])
        self.assertIn("DOC_s::P1::B0", snap["nodes"])
        self.assertEqual(snap["nodes"]["DOC_s::P1::B0"]["kind"], "heading")
        json.dumps(snap)  # 可序列化

    def test_capture_document_snapshot(self):
        tr = TraceContext("DOC_d")
        doc = make_document([make_page(1), make_page(2)])
        snap = capture_snapshot(doc, "layout", tr)
        self.assertEqual(snap["stats"]["pages"], 2)
        self.assertEqual(snap["stats"]["blocks"], 2)

    def test_capture_none_safe(self):
        tr = TraceContext("DOC_e")
        snap = capture_snapshot(None, "parse", tr)
        self.assertEqual(snap["stats"], {"pages": 0, "blocks": 0, "lines": 0})

    def test_store_chain_and_replace(self):
        store = SnapshotStore("DOC_c")
        p = make_page()
        store.add_stage(p, "parse")
        store.add_stage(p, "render")
        self.assertEqual(store.stages(), ["parse", "render"])
        store.add_stage(p, "parse")  # 同 stage 替换，顺序保持
        self.assertEqual(store.stages(), ["parse", "render"])
        self.assertEqual(store.latest()["stage"], "render")

    def test_store_serialize_binary_roundtrip(self):
        store = SnapshotStore("DOC_b")
        store.add_stage(role_page(), "layout")
        with tempfile.TemporaryDirectory() as d:
            jpath = os.path.join(d, "s.json")
            bpath = os.path.join(d, "s.bin")
            store.save(jpath)
            store.save(bpath, binary=True)
            j = SnapshotStore.load(jpath)
            b = SnapshotStore.load(bpath)
        self.assertEqual(j.stages(), ["layout"])
        self.assertEqual(b.stages(), ["layout"])
        self.assertEqual(j.digest(), store.digest())

    def test_digest_excludes_timestamp(self):
        store = SnapshotStore("DOC_h")
        p = make_page()
        store.add_stage(p, "render")
        self.assertEqual(store.digest(), store.digest())

    def test_store_diff_stages(self):
        store = SnapshotStore("DOC_x")
        store.add_stage(make_page(), "parse")
        blocks = [make_block("paragraph", "hi")]
        blocks[0].metadata["translated"] = "你好"
        store.add_stage(make_page(blocks=blocks), "render")
        report = store.diff_stages("parse", "render")
        self.assertTrue(report.entries)
        changed = [e for e in report.entries if e.kind == "changed"]
        self.assertTrue(changed)


class TestD2PassDiff(unittest.TestCase):
    def _snap(self, kinds):
        tr = TraceContext("DOC_d2")
        blocks = [make_block(k, f"t{i}") for i, k in enumerate(kinds)]
        return capture_snapshot(make_page(blocks=blocks), "stage", tr)

    def test_identical_snapshot_empty(self):
        a = self._snap(["paragraph", "heading"])
        self.assertTrue(diff_snapshots(a, a).empty)

    def test_node_added_removed(self):
        a = self._snap(["paragraph"])
        b = self._snap(["paragraph", "toc"])
        rep = diff_snapshots(a, b)
        self.assertEqual(rep.added_nodes, ["DOC_d2::P1::B1"])
        rep2 = diff_snapshots(b, a)
        self.assertEqual(rep2.removed_nodes, ["DOC_d2::P1::B1"])

    def test_field_changed(self):
        base_payload = dict(self._snap(["paragraph"])["nodes"]["DOC_d2::P1::B0"])
        a = {"nodes": {"DOC_d2::P1::B0": base_payload}}
        b_payload = dict(base_payload)
        b_payload["metadata"] = dict(base_payload["metadata"])
        b_payload["metadata"]["translated"] = "你好"
        b = {"nodes": {"DOC_d2::P1::B0": b_payload}}
        rep = diff_snapshots(a, b)
        entry = [e for e in rep.entries if e.field == "metadata.translated"]
        self.assertEqual(len(entry), 1)
        self.assertEqual(entry[0].kind, "changed")
        self.assertEqual(entry[0].after, "你好")

    def test_render_diff_report(self):
        a = self._snap(["paragraph"])
        b = self._snap(["paragraph", "toc"])
        text = render_diff_report(diff_snapshots(a, b))
        self.assertIn("PassDiff", text)
        self.assertIn("+ DOC_d2::P1::B1", text)

    def test_diff_json_file(self):
        a = self._snap(["paragraph"])
        b = self._snap(["paragraph", "toc"])
        with tempfile.TemporaryDirectory() as d:
            pa = os.path.join(d, "a.json")
            pb = os.path.join(d, "b.json")
            with open(pa, "w", encoding="utf-8") as f:
                json.dump(a, f, ensure_ascii=False)
            with open(pb, "w", encoding="utf-8") as f:
                json.dump(b, f, ensure_ascii=False)
            rep = diff_json(pa, pb)
        self.assertEqual(rep.added_nodes, ["DOC_d2::P1::B1"])


class TestD3Overlay(unittest.TestCase):
    def test_role_colors(self):
        recs = overlay_for_page(role_page())
        by_kind = {r.kind: r.color for r in recs}
        self.assertEqual(by_kind["heading"], "#2e7d32")  # 绿
        self.assertEqual(by_kind["toc"], "#1565c0")  # 蓝
        self.assertEqual(by_kind["formula"], "#f9a825")  # 黄
        self.assertEqual(by_kind["image"], "#c62828")  # 红
        self.assertEqual(by_kind["caption"], "#00838f")  # 青
        self.assertEqual(len(recs), 5)

    def test_zero_bbox_skipped(self):
        blocks = [make_block("heading", "x", x0=0.0, y0=0.0, x1=0.0, y1=0.0)]
        recs = overlay_for_page(make_page(blocks=blocks))
        self.assertEqual(recs, [])

    def test_render_svg_and_html(self):
        recs = overlay_for_page(role_page())
        svg = overlay_svg(recs, 600.0, 800.0)
        self.assertIn("<svg", svg)
        self.assertIn("#2e7d32", svg)
        html = overlay_view_html(recs)
        self.assertIn("<!doctype html>", html)

    def test_overlay_from_snapshot(self):
        tr = TraceContext("DOC_o")
        snap = capture_snapshot(role_page(), "render", tr)
        recs = overlay_from_snapshot(snap)
        self.assertEqual(len(recs), 5)
        kinds = {r.kind for r in recs}
        self.assertIn("formula", kinds)


def overlay_view_html(recs):
    from pdf2zh.v3.overlay_view import render_html

    return render_html(recs, 600.0, 800.0)


class TestD4LayoutDebug(unittest.TestCase):
    def test_line_metrics_with_glyphs(self):
        block = make_block("paragraph", "abc", x0=50.0, y0=100.0, x1=120.0, y1=110.0)
        line = block.lines[0]
        line.baseline = 108.0  # glyph y1=110（顶） y0=100（底）
        line.y0, line.y1 = 100.0, 110.0
        line.spans[0].glyphs[0].y0 = 100.0
        line.spans[0].glyphs[0].y1 = 110.0
        m = line_metrics_from_page(make_page(blocks=[block]))[0]
        self.assertEqual(m.node_id, "P1::B0::L0")
        self.assertEqual(m.baseline, 108.0)
        self.assertAlmostEqual(m.line_height, 10.0)
        self.assertAlmostEqual(m.ascender, 2.0)  # 110 - 108
        self.assertAlmostEqual(m.descender, 8.0)  # 108 - 100
        self.assertEqual(m.glyph_count, 3)

    def test_snapshot_metrics(self):
        tr = TraceContext("DOC_l")
        snap = capture_snapshot(make_page(), "render", tr)
        ms = line_metrics_from_snapshot(snap)
        self.assertTrue(ms)
        self.assertTrue(all(m.node_id for m in ms))

    def test_layout_svg_marks(self):
        block = make_block("paragraph", "abc")
        ms = line_metrics_from_page(make_page(blocks=[block]))
        svg = layout_svg(ms, 600.0, 800.0)
        self.assertIn("<svg", svg)
        self.assertIn('stroke="#e53935"', svg)  # 基线红
        self.assertIn('stroke="#8e24aa"', svg)  # asc/desc 紫
        data = json.loads(metrics_json(ms))
        self.assertEqual(len(data), 1)
        self.assertIn("baseline", data[0])


class TestD5DecisionLog(unittest.TestCase):
    def test_record_with_evidence(self):
        log = DecisionLog()
        rec = log.record(
            "DOC_x::P1::B0",
            "translate:on",
            evidence={"structure": 0.9, "formula": 0.2},
            source="toc_gate",
            stage="semantic",
        )
        self.assertEqual(rec.node_id, "DOC_x::P1::B0")
        self.assertGreaterEqual(rec.confidence, 0.0)
        self.assertLessEqual(rec.confidence, 0.99)
        self.assertEqual(log.counts()["translate:on"], 1)

    def test_query_by_node_and_stage(self):
        log = DecisionLog()
        log.record(
            "DOC_x::P1::B0", "translate:on", evidence={"a": 0.9}, stage="semantic"
        )
        log.record("DOC_x::P1::B0", "render:toc", evidence={"a": 0.8}, stage="render")
        log.record(
            "DOC_x::P1::B1", "translate:off", evidence={"a": 0.1}, stage="semantic"
        )
        self.assertEqual(len(log.for_node("DOC_x::P1::B0")), 2)
        self.assertEqual(len(log.stage_records("semantic")), 2)
        data = log.to_dict()
        self.assertEqual(len(data["records"]), 3)


class TestD6DiagnosticEngine(unittest.TestCase):
    def test_low_confidence_toc_warning(self):
        engine = DiagnosticEngine()
        block = make_block(
            "toc",
            "1.1 Intro ...... 3",
            metadata={"kind": "toc", "toc_confidence": 0.3, "toc_scan": True},
        )
        doc = make_document([make_page(blocks=[block])])
        engine.run(doc)
        text = engine.format_report()
        self.assertIn("warning", text)
        self.assertIn("Page 1", text)
        self.assertIn("[p1_0]", text)
        issues = engine.diagnostics_for("p1_0")
        self.assertTrue(issues)
        self.assertEqual(issues[0]["code"], "toc_low_confidence")

    def test_format_issue_style(self):
        from pdf2zh.v3.diagnostics import DiagnosticIssue

        issue = DiagnosticIssue(
            code="x",
            node_id="p1_2",
            page=18,
            message="Formula may overlap page",
            severity="warning",
        )
        line = engine_format(issue)
        self.assertEqual(line, "warning: Page 18: Formula may overlap page [p1_2]")


def engine_format(issue):
    return DiagnosticEngine().format_issue(issue)


class TestD7Replay(unittest.TestCase):
    def test_stage_input_store(self):
        store = StageInputStore()
        store.save_input("translation", "DOC::P1::B0", {"text": "Hello"})
        store.save_input("translation", "DOC::P1::B1", {"text": "World"})
        store.save_input("layout", "DOC::P1::B0", {"text": "Hello"})
        self.assertEqual(len(store), 3)
        self.assertEqual(len(store.inputs_for("translation")), 2)
        self.assertEqual(store.stages(), ["translation", "layout"])

    def test_replay_never_retranslates(self):
        store = StageInputStore()
        store.save_input("translation", "DOC::P1::B0", {"text": "Hello"})
        memo = TranslationMemo()
        engine_calls = {"n": 0}

        def fn(item, m):
            src = item.payload["text"]
            try:
                return m.translate(src)
            except KeyError:
                engine_calls["n"] += 1  # 只有 miss 才真正翻译
                dst = "你好"
                m.store(src, dst)
                return dst

        sys = ReplaySystem(store, memo)
        r1 = sys.replay("translation", fn)
        r2 = sys.replay("translation", fn)
        self.assertEqual(engine_calls["n"], 1)  # 第二次回放零翻译调用
        self.assertEqual(r1.translated, 1)
        self.assertEqual(r2.memo_hits, 1)
        self.assertEqual(r2.failed, 0)
        self.assertIn("memo_hit=1", r2.summary())

    def test_replay_all(self):
        store = StageInputStore()
        store.save_input("parse", "DOC::P1", {"text": ""})
        sys = ReplaySystem(store, TranslationMemo())
        reports = sys.replay_all()
        self.assertEqual([r.stage for r in reports], ["parse"])
        self.assertEqual(len(reports[0].steps), 1)


class TestD8Inspector(unittest.TestCase):
    def _bundle(self):
        tr = TraceContext("DOC_insp")
        blocks = [
            make_block("heading", "Ch 1", y0=740.0, y1=760.0),
            make_block("toc", "1.1 x ..... 3", y0=710.0, y1=725.0),
        ]
        store = SnapshotStore("DOC_insp", trace=tr)
        store.add_stage(make_page(blocks=blocks), "layout")
        log = DecisionLog()
        log.record(
            "DOC_insp::P1::B0",
            "translate:on",
            evidence={"structure": 0.9},
            stage="layout",
        )
        from pdf2zh.v3.diagnostics import DiagnosticReport

        diag = DiagnosticEngine()
        diag.report = DiagnosticReport(issues=[])
        return store, log.to_dict(), diag.to_dict()

    def test_html_contains_parts(self):
        store, dec, diag = self._bundle()
        html = build_inspector_html(
            store,
            decisions=dec,
            diagnostics=diag,
            overlays=[{"page": "Page 1", "svg": "<svg/>"}],
        )
        self.assertIn("<!doctype html>", html)
        self.assertIn("DOC_insp", html)
        self.assertIn("layout", html)
        self.assertIn("overlays", html)
        self.assertIn("nodeView", html)
        self.assertIn("lifeView", html)

    def test_js_escape_no_script_injection(self):
        store = SnapshotStore("DOC_x")
        block = make_block("paragraph", "</script><script>alert(1)</script>")
        store.add_stage(make_page(blocks=[block]), "render")
        html = build_inspector_html(store)
        self.assertNotIn("</script><script>", html.replace("\\u003c/script>", ""))
        self.assertIn("\\u003c", html)  # 已转义

    def test_from_bundle(self):
        session = make_session("DOC_b2")
        session.capture(make_page(), "render")
        bundle = session.bundle()
        html = build_inspector_html_from_bundle(bundle)
        self.assertIn("DOC_b2", html)


class TestD9Regression(unittest.TestCase):
    def _snaps(self, stage_suffix="a"):
        tr = TraceContext("DOC_r")
        p = make_page()
        snap1 = capture_snapshot(p, f"parse_{stage_suffix}", tr)
        snap2 = capture_snapshot(p, f"render_{stage_suffix}", tr)
        return [snap1, snap2]

    def test_snapshot_hash_deterministic(self):
        snaps = self._snaps()
        self.assertEqual(snapshot_hash(snaps[0]), snapshot_hash(snaps[0]))
        other = dict(snaps[0])
        other["timestamp"] = 999999.0  # 时间戳不入 hash
        self.assertEqual(snapshot_hash(other), snapshot_hash(snaps[0]))
        self.assertNotEqual(snapshot_hash(snaps[0]), snapshot_hash(snaps[1]))

    def test_build_and_diff_consistent(self):
        import copy

        with tempfile.TemporaryDirectory() as d:
            build_baseline_dir(d, [("doc_a", self._snaps("a"))])
            a = _load(os.path.join(d, "doc_a.obs.json"))
            b = copy.deepcopy(a)
            self.assertTrue(diff_records(a, b)["consistent"])
            b["hashes"]["render_a"] = "deadbeef"
            self.assertFalse(diff_records(a, b)["consistent"])
            self.assertEqual(
                diff_records(a, b)["changed_stages"],
                {"render_a": {"a": a["hashes"]["render_a"], "b": "deadbeef"}},
            )

    def test_diff_baselines_dirs(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            build_baseline_dir(a, [("d1", self._snaps("a"))])
            build_baseline_dir(b, [("d1", self._snaps("b"))])
            diffs = diff_baselines(a, b)
            self.assertEqual(len(diffs), 1)
            self.assertFalse(diffs[0]["consistent"])
            self.assertIn("changed_stages", diffs[0])

    def test_run_snapshot_regression(self):
        with tempfile.TemporaryDirectory() as d:
            build_baseline_dir(d, [("ok", self._snaps("a"))])
            rep = run_snapshot_regression([("ok", self._snaps("a"))], d)
            self.assertTrue(rep.results[0].passed)
            rep2 = run_snapshot_regression([("new", self._snaps("a"))], d)
            self.assertFalse(rep2.results[0].passed)  # 缺失基线
            rep3 = run_snapshot_regression([("ok", self._snaps("b"))], d)
            self.assertFalse(rep3.results[0].passed)  # 内容漂移
            self.assertIn("Regression", rep.summary())

    def test_record_session(self):
        session = ObsSession("DOC_rs")
        session.capture(make_page(), "render_p0")
        with tempfile.TemporaryDirectory() as d:
            rec = record_session("s1", session.bundle(), d)
            self.assertEqual(rec["stem"], "s1")
            self.assertTrue(os.path.exists(os.path.join(d, "s1.snapshots.json")))
            self.assertTrue(os.path.exists(os.path.join(d, "s1.obs.json")))


class TestObsSession(unittest.TestCase):
    def test_session_bundle(self):
        session = ObsSession("DOC_z")
        session.capture(make_page(), "parse")
        session.record(
            "DOC_z::P1::B0", "keep", evidence={"structure": 0.8}, stage="parse"
        )
        session.diagnose(make_document())
        bundle = session.bundle()
        self.assertEqual(bundle["doc_id"], "DOC_z")
        self.assertEqual(bundle["decisions"]["counts"]["keep"], 1)
        self.assertIn("snapshots", bundle)
        self.assertIn("diagnostics", bundle)

    def test_make_session(self):
        s = make_session()
        self.assertIsInstance(s, ObsSession)


def _load(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


if __name__ == "__main__":
    unittest.main()
