"""List geometry-preserving renderer — Commit 4, refactored for Commit 7E-2c.

The renderer is now a **draw-only** consumer of the unified layout layer::

    ListNode
        ↓  layout_list_item (pdf2zh.semantic.layout.list_layout)
    ListLayoutResult (marker/content/continuation LayoutResults)
        ↓  ListRenderer (draw only)
    PDF commands

Three goals, unchanged since Commit 4:

1. the list **marker never enters translation** (it is a layout object,
   not a translation object — the LLM must never see ``1.`` and be able to
   rewrite it into ``一、``);
2. the list **content** is the only thing handed to the translator;
3. the rendered output **copies the original geometry** — ``marker_x``,
   ``content_x``, ``continuation_x``, line spacing — and replaces only the
   text.  All fit decisions (wrap / overflow) come from ``lay_out`` via the
   layout adapter; this module never calls ``detect_list`` / ``parse_list`` /
   ``calculate_level`` / ``calculate_indent`` / ``wrap_lines`` /
   ``measure_text``.

Coordinate convention: v3 lower-left origin (y up).  The first line of an
item anchors at ``item.y``; wrapped content and continuation lines step
**down** the page, so their v3 ``y`` decreases (``line_step`` is negative).
The host renderer flips y when writing to the PDF.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from pdf2zh.semantic.layout.list_layout import layout_list_item
from pdf2zh.semantic.models import ListNode

__all__ = ["ListRenderer", "RenderCommand", "build_page_list_plan"]


@dataclass
class RenderCommand:
    """One positioned text run for the host renderer."""

    kind: str  # "marker" | "text"
    text: str
    x: float
    y: float
    width: float = 0.0
    level: int = 0
    bbox: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "text": self.text,
            "x": round(self.x, 1),
            "y": round(self.y, 1),
            "width": round(self.width, 1),
            "level": self.level,
        }


def _layout_lines(
    result, x: float, y: float, line_step: float, kind: str, level: int, bbox
) -> list[RenderCommand]:
    """One LayoutResult → positioned RenderCommands (no re-layout)."""
    cmds: list[RenderCommand] = []
    for i, (ln, w) in enumerate(zip(result.lines, result.line_widths)):
        if not ln:
            continue
        cmds.append(
            RenderCommand(
                kind=kind,
                text=ln,
                x=x,
                y=round(y + i * line_step, 2),
                width=round(float(w), 1),
                level=level,
                bbox=bbox,
            )
        )
    return cmds


class ListRenderer:
    """Draw-only renderer: consumes :class:`ListLayoutResult` shapes.

    ``line_height`` is the vertical step between wrapped lines in points
    (positive; applied downward via a negative ``line_step`` in v3 y-up).
    ``font_size`` and ``measure`` are passed through to the layout adapter
    for ``lay_out``; the renderer itself never measures or wraps.
    """

    def __init__(
        self,
        line_height: float = 12.0,
        font_size: float = 11.0,
        measure: Callable[[str, float], float] | None = None,
    ):
        self.line_height = float(line_height or 12.0)
        self.font_size = float(font_size or 11.0)
        self.measure = measure

    def render(
        self,
        node: ListNode,
        translate: Callable[[str], str] | None = None,
    ) -> list[RenderCommand]:
        """Translate (optional) and lay out a list tree.

        Args:
            node: parsed :class:`ListNode` (geometry already copied from the
                original PDF).
            translate: content translator (default identity). The marker is
                **never** passed through it.
        """
        cmds: list[RenderCommand] = []
        self._render_list(node, translate or (lambda s: s), cmds)
        return cmds

    def render_item(
        self,
        item,
        translate: Callable[[str], str],
        cmds: list[RenderCommand],
    ) -> None:
        """Draw one item from its settled layout (marker → content → continuation)."""
        # ── 布局：语义节点 → lay_out（marker 固定 / content 可 wrap）──
        # content 与 continuation 是唯一进翻译器的文本；marker 从不翻译。
        content_text = translate(item.content) if item.content else ""
        continuation_texts = (
            [translate(c) for c in item.continuation] if item.continuation else None
        )
        layout = layout_list_item(
            item,
            measure=self.measure,
            font_size=self.font_size,
            line_step=-self.line_height,
            content_text=content_text or None,
            continuation_texts=continuation_texts,
        )

        # ── 通道 1：marker —— FixedAnchor 单行原样绘制 ──────────────
        if layout.marker.lines and layout.marker.lines[0]:
            cmds.append(
                RenderCommand(
                    kind="marker",
                    text=layout.marker.lines[0],
                    x=item.marker_x,
                    y=item.y,
                    width=item.marker_width,
                    level=item.level,
                    bbox=item.bbox,
                )
            )

        # ── 通道 2：content —— FlowText 已定版行（y 递减向下）──────
        step = layout.line_step
        y = item.y
        cmds.extend(
            _layout_lines(
                layout.content, item.content_x, y, step, "text", item.level, item.bbox
            )
        )
        y += len(layout.content.lines) * step

        # ── 通道 3：continuation —— 钉在 content_x，逐条换行后下移 ──
        for cl in layout.continuation:
            cmds.extend(
                _layout_lines(
                    cl, item.content_x, y, step, "text", item.level, item.bbox
                )
            )
            y += len(cl.lines) * step

        # ── 嵌套列表：递归（层级/缩进来自节点，不重新计算）────────
        for child in item.children:
            self._render_list(child, translate, cmds)

    def _render_list(
        self,
        node: ListNode,
        translate: Callable[[str], str],
        cmds: list[RenderCommand],
    ) -> None:
        for item in node.items:
            self.render_item(item, translate, cmds)

    def render_plan(
        self,
        node: ListNode,
        translate: Callable[[str], str] | None = None,
    ) -> dict:
        """Debug-friendly render plan (JSON-serializable)."""
        cmds = self.render(node, translate)
        return {"commands": [c.to_dict() for c in cmds]}


# ── 页级编排：detect → parse → translate(content only) → layout → draw ──
#
# Commit 4 的接线入口：把探测器 / 解析器 / 渲染器串成一条链，供上层 PDF
# renderer（如 v3/magicpdf_renderer）与集成测试复用。marker 通道 PRESERVE
# （从不进入 translate），content 通道 TRANSLATE_KEEP_GEOMETRY。检测与解析
# 是编排职责（在页面级完成），ListRenderer 本身只负责“画”。


def _walk_items(node: ListNode):
    """深度优先遍历所有 :class:`ListItemNode`（含嵌套层级）。"""
    for item in node.items:
        yield item
        for child in item.children:
            yield from _walk_items(child)


def _detect_and_parse(paragraphs, geom=None):
    """运行 detector + parser，返回 (tree, candidates)。tree 无列表时返回 None。"""
    from pdf2zh.semantic.list_detector import detect_list_candidates
    from pdf2zh.semantic.list_parser import parse_list_tree

    cands = detect_list_candidates(paragraphs, geom)
    tree = parse_list_tree(paragraphs, cands, geom)
    return tree, cands


def build_page_list_plan(
    paragraphs: list[str],
    geom: list[dict] | None = None,
    translate: Callable[[str], str] | None = None,
    line_height: float = 12.0,
    font_size: float = 11.0,
) -> dict:
    """完整打通 ``detect → parse → translate → layout → draw``，产出 JSON 安全计划。

    Args:
        paragraphs: 本页段落（阅读顺序，与 detector 输入一致）。
        geom: 每段 ``{x0, x1, y0, size}`` 几何（可选）。
        translate: **仅 content 的** 翻译回调；marker 从不会被调用。为空时恒等。
        line_height: 延续/换行行垂直步进（points）。
        font_size: 布局测量/溢出用的字号（默认 11.0）。

    Returns:
        ``{"tree", "items", "commands", "translated_calls"}``：

        - ``items``: 每个列表项 ``{marker, marker_type, content, translated,
          marker_x, content_x, level, indent, continuation}``；
        - ``commands``: 展开后的 :class:`RenderCommand` 快照（marker/text，
          含 wrap 后的多行 content）；长内容 wrap 后产生多行 text 命令；
        - ``translated_calls``: 传给 ``translate`` 的**全部**文本 —— 只含
          content，marker 绝不在其中（验收：marker 不进入 translation）。

    纯数据，无 I/O，不修改 PDF。无列表时 ``tree``/``items``/``commands`` 为空。
    """
    tree, _cands = _detect_and_parse(paragraphs, geom)
    if tree is None:
        return {"tree": None, "items": [], "commands": [], "translated_calls": []}

    translator = translate or (lambda s: s)
    calls: list[str] = []
    seen: dict[str, str] = {}

    def _translated(s: str) -> str:
        calls.append(s)
        out = translator(s)
        seen.setdefault(s, out)
        return out

    renderer = ListRenderer(line_height=line_height, font_size=font_size)
    cmds = renderer.render(tree, translate=_translated)

    items: list[dict] = []
    for item in _walk_items(tree):
        items.append(
            {
                "marker": item.marker,
                "marker_type": item.marker_type,
                "content": item.content,
                # 复用渲染期已翻译结果，避免对 content 二次调用 translator
                "translated": seen.get(item.content, item.content),
                "marker_x": round(item.marker_x, 1),
                "content_x": round(item.content_x, 1),
                "indent": round(item.indent, 1),
                "level": item.level,
                "continuation": list(item.continuation),
            }
        )
    return {
        "tree": tree.to_dict(),
        "items": items,
        "commands": [c.to_dict() for c in cmds],
        "translated_calls": list(calls),
    }
