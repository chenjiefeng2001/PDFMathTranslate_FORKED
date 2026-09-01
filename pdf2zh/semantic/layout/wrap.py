"""Pure-Python wrapping / shrink / clip mechanics — Commit 7C.

This module holds the *mechanics* of fitting text into a fixed width,
independent of any renderer and of any semantic meaning:

- :func:`wrap_lines` — greedy word wrap with CJK-aware character breaks.
- :func:`shrink_to_fit` — reduce ``font_size`` until the text fits (closed
  form, no loop).
- :func:`clip_text` — last-resort truncation, always reported via ``overflow``.

Guarantees (why these matter for code + long identifiers):

- **Termination.** A single unbreakable token wider than ``max_width`` is kept
  whole on its own line and is flagged by the caller as ``overflow=True`` — it
  is **never** split and never loops.  ``shrink_to_fit`` uses a closed-form
  scale so it cannot loop either.
- **Code is never wrapped here.** Code regions are :class:`PreservedRegion`
  primitives and are routed to ``OverflowPolicy.PRESERVE`` by
  :mod:`pdf2zh.semantic.layout.overflow` — they never reach ``wrap_lines``.
"""

from __future__ import annotations

import re
from typing import Callable

__all__ = ["wrap_lines", "shrink_to_fit", "clip_text", "tokenize"]

# “word” matches latin/digit runs but deliberately excludes CJK/full-width
# glyphs so an adjacent CJK char is tokenised on its own (breakable on both
# sides) instead of being swallowed by an over-eager ``\w+``/``\S+`` run.
_CJK_CLASS = r"\u2E80-\u9FFF\uF900-\uFAFF\uFE30-\uFE4F\uFF00-\uFFEF"
_TOKEN_RE = re.compile(
    r"(?P<space>\s+)"
    r"|(?P<cjk>[" + _CJK_CLASS + r"])"
    r"|(?P<word>[^\s" + _CJK_CLASS + r"]+)"
)


def tokenize(text: str) -> list[tuple[str, str]]:
    """Split ``text`` into ``(kind, chunk)`` tokens.

    ``kind`` is one of ``"space"`` / ``"cjk"`` (a single full-width glyph,
    breakable on either side) / ``"word"`` (a latin/digit run, breakable at
    whitespace only).  Used by :func:`wrap_lines`; exposed for tests.
    """
    out: list[tuple[str, str]] = []
    for m in _TOKEN_RE.finditer(text or ""):
        if m.group("space"):
            out.append(("space", m.group()))
        elif m.group("cjk"):
            out.append(("cjk", m.group()))
        else:
            out.append(("word", m.group()))
    return out


def _safe_width(measure: Callable[[str], float], s: str) -> float:
    try:
        w = float(measure(s))
    except Exception:  # noqa: BLE001 -- measurement failure is non-fatal
        return 0.0
    return w if w >= 0.0 else 0.0


def wrap_lines(
    text: str,
    measure: Callable[[str], float],
    max_width: float,
) -> list[tuple[str, float]]:
    """Greedy-wrap ``text`` into ``(line, width)`` pairs within ``max_width``.

    Args:
        text: the text to wrap (``\\n`` is treated as a hard line break).
        measure: width of a substring (already bound to a font size).
        max_width: available width in points; ``<= 0`` means "no wrap".

    Returns:
        List of ``(line_text, line_width)`` in reading order.  A single
        unbreakable token wider than ``max_width`` is kept whole on its own
        line (its width may exceed ``max_width`` — that is an overflow the
        caller must report, but the text is never silently split or dropped).
        Always terminates; never raises from the measurer.
    """
    max_width = float(max_width or 0.0)
    if max_width <= 0.0 or not text:
        return [("" if not text else text, _safe_width(measure, text))]
    lines: list[tuple[str, float]] = []
    for para in (text or "").split("\n"):
        _wrap_one(para, measure, max_width, lines)
    return lines


def _wrap_one(
    text: str,
    measure: Callable[[str], float],
    max_width: float,
    lines: list[tuple[str, float]],
) -> None:
    cur = ""
    pending_space = False
    for kind, val in tokenize(text):
        if kind == "space":
            if cur:
                pending_space = True
            continue
        sep = " " if (cur and pending_space) else ""
        cand = cur + sep + val
        cand_w = _safe_width(measure, cand)
        if cur and cand_w > max_width:
            # start a new line with this token (CJK glyphs may also break here)
            lines.append((cur, _safe_width(measure, cur)))
            cur = val
        else:
            cur = cand
        pending_space = False
    if cur or not lines:
        lines.append((cur, _safe_width(measure, cur)))


def _safe_width2(measure: Callable[[str, float], float], s: str, size: float) -> float:
    try:
        w = float(measure(s, size))
    except Exception:  # noqa: BLE001 -- measurement failure is non-fatal
        return 0.0
    return w if w >= 0.0 else 0.0


def shrink_to_fit(
    text: str,
    measure: Callable[[str, float], float],
    width: float,
    font_size: float,
    min_font_size: float = 5.0,
) -> tuple[float, bool]:
    """Return ``(effective_font_size, overflow)`` fit by shrinking the font.

    Closed-form (linear in font size): the required scale is computed directly
    and clamped to ``[min_font_size, font_size]`` — it never loops.  ``overflow``
    is True only when even ``min_font_size`` still cannot fit.
    """
    width = float(width or 0.0)
    fs = float(font_size)
    if width <= 0.0 or not text:
        return fs, False
    w = _safe_width2(measure, text, fs)
    if w <= 0.0:
        return fs, True
    if w <= width:
        return fs, False
    scale = width / w
    effective = max(float(min_font_size), fs * scale)
    w_eff = _safe_width2(measure, text, effective)
    return float(effective), bool(w_eff > width)


def clip_text(
    text: str,
    measure: Callable[[str], float],
    width: float,
    ellipsis: str = "\u2026",
) -> tuple[str, bool]:
    """Last-resort truncation. Returns ``(clipped_text, overflow=True)``.

    At most ``len(text)`` iterations — guaranteed to terminate.  ``overflow``
    is always True when a clip actually happens (a clip is never silent);
    callers must surface it.
    """
    width = float(width or 0.0)
    text = text or ""
    if width <= 0.0 or not text:
        return text, False
    if _safe_width(measure, text) <= width:
        return text, False
    if ellipsis and _safe_width(measure, ellipsis) <= width:
        for i in range(len(text), 0, -1):
            cand = text[:i] + ellipsis
            if _safe_width(measure, cand) <= width:
                return cand, True
        return ellipsis, True
    for i in range(len(text), 0, -1):
        if _safe_width(measure, text[:i]) <= width:
            return text[:i], True
    return "", True
