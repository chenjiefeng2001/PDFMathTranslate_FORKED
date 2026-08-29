"""Style-preserving span translation — plan Commit 5.

End-to-end ``source styled paragraph → translated styled spans``:

    source (with per-char SpanStyle from original font name + flags)
        → inject_style_markers     wrap bold/italic runs in <bK>/<iK>
        → translator                markers ride along; only text is rewritten
        → extract_style_markers     strip markers, recover per-char styles
        → collapse into TextSpan    adjacent styled runs → styled TextSpans

The goal is *boundary preservation*: the semantic span boundaries of the
source paragraph survive translation, so the renderer applies bold / italic
to exactly the right translated characters — it never re-guesses which
translated text should be bold. ``"".join(spans)`` always equals the clean
translated text.

Robustness (strict validation + graceful fallback, per the plan)
----------------------------------------------------------------
The LLM may mangle the markers (drop a closing tag, rewrite the syntax,
reorder runs). The pipeline must never fail the paragraph on that:

- **strict**: the translated output is validated (balanced open/close per
  kind). A clean parse yields a faithful per-char style table.
- **fallback**: on a malformed parse we collapse to plain unstyled text
  (all residual tags stripped) and mark ``StyledParagraph.recovered=True`` —
  never an exception, never a lost translation.
- identity / no-op transports still round-trip the original styles.

Callers that want the bold/italic to reach actual glyphs consume ``spans``
(each :class:`TextSpan` carries its :class:`SpanStyle`).

APIs
----
- :class:`StyledParagraph` — result: clean ``text`` + ``spans`` + ``recovered``.
- :func:`translate_styled_paragraph` — the single entry point.
- :func:`collapse_styled_spans` — per-char style table → styled runs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from pdf2zh.semantic.models import SpanStyle, TextSpan
from pdf2zh.semantic.style_detector import extract_style_markers, inject_style_markers

#: Any <bK> / <iK> tag (opening or closing); used to detect residual markup.
_TAG_RE = re.compile(r"</?[bi]\d+>")

__all__ = ["StyledParagraph", "translate_styled_paragraph", "collapse_styled_spans"]


@dataclass
class StyledParagraph:
    """A translated paragraph whose style boundaries survived translation.

    Attributes:
        text: clean translated text (all style markers stripped).
        spans: styled runs; ``"".join(s.text for s in spans) == text``.
        recovered: True when the translator mangled the markers and we fell
            back to a plain (unstyled) translation instead of failing.
    """

    text: str = ""
    spans: list[TextSpan] = field(default_factory=list)
    recovered: bool = False

    def styled_text(self, style: SpanStyle) -> str:
        """Concatenate the translated characters carrying ``style``.

        Convenience for tests / renderers that need just the bold (or italic)
        payload of a paragraph.
        """
        return "".join(sp.text for sp in self.spans if sp.style == style)


def collapse_styled_spans(text: str, styles: list[SpanStyle]) -> list[TextSpan]:
    """Collapse a per-char style table into adjacent styled runs.

    ``len(styles)`` must equal ``len(text)``. Runs of identical
    :class:`SpanStyle` are merged into single :class:`TextSpan` objects so a
    renderer can apply one style decision per run instead of per glyph.
    Empty input returns ``[]``.
    """
    if not text or len(styles) != len(text):
        return []
    spans: list[TextSpan] = []
    for ch, st in zip(text, styles):
        if spans and spans[-1].style == st:
            spans[-1].text += ch  # merge adjacent same-style run
        else:
            spans.append(TextSpan(text=ch, style=st))
    return spans


def _is_balanced(marked: str) -> bool:
    """True when every open <bK>/<iK> tag has a matching close per kind.

    This is the *strict* validity gate: a dropped closing tag leaves the
    count unbalanced and triggers fallback. Reordered / relabeled tags still
    balance and are accepted (graceful).
    """
    b_open = b_close = i_open = i_close = 0
    for m in _TAG_RE.finditer(marked):
        tag = m.group(0)
        is_open = not tag.startswith("</")
        # 打开 <b3> -> 第 2 个字符是 kind；关闭 </b3> -> 第 3 个字符是 kind。
        kind = tag[1] if is_open else tag[2]
        if kind == "b":
            if is_open:
                b_open += 1
            else:
                b_close += 1
        elif kind == "i":
            if is_open:
                i_open += 1
            else:
                i_close += 1
    return b_open == b_close and i_open == i_close


def _strip_residual_tags(text: str) -> str:
    """Remove any leftover <bK> / <iK> tags (fallback only)."""
    return _TAG_RE.sub("", text)


def translate_styled_paragraph(
    source: str,
    source_styles: list[SpanStyle] | None,
    translate: Callable[[str], str],
) -> StyledParagraph:
    """Translate one styled paragraph preserving its bold/italic boundaries.

    Args:
        source: source paragraph text.
        source_styles: per-char :class:`SpanStyle` of ``source`` (index-aligned,
            one entry per character); None / misaligned → plain translation.
        translate: the translator callable (receives the marker-annotated
            text so the markers travel with the strings they decorate).

    Returns:
        :class:`StyledParagraph`. Never raises; on malformed markers the
        paragraph falls back to a plain unstyled translation
        (``recovered=True``).
    """
    if (
        not source
        or not source_styles
        or len(source_styles) != len(source)
        or not any(s.styled for s in source_styles)
    ):
        plain = translate(source)
        return StyledParagraph(text=plain, spans=collapse_styled_spans(plain, [SpanStyle()] * len(plain)))

    marked = inject_style_markers(source, source_styles)
    out = translate(marked)
    clean, styles = extract_style_markers(out) if out else (out, [])

    if out and _is_balanced(out) and len(styles) == len(clean):
        # Strict path: markers parsed cleanly → faithful styled spans.
        return StyledParagraph(
            text=clean,
            spans=collapse_styled_spans(clean, styles),
            recovered=False,
        )

    # Fallback: markers mangled / dropped → strip residual tags, plain text.
    cleaned = _strip_residual_tags(clean if out else out)
    return StyledParagraph(
        text=cleaned,
        spans=collapse_styled_spans(cleaned, [SpanStyle()] * len(cleaned)),
        recovered=True,
    )