"""P6 — Formula Object Reconstruction 子系统（规范书 §5.3）。

``pdf2zh/formula/`` 目录规范（规范书第 8 节）：
    confidence.py   # 公式置信度打分引擎
    extractor.py    # FormulaObject 抽取与解析
    anchor.py       # 翻译占位符注入与还原

本模块实现五特征加权置信度打分：

    S_formula = w1·C_font + w2·C_density + w3·C_unicode
              + w4·C_baseline + w5·C_layout
    w = [0.30, 0.25, 0.15, 0.15, 0.15]

判定阈值：
    S >= 0.75        → FormulaObject（提取并锁定几何）
    0.45 <= S < 0.75 → 待定歧义（结合上下文消歧）
    S < 0.45         → 普通 TextRun
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from statistics import median, pstdev
from typing import List, Optional, Sequence

from pdf2zh.geometry.glyph import Glyph

# ── 特征常量 ────────────────────────────────────────────────────────────

# C_font: 数学字体族关键词（Math/Sym/CM/STIX/AMS 等，规范书 §5.3）
MATH_FONT_KEYWORDS = (
    "math", "mathx", "mathcal", "sym", "symbol", "cmsy", "cmmi", "cmex",
    "cmr", "cmti", "stix", "stixgeneral", "ams", "msam", "msbm", "eusm",
    "eufm", "rsfs", "wasy", "txsy", "txmi", "latinmodernmath",
)
GREEK_FONT_KEYWORDS = ("greek", "greeksym", "greekmath")

# C_density: 特殊数学符号集合（含规范书示例 ∫ ∑ √ ≤ →）
MATH_SYMBOL_CHARS = frozenset(
    "∫∑∏√∛∜≤≥≠≈≡≅∞±∓×÷⋅⋆∙∇∂∑∏∈∉⊂⊃⊆⊇∪∩∧∨¬∀∃⇒⇔↔→←↑↓↦⟨⟩‖"
    "⊕⊗⊙∠∥⊥∴∵−∖∅ℕℤℚℝℂ⅀∂∝≪≫∼≃≲≳≈"
    "²³¹⁰⁻⁺⁴⁵⁶⁷⁸⁹₀₁₂₃₄₅₆₇₈₉"   # 上/下标字符（数学强信号）
)
ASCII_MATH_CHARS = frozenset("+-*/=^_{}()<>|~!\\")

# C_unicode: Mathematical Alphanumeric Symbols 区域（U+1D400–U+1D7FF）
# 与常用希腊字母区（U+0370–U+03FF）、数学运算符区（U+2200–U+22FF）

# ── 孤立基础运算符降级回退（规范 §3.4 模块 4）──────────────────────────
# 当检测到的数学符号为**孤立的基础运算符**（``=``/``+``/``-``/``\neq`` 等）
# 且周围无变量字形（独立成段、无任何文字上下文）时，它更可能是自然语言
# 的连字符/等号（如 ``Is = reflexive?``），而不是展示型公式 —— 强制降低
# 公式置信度，回退为普通 ``TextRun`` 参与文本翻译，杜绝「孤立算子被抽成
# FormulaObject 并锁定绝对坐标 → 译文行首撕裂」（病灶三）。
SINGLE_OPERATOR_WHITELIST = frozenset(
    {"=", "+", "-", "*", "×", "÷", "≠", "<", ">", "≤", "≥",
     "±", "∓", "∈", "∉", "⊂", "⊃", "⊆", "⊇", "∪", "∩", "∨", "∧"}
)


def is_single_operator(text: str) -> bool:
    """判定为孤立基础运算符（去除空白后仅 1 个字符且命中白名单）。"""
    t = "".join(c for c in text if not c.isspace())
    return t in SINGLE_OPERATOR_WHITELIST


def is_math_unicode(c: str) -> bool:
    """Unicode 数学字母数字符号区域 / 希腊字母 / 数学运算符判定。"""
    code = ord(c)
    return (
        0x1D400 <= code <= 0x1D7FF    # Mathematical Alphanumeric Symbols
        or 0x0370 <= code <= 0x03FF   # Greek and Coptic
        or 0x2200 <= code <= 0x22FF   # Mathematical Operators
        or 0x27C0 <= code <= 0x27EF   # Miscellaneous Mathematical Symbols-A
        or 0x2980 <= code <= 0x29FF   # Miscellaneous Mathematical Symbols-B
        or 0x2100 <= code <= 0x214F   # Letterlike Symbols (ℕ ℤ ℚ ℝ ℂ ℓ)
    )


# ── 打分结果 ────────────────────────────────────────────────────────────


@dataclass
class FormulaScore:
    """五特征打分结果 + 加权总分 + 判定。"""

    font: float = 0.0
    density: float = 0.0
    unicode: float = 0.0
    baseline: float = 0.0
    layout: float = 0.0
    total: float = 0.0
    verdict: str = "text"          # formula / ambiguous / text

    def to_dict(self) -> dict:
        return {
            "font": round(self.font, 3),
            "density": round(self.density, 3),
            "unicode": round(self.unicode, 3),
            "baseline": round(self.baseline, 3),
            "layout": round(self.layout, 3),
            "total": round(self.total, 3),
            "verdict": self.verdict,
        }


class FormulaConfidenceEngine:
    """规范书 §5.3 公式置信度打分引擎（不依赖单一字体硬编码）。"""

    WEIGHTS = (0.30, 0.25, 0.15, 0.15, 0.15)
    THRESHOLD_HIGH = 0.75
    THRESHOLD_LOW = 0.45

    def __init__(self, weights: Optional[Sequence[float]] = None,
                 threshold_high: float = 0.75,
                 threshold_low: float = 0.45) -> None:
        w = tuple(weights) if weights else self.WEIGHTS
        if len(w) != 5 or abs(sum(w) - 1.0) > 1e-6:
            raise ValueError(f"weights must sum to 1.0 over 5 features: {w}")
        self.weights = w
        self.threshold_high = threshold_high
        self.threshold_low = threshold_low

    # ── 特征打分 ──────────────────────────────────────────────────

    def font_score(self, font_name: str) -> float:
        """C_font：字体族名称包含 Math/Sym/CM/STIX/AMS 等关键词。

        命中明确数学字体 → 0.85 + 0.1×命中数（封顶 1.0）；未命中 → 0.1。
        判定特征 §5.3：C_font 对明确数学字体（CMMI10、STIXTwoMath、
        AMS 等）应接近 1，因为数学字体是公式的强信号。
        """
        name = (font_name or "").lower()
        if not name:
            return 0.1
        hit = sum(1 for kw in MATH_FONT_KEYWORDS if kw in name)
        greek_hit = sum(1 for kw in GREEK_FONT_KEYWORDS if kw in name)
        if hit:
            return min(1.0, 0.85 + 0.10 * hit)
        if greek_hit:
            return 0.85
        return 0.1

    def density_score(self, text: str) -> float:
        """C_density：特殊数学符号密度（∫ ∑ √ ≤ → 等）。"""
        if not text:
            return 0.0
        n_sym = sum(1 for c in text if c in MATH_SYMBOL_CHARS)
        n_ascii = sum(1 for c in text if c in ASCII_MATH_CHARS)
        total = len([c for c in text if not c.isspace()])
        if total == 0:
            return 0.0
        ratio = (n_sym + 0.35 * n_ascii) / total
        # 单调映射：ratio=0.1 → 0.25；0.25 → 0.6；0.4+ → 1.0
        if ratio >= 0.40:
            return 1.0
        if ratio >= 0.25:
            return 0.6 + (ratio - 0.25) / 0.15 * 0.4
        return ratio / 0.25 * 0.6

    def unicode_score(self, text: str) -> float:
        """C_unicode：Math Alphanumeric / Greek / Operators 区域占比。"""
        if not text:
            return 0.0
        total = len([c for c in text if not c.isspace()])
        if total == 0:
            return 0.0
        hit = sum(1 for c in text if is_math_unicode(c))
        return min(1.0, hit / total * 2.0)

    def baseline_score(self, glyphs: Sequence[Glyph]) -> float:
        """C_baseline：行内多层上下标导致的基线微小波动（标准差/字号）。"""
        if len(glyphs) < 2:
            return 0.0
        baselines = [g.baseline for g in glyphs]
        sizes = [g.font_size for g in glyphs]
        med_size = max(median(sizes), 0.01)
        std = pstdev(baselines)
        spread = std / med_size
        if spread >= 0.30:      # 强上下标结构
            return 1.0
        if spread >= 0.12:
            return 0.5 + (spread - 0.12) / 0.18 * 0.5
        if spread >= 0.04:
            return spread / 0.12 * 0.5
        return 0.0

    def layout_score(self, layout_class: Optional[object] = None) -> float:
        """C_layout：DocLayout 区域检测预测值。

        ``layout_class`` 为 None 时返回中性 0.5（不参与加分也不减分）；
        传入 doclayout 预测类别名（str）或类别索引（int）时按区域映射。
        """
        if layout_class is None:
            return 0.5
        if isinstance(layout_class, (int, float)):
            idx = int(layout_class)
            if idx == 0:          # abandon / 公式保留区
                return 1.0
            if idx in (5, 7):     # plain text / title
                return 0.15
            return 0.5
        name = str(layout_class).lower()
        if "formula" in name or "isolate" in name or "abandon" in name:
            return 1.0
        if "plain" in name or "title" in name or "text" in name:
            return 0.15
        return 0.5

    # ── 综合打分 ──────────────────────────────────────────────────

    def score(self, text: str, glyphs: Sequence[Glyph],
              font_name: Optional[str] = None,
              layout_class: Optional[object] = None) -> FormulaScore:
        """计算 S_formula 与判定（formula / ambiguous / text）。"""
        # 模块 4：孤立基础运算符硬性降级回退（规范 §3.4）—— 单字符
        # 基础运算符（无变量/文字上下文）强制按普通文本处理，得分压至
        # 0.10（远低于 0.45 阈值）。注意：多符号组合（如 ``a = b``）不受
        # 影响 —— ``is_single_operator`` 要求去空白后仅 1 个字符。
        if is_single_operator(text):
            return FormulaScore(
                font=0.0, density=0.0, unicode=0.0,
                baseline=0.0, layout=0.0,
                total=0.10, verdict="text",
            )
        c_font = self.font_score(font_name or "")
        c_density = self.density_score(text)
        c_unicode = self.unicode_score(text)
        c_baseline = self.baseline_score(glyphs)
        c_layout = self.layout_score(layout_class)
        total = (
            self.weights[0] * c_font
            + self.weights[1] * c_density
            + self.weights[2] * c_unicode
            + self.weights[3] * c_baseline
            + self.weights[4] * c_layout
        )
        if total >= self.threshold_high:
            verdict = "formula"
        elif total >= self.threshold_low:
            verdict = "ambiguous"
        else:
            verdict = "text"
        return FormulaScore(
            font=c_font, density=c_density, unicode=c_unicode,
            baseline=c_baseline, layout=c_layout,
            total=total, verdict=verdict,
        )


__all__ = [
    "FormulaScore", "FormulaConfidenceEngine",
    "MATH_FONT_KEYWORDS", "MATH_SYMBOL_CHARS", "is_math_unicode",
]
