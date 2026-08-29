"""Bold / italic detection + marker-based style protection (plan Phase 2).

Two layers:

1. :func:`detect_span_style` — decides whether an original span is bold
   and / or italic, using **both** the font name substrings **and** the PDF
   ``flags`` bitfield (forced-bold bit). Relying on the name alone misses
   synthetic weights and breaks across CJK font naming schemes.

2. :func:`inject_style_markers` / :func:`extract_style_markers` — the
   "style placeholder" trick from the plan. Style runs of the *source*
   paragraph are wrapped in ``<bK>.../bK>`` / ``<iK>.../iK>`` markers that
   ride through the translator untouched; after translation the markers are
   parsed back into a per-character style table aligned to the clean,
   marker-free translated string (the renderer reads it to emit bold/italic
   glyphs). If a translator mangles or drops a marker we degrade gracefully
   to unstyled text — never an exception and never a translation error.

   Marker syntax deliberately matches the existing ``<bN>`` rich-text
   placeholder convention already used by :mod:`pdf2zh.translator`.
"""

from __future__ import annotations

import re

from pdf2zh.semantic.models import SpanStyle

# Weight tokens matched against **alphanumeric segments** of the font name.
# Segmenting first (split on non-alphanumerics) means ``TimesNewRomanPS-BoldMT``
# yields tokens ``timesnewromanps`` / ``boldmt`` → bold; ``ArialMT`` yields
# ``arialmt`` → plain. Bare single letters are never matched, so names that just
# contain a "b" (Arial, Cambria) stay unstyled. Tokens are deliberately
# conservative to avoid bolding plain body text.
_BOLD_TOKENS = ("bold", "black", "heavy", "demi", "semibold")
_ITALIC_TOKENS = ("italic", "oblique", "slanted")

_TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)

#: PDF font descriptor flags bit 18 = ForcedBold.
FORCED_BOLD_FLAG = 1 << 18

#: Marker tag regexes (match the <b0>/<i3> convention; opening vs closing is
#: decided by a '/' right after '<').
_ANY_TAG_RE = re.compile(r"</?([bi])\d+>")


def detect_span_style(font_name: str | None, flags: int = 0) -> SpanStyle:
    """Decide bold / italic from the font name **and** the PDF flags.

    Args:
        font_name: original span font name (after /ABCDEF+ prefix removal).
        flags: PDF font descriptor flags; bit ``FORCED_BOLD_FLAG`` forces bold.

    Returns:
        :class:`SpanStyle` — ``bold`` / ``italic`` never ``None``.
    """
    name = (font_name or "").lower()
    tokens = _TOKEN_RE.findall(name)
    bold = bool(flags & FORCED_BOLD_FLAG)
    italic = False
    for tok in tokens:
        for b in _BOLD_TOKENS:
            if b in tok:
                bold = True
        for i in _ITALIC_TOKENS:
            if i in tok:
                italic = True
    return SpanStyle(bold=bold, italic=italic)


def inject_style_markers(text: str, styles: list[SpanStyle] | None) -> str:
    """Wrap style runs of ``text`` in ``<bK>`` / ``<iK>`` markers.

    ``styles`` must be the per-character style table aligned with ``text``
    (one entry per character). Runs are emitted with stable consecutive ids;
    a bold+italic run is nested ``<bB><iI>…</iI></bB>`` so a well-behaved
    translator preserves both. When styles are missing / misaligned the
    input is returned unchanged (no markers → no style restoration).
    """
    if not styles or len(styles) != len(text) or not text:
        return text

    # Collapse into style runs (index ranges with identical SpanStyle).
    runs: list[tuple[int, int, SpanStyle]] = []
    start = 0
    cur = styles[0]
    for i in range(1, len(text) + 1):
        nxt = styles[i] if i < len(text) else None
        if i == len(text) or nxt != cur:
            runs.append((start, i, cur))
            start = i
            cur = nxt if nxt is not None else cur

    b_id = 0
    i_id = 0
    parts: list[str] = []
    for s, e, st in runs:
        b_tag = f"<b{b_id}>" if st.bold else None
        i_tag = f"<i{i_id}>" if st.italic else None
        if b_tag:
            parts.append(b_tag)
        if i_tag:
            parts.append(i_tag)
        parts.append(text[s:e])
        if i_tag:
            parts.append(f"</i{i_id}>")
        if b_tag:
            parts.append(f"</b{b_id}>")
        if st.bold:
            b_id += 1
        if st.italic:
            i_id += 1
    return "".join(parts)


def extract_style_markers(marked: str) -> tuple[str, list[SpanStyle]]:
    """Parse ``<bK>``/``<iK>`` markers out of translated text.

    Returns:
        ``(clean_text, styles)`` — ``clean_text`` is the translation with all
        markers removed; ``styles`` is one :class:`SpanStyle` per character of
        ``clean_text`` (aligned index-for-index for the renderer). Unknown or
        mangled tags that don't match the exact syntax are kept verbatim.
    """
    if not marked:
        return marked, []

    styles: list[SpanStyle] = []
    bold = False
    italic = False
    clean_parts: list[str] = []
    pos = 0

    for m in _ANY_TAG_RE.finditer(marked):
        if m.start() > pos:
            seg = marked[pos : m.start()]
            clean_parts.append(seg)
            styles.extend([SpanStyle(bold=bold, italic=italic)] * len(seg))
        kind = m.group(1)
        is_open = marked[m.start() + 1] != "/"
        if is_open:
            if kind == "b":
                bold = True
            else:
                italic = True
        else:
            if kind == "b":
                bold = False
            else:
                italic = False
        pos = m.end()

    if pos < len(marked):
        seg = marked[pos:]
        clean_parts.append(seg)
        styles.extend([SpanStyle(bold=bold, italic=italic)] * len(seg))

    return "".join(clean_parts), styles


__all__ = [
    "SpanStyle",
    "detect_span_style",
    "inject_style_markers",
    "extract_style_markers",
    "FORCED_BOLD_FLAG",
]