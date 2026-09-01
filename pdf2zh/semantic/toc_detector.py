"""Visual TOC detection — plan Commit 6A, detector stage.

The goal is **not** "is this text the word Contents?" but "is this a region
of the page made of TOC entries?". Detection therefore fuses several signals
rather than trusting a single header line:

- **header text**: ``Contents`` / ``Table of Contents`` / ``CONTENTS`` and
  common variants (near page top, or anywhere on a standalone TOC page);
- **entry pattern**: lines that carry title → dot-leader → right page-number,
  or title + right-column page number (empty-column leaders);
- **repeated structure**: a page with **≥ 2** entry anchors is a TOC page even
  with no header (multipage refinements / contents continuing);
- **page-number column geometry**: right-aligned page numbers sit beyond a
  right-column threshold; body paragraphs ending in a number do not.

This module only *anchors and classifies* lines; it builds no AST. The parser
(:mod:`pdf2zh.semantic.toc_parser`) consumes the anchors and produces
:class:`TOCEntryNode` objects. Pure logic — no I/O, no PyMuPDF, no converter.
"""

from __future__ import annotations

import re

from pdf2zh.toc import TOC_LEADER_CHARS

__all__ = [
    "EntryMatch",
    "match_entry",
    "detect_header",
    "detect_anchors",
    "page_is_toc",
    "is_title_prefix",
    "RIGHT_COLUMN_FRACTION",
]

#: Right-column threshold for empty-column page numbers (fraction of page width).
RIGHT_COLUMN_FRACTION = 0.72

#: Numbering / structure prefixes marking a line as a *title fragment* — used to
#: attach dangling prefixed lines to a following anchor's entry (multi-line).
_TITLE_PREFIX_RE = re.compile(
    r"^\s*(?:"
    r"\d+(?:\.\d+)*[\s.、:：]|"
    r"[ivxlcdmIVXLCDM]{1,4}[\s.。)）、:]|"
    r"(?:chapter|ch\.|section|sec\.|subsection|subsec\.|part|appendix|appx\.|annex)\b|"
    r"第\s*[\d零一二三四五六七八九十百千万]+"
    r")",
    re.IGNORECASE,
)

#: Dot-leader table-of-contents entry: title + 2+ leader chars + page.
_LEADER_RE = re.compile(
    rf"^(?P<title>.*?)\s*(?P<lead>[{TOC_LEADER_CHARS}\s]*"
    rf"[{TOC_LEADER_CHARS}]{{2,}}[{TOC_LEADER_CHARS}\s]*?)"
    rf"(?P<page>\d{{1,4}}(?:[-–—]\d{{1,4}})?|[ivxlcdmIVXLCDM]{{1,4}})\s*$"
)

#: Empty-column (space-separated) entry heuristic: title + trailing right-column page.
_SPACE_PAGE_RE = re.compile(r"^(?P<title>\S.*?[^\d\s])\s{2,}(?P<page>\d{1,4})\s*$")

#: Contents / Table of Contents header variants.
_HEADER_RE = re.compile(
    r"^\s*(?:table\s+of\s+contents|contents|content)\s*[,:]?\s*$",
    re.IGNORECASE,
)


class EntryMatch:
    """Result of matching one line as a TOC entry anchor.

    ``title`` is the entry title (leading number kept); ``leader`` is the
    dot-leader substring (verbatim, empty when space-separated); ``page`` is the
    printed page label; ``leader_present`` distinguishes dot-leader entries from
    empty-column ones; ``page_x`` is the estimated page-number column x.
    """

    __slots__ = ("title", "leader", "page", "leader_present", "page_x")

    def __init__(self, title, leader, page, leader_present, page_x):
        self.title = title
        self.leader = leader
        self.page = page
        self.leader_present = leader_present
        self.page_x = page_x


def _is_right_column(page_x: float, page_width: float) -> bool:
    return page_width > 0 and page_x >= RIGHT_COLUMN_FRACTION * page_width


def _estimate_page_x(line: dict, page: str) -> float:
    """Estimate the x where the page-number digits start (for columns)."""
    try:
        x1 = float(line.get("x1") or 0.0)
        size = float(line.get("size") or 12.0)
    except (TypeError, ValueError):
        return float(line.get("x1") or 0.0)
    return x1 - len(page) * 0.5 * size


def match_entry(line: dict, page_width: float) -> EntryMatch | None:
    """Match a single line as a TOC entry anchor; None when it is not one.

    Tries in order: (1) dot-leader + page; (2) title + right-column page number
    (empty-column leader). Geometry (``x1``/``size``) is optional but improves
    the empty-column gate; ``page_width`` enables the right-column check.
    """
    text = (line.get("text") or "").rstrip()
    if not text:
        return None

    m = _LEADER_RE.match(text)
    if m is not None and len(m.group("title").strip()) >= 2:
        title = m.group("title").rstrip()
        page = m.group("page").strip()
        return EntryMatch(
            title=title,
            leader=m.group("lead").strip(),
            page=page,
            leader_present=True,
            page_x=_estimate_page_x(line, page),
        )

    sm = _SPACE_PAGE_RE.match(text)
    if sm is not None:
        page = sm.group("page")
        page_x = _estimate_page_x(line, page)
        if _is_right_column(page_x, page_width):
            return EntryMatch(
                title=sm.group("title").strip(),
                leader="",
                page=page,
                leader_present=False,
                page_x=page_x,
            )
    return None


def detect_header(lines: list[dict]) -> str | None:
    """Find a Contents/Table of Contents header line (anywhere on the page)."""
    for line in lines:
        text = (line.get("text") or "").strip()
        if text and _HEADER_RE.match(text):
            return text
    return None


def detect_anchors(lines: list[dict], page_width: float) -> list[int]:
    """Indices of lines that parse as TOC entry anchors."""
    return [i for i, ln in enumerate(lines) if match_entry(ln, page_width) is not None]


def is_title_prefix(text: str) -> bool:
    """True when ``text`` starts like a TOC title fragment (numbering/structure)."""
    return bool(text and _TITLE_PREFIX_RE.match(text))


def page_is_toc(anchors: list[int], has_header: bool) -> bool:
    """Promote a page to ``is_toc_page``.

    A header alone, or a single anchor, is not enough (a lone ``1
    Introduction`` heading must not be a TOC). We require either a real entry
    structure (≥ 2 anchors) or a header **plus** at least one entry anchor.
    """
    if has_header and anchors:
        return True
    return len(anchors) >= 2
