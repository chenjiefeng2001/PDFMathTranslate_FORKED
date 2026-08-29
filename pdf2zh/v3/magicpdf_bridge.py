"""Magic-PDF/MinerU 解析结果 → v3 规范页面模型桥接器（Step 2.2）。

把 :class:`pdf2zh.magicpdf_adapter.MagicPdfParseResult`（基于 magic-pdf /
MinerU 的 middle.json 树）转换为 v3 的规范页面树（``canonical_page``：
PageModel → BlockModel → LineModel → SpanModel → GlyphModel），使后续
标注 Pass（style/roles/formulas/render）与翻译/重排管线可以完全复用，
不触碰 magic-pdf 的任何渲染代码。

关键问题与解决
--------------
1. **坐标缺失**：magic-pdf 只提供 span 级 bbox，没有字符级（Glyph）坐标。
   :func:`interpolate_char_bboxes` 按 span 宽度均分推算单字符 bbox，保证
   下游渲染/度量不因缺坐标崩溃。
2. **坐标系翻转**：magic-pdf 的 bbox 是「左上角原点、y 向下」的 PDF 点
   坐标；v3 规范树采用 pdfminer 惯例「左下角原点、y 向上」。
   :func:`flip_bbox` 统一做 ``y_flip = page_height - y``。
3. **类别映射**：magic-pdf 布局类别 → v3 ``BlockModel.kind``，见
   :data:`MAGICPDF_CLS_TO_KIND`。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from typing import Any

logger = logging.getLogger(__name__)


def flip_bbox(bbox: Sequence[float], page_height: float) -> list[float]:
    """左上角原点 → 左下角原点（v3 规范树坐标系）。"""
    x0, y0, x1, y1 = (float(v) for v in bbox)
    return [x0, page_height - y1, x1, page_height - y0]


def _char_width_weight(ch: str) -> float:
    """近似字符宽度权重：空格窄、ASCII 中、全角/公式宽。"""
    if ch.isspace():
        return 0.30
    if ord(ch) < 0x2E80:
        return 0.55
    return 1.0


def interpolate_char_bboxes(
    span_bbox: Sequence[float], text: str
) -> list[dict[str, Any]]:
    """span 级 bbox → 近似字符级 bbox 列表（均匀内插，Step 2.2 关键算法）。

    规则：
    - 按字符宽度权重（空格 0.30 / ASCII 0.55 / 全角公式 1.00）把 span 宽度
      均分到每个字符，避免合成字形严重偏离真实排版；
    - 返回 ``[{"char", "bbox": [x0,y0,x1,y1], "width"}]``；
    - 空文本返回 ``[]``，宽度为 0 时每个字符退化为 0 宽（不抛错）。
    """
    if not text:
        return []
    x0, y0, x1, y1 = (float(v) for v in span_bbox)
    total_width = max(0.0, x1 - x0)
    weights = [_char_width_weight(c) for c in text]
    weight_sum = sum(weights)
    if weight_sum <= 0:
        return [{"char": c, "bbox": [x0, y0, x0, y1], "width": 0.0} for c in text]
    avg_w = total_width / weight_sum
    glyphs: list[dict[str, Any]] = []
    cx = x0
    for ch, w in zip(text, weights):
        gw = avg_w * w
        glyphs.append({"char": ch, "bbox": [cx, y0, cx + gw, y1], "width": gw})
        cx += gw
    return glyphs


MAGICPDF_CLS_TO_KIND: dict[str, str] = {
    "title": "heading",
    "headline": "heading",
    "subtitle": "heading",
    "abstract": "abstract",
    "author": "metadata",
    "affiliation": "metadata",
    "institution": "metadata",
    "date": "metadata",
    "body": "paragraph",
    "text": "paragraph",
    "plain text": "paragraph",
    "paragraph": "paragraph",
    "content": "paragraph",
    "list": "list",
    "figure": "figure",
    "image": "figure",
    "table": "table",
    "table_caption": "caption",
    "figure_caption": "caption",
    "caption": "caption",
    "interline_equation": "formula",
    "equation": "formula",
    "inline_equation": "formula_inline",
    "formula": "formula",
    "code": "code",
    "algorithm": "code",
    "footer": "footer",
    "header": "header",
    "page_number": "footer",
    "toc": "toc",
    "reference": "references",
    "references": "references",
    # ── MinerU 3.x 补充类别（BlockType / pp_doclayout_v2）──
    "doc_title": "heading",
    "document_title": "heading",
    "paragraph_title": "heading",
    "vertical_text": "paragraph",
    "formula_number": "formula",
    "ref_text": "references",
    "reference_content": "references",
    "index": "references",
    "aside_text": "paragraph",
    "page_footnote": "footer",
    "code_body": "code",
    "code_caption": "caption",
    "code_footnote": "caption",
    "algorithm_caption": "caption",
    "image_caption": "caption",
    "chart_caption": "caption",
    "figure": "figure",
    "chart": "figure",
    "table_body": "table",
    "discarded": "footer",
}
DEFAULT_KIND = "paragraph"


def map_magicpdf_cls(cls: str) -> str:
    """magic-pdf 布局类别 → v3 kind（小写匹配，未知回退 paragraph）。"""
    key = (cls or "").strip().lower()
    return MAGICPDF_CLS_TO_KIND.get(key, DEFAULT_KIND)


#: 伪代码/算法块文本启发式：PP-DocLayout 常把伪代码判成 plain text，
#: 布局类别保护失效时用文本特征兜底（与 doclayout_pseudocode 的
#: algorithm 保护目标一致，但不依赖额外布局模型）。
_CODE_LINE_HINT = re.compile(
    r"^\s*(?:if|else|elif|for|while|do|end|endfor|endwhile|repeat|until|"
    r"switch|case|return|def|function|procedure|begin|class|import|from|"
    r"include|print(?:ln)?|printf|int|float|bool|string|var|let)\b",
    re.IGNORECASE,
)
_PSEUDOCODE_MIN_LINES = 3


def _looks_like_pseudocode(text_or_lines: Any) -> bool:
    """轻量伪代码启发：多行 + 过半行命中代码关键字/结构词才保护。

    接受 str（按行拆分）或行序列（bridge.convert 中 normalize 后的块文本
    是多行拼成的单行，必须按原始行判断）。阈值保守（≥3 行、≥2 行命中、
    命中行占比 ≥50%），避免把带代码示例的正文段落误判为代码块而不翻译。
    """
    if isinstance(text_or_lines, str):
        lines = text_or_lines.splitlines()
    else:
        lines = list(text_or_lines or [])
    lines = [ln.strip() for ln in lines if ln.strip()]
    if len(lines) < _PSEUDOCODE_MIN_LINES:
        return False
    hits = sum(1 for ln in lines if _CODE_LINE_HINT.search(ln))
    return hits >= 2 and hits >= len(lines) / 2


class MagicPdfBridge:
    """magic-pdf 解析结果 → v3 规范页面模型转换器。

    Attributes:
        default_font: 无字体信息时兜底的字体名。
        size_scale: span 高度 → 字号估算系数（缺字号时用）。
    """

    def __init__(
        self,
        default_font: str = "",
        size_scale: float = 0.85,
    ) -> None:
        self.default_font = default_font
        self.size_scale = size_scale

    def convert(self, result) -> Any:
        """单个 :class:`MagicPdfParseResult` → :class:`PageModel`。"""
        from pdf2zh.v3.canonical_page import (
            BlockModel,
            GlyphModel,
            LineModel,
            PageModel,
            SpanModel,
        )

        height = float(result.height)
        page = PageModel(
            page_num=result.page_num,
            width=float(result.width),
            height=height,
        )
        for blk in result.blocks:
            bbox = flip_bbox(blk.get("bbox") or [0, 0, 0, 0], height)
            bm = BlockModel(
                text=blk.get("text", ""),
                kind=map_magicpdf_cls(blk.get("cls", "")),
                x0=bbox[0],
                y0=bbox[1],
                x1=bbox[2],
                y1=bbox[3],
            )
            bm.metadata["magicpdf_cls"] = blk.get("cls", "")
            bm.metadata["magicpdf_type"] = blk.get("type", "")
            if blk.get("confidence"):
                bm.metadata["confidence"] = round(float(blk["confidence"]), 4)
            if blk.get("latex"):
                bm.metadata["latex"] = blk["latex"]
            if blk.get("img"):
                bm.metadata["has_image"] = True
            for line_raw in blk.get("lines", []) or []:
                lbbox = flip_bbox(line_raw.get("bbox") or [0, 0, 0, 0], height)
                lm = LineModel(
                    text="",
                    baseline=0.0,
                    x0=lbbox[0],
                    y0=lbbox[1],
                    x1=lbbox[2],
                    y1=lbbox[3],
                )
                for span_raw in line_raw.get("spans", []) or []:
                    sbox_tl = span_raw.get("bbox") or [0, 0, 0, 0]
                    sbox = flip_bbox(sbox_tl, height)
                    text = span_raw.get("content", "")
                    size = max(0.0, float(sbox_tl[3]) - float(sbox_tl[1]))
                    sm = SpanModel(
                        font=self.default_font,
                        size=round(size * self.size_scale, 2) if size else 0.0,
                        text=text,
                        x0=sbox[0],
                        y0=sbox[1],
                        x1=sbox[2],
                        y1=sbox[3],
                    )
                    for g in interpolate_char_bboxes(sbox_tl, text):
                        gb = flip_bbox(g["bbox"], height)
                        sm.glyphs.append(
                            GlyphModel(
                                char=g["char"],
                                cid=-1,
                                font=sm.font,
                                size=sm.size,
                                x0=gb[0],
                                y0=gb[1],
                                x1=gb[2],
                                y1=gb[3],
                                decode="ok",
                            )
                        )
                    lm.spans.append(sm)
                    lm.text += text
                bm.lines.append(lm)
            # Step 1.1/1.2：algorithm/伪代码块不参与翻译（配合 BabelDOC
            # 融合布局模型的 algorithm 保护 / document_model._KEEP_KINDS）。
            # PP-DocLayout 常把伪代码判成 plain text → kind=paragraph，
            # 文本启发式兜底把它提升为 code 一并保护（须在 lines 填充后判断）。
            if bm.kind == "code" or (
                bm.kind == "paragraph"
                and _looks_like_pseudocode([lm.text for lm in bm.lines])
            ):
                if bm.kind == "paragraph":
                    bm.kind = "code"
                bm.metadata["translate"] = False
                bm.metadata["pseudocode_protected"] = True
            page.blocks.append(bm)
        return page

    def convert_all(self, results) -> list[Any]:
        """把逐页解析结果批量转成 :class:`PageModel` 列表。"""
        return [self.convert(r) for r in (results or [])]

    def to_document_model(self, pages) -> Any:
        """运行完整标注 Pass（与 ``build_document_model`` 同源），产出
        :class:`DocumentModel`（只写 metadata，不复制 IR）。"""
        from pdf2zh.v3.canonical_page import (
            annotate_formulas,
            annotate_style,
            annotate_toc_scan,
            apply_layout_splits,
        )
        from pdf2zh.v3.document_model import (
            DocumentModel,
            annotate_render,
            annotate_roles,
        )

        model = DocumentModel()
        for page in pages:
            try:
                annotate_style(page)
                apply_layout_splits(page)
            except Exception as exc:  # noqa: BLE001
                logger.debug("magicpdf_bridge: layout passes failed: %s", exc)
            try:
                annotate_roles(page)
            except Exception as exc:  # noqa: BLE001
                logger.debug("magicpdf_bridge: roles failed: %s", exc)
            try:
                annotate_formulas(page)
            except Exception as exc:  # noqa: BLE001
                logger.debug("magicpdf_bridge: formulas failed: %s", exc)
            try:
                from pdf2zh.v3.toc_analyzer import split_toc_blocks

                split_toc_blocks(page)
            except Exception as exc:  # noqa: BLE001
                logger.debug("magicpdf_bridge: toc split failed: %s", exc)
            try:
                annotate_toc_scan(page)
            except Exception as exc:  # noqa: BLE001
                logger.debug("magicpdf_bridge: toc scan failed: %s", exc)
            annotate_render(page)
            model.add_page(page)
        return model


def build_document_from_results(
    results,
    default_font: str = "",
) -> Any:
    """便捷入口：``MagicPdfBridge().to_document_model(convert_all(results))``。"""
    bridge = MagicPdfBridge(default_font=default_font)
    return bridge.to_document_model(bridge.convert_all(results))
