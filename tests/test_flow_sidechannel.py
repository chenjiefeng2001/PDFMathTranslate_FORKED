"""Commit 7E-1 — FlowText side-channel + renderer tests.

Covers ``pdf2zh.v3.flow_sidechannel`` + ``pdf2zh.semantic.renderer.flow`` and
their wiring into ``document_model``:

1.  ``flow_text_from_block`` — geometry passthrough (origin / max_width /
    max_height copied verbatim from the block bbox, nothing re-inferred);
    translated text preferred over original; font-size resolution order.
2.  ``build_block_flow_payload`` — JSON-safe ``{kind, lines, commands, ...}``
    payload with positioned line commands; only the *translated* text is
    laid out; a layout failure yields ``layout_ok=False`` (never raises).
3.  ``render_flow_text`` — the full ``FlowText → lay_out() → commands``
    pipeline: wrap / overflow decisions delegated to ``lay_out``, and the
    first-line baseline comes verbatim from the primitive origin.
4.  ``FlowTextRenderer.render`` — one command per settled line; y-up
    coordinate spaces use a negative ``line_step``.
5.  Architecture: the side-channel never calls ``wrap_lines`` /
    ``shrink_to_fit`` / ``clip_text`` directly (all fit decisions go through
    ``lay_out``), and never re-derives position from level / index / width.
6.  ``document_model`` integration — a ``paragraph`` block's render payload
    has ``kind == "flow"`` with commands built from the *translated* text.
"""

import inspect
import json
import unittest

from pdf2zh.semantic.layout.overflow import OverflowPolicy
from pdf2zh.semantic.renderer.flow import (
    FLOW_COMMAND_KIND,
    FlowTextRenderer,
    render_flow_text,
)
from pdf2zh.v3.canonical_page import BlockModel, LineModel, PageModel, SpanModel
from pdf2zh.v3.document_model import (
    DocumentModel,
    render_plan_from_model,
    translate_document,
)
from pdf2zh.v3.flow_sidechannel import (
    build_block_flow_payload,
    flow_text_from_block,
)

_SIZE = 10.0  # latin 5pt, CJK 10pt


def _measure(text, size=_SIZE):
    w = 0.0
    for ch in text or "":
        if ord(ch) >= 0x2E80:
            w += size
        else:
            w += size * 0.5
    return w


def _block(
    text="Hello flow paragraph",
    translated=None,
    kind="paragraph",
    x0=72.0,
    y0=700.0,
    x1=540.0,
    y1=722.0,
    font_size=12.0,
    baseline=0.0,
    metadata=None,
):
    """一个 v3 段落块（可带行/基线/字号 metadata）。"""
    line = LineModel(text=text, baseline=baseline, x0=x0, y0=y0, x1=x1, y1=y1)
    if font_size:
        line.spans.append(SpanModel(size=font_size, text=text, x0=x0, y0=y0, x1=x1, y1=y1))
    md = dict(metadata or {})
    if translated is not None:
        md["translated"] = translated
    return BlockModel(
        text=text,
        kind=kind,
        x0=x0,
        y0=y0,
        x1=x1,
        y1=y1,
        lines=[line],
        metadata=md,
    )


# ── 1. flow_text_from_block：几何透传 + 译文优先 + 字号解析 ──────────────

class TestFlowTextFromBlock(unittest.TestCase):
    def test_geometry_passthrough(self):
        flow = flow_text_from_block(_block(x0=72.0, y0=700.0, x1=540.0, y1=722.0))
        # origin 取块左上角 (x0, y1)，宽/高来自 bbox 差 —— 全部原样透传。
        self.assertEqual(flow.origin, (72.0, 722.0))
        self.assertEqual(flow.max_width, 540.0 - 72.0)
        self.assertEqual(flow.max_height, 722.0 - 700.0)
        self.assertEqual(flow.kind, "flow")

    def test_translated_text_preferred(self):
        flow = flow_text_from_block(
            _block(text="Original text", translated="译后文本内容")
        )
        self.assertEqual(flow.text, "译后文本内容")

    def test_falls_back_to_original_text(self):
        flow = flow_text_from_block(_block(text="Original text"))
        self.assertEqual(flow.text, "Original text")

    def test_font_size_resolution_order(self):
        # metadata font_size 优先
        b = _block(font_size=0.0, metadata={"font_size": 14.0})
        self.assertEqual(flow_text_from_block(b).line_height, 14.0 * 1.4)
        # 其次 block.font_size（来自 span）
        b2 = _block(font_size=12.0)
        self.assertEqual(flow_text_from_block(b2).line_height, 12.0 * 1.4)
        # 都没有 → 默认 11.0
        b3 = _block(font_size=0.0)
        self.assertEqual(flow_text_from_block(b3).line_height, 11.0 * 1.4)


# ── 2. build_block_flow_payload：JSON 安全 + 译文布局 + 失败兜底 ────────

