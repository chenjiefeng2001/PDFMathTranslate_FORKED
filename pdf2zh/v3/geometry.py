"""Module: Geometry Engine — 阶段二「字符 → 单词 → 行 → 段落 → 阅读顺序」纯算法恢复。

与路线图阶段二完全算法化对齐（不依赖 LLM / YOLO / doclayout）：

    glyph / character / font / matrix
                    │
                    ▼
    Char ──clustering──▶ Word ──baseline──▶ Line ──spacing/indent──▶ Paragraph
                    │
                    ▼
        XY-Cut + Topological Sort  →  Reading Order（多栏阅读顺序）

设计原则：
  * 输入为最小可观测单元 ``Char``（text + bbox + size + font），
    可从任何 PDF 解析器（pymupdf / pdfminer / 测试合成数据）提取，
    不绑定具体 PDF 库 —— 全部几何算法只依赖 bbox。
  * 输出 ``Word / Line / Paragraph`` 均携带 bbox 与统计特征，
    供 Structure Engine / Evaluation 消费。
  * 阅读顺序使用经典 **XY-Cut 递归切分**（先竖切分栏、再横切分行），
    替代「按 bbox.y 排序」的错误做法（双栏 PDF 的 y 交错问题）。

Usage::

    from pdf2zh.v3.geometry import GeometryEngine, Char
    chars = extract_chars_from_page(page)          # pymupdf 页 → Char[]
    page_model = engine.build_page(chars, page_num=0)
    for para in page_model.reading_order():        # XY-Cut 后的真实阅读顺序
        print(para.text)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

# 目录行尾模式：点线引导 + 行尾页码（用于段落合并保护，避免目录条目被
# 与下一行合并成一个段落 —— 与 pdf2zh/toc.py 的检测口径一致）
_TOC_LINE_END_RE = re.compile(r"(?:[.·…‥][\.\s·…‥]*\d{1,4})\s*$")

# ── 数据模型 ──────────────────────────────────────────────────────────────


@dataclass
class Char:
    """最小几何单元：单个字符及其版面属性。"""

    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    size: float = 12.0
    font: str = ""
    page_num: int = 0

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2.0

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2.0

    @property
    def baseline_y(self) -> float:
        """行基线近似：下边缘（PDF 坐标系 y 向上，baseline 为 y0 下沿）。"""
        return self.y0

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "x0": round(self.x0, 2),
            "y0": round(self.y0, 2),
            "x1": round(self.x1, 2),
            "y1": round(self.y1, 2),
            "size": round(self.size, 2),
            "font": self.font,
            "page": self.page_num,
        }


@dataclass
class Word:
    """由字符聚类得到的单词（词内字符横向相邻、同一基线）。"""

    chars: List[Char] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(c.text for c in self.chars)

    @property
    def x0(self) -> float:
        return min(c.x0 for c in self.chars)

    @property
    def x1(self) -> float:
        return max(c.x1 for c in self.chars)

    @property
    def y0(self) -> float:
        return min(c.y0 for c in self.chars)

    @property
    def y1(self) -> float:
        return max(c.y1 for c in self.chars)

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    @property
    def size(self) -> float:
        return max(c.size for c in self.chars) if self.chars else 12.0

    @property
    def baseline_y(self) -> float:
        return min(c.baseline_y for c in self.chars) if self.chars else 0.0

    @property
    def font(self) -> str:
        return self.chars[0].font if self.chars else ""

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "x0": round(self.x0, 2),
            "y0": round(self.y0, 2),
            "x1": round(self.x1, 2),
            "y1": round(self.y1, 2),
            "size": round(self.size, 2),
            "font": self.font,
        }


@dataclass
class Line:
    """一条物理行：同一基线上的若干单词。"""

    words: List[Word] = field(default_factory=list)

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    @property
    def x0(self) -> float:
        return min(w.x0 for w in self.words)

    @property
    def x1(self) -> float:
        return max(w.x1 for w in self.words)

    @property
    def y0(self) -> float:
        return min(w.y0 for w in self.words)

    @property
    def y1(self) -> float:
        return max(w.y1 for w in self.words)

    @property
    def baseline_y(self) -> float:
        return min(w.baseline_y for w in self.words) if self.words else 0.0

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2.0

    @property
    def size(self) -> float:
        return max(w.size for w in self.words) if self.words else 12.0

    @property
    def char_count(self) -> int:
        return sum(len(w.chars) for w in self.words)

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "x0": round(self.x0, 2),
            "y0": round(self.y0, 2),
            "x1": round(self.x1, 2),
            "y1": round(self.y1, 2),
            "size": round(self.size, 2),
        }


@dataclass
class Paragraph:
    """逻辑段落：行距连续、缩进一致的若干物理行。"""

    lines: List[Line] = field(default_factory=list)
    page_num: int = 0

    @property
    def text(self) -> str:
        return "\n".join(l.text for l in self.lines)

    @property
    def x0(self) -> float:
        return min(l.x0 for l in self.lines)

    @property
    def x1(self) -> float:
        return max(l.x1 for l in self.lines)

    @property
    def y0(self) -> float:
        return min(l.y0 for l in self.lines)

    @property
    def y1(self) -> float:
        return max(l.y1 for l in self.lines)

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    @property
    def size(self) -> float:
        return max(l.size for l in self.lines) if self.lines else 12.0

    @property
    def line_count(self) -> int:
        return len(self.lines)

    @property
    def first_line_indent(self) -> float:
        if not self.lines:
            return 0.0
        return self.lines[0].x0 - self.x0

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2.0

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2.0

    @property
    def alignment(self) -> str:
        """段落对齐：left / right / center / justify（按首末行左右边距启发）。"""
        if len(self.lines) < 2:
            return "left"
        slack_left = max(l.x0 for l in self.lines) - min(l.x0 for l in self.lines)
        slack_right = max(l.x1 for l in self.lines) - min(l.x1 for l in self.lines)
        if slack_left < 1.5 and slack_right > 4.0:
            return "left"
        if slack_right < 1.5 and slack_left > 4.0:
            return "right"
        if slack_left < 1.5 and slack_right < 1.5:
            return "center" if abs(self.x1 - self.x0) > 0 else "justify"
        return "justify"

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "page": self.page_num,
            "x0": round(self.x0, 2),
            "y0": round(self.y0, 2),
            "x1": round(self.x1, 2),
            "y1": round(self.y1, 2),
            "size": round(self.size, 2),
            "lines": self.line_count,
            "indent": round(self.first_line_indent, 2),
            "alignment": self.alignment,
        }


@dataclass
class PageGeometry:
    """单页几何模型：Char → Word → Line → Paragraph + 阅读顺序。"""

    page_num: int
    chars: List[Char] = field(default_factory=list)
    words: List[Word] = field(default_factory=list)
    lines: List[Line] = field(default_factory=list)
    paragraphs: List[Paragraph] = field(default_factory=list)
    _reading_order: List[int] = field(default_factory=list)  # paragraph index 序列

    @property
    def text(self) -> str:
        return "\n\n".join(p.text for p in self.reading_order())

    def reading_order(self) -> List[Paragraph]:
        if not self._reading_order:
            return list(self.paragraphs)
        by_index = {i: p for i, p in enumerate(self.paragraphs)}
        return [by_index[i] for i in self._reading_order if i in by_index]


# ── 字符提取（pymupdf 适配） ──────────────────────────────────────────────


def extract_chars_from_page(page, page_num: int = 0) -> List[Char]:
    """从 pymupdf 页面提取字符流（rawdict）。

    每个字符携带 c（文本）、bbox、size、font，作为 Geometry Engine 的输入。
    """
    chars: List[Char] = []
    raw = page.get_text("rawdict")
    for block in raw.get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                span_bbox = span.get("bbox")
                for ch in span.get("chars", []):
                    cb = ch.get("bbox", span_bbox)
                    if not cb:
                        continue
                    text = ch.get("c", "")
                    if not text:
                        continue
                    chars.append(
                        Char(
                            text=text,
                            x0=float(cb[0]),
                            y0=float(cb[1]),
                            x1=float(cb[2]),
                            y1=float(cb[3]),
                            size=float(span.get("size", 12.0)),
                            font=str(span.get("font", "")),
                            page_num=page_num,
                        )
                    )
    return chars


def extract_chars_from_stream(
    stream: bytes, max_pages: Optional[int] = None
) -> List[Char]:
    """从 PDF 字节流提取全部字符（跨页）。"""
    import pymupdf

    chars: List[Char] = []
    doc = pymupdf.open(stream=stream, filetype="pdf")
    try:
        n = doc.page_count
        if max_pages is not None:
            n = min(n, max_pages)
        for i in range(n):
            chars.extend(extract_chars_from_page(doc.load_page(i), page_num=i))
    finally:
        doc.close()
    return chars


def chars_from_ltpage(ltpage, page_num: int = 0) -> List[Char]:
    """从 pdfminer LTPage/LTFigure 提取字符流（V8.3 IR 主链路适配器）。

    与 ``extract_chars_from_page`` 对应，但输入是 pdfminer 的页面对象而非
    pymupdf —— 这样 Geometry Engine 可以直接消费 legacy 解析器（TranslateConverter
    ``receive_layout``）正在遍历的同一份字符流，实现双轨并存的收敛。
    """
    chars: List[Char] = []
    if ltpage is None:
        return chars
    for child in ltpage:
        # 叶子字符：LTChar 机会有 size 属性；容器（LTTextContainer/
        # LTTextLine）也有 get_text/x0 但无 size，应继续向下递归。
        if hasattr(child, "size"):
            text = child.get_text()
            if not text:
                continue
            try:
                size = float(getattr(child, "size", 12.0) or 12.0)
            except (TypeError, ValueError):
                size = 12.0
            bbox = list(getattr(child, "bbox", (0, 0, 0, 0)) or (0, 0, 0, 0))
            x0 = float(getattr(child, "x0", bbox[0]) or bbox[0])
            y0 = float(getattr(child, "y0", bbox[1]) or bbox[1])
            x1 = float(getattr(child, "x1", bbox[2]) or bbox[2])
            y1 = float(getattr(child, "y1", bbox[3]) or bbox[3])
            chars.append(
                Char(
                    text=text,
                    x0=x0,
                    y0=y0,
                    x1=x1,
                    y1=y1,
                    size=size,
                    font=str(getattr(child, "fontname", "") or ""),
                    page_num=page_num,
                )
            )
        elif hasattr(child, "__iter__"):
            chars.extend(chars_from_ltpage(child, page_num=page_num))
    return chars


# ── 聚类参数 ──────────────────────────────────────────────────────────────


@dataclass
class GeometryConfig:
    """Geometry Engine 聚类参数（全部基于字号自适应，无魔法硬编码）。"""

    gap_word_ratio: float = 0.45
    """词内最大横向间距 / 中位字符宽；超过则切词（空格间隙）。"""

    column_gap_ratio: float = 2.5
    """栏级空白带阈值 / 字号：同一基线单词间隙达到该值即拆分为两条物理行。"""

    baseline_tol_ratio: float = 0.35
    """同一行基线的 y 容差 / 字号；超过则分行。"""

    line_gap_ratio: float = 1.9
    """段落内最大行距 / 字号；超过则视为段间空白。"""

    indent_tol: float = 4.0
    """首行缩进识别容差（pt）。"""

    min_word_len: int = 1
    """少于该字符数的词丢弃（孤立标点等噪声）。"""

    # ── 竖向文本条 / 旋转文本剔除参数（P2 通用化）───────────────────
    vertical_strip_min_stack: int = 3
    """竖向文本条最少堆叠行数；少于该数目不剔除。"""

    vertical_strip_x_tol: float = 3.0
    """竖向文本条判定时行间 x 范围的允许偏差（pt）。"""

    vertical_strip_max_chars: int = 3
    """竖向文本条单行最大字符数（旋转页边文字每行即一字符或短词）。"""

    vertical_strip_aspect: float = 1.6
    """旋转文本字形纵横比下限：字符高/宽 ≥ 该值视为旋转字形（如旋转 90° 的
    页边编号字形高远大于宽），用于识别不满足「同 x 范围」但字形旋转的条带。"""


# ── 引擎 ─────────────────────────────────────────────────────────────────


class GeometryEngine:
    """纯算法几何恢复：Char → Word → Line → Paragraph → 阅读顺序。

    每个阶段都是确定性函数，输出完整中间统计，
    供 Structure Engine / Evaluator / 调试复现使用。
    """

    def __init__(self, config: Optional[GeometryConfig] = None) -> None:
        self.config = config or GeometryConfig()

    # ── 阶段 1：Char → Word ─────────────────────────────────────────

    def build_words(self, chars: Sequence[Char]) -> List[Word]:
        """按基线与横向间距聚类字符为单词。

        算法：按 (baseline 行分组) → 组内按 x 排序 → 空格字符为**硬切词符**
        （真实 PDF 提取出的空格即词边界）；对不显式输出空格的 PDF，
        间距超过 ``gap_word_ratio * 中位字宽`` 处切词作为兜底。
        """
        chars = sorted(chars, key=lambda c: (round(c.baseline_y, 1), c.x0))
        rows: List[List[Char]] = []
        for c in chars:
            for row in rows:
                ref = row[0]
                tol = self.config.baseline_tol_ratio * max(c.size, ref.size)
                if abs(c.baseline_y - ref.baseline_y) <= tol:
                    row.append(c)
                    break
            else:
                rows.append([c])
        words: List[Word] = []
        for row in rows:
            row.sort(key=lambda c: c.x0)
            prev_is_space = True
            for ch in row:
                if ch.text.isspace():
                    prev_is_space = True
                    continue
                if not words:
                    words.append(Word([ch]))
                    prev_is_space = False
                    continue
                last = words[-1]
                ref_size = max(ch.size, last.chars[-1].size)
                gap = ch.x0 - last.x1
                same_baseline = (
                    abs(ch.baseline_y - last.baseline_y)
                    <= self.config.baseline_tol_ratio * ref_size
                )
                if (
                    same_baseline
                    and not prev_is_space
                    and gap <= self.config.gap_word_ratio * ref_size
                ):
                    last.chars.append(ch)
                else:
                    words.append(Word([ch]))
                prev_is_space = False
        return [w for w in words if len(w.chars) >= self.config.min_word_len]

    # ── 阶段 2：Word → Line ─────────────────────────────────────────

    def build_lines(self, words: Sequence[Word]) -> List[Line]:
        """按基线聚类单词为物理行，行内按 x 排序。

        同一基线上的单词若存在**栏级空白带**（间距远超正常词距），
        拆分为两条物理行 —— 这是双栏 PDF 在同一基线交错排版的直接证据。
        最后剔除**竖向文本条**（旋转 90° 的页边文字，如 arXiv 侧边编号）。
        """
        words = sorted(words, key=lambda w: (round(w.baseline_y, 1), w.x0))
        lines: List[Line] = []
        for w in words:
            for line in lines:
                ref = line.words[0]
                tol = self.config.baseline_tol_ratio * max(w.size, ref.size)
                if abs(w.baseline_y - ref.baseline_y) <= tol:
                    line.words.append(w)
                    break
            else:
                lines.append(Line([w]))
        for line in lines:
            line.words.sort(key=lambda w: w.x0)
        split: List[Line] = []
        for line in lines:
            split.extend(self._split_column_gaps(line))
        cleaned = self._drop_vertical_strips(split)
        cleaned.sort(key=lambda l: -l.baseline_y)
        return cleaned

    def _drop_vertical_strips(self, lines: Sequence[Line]) -> List[Line]:
        """剔除竖向文本条：≥3 条同 x 范围、极短、纵向堆叠的行（通用化版）。

        覆盖两类旋转/竖向页边文字（arXiv 侧边编号 / 书脊文字 / 旋转标题）：

        1. **同 x 范围堆叠**：同一 x 范围、每条 ≤``vertical_strip_max_chars``
           字符、纵向等距堆叠 ≥``vertical_strip_min_stack`` 条 → 剔除；
        2. **旋转字形纵横比**：单条行内字符字形高/宽 ≥``vertical_strip_aspect``
           （旋转 90° 的字形），且纵向堆叠 ≥``vertical_strip_min_stack`` 条
           同 x 中心 → 剔除（覆盖不满足规则 1 的旋转文本，如旋转标题）。

        两条规则都通过 ``GeometryConfig`` 可调，避免硬编码阈值。
        """
        cfg = self.config
        if len(lines) < cfg.vertical_strip_min_stack:
            return list(lines)
        keep = [True] * len(lines)

        def _rotated(line: Line) -> bool:
            """行内所有字符均为旋转字形（高 ≥ aspect×宽）。"""
            if not line.words:
                return False
            return all(
                (
                    c.height >= cfg.vertical_strip_aspect * max(c.width, 0.01)
                    or c.width <= 0.01
                )
                for w in line.words
                for c in w.chars
            )

        for i in range(len(lines)):
            if not keep[i]:
                continue
            a = lines[i]
            if a.char_count > cfg.vertical_strip_max_chars and not _rotated(a):
                continue
            group = [i]
            for j in range(len(lines)):
                if (
                    i != j
                    and keep[j]
                    and (
                        lines[j].char_count <= cfg.vertical_strip_max_chars
                        or _rotated(lines[j])
                    )
                ):
                    b = lines[j]
                    same_x = (
                        abs(a.x0 - b.x0) <= cfg.vertical_strip_x_tol
                        and abs(a.x1 - b.x1) <= cfg.vertical_strip_x_tol
                    )
                    same_center = abs(a.cx - b.cx) <= cfg.vertical_strip_x_tol
                    if same_x or (_rotated(a) and _rotated(b) and same_center):
                        group.append(j)
            if len(group) >= cfg.vertical_strip_min_stack:
                for idx in group:
                    keep[idx] = False
        return [l for l, k in zip(lines, keep) if k]

    @staticmethod
    def _split_column_gaps(line: Line) -> List[Line]:
        """把同一基线、但存在栏级横向空白带的单词行拆为多条物理行。

        栏间隙判定：间隙 ≥ ``column_gap_ratio(2.5) × 字号``。
        正常词距（含两端对齐拉开的词距）通常 ≤ 1.5 倍字号，
        2.5 倍字号足以区分「拉开的词距」与「栏间空白带」。
        """
        ws = line.words
        if len(ws) < 2:
            return [line]
        threshold = GeometryConfig.column_gap_ratio * line.size
        chunks: List[Line] = []
        cur = Line([ws[0]])
        for i in range(1, len(ws)):
            gap = ws[i].x0 - ws[i - 1].x1
            if gap >= threshold:
                chunks.append(cur)
                cur = Line([ws[i]])
            else:
                cur.words.append(ws[i])
        chunks.append(cur)
        return chunks

    # ── 阶段 3：Line → Paragraph ─────────────────────────────────────

    def build_paragraphs(
        self, lines: Sequence[Line], page_num: int = 0
    ) -> List[Paragraph]:
        """按行距连续性 + 缩进/对齐把物理行聚为逻辑段落。

        行距 ≤ ``line_gap_ratio * 字号`` 且缩进突变（首行缩进）不打断段落；
        行距超过阈值视为段间空白。列内 x 范围不重叠的行不合并（保护双栏）。
        """
        lines = sorted(lines, key=lambda l: (-l.baseline_y, l.x0))
        paragraphs: List[Paragraph] = []
        for line in lines:
            if not paragraphs:
                paragraphs.append(Paragraph([line], page_num=page_num))
                continue
            last = paragraphs[-1]
            prev = last.lines[-1]
            gap = prev.y0 - line.y0  # PDF 坐标系 y 向上，下一行 y 更小
            size_ref = max(line.size, prev.size, 1e-6)
            x_overlap = (
                line.x0 < prev.x1 - 1.0 and line.x1 > prev.x0 + 1.0
            ) or line.x0 - prev.x1 < 0.5 * size_ref
            # 目录行保护：上一行以「点线 + 页码」结尾时不与下一行合并
            toc_break = bool(_TOC_LINE_END_RE.search(prev.text))
            if (
                gap > 0
                and gap <= self.config.line_gap_ratio * size_ref
                and x_overlap
                and not toc_break
            ):
                last.lines.append(line)
            else:
                paragraphs.append(Paragraph([line], page_num=page_num))
        return paragraphs

    # ── 阶段 4：Reading Order（XY-Cut + 栏检测） ──────────────────────

    def reading_order(self, paragraphs: Sequence[Paragraph]) -> List[int]:
        """XY-Cut 递归切分恢复真实阅读顺序，返回 paragraph 下标序列。

        算法（栏感知 XY-Cut，解决纯 XY-Cut 在目录页/交排页上的误切）：

        1. 对当前区域先做**栏检测**：按段落中心 x 聚簇，簇满足
           「≥2 段 且 y 跨度与另一簇相交」即视为并列栏；
        2. 存在并列栏 → 按栏（x 由小到大）递归，栏内回到步骤 1；
        3. 无并列栏 → 寻找横空白带切分（行/区块），上下递归；
        4. 无法切分 → 退化为按 y 排序（单栏）。

        双栏论文（栏内 y 交错）与目录页（底部页码/整宽目录行）均不会
        被误判，因为栏检测以「簇间 y 跨度相交」为硬条件。
        """
        indices = list(range(len(paragraphs)))
        if len(indices) <= 1:
            return indices
        order: List[int] = []

        def _xy_cut(idx_list: List[int]) -> None:
            if not idx_list:
                return
            if len(idx_list) == 1:
                order.append(idx_list[0])
                return
            # 1. 栏检测：按段落中心 x 聚簇
            cols = self._detect_columns(idx_list, paragraphs)
            if len(cols) >= 2:
                for col in cols:
                    _xy_cut(col)
                return
            # 2. 横切：寻找能一分为二的横空白带
            h_cut = self._find_horizontal_cut(idx_list, paragraphs)
            if h_cut is not None:
                above = [i for i in idx_list if paragraphs[i].y0 >= h_cut]
                below = [i for i in idx_list if paragraphs[i].y1 <= h_cut]
                if above and below and len(above) + len(below) == len(idx_list):
                    _xy_cut(above)
                    _xy_cut(below)
                    return
            # 3. 无法切分：退化为按 y 排序（单栏）
            for i in sorted(idx_list, key=lambda i: -paragraphs[i].y0):
                order.append(i)

        _xy_cut(indices)
        return order

    def _detect_columns(
        self, idx_list: List[int], paragraphs: Sequence[Paragraph]
    ) -> List[List[int]]:
        """把段落下标按 x 中心聚簇，返回并列栏（≥2 且 y 跨度相交的簇）。

        未归属任何并列栏的孤立段（如居中页码）并入最近并列栏。
        无并列栏时返回单元素列表（整组即一栏）。
        """
        if len(idx_list) <= 1:
            return [list(idx_list)]
        region_x0 = min(paragraphs[i].x0 for i in idx_list)
        region_x1 = max(paragraphs[i].x1 for i in idx_list)
        region_w = max(1e-6, region_x1 - region_x0)
        # 整宽块（标题/目录行/表格等跨栏块）：不参与栏聚簇，
        # 按其 y 位置在栏流之前/之后整体排序
        full_wide = [i for i in idx_list if paragraphs[i].width >= 0.65 * region_w]
        idx_list = [i for i in idx_list if i not in full_wide]
        if not idx_list:
            return [sorted(full_wide, key=lambda i: -paragraphs[i].y0)]
        items = sorted(idx_list, key=lambda i: paragraphs[i].cx)
        widths = [paragraphs[i].width for i in idx_list]
        median_w = sorted(widths)[len(widths) // 2] if widths else 200.0
        gap_threshold = max(0.35 * median_w, 40.0)
        clusters: List[List[int]] = []
        for i in items:
            if not clusters:
                clusters.append([i])
                continue
            if paragraphs[i].cx - paragraphs[clusters[-1][-1]].cx > gap_threshold:
                clusters.append([i])
            else:
                clusters[-1].append(i)
        if len(clusters) < 2:
            return [list(idx_list)]

        # 并列栏资格：≥2 段 且 y 跨度与另一合格簇相交
        # （spans 为 (y0=下缘, y1=上缘)；两簇相交 ⇔ y0_a < y1_b 且 y0_b < y1_a）
        spans = []
        for cl in clusters:
            y0 = min(paragraphs[i].y0 for i in cl)
            y1 = max(paragraphs[i].y1 for i in cl)
            spans.append((y0, y1))
        qualifying = [False] * len(clusters)
        for a in range(len(clusters)):
            if len(clusters[a]) < 2:
                continue
            for b in range(len(clusters)):
                if a == b or len(clusters[b]) < 2:
                    continue
                y0_a, y1_a = spans[a]
                y0_b, y1_b = spans[b]
                if y0_a < y1_b and y0_b < y1_a:
                    qualifying[a] = True
                    break
        cols = [cl for cl, q in zip(clusters, qualifying) if q]
        if len(cols) < 2:
            return [list(idx_list)]
        # 非栏块（整宽块 + 孤立段，如居中页码）：按 y 位置归类 ——
        # 高于栏流顶 → 最前；低于栏流底 → 最后；与栏流 y 相交 → 并入最近栏
        col_ids = [i for cl in cols for i in cl]
        col_top = max(paragraphs[i].y1 for i in col_ids)
        col_bottom = min(paragraphs[i].y0 for i in col_ids)
        stragglers = [i for i in full_wide] + [
            i for cl in clusters if cl not in cols for i in cl
        ]
        above = sorted(
            [i for i in stragglers if paragraphs[i].y1 > col_top],
            key=lambda i: -paragraphs[i].y0,
        )
        below = sorted(
            [i for i in stragglers if paragraphs[i].y0 < col_bottom],
            key=lambda i: -paragraphs[i].y0,
        )
        inside = [i for i in stragglers if i not in above and i not in below]
        col_cx = [sum(paragraphs[i].cx for i in cl) / len(cl) for cl in cols]
        merged: List[List[int]] = [list(cl) for cl in cols]
        for i in inside:
            nearest = min(
                range(len(cols)), key=lambda k: abs(paragraphs[i].cx - col_cx[k])
            )
            merged[nearest].append(i)
        if above:
            merged.insert(0, above)
        if below:
            merged.append(below)
        return merged

    @staticmethod
    def _find_horizontal_cut(idx_list: List[int], paragraphs: Sequence[Paragraph]):
        """寻找横空白带 y = cut：上下两组互不跨界且都非空。"""
        ys: List[float] = []
        for i in idx_list:
            ys.extend([paragraphs[i].y0, paragraphs[i].y1])
        ys.sort()
        for cut in ys[1:-1]:
            above = [i for i in idx_list if paragraphs[i].y0 >= cut - 0.5]
            below = [i for i in idx_list if paragraphs[i].y1 <= cut + 0.5]
            if above and below and len(above) + len(below) == len(idx_list):
                return cut
        return None

    # ── 整页构建 ─────────────────────────────────────────────────────

    def build_page(self, chars: Sequence[Char], page_num: int = 0) -> PageGeometry:
        """完整流水线：Char → Word → Line → Paragraph → Reading Order。"""
        page = PageGeometry(page_num=page_num, chars=list(chars))
        page.words = self.build_words(page.chars)
        page.lines = self.build_lines(page.words)
        page.paragraphs = self.build_paragraphs(page.lines, page_num=page_num)
        page._reading_order = self.reading_order(page.paragraphs)
        return page

    def build_document(
        self, chars_by_page: Sequence[Sequence[Char]]
    ) -> List[PageGeometry]:
        """多页构建，返回按页号排序的 PageGeometry 列表。"""
        pages: List[PageGeometry] = []
        for page_num, chars in enumerate(chars_by_page):
            pages.append(self.build_page(chars, page_num=page_num))
        return pages


__all__ = [
    "Char",
    "Word",
    "Line",
    "Paragraph",
    "PageGeometry",
    "GeometryConfig",
    "GeometryEngine",
    "extract_chars_from_page",
    "extract_chars_from_stream",
]
