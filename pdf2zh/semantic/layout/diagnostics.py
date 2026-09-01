"""Layout observability / diagnostics — Commit 7F-7.

A thin **record-only** layer that turns the already-settled results of the
layout pipeline into one machine-readable diagnostic chain:::

    LayoutResult
        ↓
    LayoutDiagnostic      <- this module (7F-7a)
        ↓
    debug/layout.json     (7F-7c)   +   evaluator (7F-7e)

Hard rules (enforced by ``tests/test_layout_diagnostics_7f7.py``):

- **no recomputation** — a diagnostic is built from a *settled* render plan
  entry (its ``render_payload`` already carries the final
  ``overflow``/``policy``/``font_size``/``recovery``/``trace``), never by
  re-running ``lay_out`` / ``adaptive_layout``;
- **no re-layout of aggregates** — List / TOC entries are read from their
  settled payload (channel geometry copied verbatim); nothing here re-derives
  geometry from ``level`` / ``index``;
- **no detector / parser / translator / renderer** imports — this module is
  pure layout observability.

Schema (``LayoutDiagnostic.to_dict()``):::

    {
      "page": 3, "block_index": 12, "kind": "toc",
      "primitive_kind": "fixed_anchor", "target": "title",
      "source_text": "...", "translated_text": "...",
      "bbox": [...], "resolved_bbox": [...],
      "overflow": false,
      "recovery": {reason, decision, steps, original_font_size, final_font_size} | null,
      "trace": [{decision, overflow, line_count, font_size}, ...] | []
    }

``stable_fields(diag)`` returns only the fields the golden gate (7F-7f)
compares — the schema, decision ladder, overflow flag, and the immovable
geometry anchors — so font / PyMuPDF version / unrelated-field noise never
breaks the baseline.
"""

from __future__ import annotations

from typing import Any, Optional

__all__ = [
    "LayoutDiagnostic",
    "collect_layout_diagnostics",
    "diagnostic_from_plan_entry",
    "stable_fields",
    "summarize_diagnostics",
    "SCHEMA_VERSION",
]

SCHEMA_VERSION = 1

#: Immovable geometry anchors per block kind — a diagnostic never recomputes
#: these; it copies them verbatim from the settled payload.
_ANCHOR_KEYS = {
    "list": ("marker_x", "content_x", "continuation_x"),
    "toc": ("title_x", "page_x", "continuation_x"),
}

_PRIMITIVE_KIND_BY_KIND = {
    "flow": "flow",
    "paragraph": "flow",
    "list": "list",
    "toc": "toc",
    "code": "preserved",
    "formula": "preserved",
    "preserve": "preserved",
}


class LayoutDiagnostic:
    """One JSON-safe diagnostic record for one settled layout result.

    Read-only view over a render-plan entry; every field is either copied
    verbatim from the settled payload or derived from *existing* fields
    (never recomputed geometry, never re-layout).
    """

    __slots__ = (
        "page",
        "block_index",
        "kind",
        "primitive_kind",
        "target",
        "source_text",
        "translated_text",
        "bbox",
        "resolved_bbox",
        "overflow",
        "recovery",
        "trace",
        "anchors",
        "font_size",
    )

    def __init__(
        self,
        *,
        page: int,
        block_index: int,
        kind: str,
        primitive_kind: str,
        target: Optional[str],
        source_text: str,
        translated_text: str,
        bbox: tuple,
        resolved_bbox: tuple,
        overflow: bool,
        recovery: Optional[dict],
        trace: list,
        anchors: dict,
        font_size: float,
    ) -> None:
        self.page = int(page)
        self.block_index = int(block_index)
        self.kind = str(kind)
        self.primitive_kind = str(primitive_kind)
        self.target = target
        self.source_text = str(source_text or "")
        self.translated_text = str(translated_text or "")
        self.bbox = tuple(float(v) for v in (bbox or (0.0, 0.0, 0.0, 0.0)))
        self.resolved_bbox = tuple(float(v) for v in (resolved_bbox or self.bbox))
        self.overflow = bool(overflow)
        self.recovery = dict(recovery) if isinstance(recovery, dict) else None
        self.trace = [dict(t) for t in (trace or [])]
        self.anchors = dict(anchors or {})
        self.font_size = float(font_size or 0.0)

    def to_dict(self) -> dict:
        return {
            "page": self.page,
            "block_index": self.block_index,
            "kind": self.kind,
            "primitive_kind": self.primitive_kind,
            "target": self.target,
            "source_text": self.source_text,
            "translated_text": self.translated_text,
            "bbox": [round(v, 2) for v in self.bbox],
            "resolved_bbox": [round(v, 2) for v in self.resolved_bbox],
            "overflow": self.overflow,
            "recovery": self.recovery,
            "trace": self.trace,
            "anchors": {k: round(float(v), 2) for k, v in self.anchors.items()},
            "font_size": round(self.font_size, 2),
        }


def _block_index(block_id: str) -> int:
    """Parse ``p3_12`` → 12 (best-effort; 0 when unparseable)."""
    if "_" in str(block_id or ""):
        try:
            return int(str(block_id).rsplit("_", 1)[1])
        except (TypeError, ValueError):
            return 0
    return 0