class TestBuildBlockFlowPayload(unittest.TestCase):
    def test_payload_shape_json_safe(self):
        payload = build_block_flow_payload(
            _block(text="A short paragraph.", translated="一个简短短落。")
        )
        json.dumps(payload)  # 必须可序列化
        self.assertEqual(payload["kind"], "flow")
        self.assertEqual(payload["text"], "一个简短短落。")
        self.assertEqual(payload["policy"], OverflowPolicy.WRAP.value)
        self.assertTrue(payload["layout_ok"])
        self.assertTrue(payload["commands"])
        for c in payload["commands"]:
            self.assertEqual(c["kind"], FLOW_COMMAND_KIND)
            self.assertIn("text", c)
            self.assertIn("x", c)
            self.assertIn("y", c)

    def test_commands_use_translated_text_only(self):
        payload = build_block_flow_payload(
            _block(text="Source paragraph.", translated="译后文本段落。")
        )
        joined = "".join(c["text"] for c in payload["commands"])
        self.assertEqual(joined, "译后文本段落。")
        self.assertNotIn("Source", joined)

    def test_geometry_never_reinferred(self):
        """首行基线 = 块首行 baseline（缺失时 = y1），x = 块 x0 —— 只透传。"""
        payload = build_block_flow_payload(
            _block(text="Hello world", x0=120.0, y1=740.0, baseline=712.5)
        )
        self.assertEqual(payload["commands"][0]["x"], 120.0)
        self.assertEqual(payload["commands"][0]["y"], 712.5)
        # 无 baseline → 锚定 y1（块顶），不参与任何 level/index 推导
        payload2 = build_block_flow_payload(
            _block(text="Hello world", x0=120.0, y1=740.0, baseline=0.0)
        )
        self.assertEqual(payload2["commands"][0]["y"], 740.0)

    def test_failure_returns_layout_ok_false_never_raises(self):
        """布局层异常 → layout_ok=False 载荷（renderer 可观测降级），绝不抛。

        7F-6b：Flow 已接 bounded executor（adaptive_layout），失败注入点
        随之迁移 —— 布局层异常仍绝不向上抛。
        """
        from unittest.mock import patch

        with patch(
            "pdf2zh.semantic.renderer.flow.adaptive_layout",
            side_effect=RuntimeError("boom"),
        ):
            payload = build_block_flow_payload(_block(text="x", translated="y"))
        self.assertFalse(payload["layout_ok"])
        self.assertEqual(payload["policy"], OverflowPolicy.CLIP.value)
        self.assertTrue(payload["overflow"])


# ── 3. render_flow_text：完整管线（wrap / overflow 委托 lay_out）────────

class TestRenderFlowText(unittest.TestCase):
    def test_short_text_single_line(self):
        out = render_flow_text(
            "Hello", origin=(72.0, 722.0), max_width=468.0, max_height=22.0,
            font_size=12.0, measure=_measure,
        )
        self.assertEqual(out["kind"], "flow")
        self.assertEqual(out["lines"], ["Hello"])
        self.assertFalse(out["overflow"])
        self.assertTrue(out["layout_ok"])
        self.assertEqual(out["commands"][0]["x"], 72.0)
        self.assertEqual(out["commands"][0]["y"], 722.0)

    def test_long_text_wraps_with_negative_step_y_up(self):
        text = "This is a long paragraph that must wrap over several lines"
        out = render_flow_text(
            text, origin=(72.0, 722.0), max_width=120.0, max_height=200.0,
            font_size=10.0, measure=_measure, line_step=-14.0,
        )
        self.assertGreaterEqual(len(out["lines"]), 2)
        ys = [c["y"] for c in out["commands"]]
        # y-up 空间：换行向下 → 基线递减（magicpdf 翻转后落在正确位置）
        self.assertLess(ys[1], ys[0])
        self.assertEqual(round(ys[1], 2), round(ys[0] - 14.0, 2))
        # 换行断在词边界，空格在断点被丢弃 —— 用空格重建应还原原文
        self.assertEqual(" ".join(out["lines"]), text)

    def test_last_line_flagged_when_overflow(self):
        # 7I-5C: a 40pt box now re-wraps (WRAP -> SHRINK re-wrap fits) with no
        # overflow.  Use a genuinely narrow box so the text clips — the overflow
        # marker must land only on the last line (renderer reports the
        # observable overflow there).
        out = render_flow_text(
            "This text is definitely too wide for the box at all",
            origin=(0.0, 100.0), max_width=8.0, max_height=400.0,
            font_size=10.0, measure=_measure,
        )
        flagged = [c for c in out["commands"] if c["overflow"]]
        self.assertTrue(flagged)
        self.assertTrue(all(c["is_last"] for c in flagged))

    def test_layout_failure_never_raises(self):
        from unittest.mock import patch

        with patch(
            "pdf2zh.semantic.renderer.flow.adaptive_layout",
            side_effect=RuntimeError("layout broke"),
        ):
            out = render_flow_text(
                "anything", origin=(0.0, 0.0), max_width=100.0, max_height=100.0,
                font_size=10.0,
            )
        self.assertFalse(out["layout_ok"])
        self.assertTrue(out["overflow"])
        self.assertEqual(out["policy"], OverflowPolicy.CLIP.value)


