"""PDF native Outline / Bookmark reconstruction — plan Commit 6D.

Builds the *document-level* PDF outline (``/Outlines`` tree) from the
structured semantic TOC, keeping it fully independent of the visual TOC
renderer (``pdf2zh.semantic.renderer.toc``). Visual geometry and destinations
do not interfere: the visual renderer lays text on a page, this module writes
bookmark destinations.

Pipeline (Commit 6D):
::

    TOCNode ─ TOCEntryNode ─ document_model.toc_entries
        │
        ├─ translated_title      -> outline title
        ├─ level                 -> outline hierarchy (no numbering re-inference)
        ├─ heading_ref           -> preferred destination (more precise)
        └─ destination_page      -> fallback destination

Indexing contract (single boundary)
-----------------------------------
- ``page_number``: the **printed** label in the visual TOC column. It is
  never, ever used as a bookmark destination (``page_number !=
  destination_page`` is the rule, see Commit 6B).
- ``destination_page``: a **1-based** document page index (the final PDF page
  to jump to). PyMuPDF's ``set_toc`` is also 1-based, so both live in the
  same space at this one adapter boundary — no scattered ``+1/-1`` in
  renderers.
- heading destination: a heading block's page is resolved to a 1-based index
  here before being used (page ``p`` in the model is 0-based; +1 at this
  boundary only).

Preference order per entry: ``heading_ref`` destination (when resolvable) →
``destination_page``. Heading resolution failure never blocks the outline:
unmatchable entries still fall back to ``destination_page``.

Hierarchy is built strictly from ``level`` (semantic parser output) with a
stack algorithm — numbering prefixes are never used to re-infer depth.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

log = logging.getLogger(__name__)

__all__ = [
    "extract_outline_entries",
    "resolve_entry_destination",
    "build_outline_toc",
    "OUTLINE_SOURCE_SEMANTIC",
]

#: Outline source marker: outline was built from semantic TOC entries.
OUTLINE_SOURCE_SEMANTIC = "semantic"
#: Outline source marker: no semantic entries; outline untouched/absent.
OUTLINE_SOURCE_NONE = "none"


def _page_num_from_id(block_id: str) -> Optional[int]:
    """``p{page}_{index}`` → 1-based page index (or None on parse failure)."""
    try:
        _, rest = block_id.split("p", 1)
        page = int(rest.split("_", 1)[0])
        return page + 1  # single +1 boundary: model is 0-based, outline 1-based
    except Exception:  # noqa: BLE001
        return None


def _heading_pages(document_model: Mapping) -> Dict[str, int]:
    """Heading block id → 1-based page index map (for heading_ref resolution)."""
    out: Dict[str, int] = {}
    try:
        for page in (document_model or {}).get("pages", []) or []:
            pno = int(page.get("page", 0) or 0)
            for i, block in enumerate(page.get("blocks", []) or []):
                md = block.get("metadata") or {}
                kind = block.get("kind") or md.get("kind") or md.get("role") or ""
                if kind == "heading":
                    out[f"p{pno}_{i}"] = pno + 1
    except Exception:  # noqa: BLE001
        log.debug("outline: heading page map build failed", exc_info=True)
    return out


def extract_outline_entries(document_model: Mapping) -> List[dict]:
    """Collect structured TOC entries (translated titles + levels + dests).

    Walks the document model's ``toc`` blocks and pulls their
    ``metadata["toc_entries"]`` (the Commit 6B structured entries, already
    carrying ``translated_title`` / ``level`` / ``destination_page`` /
    ``heading_ref``). Returns entries in document reading order (page order,
    then block order, then entry order). Failure yields an empty list.
    """
    entries: List[dict] = []
    try:
        if not document_model:
            return entries
        heading_pages = _heading_pages(document_model)
        for page in (document_model or {}).get("pages", []) or []:
            for block in page.get("blocks", []) or []:
                md = block.get("metadata") or {}
                kind = block.get("kind") or md.get("kind") or ""
                toc_entries = md.get("toc_entries")
                if kind != "toc" or not toc_entries:
                    continue
                for e in toc_entries or []:
                    d = dict(e or {})
                    d.setdefault(
                        "_heading_page",
                        heading_pages.get(d.get("heading_ref") or "", None),
                    )
                    entries.append(d)
        return entries
    except Exception:  # noqa: BLE001 -- 提取失败返回空，绝不阻塞 outline
        log.debug("outline: extract_outline_entries failed", exc_info=True)
        return entries


def resolve_entry_destination(entry: Mapping) -> Optional[int]:
    """Resolve an entry's 1-based bookmark destination.

    Preference order: ``heading_ref`` destination → ``destination_page``.
    Returns ``None`` when neither is resolveable (caller decides fallback).
    """
    heading = entry.get("_heading_page")
    if isinstance(heading, (int, float)) and int(heading) > 0:
        return int(heading)
    dest = entry.get("destination_page")
    if isinstance(dest, (int, float)) and int(dest) > 0:
        return int(dest)
    # legacy toc block annotation (number/title/page via annotate_toc_scan) —
    # these carry the printed page; we still never trust printed page_number.
    return None


def build_outline_toc(
    entries: Sequence[Mapping],
    *,
    default_page: int = 1,
) -> List[List]:
    """Turn ordered semantic entries into a flat ``set_toc``-ready outline.

    Returns ``[[level, title, page], ...]`` where ``level`` is a positive
    hierarchy depth built from the entries' ``level`` field via a stack
    (numbering prefixes are never re-parsed), ``title`` is the translated
    title, and ``page`` is the resolved 1-based destination.

    Entries are already translated by the document body pass (Commit 6B
    populates ``translated_title``); this adapter only resolves destinations
    and the hierarchy. Missing destinations use ``default_page`` rather than
    dropping the bookmark (a missing heading must never void an entry).
    """
    if not entries:
        return []
    items: List[Tuple[int, str, int]] = []
    for e in entries or []:
        title = (
            e.get("translated_title") or e.get("title_only") or e.get("title") or ""
        ).strip()
        if not title:
            continue
        raw_level = e.get("level")
        try:
            lvl = int(raw_level) if raw_level is not None else 0
        except (TypeError, ValueError):
            lvl = 0
        dest = resolve_entry_destination(e)
        if dest is None:
            dest = default_page
        items.append((max(0, lvl), title, max(1, int(dest))))

    if not items:
        return []

    # ── hierarchy assembly from level (stack, not numbering prefixes) ──
    min_level = min(lvl for lvl, _, _ in items)
    outline: List[List] = []
    # stack of (outline_level, index_in_outline) for open ancestors
    stack: List[Tuple[int, int]] = []
    for lvl, title, page in items:
        # normalize so the shallowest entry is outline level 1
        norm = max(1, lvl - min_level + 1)
        # pop ancestors that are not shallower than this entry
        while stack and stack[-1][0] >= norm:
            stack.pop()
        if stack:
            # this entry is a child of the nearest open shallower ancestor
            ol_level = stack[-1][0] + 1
        else:
            ol_level = norm
        outline.append([ol_level, title, page])
        stack.append((ol_level, len(outline) - 1))
    return outline
