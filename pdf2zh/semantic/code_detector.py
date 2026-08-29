"""Code-region detection for the legacy translate pipeline (plan Phase 1).

Scoring-based rather than one hard rule, honoring the plan's design: each
signal contributes evidence and a *conservative* threshold decides. The most
important invariant is that ordinary body text is **never** mistaken for code
(which would silently stop translating a paragraph), so monospace-font
evidence is treated as a near-requirement: without an identifiable
monospace / code font in the paragraph, we won't classify it as code and
risk losing translation. This deliberately under-covers badly-typeset code
rather than ever skipping real paragraphs.
"""

from __future__ import annotations

import re
from enum import Enum

from pdf2zh.semantic.models import CodeBlockNode

#: 代码字体名模式（等宽 / 代码字体；与计划中 CODE_FONT_PATTERNS 一致）。
_CODE_FONT_RE = re.compile(
    r"(mono|code|courier|consolas|menlo|firacode|fira code|sourcecode|"
    r"source code|inconsolata|dejavu sans mono|liberation mono|"
    r"ubuntu mono|droid sans mono|jetbrains mono|andale mono|"
    r"lucida console|deck|terminal|dotumche|officecodepro|"
    r"hack|cousine|roboto mono|sf mono|monospace|ocr-a)",
    re.IGNORECASE,
)

#: 常见编程关键字（含伪代码 / 脚本词）。
_PROG_KEYWORDS = frozenset(
    {
        "def", "class", "return", "import", "from", "if", "elif", "else",
        "for", "while", "in", "not", "and", "or", "try", "except",
        "finally", "with", "as", "lambda", "yield", "pass", "break",
        "continue", "raise", "assert", "global", "nonlocal", "del",
        "None", "True", "False", "null", "void", "int", "string",
        "boolean", "float", "double", "char", "function", "procedure",
        "begin", "end", "then", "do", "case", "switch", "default",
        "public", "private", "protected", "static", "const", "var",
        "let", "new", "this", "true", "false", "printf", "scanf",
        "cout", "namespace", "struct", "enum", "using", "#include",
        "python", "bash", "git", "npm", "node", "export", "print",
    }
)

#: 代码符号字符集（用于"符号密度"信号）。
_SYMBOL_CHARS = frozenset("()[]{};=<>+-*/%&|!~?.,:'\"\\@#$^_`")

#: 缩进行 / 行号行前缀。
_INDENT_RE = re.compile(r"^\s{2,}")
_LINE_NO_RE = re.compile(r"^\s*\d{1,3}[.:]\s")

#: 判定阈值与"等宽字体近乎必需"策略。
CODE_THRESHOLD = 6.0
REQUIRE_MONO = True


class CodeProfile(Enum):
    """Detection profile: how aggressively to treat paragraphs as code.

    ``STRICT`` (default): monospace/code font is near-required → **宁可漏
    识别代码，也不要误伤正文**。``TECHNICAL`` (balanced): 不要求等宽字体，
    但需要缩进 + 符号密度 + 关键字密度 + 行结构达到更高阈值（适合
    技术论文里用 Times/Roman 等比例字体排版的伪代码）。
    """

    STRICT = "strict"
    TECHNICAL = "technical"

    @property
    def threshold(self) -> float:
        return CODE_THRESHOLD if self is CodeProfile.STRICT else 8.0

    @property
    def require_mono(self) -> bool:
        return self is CodeProfile.STRICT

#: 单项证据权重（与计划一致）。
_W_MONO = 5.0
_W_INDENT = 2.0
_W_SYMBOL = 2.0
_W_KEYWORD = 2.0
_W_STRUCT = 2.0


def is_monospace_font(font_name: str | None) -> bool:
    """True when a font name looks monospace / code (Courier, Consolas, …)."""
    if not font_name:
        return False
    return bool(_CODE_FONT_RE.search(font_name))