# ── 4. FlowTextRenderer：命令形状 ────────────────────────────────────────

class TestFlowTextRenderer(unittest.TestCase):
    def test_command_shape(self):
        rr = FlowTextRenderer(line_height=1.4)
        cmds = rr.render(
            _LayoutResult(lines=["one", "two"]), origin=(10.0, 20.0), line_step=-14.0
        )
        self.assertEqual(len(cmds), 2)
        c0, c1 = cmds
        self.assertEqual(c0["kind"], FLOW_COMMAND_KIND)
        self.assertEqual(c0["x"], 10.0)
        self.assertEqual(c0["y"], 20.0)
        self.assertEqual(c1["y"], 6.0)
        self.assertFalse(c0["is_last"])
        self.assertTrue(c1["is_last"])

    def test_default_step_is_font_based(self):
        from pdf2zh.semantic.layout.overflow import LayoutResult

        rr = FlowTextRenderer(line_height=1.4)
        cmds = rr.render(
            LayoutResult(
                text="x", lines=["a", "b"], line_widths=[1.0, 1.0],
                font_size=10.0,
            )
        )
        self.assertEqual(round(cmds[1]["y"] - cmds[0]["y"], 2), 14.0)


# ── 5. 架构：不直接调 wrap/shrink/clip、不重推断几何 ────────────────────

class TestFlowArchitecture(unittest.TestCase):
    def test_sidechannel_never_calls_wrap_shrink_clip_directly(self):
        src = inspect.getsource(build_block_flow_payload) + inspect.getsource(
            flow_text_from_block
        )
        for banned in ("wrap_lines(", "shrink_to_fit(", "clip_text("):
            self.assertNotIn(banned, src)
        self.assertIn("render_flow_text(", src)  # 统一走 lay_out 管线

    def test_renderer_flow_never_calls_wrap_shrink_clip_directly(self):
        """7F-6b：Flow 通过 bounded executor（adaptive_layout）消费 recovery；
        renderer 本身仍不直接执行 wrap/shrink/clip。"""
        import pdf2zh.semantic.renderer.flow as mod

        src = inspect.getsource(mod)
        for banned in ("wrap_lines(", "shrink_to_fit(", "clip_text("):
            self.assertNotIn(banned, src)
        self.assertIn("adaptive_layout(", src)

    def test_payload_geometry_passthrough_no_reinference(self):
        """flow 载荷不得含 level/index/页宽 推导痕迹。"""
        src = inspect.getsource(build_block_flow_payload)
        for banned in ("level *", "index *", "page_width"):
            self.assertNotIn(banned, src)


# ── 6. document_model 集成：paragraph → render_payload.kind == flow ─────

def _model_with_paragraph(text="A translated paragraph."):
    page = PageModel(page_num=1)
    page.blocks.append(
        _block(text="Source paragraph.", translated=text, x0=72.0, y0=700.0, x1=540.0, y1=722.0)
    )
    model = DocumentModel()
    model.pages = [page]
    return model


class TestDocumentModelFlowIntegration(unittest.TestCase):
    def test_render_plan_flow_payload_with_commands(self):
        model = _model_with_paragraph()
        translate_document(model, lambda s: "译_" + s)
        plan = render_plan_from_model(model)
        self.assertEqual(len(plan), 1)
        entry = plan[0]
        rp = entry["render_payload"]
        self.assertEqual(rp["kind"], "flow")
        self.assertTrue(rp["commands"], "flow 块应带已定版的行命令")
        joined = "".join(c["text"] for c in rp["commands"])
        self.assertEqual(joined, "译_Source paragraph.")

    def test_flow_commands_drawable_by_renderer(self):
        """生成的 flow 命令可以直接被 magicpdf_renderer 消费（端到端冒烟）。"""
        from pdf2zh.v3.magicpdf_renderer import render_plan_to_pdf

        model = _model_with_paragraph()
        translate_document(model, lambda s: "译_" + s)
        plan = render_plan_from_model(model)
        pdf, stats = render_plan_to_pdf(
            plan, page_sizes={1: [612.0, 792.0]}, cjk_font=True
        )
        self.assertEqual(stats["flow_layout_used"], 1)
        self.assertNotIn("flow_legacy_fallback", stats)
        import pymupdf

        doc = pymupdf.open(stream=pdf, filetype="pdf")
        self.assertIn("译_Source paragraph.", doc[0].get_text())
        doc.close()


class _LayoutResult:
    """轻量 LayoutResult 替身（避免测试依赖 overflow 内部实现细节）。"""

    def __init__(self, lines, font_size=10.0):
        self.text = "\n".join(lines)
        self.lines = lines
        self.line_widths = [len(l) for l in lines]
        self.bbox = (0.0, 0.0, 100.0, 100.0)
        self.overflow = False
        self.policy = OverflowPolicy.WRAP
        self.font_size = font_size
        self.primitive_kind = "flow"


if __name__ == "__main__":
    unittest.main()
