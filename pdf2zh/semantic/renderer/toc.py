"""TOC geometry-preserving renderer — plan Commit 6C.

Renders structured TOC entries back into the PDF while preserving the
original visual geometry. Mirrors :mod:`pdf2zh.semantic.renderer.list` in
spirit: the renderer never touches a translator (a ``translate`` callable is
injected by the caller), and every command's *horizontal* geometry is copied
straight from the parsed entry node — ``title_x`` / ``page_x`` / ``indent`` /
``level`` / ``bbox`` — never recomputed from ``level``, entry index, page
width, or fixed constants.

Per-entry rendering model (Commit 6C spec):
::

    number      -> PRESERVE        (verbatim, never translated/renumbered)
    title       -> translated      (title-only; numbering prefix excluded)
    leader      -> regenerate      (fill to original page_x, by actual width)
    page_number -> PRESERVE        (verbatim at original page_x, never moved)

Decisions that matter:

- ``title_x`` (left edge of the title column) is copied verbatim; the
  translated title starts there and the leader is measured from its **actual**
  rendered width (never an English-character count heuristic).
- ``page_x`` (right-aligned page-number column) is copied verbatim; a
  longer-than-original translated title shrinks the leader instead of moving
  the page number.
- ``leader_present`` decides whether to emit a dot leader; a no-leader TOC
  is **never** forced to add dots.
- nested levels keep the original ``indent`` / ``title_x`` (no
  ``x = level * constant``).
- multi-line entries: continuation lines keep vertical progression under the
  title column and the page number stays in the first line's page_x column.

Output is a flat list of :class:`RenderCommand` positionable runs. Vertical
(y) comes from the hosted page pipeline (a per-entry ``y`` map); horizontal
is decided here. Pure logic — no I/O, no PyMuPDF, no converter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Mapping, Sequence

from pdf2zh.semantic.layout.measure import measure_text
from pdf2zh.semantic.renderer.list import RenderCommand

__all__ = ["TocRenderer", "build_page_toc_plan", "RenderCommand"]


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

    def _emit_title_cmd(
        self,
        cmds: list[RenderCommand],
        text: str,
        x: float,
        y: float,
        size: float,
        entry,
        kind: str,
    ) -> float:
        """Place one title-ish run; returns the run's right edge."""
        cmds.append(
            RenderCommand(
                kind=kind,
                text=text,
                x=x,
                y=y,
                width=self._measure(text, size),
                level=int(entry.get("level", 0) or 0),
                bbox=tuple(entry.get("bbox") or (0.0, 0.0, 0.0, 0.0)),
            )
        )
        return x + self._measure(text, size)

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
            title_x = float(e.get("title_x") or 0.0)
            page_x = float(e.get("page_x") or 0.0)
            number = (e.get("number") or "").strip()
            title_only = (e.get("title_only") or e.get("title") or "").strip()

            # ── Channel 1: numbering prefix —— PRESERVE ────────────────
            cursor = title_x
            if number:
                cursor = self._emit_title_cmd(cmds, number, cursor, y, size, e, "number")
                cursor += self.leader_gap

            # ── Channel 2: title —— TRANSLATE (only translatable part) ─
            # Prefer the already-translated title (set by the caller, e.g.
            # ``toc_sidechannel.translate_toc_entries`` writes
            # ``translated_title``); fall back to translating ``title_only``
            # only when no translation was pre-computed.
            if title_only:
                pre = (e.get("translated_title") or "").strip()
                translated = pre if pre else tr(title_only)
                title_end = self._emit_title_cmd(
                    cmds, translated, cursor, y, size, e, "title"
                )
            else:
                title_end = cursor

            # ── Channel 3: dot leader —— regenerate to original page_x ─
            if e.get("leader_present") and page_x > title_end + self.leader_gap:
                available = page_x - title_end
                unit = self._measure(".", size)
                n = max(1, int((available - self.leader_gap) // unit))
                leader = "." * n
                _ = self._emit_title_cmd(cmds, leader, title_end, y, size, e, "leader")

            # ── Channel 4: page number —— PRESERVE at original page_x ──
            page_text = str(e.get("page_number") or "").strip()
            if page_text:
                _ = self._emit_title_cmd(cmds, page_text, page_x, y, size, e, "page")

            # ── Continuation lines: keep vertical progression under title_x ──
            for k, cont in enumerate((e.get("continuation") or []), start=1):
                if not (cont or "").strip():
                    continue
                cc = tr(cont.strip())
                self._emit_title_cmd(
                    cmds,
                    cc,
                    title_x + size * 1.0,
                    y + k * self.line_height,
                    size,
                    e,
                    "title",
                )

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

    m = re.match(
        r"^(\s*(?:\d+(?:\.\d+)*|[a-zA-Zа-яА-Я])[\s.、:：)）.．]*\s*)", t
    )
    roman = re.match(r"^\s*[ivxlcdmIVXLCDM]{1,4}[\s.、:：)）.．]+\s*", t)
    if m and (m.group(1).strip()[-1:] in ".、:：)）.．" or len(m.group(1).split('.')) > 1):
        lead = m.group(1)
        rest = t[len(lead):]
    elif roman:
        lead = roman.group(0)
        rest = t[len(lead):]
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