def _symbol_density(text: str) -> float:
    """Ratio of code-symbol characters among non-whitespace characters."""
    non_ws = [ch for ch in text if not ch.isspace()]
    if not non_ws:
        return 0.0
    return sum(1 for ch in non_ws if ch in _SYMBOL_CHARS) / len(non_ws)


def _keyword_density(text: str) -> float:
    """Ratio of programming keywords among alpha tokens."""
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text)
    if not tokens:
        return 0.0
    return sum(1 for t in tokens if t in _PROG_KEYWORDS) / len(tokens)


def _indent_fraction(text: str) -> float:
    """Fraction of non-empty lines that are indented or line-number-prefixed."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return 0.0
    hit = 0
    for ln in lines:
        if _INDENT_RE.match(ln) or _LINE_NO_RE.match(ln):
            hit += 1
    return hit / len(lines)


def _structure_score(text: str) -> float:
    """Repeated-line-structure signal: how often lines share a trailing char.

    Code lines frequently end in the same separator (``;``, ``{``, ``}``, ``)``);
    prose almost never does. Requires ≥ 3 non-empty lines.
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) < 3:
        return 0.0
    tails = [ln[-1] for ln in lines]
    common = max(tails.count(c) for c in set(tails))
    return common / len(tails)


def score_code(text: str, font_names) -> tuple[float, list[str]]:
    """Compute a code-confidence score and a list of hit reasons.

    Returns ``(score, reasons)``. ``font_names`` is an iterable of the
    paragraph's font names (may be ``None`` / empty).
    """
    reasons: list[str] = []
    score = 0.0
    names = font_names or []
    has_mono = any(is_monospace_font(n) for n in names)
    if has_mono:
        score += _W_MONO
        reasons.append("monospace_font")
    if _indent_fraction(text) >= 0.5:
        score += _W_INDENT
        reasons.append("indentation")
    if _symbol_density(text) >= 0.25:
        score += _W_SYMBOL
        reasons.append("high_symbol_density")
    if _keyword_density(text) >= 0.15:
        score += _W_KEYWORD
        reasons.append("programming_keyword_density")
    if _structure_score(text) >= 0.5:
        score += _W_STRUCT
        reasons.append("repeated_line_structure")
    return score, reasons


def detect_code(
    text: str | None,
    font_names=None,
    threshold: float | None = None,
    require_mono: bool | None = None,
    profile: CodeProfile = CodeProfile.STRICT,
) -> tuple[bool, float, list[str]]:
    """Classify a paragraph as code.

    Returns ``(is_code, score, reasons)``. When ``require_mono`` is True
    (default, safety-first) a paragraph without a monospace/code font is
    never classified as code, regardless of other signals.

    ``threshold`` / ``require_mono`` explicitly override the ``profile``
    settings (backwards-compatible with callers that pass them).
    """
    text = text or ""
    if not text.strip():
        return False, 0.0, []
    names = list(font_names or [])
    th = threshold if threshold is not None else profile.threshold
    rm = require_mono if require_mono is not None else profile.require_mono
    score, reasons = score_code(text, names)
    if rm and not any(is_monospace_font(n) for n in names):
        return False, score, reasons
    return score >= th, score, reasons


def detect_code_block(
    text: str | None,
    font_names=None,
    profile: CodeProfile = CodeProfile.STRICT,
) -> CodeBlockNode | None:
    """Classify a paragraph as code and return a :class:`CodeBlockNode`.

    ``None`` when the paragraph is not code. This is the node-level entry
    point that the future semantic pipeline consumes (the legacy converter
    currently derives its ``keep`` mask from :func:`detect_code`).
    """
    is_code, score, reasons = detect_code(text, font_names, profile=profile)
    if not is_code:
        return None
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    return CodeBlockNode(lines=lines)


__all__ = [
    "CODE_THRESHOLD",
    "REQUIRE_MONO",
    "CodeProfile",
    "is_monospace_font",
    "score_code",
    "detect_code",
    "detect_code_block",
]