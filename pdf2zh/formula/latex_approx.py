"""P6.2 — LaTeX 轻量近似（规范书 §4.3 ``FormulaObject.raw_latex_approx``）。

遗留项 1 完成：``raw_latex_approx`` 不再恒为 ``None``。本模块用符号
映射表把数学 Unicode / 希腊字母 / 上标下标字符近似为 LaTeX 记号，供
QA / 日志 / 语义摘要消费；**不引入完整 LaTeX 解析器**（几何排版不受
影响 —— 公式对象几何绝对不可变，LaTeX 仅为文本近似）。

    输入: "f(x) = ∫ x² dx + 1 ≤ ∞"
    输出: "f(x) = \\int x^{2} dx + 1 \\leq \\infty"
"""

from __future__ import annotations

from typing import Dict

# ── 符号映射表 ──────────────────────────────────────────────────────

_GREEK_LOWER = {
    "α": "\\alpha",
    "β": "\\beta",
    "γ": "\\gamma",
    "δ": "\\delta",
    "ε": "\\epsilon",
    "ζ": "\\zeta",
    "η": "\\eta",
    "θ": "\\theta",
    "ι": "\\iota",
    "κ": "\\kappa",
    "λ": "\\lambda",
    "μ": "\\mu",
    "ν": "\\nu",
    "ξ": "\\xi",
    "ο": "\\omicron",
    "π": "\\pi",
    "ρ": "\\rho",
    "σ": "\\sigma",
    "τ": "\\tau",
    "υ": "\\upsilon",
    "φ": "\\phi",
    "χ": "\\chi",
    "ψ": "\\psi",
    "ω": "\\omega",
    "ϵ": "\\varepsilon",
    "ϑ": "\\vartheta",
    "ϖ": "\\varpi",
    "ϱ": "\\varrho",
    "ς": "\\varsigma",
    "ϕ": "\\varphi",
    "ϰ": "\\varkappa",
}

_GREEK_UPPER = {
    "Α": "\\Alpha",
    "Β": "\\Beta",
    "Γ": "\\Gamma",
    "Δ": "\\Delta",
    "Ε": "\\Epsilon",
    "Ζ": "\\Zeta",
    "Η": "\\Eta",
    "Θ": "\\Theta",
    "Ι": "\\Iota",
    "Κ": "\\Kappa",
    "Λ": "\\Lambda",
    "Μ": "\\Mu",
    "Ν": "\\Nu",
    "Ξ": "\\Xi",
    "Ο": "\\Omicron",
    "Π": "\\Pi",
    "Ρ": "\\Rho",
    "Σ": "\\Sigma",
    "Τ": "\\Tau",
    "Υ": "\\Upsilon",
    "Φ": "\\Phi",
    "Χ": "\\Chi",
    "Ψ": "\\Psi",
    "Ω": "\\Omega",
}

_OPERATORS = {
    "±": "\\pm",
    "∓": "\\mp",
    "×": "\\times",
    "÷": "\\div",
    "⋅": "\\cdot",
    "∙": "\\bullet",
    "∘": "\\circ",
    "∗": "\\ast",
    "⊕": "\\oplus",
    "⊗": "\\otimes",
    "⊙": "\\odot",
    "∧": "\\wedge",
    "∨": "\\vee",
    "∩": "\\cap",
    "∪": "\\cup",
    "⊓": "\\sqcap",
    "⊔": "\\sqcup",
    "⋆": "\\star",
    "·": "\\cdot",
}

_RELATIONS = {
    "≤": "\\leq",
    "≥": "\\geq",
    "≠": "\\neq",
    "≈": "\\approx",
    "≃": "\\simeq",
    "∼": "\\sim",
    "≅": "\\cong",
    "≡": "\\equiv",
    "≪": "\\ll",
    "≫": "\\gg",
    "∝": "\\propto",
    "⊂": "\\subset",
    "⊃": "\\supset",
    "⊆": "\\subseteq",
    "⊇": "\\supseteq",
    "∈": "\\in",
    "∉": "\\notin",
    "∋": "\\ni",
    "∣": "\\mid",
    "∥": "\\parallel",
    "⊥": "\\perp",
    "≺": "\\prec",
    "≻": "\\succ",
}

_ARROWS = {
    "→": "\\rightarrow",
    "←": "\\leftarrow",
    "↔": "\\leftrightarrow",
    "⇒": "\\Rightarrow",
    "⇐": "\\Leftarrow",
    "⇔": "\\Leftrightarrow",
    "↦": "\\mapsto",
    "↑": "\\uparrow",
    "↓": "\\downarrow",
    "⟶": "\\longrightarrow",
    "⟵": "\\longleftarrow",
    "⟹": "\\implies",
    "⟺": "\\iff",
}

