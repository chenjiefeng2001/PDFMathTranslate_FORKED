"""Overflow diagnosis + recovery policy — Commit 7F (7F-1 / 7F-2).

7F answers one question:

> 译文放不下、且我们不能破坏原始锚点的时候，怎么恢复版面？

It does **not** decide "what this is" (that ended in Semantic), does **not**
translate, and does **not** draw.  It is a pure policy layer that looks at an
already-computed :class:`~pdf2zh.semantic.layout.overflow.LayoutResult` and
classifies *why* it overflowed, then — given a
:class:`LayoutBudget` — decides *what recovery action* to take.

    SemanticNode
        ↓ Translation
    Layout Primitive
        ↓ lay_out()
    LayoutResult (overflow)
        ↓  classify_reason         <- this module (7F-1)
    OverflowReason
        ↓  decide_recovery         <- this module (7F-3 policy)
    RecoveryDecision / OverflowDiagnosis
        ↓
    Renderer (executes, draw only)

Vocabulary:

- :class:`OverflowReason`  — the *why*: WIDTH / HEIGHT / UNBREAKABLE_TOKEN /
  FIXED_COLUMN_COLLISION / PRESERVED_REGION.
- :class:`RecoveryDecision` — the *what*: NO_ACTION / WRAP / SHRINK / CLIP /
  PRESERVE_OVERFLOW.
- :class:`OverflowDiagnosis` — a JSON-safe ``(reason, decision, ratio,
  effective_font_size, message)`` record, so logging can say
  ``TOC entry → WIDTH overflow → SHRINK → font 10.5 → 9.7`` instead of just
  ``overflow=True``.
- :class:`LayoutBudget` — per-primitive recovery budgets (allow_shrink /
  allow_clip / max_extra_lines / min_font_size …) so policies can be tuned by
  PDF kind without scattering ``if`` everywhere (7F-6).

Decision ladder per primitive kind (7F-3 policy, enforced by
:func:`decide_recovery`):

    FlowText   : WRAP → SHRINK → CLIP          (aggressive)
    List content : WRAP → SHRINK → CLIP         (marker NEVER touched)
    List marker  : PRESERVE_OVERFLOW            (marker never wrap/shrink)
    TOC title    : WRAP → SHRINK → PRESERVE_OVERFLOW   (7F-5a; page_x NEVER
                   moves, leader only shrinks, **never CLIP**)
    TOC page column : PRESERVE_OVERFLOW         (FixedColumn never moves)
    Code         : PRESERVE                     (never WRAP/SHRINK/CLIP)

Architecture guarantees (locked by ``tests/test_layout_recovery_architecture.py``):

- imports / references **no detector, no parser, no renderer, no translator**;
- never derives geometry from ``level`` / ``index`` (no ``level *`` /
  ``index *``);
- consumes only ``primitive`` / ``result`` / ``budget`` / ``measurement``;
- never calls ``wrap_lines`` / ``shrink_to_fit`` / ``clip_text`` **directly**
  — it only **decides**; the layout side-channel executes via ``lay_out``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from pdf2zh.semantic.layout.wrap import tokenize  # classify UNBREAKABLE_TOKEN

__all__ = [
    "LayoutBudget",
    "OverflowDiagnosis",
    "OverflowReason",
    "RecoveryDecision",
    "budget_for_kind",
    "classify_reason",
    "decide_recovery",
    "default_budget",
    "diagnose_overflow",
]

_TOL = 1e-6


class OverflowReason(Enum):
    """*Why* a translated run does not fit its constraint."""

    WIDTH = "width"                              # breaks, but exceeds available width
    HEIGHT = "height"                            # fits width but more lines than height
    UNBREAKABLE_TOKEN = "unbreakable_token"      # a single wide token cannot wrap
    FIXED_COLUMN_COLLISION = "fixed_column_collision"  # hits an immovable column (page_x)
    PRESERVED_REGION = "preserved_region"        # a preserve region (code) itself overflows


class RecoveryDecision(Enum):
    """*What* recovery action to take.  NEVER silently succeeds."""

    NO_ACTION = "no_action"              # not actually overflowing
    WRAP = "wrap"                        # reflow into more lines
    SHRINK = "shrink"                    # reduce font_size (budgeted)
    CLIP = "clip"                        # truncate — always explicit overflow
    PRESERVE_OVERFLOW = "preserve_overflow"  # keep geometry, report overflow


@dataclass(frozen=True)
class LayoutBudget:
    """Per-primitive recovery budget (7F-6).  ``None`` means unbounded."""

    max_extra_lines: int | None = None          # extra wrapped lines allowed
    max_height_expansion: float | None = None    # points the box may grow
    min_font_size: float | None = None           # hard floor for SHRINK
    max_font_reduction: float | None = None      # max drop in points below original
    allow_wrap: bool = True
    allow_shrink: bool = False
    allow_clip: bool = False


@dataclass
class OverflowDiagnosis:
    """Structured, JSON-safe record of an overflow + chosen recovery."""

    reason: OverflowReason
    decision: RecoveryDecision
    primitive_kind: str = "flow"
    measure_ratio: float | None = None       # required_width / available_width
    effective_font_size: float | None = None
    extra_lines: int = 0
    message: str = ""

    def to_dict(self) -> dict:
        return {
            "reason": self.reason.value,
            "decision": self.decision.value,
            "primitive_kind": self.primitive_kind,
            "measure_ratio": self.measure_ratio,
            "effective_font_size": round(self.effective_font_size, 2)
            if self.effective_font_size is not None else None,
            "extra_lines": self.extra_lines,
            "message": self.message,
        }


# ---------------------------------------------------------------------------
# 7F-6 default budgets — grouped by primitive kind so PDF types can tune once.
# ---------------------------------------------------------------------------


def budget_for_kind(kind: str) -> LayoutBudget:
    """Default :class:`LayoutBudget` for a primitive ``kind``.

    - ``flow`` / ``continuation`` — aggressive: wrap, then shrink, then clip.
    - ``list_content`` (7F-6c) — list content / continuation run the full
      aggressive ladder; the marker is handled by ``decide_recovery``'s target
      rule, NOT by budget.
    - ``anchor`` — generic anchor (wrap then shrink; list marker handled by
      ``decide_recovery``'s target rule, NOT by budget).
    - ``toc_title`` — TOC title ladder (7F-5a): WRAP → SHRINK →
      PRESERVE_OVERFLOW, **never CLIP** — the page column (``page_x``) is
      immovable and a title must never be truncated into it.
    - ``column`` (toc page_x) — nothing may move or shrink.
    - ``preserved`` (code) — never wrap / shrink / clip.
    """
    if kind in ("flow", "continuation"):
        return LayoutBudget(allow_wrap=True, allow_shrink=True, allow_clip=True)
    if kind == "list_content":
        # 7F-6c-1: list content / continuation run the full aggressive ladder
        # (WRAP → SHRINK → CLIP).  The marker is handled by ``decide_recovery``'s
        # target rule, NOT by this budget.
        return LayoutBudget(
            allow_wrap=True, allow_shrink=True, allow_clip=True, max_extra_lines=2
        )
    if kind == "anchor":
        return LayoutBudget(
            allow_wrap=True, allow_shrink=True, allow_clip=True, max_extra_lines=2
        )
    if kind in ("toc_title", "toc"):
        # 7F-5a: page_x never moves; a too-long title degrades to explicit
        # overflow (PRESERVE_OVERFLOW), never CLIP, never font-truncation.
        return LayoutBudget(
            allow_wrap=True, allow_shrink=True, allow_clip=False, max_extra_lines=2
        )
    if kind == "column":
        return LayoutBudget(
            allow_wrap=False, allow_shrink=False, allow_clip=False
        )
    if kind == "preserved":
        return LayoutBudget(
            allow_wrap=False, allow_shrink=False, allow_clip=False
        )
    return LayoutBudget(allow_wrap=True, allow_shrink=True, allow_clip=True)


def default_budget(kind: str) -> LayoutBudget:
    """Alias for :func:`budget_for_kind` (kept for caller ergonomics)."""
    return budget_for_kind(kind)


# ---------------------------------------------------------------------------
# 7F-1 classify_reason — why did it overflow?
# ---------------------------------------------------------------------------


def _is_unbreakable(text: str, avail_width: float) -> bool:
    """True when ``text`` is a single token that cannot be wrapped.

    A run with no whitespace and no CJK / full-width glyph is unbreakable by
    :func:`wrap_lines` — if it is wider than ``avail_width`` the only
    in-place recoveries are SHRINK / CLIP.
    """
    kinds = [k for k, _v in tokenize(text or "")]
    if "space" in kinds or "cjk" in kinds:
        return False
    words = [v for k, v in tokenize(text or "") if k == "word"]
    return bool(text) and len(words) == 1 and avail_width > 0.0


def classify_reason(
    result,
    *,
    avail_width: float = 0.0,
    avail_height: float = 0.0,
) -> OverflowReason:
    """Classify *why* a :class:`~pdf2zh.semantic.layout.overflow.LayoutResult`
    overflowed.

    The ``result`` must expose ``primitive_kind`` / ``line_widths`` / ``lines``
    / ``font_size`` (the :class:`LayoutResult` shape).  When the result does
    not actually overflow, returns ``WIDTH`` as a harmless default that maps to
    ``NO_ACTION`` in :func:`decide_recovery`.
    """
    kind = getattr(result, "primitive_kind", "flow")
    widths = getattr(result, "line_widths", None) or []
    lines = getattr(result, "lines", None) or []
    font_size = float(getattr(result, "font_size", 0.0) or 0.0)
    max_w = max(widths) if widths else 0.0

    if kind == "preserved":
        return OverflowReason.PRESERVED_REGION
    if kind == "column":
        return OverflowReason.FIXED_COLUMN_COLLISION

    width_overflow = avail_width > 0.0 and max_w > avail_width + _TOL
    height_overflow = avail_height > 0.0 and (
        len(lines) * (font_size or 1.0) > avail_height + _TOL
    )

    if width_overflow:
        text = str(getattr(result, "text", "") or "")
        if _is_unbreakable(text, avail_width):
            return OverflowReason.UNBREAKABLE_TOKEN
        return OverflowReason.WIDTH
    if height_overflow:
        return OverflowReason.HEIGHT
    if kind == "anchor" and avail_width > 0.0 and max_w > 0.0:
        # an anchor (toc title / list content) just past any bound
        return OverflowReason.WIDTH
    return OverflowReason.WIDTH


# ---------------------------------------------------------------------------
# 7F-3 decide_recovery — what recovery action to take (budget-gated ladder)
# ---------------------------------------------------------------------------


def decide_recovery(
    kind: str,
    reason: OverflowReason,
    *,
    budget: LayoutBudget | None = None,
    target: str | None = None,
) -> RecoveryDecision:
    """Pick the recovery action for ``kind`` overflowing with ``reason``.

    ``target`` refines an ``anchor`` primitive: ``"marker"`` is never
    wrap/shrink (a list marker must stay put); any other anchor (list content /
    toc title) follows the wrap→shrink ladder.

    Policy ladder (7F-3)::

        WRAP  →  SHRINK  →  CLIP  /  PRESERVE_OVERFLOW

    with per-kind exemptions:

    - ``preserved`` /  ``column``  →  PRESERVE_OVERFLOW (never move).
    - ``anchor`` + ``target=marker`` → PRESERVE_OVERFLOW.
    - ``UNBREAKABLE_TOKEN`` → skip WRAP, go straight to SHRINK/CLIP.
    - ``TOC title`` (anchor, non-marker) and ``column`` → never CLIP (page_x
      must not be truncated); falls to PRESERVE_OVERFLOW after SHRINK.
    """
    b = budget or budget_for_kind(kind)

    # immovable: code / toc page column / marker
    if kind in ("preserved", "column"):
        return RecoveryDecision.PRESERVE_OVERFLOW
    if kind == "anchor" and target == "marker":
        return RecoveryDecision.PRESERVE_OVERFLOW

    # unbreakable token cannot wrap — go straight to shrink/clip
    if reason == OverflowReason.UNBREAKABLE_TOKEN:
        if b.allow_shrink:
            return RecoveryDecision.SHRINK
        if b.allow_clip:
            return RecoveryDecision.CLIP
        return RecoveryDecision.PRESERVE_OVERFLOW

    # marker/toc-title anchor is never truncated either (page_x preservation)
    if kind == "anchor":
        if b.allow_wrap and reason in (OverflowReason.WIDTH, OverflowReason.HEIGHT):
            return RecoveryDecision.WRAP
        if b.allow_shrink:
            return RecoveryDecision.SHRINK
        return RecoveryDecision.PRESERVE_OVERFLOW

    # 7F-5a: TOC title — WRAP → SHRINK → PRESERVE_OVERFLOW, never CLIP
    # (page_x / page_number / title_x are immovable; the leader only shrinks).
    if kind in ("toc_title", "toc"):
        if reason == OverflowReason.UNBREAKABLE_TOKEN:
            return RecoveryDecision.SHRINK if b.allow_shrink else RecoveryDecision.PRESERVE_OVERFLOW
        if b.allow_wrap and reason in (OverflowReason.WIDTH, OverflowReason.HEIGHT):
            return RecoveryDecision.WRAP
        if b.allow_shrink:
            return RecoveryDecision.SHRINK
        return RecoveryDecision.PRESERVE_OVERFLOW

    # flow / continuation ladder
    if b.allow_wrap and reason in (OverflowReason.WIDTH, OverflowReason.HEIGHT):
        return RecoveryDecision.WRAP
    if b.allow_shrink:
        return RecoveryDecision.SHRINK
    if b.allow_clip:
        return RecoveryDecision.CLIP
    return RecoveryDecision.PRESERVE_OVERFLOW


# ---------------------------------------------------------------------------
# diagnose_overflow — one entry point: result -> (reason, decision)
# ---------------------------------------------------------------------------


def diagnose_overflow(
    result,
    *,
    avail_width: float = 0.0,
    avail_height: float = 0.0,
    budget: LayoutBudget | None = None,
    target: str | None = None,
    original_font_size: float | None = None,
) -> OverflowDiagnosis:
    """Combine :func:`classify_reason` + :func:`decide_recovery` into one
    :class:`OverflowDiagnosis` record.

    Args:
        result: the :class:`LayoutResult` from ``lay_out`` (``primitive_kind`` /
            ``line_widths`` / ``lines`` / ``font_size`` / ``text``).
        avail_width / avail_height: the available box for this run.
        budget: per-primitive recovery budget (defaults by ``kind``).
        target: ``"marker"`` for list markers; ``None`` otherwise.
        original_font_size: the pre-shrink font size, for the log message
            (``effective_font_size`` comes from ``result.font_size``).

    Returns:
        :class:`OverflowDiagnosis`.  Never raises.
    """
    kind = getattr(result, "primitive_kind", "flow")
    reason = classify_reason(
        result, avail_width=avail_width, avail_height=avail_height
    )
    decision = decide_recovery(kind, reason, budget=budget, target=target)

    widths = getattr(result, "line_widths", None) or []
    max_w = max(widths) if widths else 0.0
    ratio = round(max_w / avail_width, 3) if avail_width > 0.0 and max_w else None
    eff = getattr(result, "font_size", None)
    eff = float(eff) if eff else original_font_size

    lines = getattr(result, "lines", None) or []
    orig = float(original_font_size) if original_font_size else 0.0
    extra = max(0, len(lines) - 1) if lines else 0

    if decision is RecoveryDecision.WRAP and extra:
        detail = f"wrap +{extra} line{'s' if extra > 1 else ''}"
    elif decision is RecoveryDecision.SHRINK and eff and orig > eff:
        detail = f"shrink {orig:g}->{eff:g}"
    else:
        detail = decision.value
    message = f"{reason.value} overflow -> {detail}"

    return OverflowDiagnosis(
        reason=reason,
        decision=decision,
        primitive_kind=kind,
        measure_ratio=ratio,
        effective_font_size=eff,
        extra_lines=extra,
        message=message,
    )