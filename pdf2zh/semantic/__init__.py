"""Semantic PDF analysis subpackage (plan: native-info-first + layout assist).

Phase 1 + Phase 2 live here and are wired into the legacy converter:

- :mod:`pdf2zh.semantic.code_detector`  — code region protection (Phase 1).
- :mod:`pdf2zh.semantic.style_detector` — bold / italic detection + marker
  style restoration (Phase 2).
- :mod:`pdf2zh.semantic.models`          — TextSpan / TextBlock / RegionType.

Environments toggles (default all **on**; set ``0`` to disable):
``PDF2ZH_SEMANTIC_CODE`` / ``PDF2ZH_SEMANTIC_STYLE``.
"""

from __future__ import annotations

import os

__all__ = [
    "code_protect_enabled",
    "style_protect_enabled",
    "RegionType",
    "ProtectionPolicy",
    "REGION_POLICY",
    "SpanStyle",
    "TextSpan",
    "TextBlock",
    "SemanticNode",
    "ParagraphNode",
    "HeadingNode",
    "CodeBlockNode",
    "ListItemNode",
    "ListNode",
    "TOCEntryNode",
    "TOCNode",
    "parse_toc",
    "detect_span_style",
    "inject_style_markers",
    "extract_style_markers",
    "StyledParagraph",
    "translate_styled_paragraph",
    "collapse_styled_spans",
    "detect_code",
    "detect_code_block",
    "CodeProfile",
    "is_monospace_font",
    "ListCandidate",
    "match_marker",
    "detect_list_candidates",
    "list_debug_dict",
    "parse_list_tree",
]


def _env_flag(name: str, default: bool = True) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() not in {"0", "false", "off", "no"}


def code_protect_enabled() -> bool:
    """Phase 1 toggle: code regions never enter the translator."""
    return _env_flag("PDF2ZH_SEMANTIC_CODE")


def style_protect_enabled() -> bool:
    """Phase 2 toggle: bold/italic restoration via style markers."""
    return _env_flag("PDF2ZH_SEMANTIC_STYLE")


from pdf2zh.semantic.code_detector import (  # noqa: E402
    CodeProfile,
    detect_code,
    detect_code_block,
    is_monospace_font,
)
from pdf2zh.semantic.list_detector import (  # noqa: E402
    ListCandidate,
    detect_list_candidates,
    list_debug_dict,
    match_marker,
)
from pdf2zh.semantic.list_parser import parse_list_tree  # noqa: E402
from pdf2zh.semantic.toc_parser import parse_toc  # noqa: E402
from pdf2zh.semantic.models import (  # noqa: E402
    CodeBlockNode,
    HeadingNode,
    ListItemNode,
    ListNode,
    ParagraphNode,
    ProtectionPolicy,
    REGION_POLICY,
    RegionType,
    SemanticNode,
    SpanStyle,
    TOCEntryNode,
    TOCNode,
    TextBlock,
    TextSpan,
)
from pdf2zh.semantic.style_detector import (  # noqa: E402
    detect_span_style,
    extract_style_markers,
    inject_style_markers,
)
from pdf2zh.semantic.style_translate import (  # noqa: E402
    StyledParagraph,
    collapse_styled_spans,
    translate_styled_paragraph,
)