_BIG_OPS = {
    "∑": "\\sum",
    "∏": "\\prod",
    "∐": "\\coprod",
    "∫": "\\int",
    "∬": "\\iint",
    "∭": "\\iiint",
    "∮": "\\oint",
    "⋂": "\\bigcap",
    "⋃": "\\bigcup",
    "⨁": "\\bigoplus",
    "⨂": "\\bigotimes",
    "⋀": "\\bigwedge",
    "⋁": "\\bigvee",
}

_RADICALS = {"√": "\\sqrt", "∛": "\\sqrt[3]", "∜": "\\sqrt[4]"}

_LETTERLIKE = {
    "∞": "\\infty",
    "∂": "\\partial",
    "∇": "\\nabla",
    "ℏ": "\\hbar",
    "∅": "\\emptyset",
    "∀": "\\forall",
    "∃": "\\exists",
    "¬": "\\neg",
    "∠": "\\angle",
    "ℓ": "\\ell",
    "ℵ": "\\aleph",
    "ℕ": "\\mathbb{N}",
    "ℤ": "\\mathbb{Z}",
    "ℚ": "\\mathbb{Q}",
    "ℝ": "\\mathbb{R}",
    "ℂ": "\\mathbb{C}",
    "ℑ": "\\Im",
    "ℜ": "\\Re",
    "℘": "\\wp",
    "△": "\\triangle",
}

_SUPERSCRIPTS = {
    "⁰": "^{0}",
    "¹": "^{1}",
    "²": "^{2}",
    "³": "^{3}",
    "⁴": "^{4}",
    "⁵": "^{5}",
    "⁶": "^{6}",
    "⁷": "^{7}",
    "⁸": "^{8}",
    "⁹": "^{9}",
    "⁺": "^{+}",
    "⁻": "^{-}",
    "ⁿ": "^{n}",
    "ⁱ": "^{i}",
    "ᵃ": "^{a}",
    "ᵇ": "^{b}",
    "ᶜ": "^{c}",
    "ᵈ": "^{d}",
    "ᵉ": "^{e}",
}

_SUBSCRIPTS = {
    "₀": "_{0}",
    "₁": "_{1}",
    "₂": "_{2}",
    "₃": "_{3}",
    "₄": "_{4}",
    "₅": "_{5}",
    "₆": "_{6}",
    "₇": "_{7}",
    "₈": "_{8}",
    "₉": "_{9}",
    "₊": "_{+}",
    "₋": "_{-}",
    "ₐ": "_{a}",
    "ₑ": "_{e}",
    "ₓ": "_{x}",
    "ₙ": "_{n}",
    "ᵢ": "_{i}",
    "ⱼ": "_{j}",
}

_PUNCT = {
    "…": "\\dots",
    "⋯": "\\cdots",
    "⋮": "\\vdots",
    "⋱": "\\ddots",
    "′": "^{\\prime}",
    "″": "^{\\prime\\prime}",
    "°": "^{\\circ}",
}

# LaTeX 保留字符 → 转义（写原始命令文本；外层不重复转义）
_LATEX_SPECIAL = {
    "#": "\\#",
    "$": "\\$",
    "%": "\\%",
    "&": "\\&",
    "_": "\\_",
    "{": "\\{",
    "}": "\\}",
    "~": "\\textasciitilde{}",
    "^": "\\textasciicircum{}",
    "\\": "\\textbackslash{}",
}

# 保留原样的 ASCII 数学常用字符（不转义也不改写）
_KEEP_ASCII = set(
    "()[],;:+-*/=<>!|.abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'\"`"
)

_UNICODE_TO_LATEX: Dict[str, str] = {}
for _m in (
    _GREEK_LOWER,
    _GREEK_UPPER,
    _OPERATORS,
    _RELATIONS,
    _ARROWS,
    _BIG_OPS,
    _RADICALS,
    _LETTERLIKE,
    _SUPERSCRIPTS,
    _SUBSCRIPTS,
    _PUNCT,
    _LATEX_SPECIAL,
):
    _UNICODE_TO_LATEX.update(_m)


def escape_latex(text: str) -> str:
    """把 LaTeX 特殊字符（# $ % & _ { } ~ ^ \\）转义为文本命令。"""
    return "".join(_LATEX_SPECIAL.get(c, c) for c in text)


def to_latex_approx(text: str) -> str:
    """把数学文本近似为 LaTeX（未知字符原样保留，防信息丢失）。"""
    out: list = []
    for c in text:
        if c in _UNICODE_TO_LATEX:
            out.append(_UNICODE_TO_LATEX[c])
        elif c in _KEEP_ASCII:
            out.append(c)
        elif c.isspace():
            out.append(" ")
        else:
            out.append(c)  # 未知：原样保留
    return "".join(out)


__all__ = ["to_latex_approx", "escape_latex", "UNICODE_TO_LATEX"]
