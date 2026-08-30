"""FlowText render/settle layer — Commit 7E-1, extended by 7F-6b.

Turns a :class:`~pdf2zh.semantic.layout.primitives.FlowText` primitive into a
:class:`~pdf2zh.semantic.layout.overflow.LayoutResult` and then into
positioned, drawable line commands.  The PDF renderer consumes only the
resulting commands — it never re-applies the fit / wrap / clip decision
itself::

    FlowText
        ↓
    adaptive_layout()   (7F-6b: bounded WRAP → SHRINK → CLIP executor)
        ↓ lay_out()
    LayoutResult (with recovery record)
        ↓
    commands[]          → host renderer draws (no re-layout)

7F-6b: Flow officially consumes **recovery**.  ``render_flow_text`` routes
through :func:`pdf2zh.semantic.layout.adaptive.adaptive_layout` — the finite
non-looping executor over ``recovery.py`` decisions — with the ``flow`` budget
(``WRAP → SHRINK → CLIP``, clamped by ``LayoutBudget.min_font_size`` /
``max_font_reduction``).  The resulting ``LayoutResult`` carries the
``recovery`` record (``reason`` / ``decision`` / ``steps`` / original vs final
font size), which is surfaced in the returned payload so evaluators can see
what happened (7F-6a unified ``recovery`` member).

Layer rules (enforced by architecture assertions in ``test_flowtext_renderer``):

- This module never calls ``wrap_lines`` / ``shrink_to_fit`` / ``clip_text``
  directly, and never re-judges ``text_width > bbox.width``.  Every fit
  decision is delegated to ``lay_out`` inside ``adaptive_layout`` →
  ``LayoutResult``; the renderer is a pure consumer of that result.
- Recovery is bounded: at most WRAP (1) → SHRINK (1) → CLIP (1); never a
  ``while overflow`` loop (the executor is a finite state machine).
- Geometry flows through verbatim: the first command line inherits the
  primitive's ``x`` / baseline ``y`` and the **settled** font size (a SHRINK
  recovery carries its reduced font to the draw call).  Nothing here re-derives
  position from level / index / page width.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Callable, Optional

from pdf2zh.semantic.layout.adaptive import adaptive_layout
from pdf2zh.semantic.layout.overflow import (
    LayoutResult,
    OverflowPolicy,
)
from pdf2zh.semantic.layout.primitives import FlowText
from pdf2zh.semantic.layout.recovery import budget_for_kind

__all__ = [
    "FlowTextRenderer",
    "render_flow_text",
    "FLOW_COMMAND_KIND",
    "LAYOUT_OK_POLICIES",
]

#: Kind tag on every command produced by this layer.
FLOW_COMMAND_KIND = "flow-text"

#: Policies that mean "the FlowText layout path succeeded cleanly".  CLIP is
#: the explicit last-resort recovery: it still emits commands, but is never
#: reported as ``layout_ok`` (overflow stays observable downstream).
LAYOUT_OK_POLICIES = (OverflowPolicy.WRAP, OverflowPolicy.SHRINK)


class FlowTextRenderer:
    """Consume a :class:`LayoutResult` and emit positioned line commands.

    Pure consumption of an already-settled layout: column ``x`` and the
    first-line baseline come verbatim from the result's geometry / primitive;
    the only value synthesized here is the vertical step between wrapped lines
    (``|line_step|`` defaulting to ``font_size * line_height``), which is a
    placement constant, not a fit/wrap decision.
    """

    def __init__(self, line_height: float = 1.4):
        self.line_height = line_height

    def render(
        self,
        result: LayoutResult,
        origin: Optional[tuple[float, float]] = None,
        line_step: Optional[float] = None,
    ) -> list[dict]:
        """Emit one command per ``LayoutResult.lines`` entry at ``origin + i*step``.

        Args:
            result: a :class:`LayoutResult` produced by :func:`lay_out`.
            origin: ``(x, baseline_y)`` for the first line; defaults to the
                result bbox top-left ``(bbox[0], bbox[1])``.
            line_step: signed delta between consecutive line baselines;
                defaults to ``+font_size * line_height``.  Pass a negative
                value for y-up (bottom-left) coordinate spaces so wrapped
                lines flow downward.

        Returns:
            list of ``{kind, text, x, y, width, line, is_last, overflow}``
            where ``x``/``y`` are line baselines in the target coordinate
            space.
        """
        fs = float(result.font_size or 11.0)
        step = (
            float(line_step)
            if line_step is not None
            else fs * float(self.line_height)
        )
        ox, oy = origin or (float(result.bbox[0]), float(result.bbox[1]))
        n = len(result.lines)
        cmds: list[dict] = []
        for i, (ln, w) in enumerate(zip(result.lines, result.line_widths)):
            cmds.append(
                {
                    "kind": FLOW_COMMAND_KIND,
                    "text": ln,
                    "x": round(float(ox), 2),
                    "y": round(float(oy) + i * step, 2),
                    "width": round(float(w), 2),
                    "line": i,
                    "is_last": i == n - 1,
                    "overflow": bool(result.overflow) and i == n - 1,
                    # 7F-6b: settled font size (SHRINK recovery reduces it) —
                    # the draw layer must render at the settled size, not the
                    # block's nominal font.
                    "font_size": round(fs, 2),
                }
            )
        return cmds


def render_flow_text(
    text: str,
    *,
    origin: tuple[float, float] = (0.0, 0.0),
    max_width: float = 0.0,
    max_height: float = 0.0,
    line_height: float = 1.4,
    font_size: float = 11.0,
    measure: Optional[Callable[[str, float], float]] = None,
    allow_shrink: bool = True,
    line_step: Optional[float] = None,
    renderer: Optional[FlowTextRenderer] = None,
) -> dict:
    """Run the full ``FlowText → adaptive_layout() → LayoutResult → commands``
    pipeline (7F-6b: Flow consumes recovery).

    Args:
        text: the (translated) paragraph text.
        origin: ``(x, y)`` anchor from the original block — passed verbatim;
            nothing here recomputes it.
        max_width / max_height: available region from the original block.
        line_height: vertical step multiple (relative to ``font_size``).
        font_size: nominal font size in points.
        measure: ``(text, font_size) -> width`` measurer; defaults to the
            layout layer's CJK-aware estimate.
        allow_shrink: whether Flow may use the SHRINK (and last-resort CLIP)
            recovery stages.  Default **True** since 7F-6b (Flow officially
            runs the ``WRAP → SHRINK → CLIP`` ladder); ``False`` restricts the
            run to WRAP then honest PRESERVE_OVERFLOW (never silent shrink /
            clip).
        line_step: explicit signed vertical step for the commands (overrides
            ``font_size * line_height``); used to adapt to y-up spaces.
        renderer: command-builder override (defaults to :class:`FlowTextRenderer`).

    Returns:
        JSON-safe dict ``{kind, text, lines, line_widths, commands, overflow,
        policy, font_size, primitive_kind, layout_ok, bbox, recovery}`` where
        ``recovery`` is the 7F-6a unified record (``None`` when nothing ran).
        Never raises: on a layout failure it emits a singled-line
        overflow-flagged result so callers can observe and cascade to a legacy
        path deterministically.
    """
    fs = float(font_size or 11.0)
    lh = float(line_height or 1.4)
    o0, o1 = float(origin[0]), float(origin[1])
    flow = FlowText(
        text=text,
        origin=(o0, o1),
        max_width=float(max_width),
        max_height=float(max_height),
        line_height=fs * lh,
    )
    budget = budget_for_kind("flow")
    if not allow_shrink:
        budget = replace(budget, allow_shrink=False, allow_clip=False)
    _failed = False
    try:
        result = adaptive_layout(
            flow,
            measure=measure,
            avail_width=float(max_width),
            avail_height=float(max_height),
            font_size=fs,
            budget=budget,
        )
    except Exception:  # noqa: BLE001 -- layout is never allowed to be fatal
        _failed = True
        result = LayoutResult(
            text=text,
            lines=[text],
            line_widths=[0.0],
            bbox=(o0, o1, o0 + float(max_width), o1 + float(max_height)),
            overflow=True,
            policy=OverflowPolicy.CLIP,
            font_size=fs,
            primitive_kind="flow",
        )
    rr = renderer or FlowTextRenderer(line_height=lh)
    cmds = rr.render(result, origin=(o0, o1), line_step=line_step)
    return {
        "kind": "flow",
        "text": text,
        "lines": list(result.lines),
        "line_widths": [round(w, 1) for w in result.line_widths],
        "commands": cmds,
        "overflow": bool(result.overflow),
        "policy": result.policy.value,
        "font_size": round(result.font_size, 1),
        "primitive_kind": result.primitive_kind,
        "layout_ok": (not _failed) and result.policy in LAYOUT_OK_POLICIES,
        "bbox": [round(v, 1) for v in result.bbox],
        "recovery": result.recovery,
        # 7F-7d: optional per-stage recovery trace ([] when none ran).
        "trace": [dict(t) for t in result.recovery_trace],
    }