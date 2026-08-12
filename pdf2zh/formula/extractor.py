"""P6.2 — FormulaObject 抽取与解析（规范书 §5.3 + §4.3）。

把 ``LogicalParagraph`` 内的 ``VisualLine`` / ``StyleRun`` 按置信度打分
转化为 ``FormulaObject``（一等排版对象，几何不可变）或 ``InlineTextRun``。

判定阈值（规范书 §5.3）：
    S >= 0.75        → FormulaObject（提取并锁定几何）
    0.45 <= S < 0.75 → 待定歧义（结合上下文消歧）
    S < 0.45         → 普通 TextRun
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from pdf2zh.geometry.glyph import Glyph, GlyphBBox
from pdf2zh.geometry.line import VisualLine
from pdf2zh.geometry.style_run import StyleRun
from pdf2zh.formula.confidence import (
    FormulaConfidenceEngine,
    FormulaScore,
    is_single_operator,
)


@dataclass
class FormulaObject:
    """不可变公式对象（一等排版对象，内部几何绝对不可变）。"""

    formula_id: str
    glyphs: List[Glyph]
    bbox: GlyphBBox                       # (x0, y0, x1, y1)
    baseline: float
    raw_latex_approx: Optional[str] = None
    is_display_mode: bool = False
    confidence_score: float = 0.0

    @property
    def text(self) -> str:
        return "".join(g.char for g in self.glyphs)

    @property
    def x0(self) -> float:
        return self.bbox[0]

    @property
    def y0(self) -> float:
        return self.bbox[1]

    @property
    def x1(self) -> float:
        return self.bbox[2]

    @property
    def y1(self) -> float:
        return self.bbox[3]

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return max(0.0, self.y1 - self.y0)

    def to_dict(self) -> dict:
        return {
            "formula_id": self.formula_id,
            "text": self.text,
            "bbox": [round(v, 2) for v in self.bbox],
            "baseline": round(self.baseline, 2),
            "is_display_mode": self.is_display_mode,
            "confidence_score": round(self.confidence_score, 3),
            "glyph_count": len(self.glyphs),
        }


@dataclass
class InlineTextRun:
    """行内普通文本段（P7 消费端与 FormulaObject 并列）。"""

    text: str
    style_runs: List[StyleRun]
    bbox: GlyphBBox
    font_size: float = 12.0

    @property
    def x0(self) -> float:
        return self.bbox[0]

    @property
    def y0(self) -> float:
        return self.bbox[1]

    @property
    def x1(self) -> float:
        return self.bbox[2]

    @property
    def y1(self) -> float:
        return self.bbox[3]

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return max(0.0, self.y1 - self.y0)

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "bbox": [round(v, 2) for v in self.bbox],
            "font_size": round(self.font_size, 2),
            "style_runs": [r.to_dict() for r in self.style_runs],
        }


# InlineObject 联合类型：InlineTextRun | FormulaObject
InlineObject = object


def _glyph_slice(glyphs: Sequence[Glyph],
                 start: int, end: int) -> Tuple[List[Glyph], GlyphBBox, float]:
    """取 [start, end] 字形切片 + bbox + 基线（按字号加权）。"""
    seg = list(glyphs[start:end + 1])
    bbox = (
        min(g.x0 for g in seg),
        min(g.y0 for g in seg),
        max(g.x1 for g in seg),
        max(g.y1 for g in seg),
    )
    total_w = sum(max(g.font_size, 0.01) for g in seg)
    baseline = (sum(g.baseline * max(g.font_size, 0.01) for g in seg) / total_w
                if total_w > 0 else seg[0].baseline)
    return seg, bbox, baseline


class FormulaExtractor:
    """按 StyleRun 逐段打分，把段落切分为 FormulaObject / InlineTextRun。"""

    def __init__(self, engine: Optional[FormulaConfidenceEngine] = None,
                 threshold_high: float = 0.75,
                 threshold_low: float = 0.45,
                 context_resolve: bool = True) -> None:
        self.engine = engine or FormulaConfidenceEngine(
            threshold_high=threshold_high, threshold_low=threshold_low)
        self.threshold_high = threshold_high
        self.threshold_low = threshold_low
        self.context_resolve = context_resolve
        # F2 修复：formula_id 唯一性 —— 原 ``object_id`` 方案在「同一 x 起始的
        # 多个 display 公式」（居中公式行）会撞 id，导致 solver 的
        # ``formula_by_id`` 覆盖、``formula_placements`` 全部指向最后一个公式，
        # F2 display 标记漏标。加实例序号保证页内唯一。
        self._formula_seq = 0

    # ── 行级抽取 ──────────────────────────────────────────────────

    def extract_line(self, line: VisualLine,
                     layout_class: Optional[object] = None,
                     formula_prefix: str = "formula") -> Tuple[List, List]:
        """抽取单行：返回 (objects, scores)。

        ``objects`` 元素为 ``FormulaObject`` 或 ``InlineTextRun``；
        ``scores`` 与 objects 一一对应（FormulaScore）。
        """
        glyphs = line.glyphs
        runs = line.style_runs
        if not glyphs:
            return [], []
        if not runs:
            # 无 style_runs（退化场景）：整行作为单一对象
            text = line.text
            score = self.engine.score(text, glyphs, glyphs[0].font_name,
                                      layout_class)
            obj = self._make_object(text, glyphs, line.bbox, line.master_baseline,
                                    score, formula_prefix)
            return [obj], [score]
        objects: List = []
        scores: List[FormulaScore] = []
        texts = [glyphs[r.start_index:r.end_index + 1] for r in runs]
        for i, (run, seg) in enumerate(zip(runs, texts)):
            seg_glyphs = list(seg)
            if not seg_glyphs:
                continue
            text = "".join(g.char for g in seg_glyphs)
            score = self.engine.score(text, seg_glyphs, run.font_name,
                                      layout_class)
            # 待定歧义 + 上下文消歧：相邻段同为公式则向公式倾斜
            if (score.verdict == "ambiguous" and self.context_resolve
                    and len(texts) > 1):
                neighbors = [j for j in (i - 1, i + 1)
                             if 0 <= j < len(texts)]
                math_neighbors = sum(
                    1 for j in neighbors
                    if self.engine.score(
                        "".join(g.char for g in texts[j]),
                        list(texts[j]),
                        runs[j].font_name, layout_class,
                    ).verdict in ("formula", "ambiguous")
                )
                if math_neighbors >= len(neighbors):
                    score.verdict = "formula"
                    score.total = max(score.total, self.threshold_high)
            # 强信号消歧：明确数学字体（C_font>=0.85）+ 数学语法结构
            # （括号对 / 等号 / 运算符 / 上下标）→ 提升为公式。
            # 规范 §5.3 原则：不依赖单一字体硬编码，须字体与结构联合判定。
            if (score.verdict == "ambiguous"
                    and score.font >= 0.85
                    and self._structure_hint(text)):
                score.verdict = "formula"
                score.total = max(score.total, self.threshold_high)
            # 整行公式强信号（用户驱动修复 §1）：独占一行的全数学字体
            # 文本（整行所有字形 font_score >= 0.85）+ 数学结构 → 块级
            # 展示公式。即使逐段打分略低也提升为公式对象 —— 修复
            # 「R1 = {(1,1), ...} 独立成行展示公式被误判为行内文本」，
            # 使 P6 真正产出 FormulaObject（Display/Inline 二分的前提）。
            if (score.verdict != "formula"
                    and self._whole_line_math_hint(line.glyphs, text)):
                score.verdict = "formula"
                score.total = max(score.total, self.threshold_high)
            seg_bbox = (
                min(g.x0 for g in seg_glyphs),
                min(g.y0 for g in seg_glyphs),
                max(g.x1 for g in seg_glyphs),
                max(g.y1 for g in seg_glyphs),
            )
            total_w = sum(max(g.font_size, 0.01) for g in seg_glyphs)
            baseline = (sum(g.baseline * max(g.font_size, 0.01)
                            for g in seg_glyphs) / total_w
                        if total_w > 0 else line.master_baseline)
            obj = self._make_object(text, seg_glyphs, seg_bbox, baseline,
                                    score, formula_prefix)
            objects.append(obj)
            scores.append(score)
        return objects, scores

    def _make_object(self, text: str, glyphs: List[Glyph], bbox: GlyphBBox,
                     baseline: float, score: FormulaScore,
                     formula_prefix: str):
        if score.verdict == "formula":
            _seq = self._formula_seq
            self._formula_seq += 1
            return FormulaObject(
                formula_id=f"{formula_prefix}_{_seq}_{glyphs[0].object_id}",
                glyphs=glyphs,
                bbox=bbox,
                baseline=baseline,
                raw_latex_approx=self._approx_latex(text),
                is_display_mode=False,
                confidence_score=score.total,
            )
        return InlineTextRun(text=text, style_runs=[], bbox=bbox,
                             font_size=max((g.font_size for g in glyphs),
                                           default=12.0))

    # ── 段落级抽取 ────────────────────────────────────────────────

    def extract_paragraph(self, para, layout_class: Optional[object] = None,
                          formula_prefix: str = "formula",
                          whole_line_formula_ok: bool = True) -> List:
        """抽取整段：把每行切分为 InlineObject 序列并回填到 para.inline_objects。

        **Display/Inline 公式二分（用户驱动修复）**：行级抽取完成后，对每行
        对象执行块级展示公式判定（整行公式 / 超宽公式 / 居中公式 → display），
        使块级展示公式占据独立垂直高度 —— 这是 P9 LayoutSolver 垂直流堆叠
        （Vertical Flow Stacking）的输入，杜绝「译文与展示公式重叠」。

        同时记录 ``para._line_objects``（按行分组），供 P7 TranslationUnit
        在行间插入 ``\\n`` 保留源段落行结构（§6.2 三阶段坐标的行级求解依据）。
        """
        objects: List = []
        line_objects: List[List] = []
        for line in para.lines:
            objs, _ = self.extract_line(line, layout_class, formula_prefix)
            if objs:
                self._mark_display_flags(objs, para, line)
                line_objects.append(objs)
                objects.extend(objs)
        para.inline_objects = objects
        para._line_objects = line_objects
        return objects

    @staticmethod
    def _mark_display_flags(objs: Sequence, para, line) -> None:
        """块级展示公式判定（Display/Inline 二分，用户驱动修复 §1）。

        判定规则（满足任一即 ``is_display_mode=True``，作为独立垂直块）：
        1. **整行公式**：该行无普通文本对象，且公式宽度 >= 2×字号；
        2. **超宽公式**：公式宽度 > 0.6 × 段落宽度（规范书 §5.3 提案）；
        3. **居中公式**：公式占该行宽度主体（>= 50%）且水平中心偏离段落
           中心 < 0.1 × 段落宽度 —— 半宽门槛杜绝「``Let f(x) be`` 中
           行内公式」被误判为块级展示公式。

        **单符号排除**：宽度 < 2×字号的公式（``=``/``≠``/``±`` 等）保持
        Inline —— 杜绝「独立符号被拉成块级展示公式」导致的译文行首/行尾
        布局失向（用户反馈病灶三）。
        """
        formulas = [o for o in objs if isinstance(o, FormulaObject)]
        texts = [o for o in objs if not isinstance(o, FormulaObject)]
        if not formulas:
            return
        para_width = max(
            float(getattr(para, "x1", 0.0) or 0.0)
            - float(getattr(para, "x0", 0.0) or 0.0), 1e-6)
        line_width = max(
            float(getattr(line, "x1", 0.0) or 0.0)
            - float(getattr(line, "x0", 0.0) or 0.0), 1e-6)
        line_size = max(
            (float(getattr(o, "font_size", 0.0) or 0.0) for o in objs),
            default=12.0)
        center = (float(para.x0) + float(para.x1)) / 2.0
        whole_line = not texts
        for f in formulas:
            if whole_line:
                # 整行公式：该行语义即「块级展示公式」，所有段（含窄段）
                # 均标记 display —— 整行判定优先于单符号排除。
                f.is_display_mode = True
                continue
            if f.width < 2.0 * max(line_size, 1e-6):
                continue                # 单符号：保持 Inline
            if f.width > 0.6 * para_width:
                f.is_display_mode = True
                continue
            if f.width >= 0.5 * line_width:   # 公式占行宽主体才允许居中判定
                f_center = (f.x0 + f.x1) / 2.0
                if abs(f_center - center) < 0.1 * para_width:
                    f.is_display_mode = True

    # ── 近似 LaTeX ────────────────────────────────────────────────

    def _whole_line_math_hint(self, glyphs, text: str) -> bool:
        """整行数学字体 + 数学结构强信号（行级联合判定，用户驱动修复）。

        独占一行的全数学字体文本是块级展示公式的最强信号 —— 无论逐段
        打分如何，只要整行所有字形均为数学字体（``font_score >= 0.85``）
        且文本含数学结构（``=``/括号/运算符等），即提升为公式对象。

        与文本行混排的数学符号（``Is = reflexive?`` 中的 ``=``）因行内
        混有普通字体（Helv 等）而不满足 ``all()``，保持 Inline —— 兼容
        「单符号不被拉成块级公式」的判定（用户反馈病灶三）。
        """
        if not glyphs or not text:
            return False
        if is_single_operator(text):
            # 模块 4（规范 §3.4）：孤立基础运算符（独占一行、无变量/文字
            # 上下文）不因「整行数学字体 + 数学结构」提升为公式对象 ——
            # 保持普通 TextRun 参与文本翻译，杜绝孤立算子行首撕裂。
            return False
        if not all(self.engine.font_score(g.font_name) >= 0.85
                   for g in glyphs):
            return False
        return self._structure_hint(text)

    @staticmethod
    def _structure_hint(text: str) -> bool:
        """数学语法结构提示（与数学字体联合判定，见 extract_line）。

        - 括号配对（f(x)、[a,b]）
        - 运算符（= + - * / ^ _ ≤ ≥ ∈ ⊂ 等）
        - 上下标（Unicode 上标/下标字符）
        """
        if not text:
            return False
        ops = set("=+-*/^_<>=≤≥≠±×÷⋅∈⊂⊃∪∩∑∏∫√→←⇒⇔")
        if any(c in ops for c in text):
            return True
        if "(" in text and ")" in text:
            return True
        if any("\u2070" <= c <= "\u209f" or "\u2080" <= c <= "\u209c" for c in text):
            return True
        return False

    @staticmethod
    def _approx_latex(text: str) -> Optional[str]:
        """轻量 LaTeX 近似（符号映射表，不引入完整解析器，遗留项 1）。

        之前恒返回 None；现委托 ``formula.latex_approx.to_latex_approx``，
        对未知字符原样保留（防信息丢失），供 QA / 语义摘要消费。
        """
        from pdf2zh.formula.latex_approx import to_latex_approx
        return to_latex_approx(text)


__all__ = [
    "FormulaObject", "InlineTextRun", "FormulaExtractor",
]
