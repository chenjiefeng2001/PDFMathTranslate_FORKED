"""TOC geometry-preserving renderer — plan Commit 6C, refactored for 7E-3.

The renderer is now a **draw-only** consumer of the unified layout layer::

    TOCEntryNode / entry dict
        ↓  layout_toc_entry (pdf2zh.semantic.layout.toc_layout)
    TocEntryLayoutResult (number/title/leader/page LayoutResults)
        ↓  toc_layout_commands → TocRenderer (draw only)
    PDF commands

The existing renderer stays the **golden implementation**: the layout adapter
reproduces its per-entry geometry exactly (title_x / page_x / indent / bbox
verbatim; leader regenerated to the original page_x from the translated
title's actual width; page number PRESERVE; no forced dots for no-leader
entries), so all pre-7E-3 behavior is unchanged.

Renderer rules (unchanged since 6C):

- **no translator inside**: a ``translate`` callable is injected by the
  caller; only the title (and continuation lines) may be translated —
  numbering prefix, leader and page number never enter it;
- **no geometry recomputation**: every command's horizontal geometry comes
  from the entry node; never from ``level``, entry index or page width;
- **no fit decisions here**: wrapping / overflow are decided by ``lay_out``
  via the layout adapter; the renderer only draws the settled result.

Measurement: the unified ``measure_text`` is the default; the
``measure_width`` injection seam is retained (tests / host measurers keep
working) — it is passed through to the layout adapter.

Coordinate convention: v3 lower-left origin (y up).  Continuation lines step
**down** the page (negative offset), so the host renderer's y-flip places
them under the entry's first line.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from pdf2zh.semantic.layout.measure import measure_text
from pdf2zh.semantic.layout.toc_layout import layout_toc_entry, toc_layout_commands
from pdf2zh.semantic.renderer.list import RenderCommand

__all__ = ["RenderCommand", "TocRenderer", "build_page_toc_plan"]


@dataclass
class TocRenderer:
    """Renders structured TOC entry dicts into positioned commands.

    The entry dicts follow the ``toc_sidechannel.entry_to_dict`` schema
    (title / number / title_only / level / page_number / indent / title_x /
    page_x / dot_leader / leader_present / continuation). Vertical position is
    supplied per entry (``ys``); horizontal geometry always comes from the
    node. Only the *title* may be translated; numbering prefix, leader and page
    number never enter the translator.
    """

    measure_width: Callable[[str, float], float] | None = None
    font: object | None = None
    leader_gap: float = 4.0
    line_height: float = 14.0
    continuation_gap: float = 3.0

    def _measure(self, text: str, size: float) -> float:
        """Measure a title run via the injected measurer or the unified API.

        When ``measure_width`` is supplied it wins verbatim (existing TOC
        behavior is unchanged).  Otherwise the layout layer's ``measure_text``
        is used, which degrades to a CJK-aware word-scale estimate when no
        ``font`` is set — byte-equivalent to the pre-7B default.
        """
        if self.measure_width is not None:
            try:
                return float(self.measure_width(text, size))
            except Exception:  # noqa: BLE001 -- measurement failure is non-fatal
                return measure_text(text, self.font, size)
        return measure_text(text, self.font, size)

    def render(
        self,
        entries: Sequence[Mapping],
        ys: Sequence[float] | None = None,
        size: float = 10.0,
        translate: Callable[[str], str] | None = None,
    ) -> list[RenderCommand]:
        """Produce one command set for the whole TOC page/region.

        Args:
            entries: entry dicts (toc_sidechannel schema), reading order.
            ys: per-entry vertical baselines; when absent, ``index *
                line_height`` is used (host should prefer passing real baselines).
            size: nominal font size used for width measurement.
            translate: title-only translator; numbering prefix / leader /
                page number are **never** passed to it (identity fallback).

        Returns:
            List of :class:`RenderCommand`: ``number`` / ``title`` / ``leader``
            / ``page`` runs in reading order.
        """
        tr = translate or (lambda s: s)
        cmds: list[RenderCommand] = []
        entries = list(entries or [])
        for i, e in enumerate(entries):
            if ys is not None and i < len(ys):
                y = float(ys[i] or 0.0)
            else:
                y = float(i * self.line_height)

            # ── 翻译只覆盖 title_only 与延续行（number/leader/page 绝不）──
            title_only = (e.get("title_only") or e.get("title") or "").strip()
            translated_title: str | None = None
            if title_only:
                pre = (e.get("translated_title") or "").strip()
                translated_title = pre if pre else tr(title_only)
            conts: list[str] = []
            for c in e.get("continuation") or []:
                if (c or "").strip():
                    conts.append(tr(c.strip()))

            # ── 布局：语义节点 → lay_out（几何全来自节点）──────────────
            result = layout_toc_entry(
                e,
                measure=self._measure,
                size=size,
                leader_gap=self.leader_gap,
                line_height=self.line_height,
                y=y,
                translated_title=translated_title,
                translated_continuation=conts,
            )
            for d in toc_layout_commands(result):
                cmds.append(RenderCommand(**d))

        return cmds

    def render_plan(
        self,
        entries: Sequence[Mapping],
        ys: Sequence[float] | None = None,
        size: float = 10.0,
        translate: Callable[[str], str] | None = None,
    ) -> dict:
        """JSON-serializable debug plan (commands + translated-call log)."""
        calls: list[str] = []

        def _tr(s: str) -> str:
            calls.append(s)
            return (translate or (lambda t: t))(s)

        cmds = self.render(entries, ys=ys, size=size, translate=_tr)
        return {
            "commands": [c.to_dict() for c in cmds],
            "translated_calls": calls,
        }


def _split_number_title(title: str):
    """Split ``2.3.1 Dataset`` into (number, title_only) for the plan builder.

    Light, congruent with ``toc_sidechannel._entry_translation_split`` but
    kept local to keep the renderer dependency-light. ``(a)`` prefixes are
    preserved as the numbering prefix too.
    """
    t = (title or "").strip()
    if not t:
        return "", ""
    lead = ""
    rest = t
    # dotted decimal / parenthesised letter / roman numbering prefixes
    import re

    m = re.match(r"^(\s*(?:\d+(?:\.\d+)*|[a-zA-Zа-яА-Я])[\s.、:：)）.．]*\s*)", t)
    roman = re.match(r"^\s*[ivxlcdmIVXLCDM]{1,4}[\s.、:：)）.．]+\s*", t)
    if m and (
        m.group(1).strip()[-1:] in ".、:：)）.．" or len(m.group(1).split(".")) > 1
    ):
        lead = m.group(1)
        rest = t[len(lead) :]
    elif roman:
        lead = roman.group(0)
        rest = t[len(lead) :]
    return lead.strip(), rest.strip()


def build_page_toc_plan(
    lines: Sequence[Mapping],
    page_width: float,
    *,
    translate: Callable[[str], str] | None = None,
    ys: Sequence[float] | None = None,
    size: float = 10.0,
    measure_width: Callable[[str, float], float] | None = None,
) -> dict:
    """Full ``detect -> parse -> split -> translate -> render`` chain.

    Args:
        lines: page lines ``{text, x0, x1, size}`` (reading order).
        page_width: page width (pt) for right-column page-number gating.
        translate: title-only translator; numbering/leader/page never pass it.
        ys: per-entry baselines (optional; renderer derives vertical stepping).
        size: nominal font size.
        measure_width: font-accurate width measurer (optional).

    Returns JSON-safe plan ``{tree, entries, commands, translated_calls}``.
    When the page is not a TOC page, ``tree`` is ``None`` and the lists are
    empty (identity for normal pages).
    """
    from pdf2zh.semantic.toc_parser import parse_toc

    node = parse_toc([dict(ln) for ln in lines or []], float(page_width))
    if node is None:
        return {"tree": None, "entries": [], "commands": [], "translated_calls": []}

    rendered_entries: list[dict] = []
    for pno, en in enumerate(node.entries):
        rendered_entries.append(
            {
                "title": en.title,
                "number": _split_number_title(en.title)[0],
                "title_only": _split_number_title(en.title)[1],
                "level": int(en.level or 0),
                "page_number": str(en.page_number or ""),
                "indent": round(float(en.indent or 0.0), 1),
                "title_x": round(float(en.title_x or 0.0), 1),
                "page_x": round(float(en.page_x or 0.0), 1),
                "dot_leader": en.dot_leader or "",
                "leader_present": bool(en.leader_present),
                "continuation": list(en.continuation or []),
                "bbox": list(en.bbox or (0, 0, 0, 0)),
            }
        )

    renderer = TocRenderer(measure_width=measure_width)
    calls: list[str] = []
    seen: dict[str, str] = {}

    def _tr(s: str) -> str:
        calls.append(s)
        out = (translate or (lambda t: t))(s)
        seen.setdefault(s, out)
        return out

    cmds = renderer.render(rendered_entries, ys=ys, size=size, translate=_tr)
    for e in rendered_entries:
        e.setdefault("translated_title", seen.get(e["title_only"], e["title_only"]))
    return {
        "tree": node.to_dict(),
        "entries": rendered_entries,
        "commands": [c.to_dict() for c in cmds],
        "translated_calls": list(calls),
    }
