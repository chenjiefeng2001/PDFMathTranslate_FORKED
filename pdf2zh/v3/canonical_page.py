"""Module: CanonicalPage — 统一页面模型（Page → Block → Line → Span → Glyph）。

对应 V11 架构的第一步「建立唯一数据模型」：整页只有一棵树，Glyph 是叶子，
Span/Line/Block 逐级聚合，每个节点带 ``metadata`` 字典。**所有后续 Pass
（TOC / Formula / Image / Style / Translate）只写 metadata，不再各自重新
解析页面** —— 不会新增第二套 IR，Document IR 仍只是这棵树的序列化视图。

    PageModel
     └── BlockModel (bbox/text/kind/metadata)
          └── LineModel (bbox/text/baseline)
               └── SpanModel (font/size/text/bbox/metadata)
                    └── GlyphModel (char/cid/font/size/bbox/decode)

标注 Pass（纯函数，只写 metadata）：
- ``annotate_toc``      → Block.metadata.kind="toc" + number/title/page/confidence
- ``annotate_formulas`` → Span.metadata.math=True + Block.metadata.formula_density
- ``annotate_style``    → Block.metadata.fonts / multifont（多字体错乱信号）

数据源仍是 pdfminer 的 LTChar 流（Glyph Extraction 不做翻译/TOC/图片判断），
与 ``TranslateConverter.receive_layout`` 消费同一份字符流 —— 收敛点不变。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

log = logging.getLogger(__name__)

_RE_CID_NOTDEF = __import__("re").compile(r"\(cid:\d+\)")


@dataclass
class GlyphModel:
    char: str = ""
    cid: int = -1
    font: str = ""
    size: float = 0.0
    x0: float = 0.0
    y0: float = 0.0
    x1: float = 0.0
    y1: float = 0.0
    decode: str = "ok"  # ok | fffd | notdef
    metadata: Dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "char": self.char, "cid": self.cid, "font": self.font,
            "size": round(self.size, 2),
            "x0": round(self.x0, 1), "y0": round(self.y0, 1),
            "x1": round(self.x1, 1), "y1": round(self.y1, 1),
            "decode": self.decode, "metadata": dict(self.metadata),
        }


@dataclass
class SpanModel:
    font: str = ""
    size: float = 0.0
    text: str = ""
    x0: float = 0.0
    y0: float = 0.0
    x1: float = 0.0
    y1: float = 0.0
    glyphs: List[GlyphModel] = field(default_factory=list)
    metadata: Dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "font": self.font, "size": round(self.size, 2),
            "text": self.text,
            "x0": round(self.x0, 1), "y0": round(self.y0, 1),
            "x1": round(self.x1, 1), "y1": round(self.y1, 1),
            "glyphs": [g.to_dict() for g in self.glyphs],
            "metadata": dict(self.metadata),
        }


@dataclass
class LineModel:
    text: str = ""
    baseline: float = 0.0
    x0: float = 0.0
    y0: float = 0.0
    x1: float = 0.0
    y1: float = 0.0
    spans: List[SpanModel] = field(default_factory=list)
    metadata: Dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "text": self.text, "baseline": round(self.baseline, 1),
            "x0": round(self.x0, 1), "y0": round(self.y0, 1),
            "x1": round(self.x1, 1), "y1": round(self.y1, 1),
            "spans": [s.to_dict() for s in self.spans],
            "metadata": dict(self.metadata),
        }


@dataclass
class BlockModel:
    text: str = ""
    kind: str = "paragraph"
    x0: float = 0.0
    y0: float = 0.0
    x1: float = 0.0
    y1: float = 0.0
    lines: List[LineModel] = field(default_factory=list)
    metadata: Dict = field(default_factory=dict)

    @property
    def bbox(self) -> tuple:
        return (self.x0, self.y0, self.x1, self.y1)

    @property
    def font_size(self) -> float:
        sizes = [s.size for l in self.lines for s in l.spans if s.size > 0]
        return max(sizes) if sizes else 0.0

    @property
    def line_count(self) -> int:
        return max(1, len(self.lines))

    def to_dict(self) -> dict:
        return {
            "kind": self.kind, "text": self.text,
            "x0": round(self.x0, 1), "y0": round(self.y0, 1),
            "x1": round(self.x1, 1), "y1": round(self.y1, 1),
            "lines": [l.to_dict() for l in self.lines],
            "metadata": dict(self.metadata),
        }


@dataclass
class PageModel:
    page_num: int = 0
    width: float = 0.0
    height: float = 0.0
    blocks: List[BlockModel] = field(default_factory=list)
    unassigned_glyphs: List[GlyphModel] = field(default_factory=list)
    metadata: Dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "page": self.page_num,
            "width": round(self.width, 1), "height": round(self.height, 1),
            "blocks": [b.to_dict() for b in self.blocks],
            "unassigned_glyphs": [g.to_dict() for g in self.unassigned_glyphs],
            "stats": self.stats(),
            "metadata": dict(self.metadata),
        }

    def stats(self) -> dict:
        glyphs = sum(len(s.glyphs) for b in self.blocks
                     for l in b.lines for s in l.spans)
        spans = sum(len(l.spans) for b in self.blocks for l in b.lines)
        lines = sum(len(b.lines) for b in self.blocks)
        return {
            "blocks": len(self.blocks),
            "lines": lines,
            "spans": spans,
            "glyphs": glyphs,
            "unassigned_glyphs": len(self.unassigned_glyphs),
            "replacement_glyphs": sum(
                1 for b in self.blocks for l in b.lines for s in l.spans
                for g in s.glyphs if g.decode != "ok"),
        }


# ── Glyph Extraction（LTChar 流 → GlyphModel） ────────────────────────────


def _iter_char_like(container):
    """递归遍历 LTChar（兼容 LTTextContainer/LTFigure 嵌套）。"""
    try:
        children = list(container)
    except Exception:  # noqa: BLE001
        return
    for child in children:
        if hasattr(child, "size") and hasattr(child, "get_text"):
            yield child
        else:
            yield from _iter_char_like(child)


def _decode_of(text: str) -> str:
    if _RE_CID_NOTDEF.search(text):
        return "notdef"
    if "\ufffd" in text:
        return "fffd"
    return "ok"


def _collect_glyphs(ltpage, max_glyphs: int = 2000) -> List[GlyphModel]:
    glyphs: List[GlyphModel] = []
    for child in _iter_char_like(ltpage):
        if len(glyphs) >= max_glyphs:
            break
        text = child.get_text() or ""
        if not text:
            continue
        font = ""
        try:
            font = getattr(child, "fontname", "") or ""
        except Exception:  # noqa: BLE001
            font = ""
        glyphs.append(GlyphModel(
            char=text,
            cid=int(getattr(child, "cid", 0) or 0),
            font=font,
            size=float(getattr(child, "size", 0.0) or 0.0),
            x0=float(child.x0), y0=float(child.y0),
            x1=float(child.x1), y1=float(child.y1),
            decode=_decode_of(text),
        ))
    return glyphs


# ── 树构建（Glyph → Span → Line → Block） ────────────────────────────────


def _assign_and_span(glyphs: List[GlyphModel], lines, gap: float = 2.0):
    """按字形中心落入行 bbox 分配；行内同字体+字号连续字形聚成 Span。"""
    spans_by_line: Dict[int, List[SpanModel]] = {}
    unassigned: List[GlyphModel] = []
    for g in glyphs:
        cx, cy = (g.x0 + g.x1) / 2.0, (g.y0 + g.y1) / 2.0
        hit = None
        for li, (l0, l1) in enumerate(lines):
            if l0 - 1.5 <= cy <= l1 + 1.5:
                hit = li
                break
        if hit is None:
            unassigned.append(g)
            continue
        spans = spans_by_line.setdefault(hit, [])
        if spans and spans[-1].font == g.font and abs(spans[-1].size - g.size) < 0.5 \
                and g.x0 - spans[-1].x1 <= gap:
            last = spans[-1]
            last.glyphs.append(g)
            last.text += g.char
            last.x0 = min(last.x0, g.x0)
            last.y0 = min(last.y0, g.y0)
            last.x1 = max(last.x1, g.x1)
            last.y1 = max(last.y1, g.y1)
        else:
            spans.append(SpanModel(
                font=g.font, size=g.size, text=g.char,
                x0=g.x0, y0=g.y0, x1=g.x1, y1=g.y1, glyphs=[g],
            ))
    return spans_by_line, unassigned


def build_page_model(ltpage, page_num: Optional[int] = None,
                     max_glyphs: int = 2000) -> PageModel:
    """LTChar 流 → 规范页面树（Glyph → Span → Line → Block）。

    块/行分组复用 Geometry Engine（与 receive_layout 同一份字符流），
    只做结构恢复，不做任何翻译/TOC/图片判断。
    """
    from pdf2zh.v3.geometry import GeometryEngine, chars_from_ltpage
    pageid = page_num if page_num is not None \
        else getattr(ltpage, "pageid", 0)
    page = PageModel(
        page_num=pageid,
        width=float(getattr(ltpage, "width", 0.0) or 0.0),
        height=float(getattr(ltpage, "height", 0.0) or 0.0),
    )
    glyphs = _collect_glyphs(ltpage, max_glyphs=max_glyphs)
    chars = chars_from_ltpage(ltpage, page_num=pageid)
    if not chars or not glyphs:
        page.unassigned_glyphs = glyphs
        return page

    g_page = GeometryEngine().build_page(chars, page_num=pageid)
    lines_bbox: List[tuple] = []
    for para in g_page.reading_order():
        for line in getattr(para, "lines", []) or []:
            lines_bbox.append((line.y0, line.y1))
    spans_by_line, unassigned = _assign_and_span(glyphs, lines_bbox)
    page.unassigned_glyphs = unassigned

    span_i = 0
    for pi, para in enumerate(g_page.reading_order()):
        block = BlockModel(
            text=para.text,
            x0=para.x0, y0=para.y0, x1=para.x1, y1=para.y1,
        )
        for li, line in enumerate(getattr(para, "lines", []) or []):
            lm = LineModel(
                text=line.text,
                baseline=float(getattr(line, "y0", 0.0) or 0.0),
                x0=line.x0, y0=line.y0, x1=line.x1, y1=line.y1,
            )
            lm.spans = spans_by_line.get(span_i, [])
            span_i += 1
            block.lines.append(lm)
        page.blocks.append(block)
    return page


# ── 标注 Pass（只写 metadata，不重新解析） ───────────────────────────────


def annotate_toc(page: PageModel, toc_entries: Sequence[dict]) -> int:
    """TOC Pass：把解析好的目录条目写到对应 Block.metadata。

    ``toc_entries`` 为 ``pipeline_dump.toc_dump`` 输出。按「编号 + 标题
    同时出现在块文本中」匹配（best-effort）。返回命中块数。
    """
    hits = 0
    for entry in toc_entries or []:
        number = str(entry.get("number", "")).strip()
        title = str(entry.get("title", "")).strip()
        for block in page.blocks:
            text = block.text or ""
            if number and number not in text:
                continue
            if title and title not in text and entry.get("raw") not in text:
                continue
            block.kind = "toc"
            block.metadata["kind"] = "toc"
            block.metadata["toc_number"] = number
            block.metadata["toc_title"] = title
            block.metadata["toc_page"] = str(entry.get("page", ""))
            block.metadata["toc_confidence"] = entry.get("confidence", 0.0)
            block.metadata["toc_line"] = entry.get("line")
            hits += 1
            break
    return hits


# TOC 行模式：编号? + 标题 + leader（≥3 点）+ 行尾页码 —— 块级自扫描用
_RE_TOC_LINE = __import__("re").compile(
    r"^(?P<num>\d+(?:\.\d+)*)?\s*(?P<title>.*?)\s*"
    r"(?P<leader>[.·…‥]{3,})\s*(?P<page>\d{1,5})\s*$")


def annotate_toc_scan(page: PageModel, lang_out: str = "zh-CN") -> int:
    """TOC Pass（自扫描）：legacy 检测失败时，从树内块文本直接识别目录行。

    当 legacy gate 记录因段落合并等丢失 TOC 标记时，canonical 树仍保留
    正确的块结构（geometry 拆分未失效）—— 本 Pass 按「leader + 行尾页码」
    模式在**块级**识别（不是整页 regex），解析结果写 Block.metadata。
    返回新标记的块数（已带 toc_number 的块跳过，role 预标不阻塞补号）。
    """
    hits = 0
    for block in page.blocks:
        if block.metadata.get("toc_number"):
            continue
        text = (block.text or "").strip()
        if "\n" in text:
            continue  # 多行块不判定（仍是疑似合并，交行级诊断）
        m = _RE_TOC_LINE.match(text)
        if not m:
            continue
        num = (m.group("num") or "").strip()
        title = (m.group("title") or "").strip()
        page_digits = m.group("page")
        if not num:
            continue  # 无编号目录行不在自扫描范围（避免误报）
        block.kind = "toc"
        block.metadata["kind"] = "toc"
        block.metadata["toc_number"] = num
        block.metadata["toc_title"] = title
        block.metadata["toc_page"] = page_digits
        block.metadata["toc_confidence"] = 0.55
        block.metadata["toc_scan"] = True
        hits += 1
    return hits


def annotate_formulas(page: PageModel) -> int:
    """Formula Pass：Span 级数学标注（Span.metadata.math）。

    复用 structure 的公式符号正则；Block 级给 formula_density（math span
    占比）。返回标记的 span 数。
    """
    try:
        from pdf2zh.v3.structure import _RE_FORMULA_SYMBOLS, _RE_MATH_SYMBOL
    except Exception:  # noqa: BLE001
        return 0
    marked = 0
    for block in page.blocks:
        spans = [s for l in block.lines for s in l.spans]
        math_spans = 0
        for span in spans:
            text = span.text
            if text and (_RE_FORMULA_SYMBOLS.match(text) or
                         sum(1 for _ in _RE_MATH_SYMBOL.finditer(text)) >= 2):
                span.metadata["math"] = True
                marked += 1
                math_spans += 1
        if spans:
            block.metadata["formula_density"] = round(
                math_spans / len(spans), 3)
    return marked


def annotate_style(page: PageModel) -> None:
    """Style Pass：块级字体/字号清单 + 多字体信号（供渲染/诊断）。"""
    for block in page.blocks:
        fonts: Dict[str, List[float]] = {}
        for span in (s for l in block.lines for s in l.spans):
            fonts.setdefault(span.font, [])
            if span.size not in fonts[span.font]:
                fonts[span.font].append(round(span.size, 2))
        block.metadata["fonts"] = {f: sorted(s) for f, s in fonts.items()}
        block.metadata["multifont"] = len(fonts) > 1


__all__ = [
    "GlyphModel", "SpanModel", "LineModel", "BlockModel", "PageModel",
    "build_page_model", "annotate_toc", "annotate_formulas", "annotate_style",
]