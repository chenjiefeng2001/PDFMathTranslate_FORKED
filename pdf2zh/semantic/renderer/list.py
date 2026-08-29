"""List geometry-preserving renderer — plan Commit 4.

Minimally invasive first version. Three goals only:

1. the list **marker never enters translation** (it is a layout object,
   not a translation object — the LLM must never see ``1.`` and be able to
   rewrite it into ``一、``);
2. the list **content** is the only thing handed to the translator;
3. the rendered output **copies the original geometry** — ``marker_x``,
   ``content_x``, ``indent``, line spacing — and replaces only the text.

Renderer design rules:

- **no translator inside**: the caller passes a ``translate`` callable (or a
  pre-built ``translated`` map); the renderer itself never touches the
  translator, keeping the plan's translator/renderer separation;
- **no level recomputation**: indents come straight from the parsed nodes
  (``node.indent`` / ``node.marker_x`` / ``node.content_x``), never from
  ``level * 20``-style math;
- **continuation lines render at ``content_x``**, not ``marker_x``;
- text layout (wrapping / CJK / glyph placement) is delegated to an injected
  :class:`TextRenderer` — this module only decides *where* each command goes.

Output is a flat list of :class:`RenderCommand` positionable runs (marker /
text), so the actual PDF emission can stay in the host pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol

from pdf2zh.semantic.models import ListNode

__all__ = ["RenderCommand", "TextRenderer", "ListRenderer", "build_page_list_plan"]


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


class TextRenderer(Protocol):
    """Host text layout hook: wrap + place translated text.

    Implementations own word/CJK wrapping, font fallback and glyph
    placement; the ListRenderer only supplies the anchor (``x``/``y``) and
    the available width.
    """

    def render_text(
        self,
        text: str,
        x: float,
        y: float,
        max_width: float,
        **kwargs,
    ) -> list[RenderCommand]:
        ...


class _PlainTextRenderer:
    """Default single-line text renderer (no wrapping) for tests / CLI."""

    def render_text(self, text, x, y, max_width, **kwargs):
        if not text:
            return []
        return [RenderCommand(kind="text", text=text, x=x, y=y, width=max_width)]


class ListRenderer:
    """Renders a parsed :class:`ListNode` tree into positioned commands.

    The marker channel is ``PRESERVE`` (verbatim), the content channel is
    ``TRANSLATE_KEEP_GEOMETRY`` — only ``translate(text)`` output is placed.
    """

    def __init__(
        self,
        text_renderer: TextRenderer | None = None,
        line_height: float = 12.0,
    ):
        self.text_renderer = text_renderer or _PlainTextRenderer()
        self.line_height = line_height

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
        """Layout one item: marker at marker_x, content at content_x."""
        # ── 通道 1：marker —— PRESERVE，绝不进翻译器 ─────────────
        if item.marker:
            cmds.append(
                RenderCommand(
                    kind="marker",
                    text=item.marker,
                    x=item.marker_x,
                    y=item.y,
                    width=item.marker_width,
                    level=item.level,
                    bbox=item.bbox,
                )
            )
        # ── 通道 2：content —— TRANSLATE_KEEP_GEOMETRY ──────────
        if item.content:
            cmds.extend(
                self.text_renderer.render_text(
                    translate(item.content),
                    x=item.content_x,
                    y=item.y,
                    max_width=item.content_width,
                )
            )
        # ── 延续行：x 用 content_x（不是 marker_x）───────────────
        for ln, cont in enumerate(item.continuation, start=1):
            cmds.extend(
                self.text_renderer.render_text(
                    translate(cont),
                    x=item.content_x,
                    y=item.y + ln * self.line_height,
                    max_width=item.content_width,
                )
            )
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


# ── 页级编排：detect → parse → translate(content only) → render ──────────
#
# Commit 4 的接线入口：把探测器 / 解析器 / 渲染器串成一条链，供上层 PDF
# renderer（如 v3/magicpdf_renderer）与集成测试复用。marker 通道 PRESERVE
# （从不进入 translate），content 通道 TRANSLATE_KEEP_GEOMETRY。


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
) -> dict:
    """完整打通 ``detect → parse → translate → render``，产出 JSON 安全计划。

    Args:
        paragraphs: 本页段落（阅读顺序，与 detector 输入一致）。
        geom: 每段 ``{x0, x1, y0, size}`` 几何（可选）。
        translate: **仅 content 的** 翻译回调；marker 从不会被调用。为空时恒等。
        line_height: 延续行垂直步进。

    Returns:
        ``{"tree", "items", "commands", "translated_calls"}``：

        - ``items``: 每个列表项 ``{marker, marker_type, content, translated,
          marker_x, content_x, level, indent, continuation}``；
        - ``commands``: 展开后的 :class:`RenderCommand` 快照（marker/text）；
        - ``translated_calls``: 传给 ``translate`` 的**全部**文本 —— 只含 content，
          marker 绝不在其中（验收：marker 不进入 translation）。

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

    renderer = ListRenderer(line_height=line_height)
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