def _payload_of(entry: dict) -> dict:
    p = entry.get("render_payload")
    return p if isinstance(p, dict) else {}


def diagnostic_from_plan_entry(entry: dict) -> LayoutDiagnostic:
    """Build one :class:`LayoutDiagnostic` from a *settled* render-plan entry.

    Pure read of the settled payload — never re-lays-out, never re-derives
    geometry.  List / TOC channel geometry (``marker_x`` / ``content_x`` /
    ``title_x`` / ``page_x`` …) is copied verbatim from the entry's payload.
    """
    kind = str(entry.get("kind") or _payload_of(entry).get("kind") or "flow")
    payload = _payload_of(entry)
    bbox = tuple(entry.get("src_box") or entry.get("bbox") or (0.0, 0.0, 0.0, 0.0))
    resolved = tuple(entry.get("dst_box") or bbox)
    # primitive kind: settled payload wins (flow carries it explicitly);
    # otherwise map from the semantic block kind.
    primitive_kind = str(
        payload.get("primitive_kind") or ""
    ) or _PRIMITIVE_KIND_BY_KIND.get(kind, kind)
    # List / TOC channel geometry comes from the settled structured payloads.
    anchors: dict[str, float] = {}
    list_items = entry.get("list_items") or {}
    if kind == "list" and isinstance(list_items, dict):
        items = list_items.get("items") or []
        for it in items[:1]:
            for k in _ANCHOR_KEYS["list"]:
                v = it.get(k)
                if isinstance(v, (int, float)):
                    anchors[k] = float(v)
    toc_entries = entry.get("toc_entries") or entry.get("entries") or []
    if kind == "toc" and toc_entries:
        for e in toc_entries[:1]:
            for k in _ANCHOR_KEYS["toc"]:
                v = e.get(k)
                if isinstance(v, (int, float)):
                    anchors[k] = float(v)
    # target: channel label for list / toc; flow/code have no channel.
    target = None
    if kind == "list":
        target = "list_item"
    elif kind == "toc":
        target = "toc_entry"
    # font size: settled payload (SHRINK result) else block nominal.
    font_size = payload.get("font_size") or entry.get("font_size") or 0.0
    return LayoutDiagnostic(
        page=int(entry.get("page") or 0),
        block_index=_block_index(str(entry.get("block_id") or "")),
        kind=kind,
        primitive_kind=primitive_kind,
        target=target,
        source_text=str(entry.get("text") or ""),
        translated_text=str(entry.get("translated") or entry.get("text") or ""),
        bbox=bbox,
        resolved_bbox=resolved,
        overflow=bool(payload.get("overflow", False)),
        recovery=payload.get("recovery"),
        trace=payload.get("trace") or [],
        anchors=anchors,
        font_size=font_size,
    )


def collect_layout_diagnostics(plan) -> list[LayoutDiagnostic]:
    """Collect one diagnostic per render-plan entry (Flow / List / TOC / Code).

    The plan is the **settled** result carrier (``render_payload`` already
    holds the final layout + recovery record); this collector only reads it.
    """
    out: list[LayoutDiagnostic] = []
    for entry in plan or []:
        if not isinstance(entry, dict):
            continue
        try:
            out.append(diagnostic_from_plan_entry(entry))
        except Exception:  # noqa: BLE001 -- observability never blocks
            continue
    return out


def stable_fields(diag: LayoutDiagnostic) -> dict:
    """The golden-gate stable projection of a diagnostic (7F-7f).

    Only fields that are architecture-meaningful and version-stable:
    schema / primitive kind / decision ladder / overflow flag / channel
    geometry anchors.  Font metrics, text contents and other noisy fields are
    intentionally excluded so PyMuPDF / font-version churn cannot break the
    baseline.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": diag.kind,
        "primitive_kind": diag.primitive_kind,
        "target": diag.target,
        "overflow": diag.overflow,
        "recovery_decision": (diag.recovery or {}).get("decision"),
        "recovery_steps": (diag.recovery or {}).get("steps") or [],
        "anchors": {k: round(float(v), 2) for k, v in sorted(diag.anchors.items())},
    }


def summarize_diagnostics(diags: list[LayoutDiagnostic]) -> dict:
    """Top-level summary for ``debug/layout.json`` (7F-7c)."""
    recovered = 0
    preserved_overflow = 0
    overflow = 0
    for d in diags:
        if d.overflow:
            overflow += 1
        if d.recovery:
            recovered += 1
            if d.recovery.get("decision") == "preserve_overflow":
                preserved_overflow += 1
    return {
        "blocks": len(diags),
        "overflow": overflow,
        "recovered": recovered,
        "preserved_overflow": preserved_overflow,
    }


def dump_layout_diagnostics_json(plan, out_path: str) -> dict:
    """Write the full 7F-7c ``debug/layout.json`` payload.

    ``out_path`` is a file path (JSON written with ``ensure_ascii=False``).
    Returns the payload dict.
    """
    import json
    import os

    diags = collect_layout_diagnostics(plan)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "diagnostics": [d.to_dict() for d in diags],
        "summary": summarize_diagnostics(diags),
    }
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
    return payload
