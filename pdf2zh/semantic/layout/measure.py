"""Unified text measurement API — Commit 7B.

The layout layer owns one font-accurate entry point for measuring a text
run's width, decoupled from any particular PDF renderer::

    measure_text(text, font=None, font_size=11.0) -> float

Design rules:

- **CJK-aware and never a raw English-character count.**  The default (no
  ``font``) path is a word-scale estimate: full-width / CJK glyphs are ~1em,
  Latin ~0.5em.  When a ``font`` is provided (a ``pymupdf.Font`` or a
  ``pymupdf`` built-in font name), the actual advance metrics are used.
- **Empty text measures to ``0.0``.**
- **Renderer-independent.**  importing ``pymupdf`` is deferred inside the
  function so module import stays cheap and pure-logic code paths (tests /
  plan builders that never touch a font) don't pay for it.

Compat note (Commit 7B): the TOC renderer (:mod:`pdf2zh.semantic.renderer.toc`)
consumes this API.  When no measurer is injected and no font is available, the
fallback here is byte-equivalent to the old ``_measure_default`` so existing
measurement behavior is unchanged.
"""

from __future__ import annotations

from typing import Any

__all__ = ["measure_text", "measure_text_estimate"]


def measure_text_estimate(text: str, font_size: float) -> float:
    """Word-scale width estimate (CJK-aware), never a raw char count.

    Full-width / CJK glyphs are ~1em, Latin ~0.5em, thin (space / dot) ~0.3em.
    This is the fallback used when no font is supplied; keeping it localised
    here makes the renderer's default measurement behavior centrally defined
    (and byte-compatible with the pre-7B TOC default).
    """
    w = 0.0
    for ch in text or "":
        if ord(ch) >= 0x2E80:  # CJK / full-width
            w += font_size
        elif ch in " .":
            w += font_size * 0.28
        else:
            w += font_size * 0.5
    return w


def measure_text(
    text: str,
    font: Any = None,
    font_size: float = 11.0,
) -> float:
    """Measure the horizontal advance width of ``text``.

    Args:
        text: the string to measure (empty str -> ``0.0``).
        font: an optional measurer provider — a ``pymupdf.Font`` instance
            (uses ``font.text_length``) or a ``pymupdf`` built-in font name
            string (uses ``pymupdf.get_text_length``).  ``None`` (default)
            uses the CJK-aware word-scale estimate.
        font_size: nominal font size in points.

    Returns:
        Horizontal width in points.  Never raises: on any measurement failure
        it falls back to :func:`measure_text_estimate`.
    """
    text = text or ""
    if not text:
        return 0.0
    if font is not None:
        try:
            if hasattr(font, "text_length"):
                val = font.text_length(text, font_size)
                if float(val) >= 0.0:
                    return float(val)
            elif isinstance(font, str):
                import pymupdf  # deferred: keep import cheap for pure-logic paths

                val = pymupdf.get_text_length(
                    text, fontname=font, fontsize=float(font_size)
                )
                if float(val) >= 0.0:
                    return float(val)
        except Exception:  # noqa: BLE001 -- measurement failure is non-fatal
            pass
    return measure_text_estimate(text, float(font_size))
