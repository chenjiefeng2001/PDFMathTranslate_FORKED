"""Semantic protection side-channel — plan Phase 1 + 2.

Houses the heavy logic so ``converter.py`` stays under the strangulation line
gate: converter only carries the few character-level tracking hooks and
render-loop style ops; code-protection / style-marker orchestration lives here.

- :func:`char_span_style`     — per-character bold/italic (font name + flags).
- :func:`prepare_protection`  — phase 1: tag code paragraphs; phase 2: inject
  style markers into source text destined for the translator.
- :func:`restore_protection`  — phase 2: parse style markers out of the
  translation into a clean string + a per-character style table.
- :func:`gen_text_op`         — emit a PDF text op with pseudo-bold / italic.
"""

from __future__ import annotations

import logging

from pdf2zh.semantic import (
    CodeBlockNode,
    SpanStyle,
    code_protect_enabled,
    detect_code,
    detect_code_block,
    detect_span_style,
    extract_style_markers,
    inject_style_markers,
    style_protect_enabled,
)

log = logging.getLogger(__name__)

__all__ = [
    "char_span_style",
    "code_flags_for",
    "prepare_protection",
    "restore_protection",
    "gen_text_op",
]


def code_flags_for(sstk, pfkstk, pageid) -> list[bool]:
    """Phase 1: boolean mask of code paragraphs (never reach the translator).

    One entry aligned with ``sstk``; True → the paragraph is protected code
    and is returned verbatim, kept out of translation batches and the cache.

    Classification runs through :func:`detect_code_block` so the pipeline
    operates on :class:`CodeBlockNode` (the semantic node model); only the
    boolean mask is surfaced to the legacy converter.
    """
    flags: list[bool] = []
    for i, txt in enumerate(sstk):
        fonts = list(pfkstk[i]) if i < len(pfkstk) else None
        if not code_protect_enabled():
            flags.append(False)
            continue
        node: CodeBlockNode | None = detect_code_block(txt, fonts)
        if node is not None:
            log.debug(
                "code-protect page=%s block=%d lines=%d",
                pageid,
                i,
                len(node.lines),
            )
        flags.append(node is not None)
    return flags


def char_span_style(ch) -> SpanStyle:
    """Bold/italic of one LTChar (font name + PDF flags)."""
    fn = ""
    flags = 0
    try:
        fn = _extract_font_name(ch.fontname)
    except Exception:
        pass
    try:
        _fo = getattr(ch, "font", None)
        if _fo is not None:
            flags = int(getattr(_fo, "flags", 0) or 0)
    except Exception:
        pass
    return detect_span_style(fn, flags)


def _extract_font_name(font: str) -> str:
    if isinstance(font, bytes):
        try:
            font = font.decode("utf-8")
        except UnicodeDecodeError:
            return ""
    if "+" in font:
        font = font.split("+")[-1]
    return font


def prepare_protection(sstk, pfkstk, cstyles, toc_specs, pageid):
    """Tag code paragraphs and inject style markers for translation.

    Returns ``(send_text, code_flags, styled_ids)``:
    - ``send_text``: per-paragraph text to hand the translator (code and
      unstyled paragraphs unchanged; styled paragraphs carry ``<bK>/<iK>``
      markers).
    - ``code_flags``: aligned boolean list; True → the paragraph must never
      reach the translator (returned verbatim, kept out of batches/cache).
    - ``styled_ids``: indexes whose text had style markers injected (phase 2).
    """
    code_flags: list[bool] = []
    send_text = list(sstk)
    styled_ids: list[int] = []
    _code = code_protect_enabled()
    _style = style_protect_enabled()

    for i, txt in enumerate(sstk):
        fonts = list(pfkstk[i]) if i < len(pfkstk) else None
        is_code = False
        if _code:
            is_code, score, reasons = detect_code(txt, fonts)
            if is_code:
                log.debug(
                    "code-protect page=%s block=%d score=%.1f reasons=%s",
                    pageid,
                    i,
                    score,
                    reasons,
                )
        code_flags.append(is_code)
        if not (_style and not is_code and (toc_specs[i] is None)):
            continue
        arr = cstyles[i] if i < len(cstyles) else None
        if arr is not None and len(arr) == len(txt) and any(s.styled for s in arr):
            send_text[i] = inject_style_markers(txt, arr)
            styled_ids.append(i)
    return send_text, code_flags, styled_ids


def restore_protection(news, code_flags, toc_specs, styled_ids):
    """Strip style markers from translations; build a per-char style table.

    Returns ``(news2, para_styles)``: news without markers and per-paragraph
    style lists aligned to ``news2`` (None when a paragraph got no styles).
    """
    para_styles: list = [None] * len(news)
    news2 = list(news)
    for i, n in enumerate(news):
        if code_flags[i] or (toc_specs[i] is not None) or i not in styled_ids:
            continue
        clean, stlist = extract_style_markers(n) if n else (n, [])
        news2[i] = clean
        para_styles[i] = stlist or None
    return news2, para_styles


def gen_text_op(fmt, font, size, x, y, rtxt, bold=False, italic=False):
    """Emit a PDF text op; bold = pseudo-bold (Tr 2 + stroke), italic = shear."""
    return (
        f"/{font} {fmt(size)} Tf "
        + (f"q 2 Tr {fmt(max(size * 0.04, 0.3))} w " if bold else "")
        + f"1 0 {'0.2000' if italic else '0'} 1 {fmt(x)} {fmt(y)} Tm "
        + f"[<{rtxt}>] TJ "
        + (" Q " if bold else "")
    )
