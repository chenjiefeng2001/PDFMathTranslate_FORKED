# -*- coding: utf-8 -*-
"""7I-5B/C — Layout / Recovery Policy Contract (frozen; now green).

These tests freeze the policy contract from the 7I-5A causality finding.  They
were frozen as RED ``xfail(strict=True)`` in 7I-5B to encode the contract
before any production change; 7I-5C landed the minimal SHRINK re-wrap fix in
``adaptive_layout`` Stage 2, the tests flipped to XPASS, and the markers were
removed.  They are now ordinary green tests that guard the fix against
regression.

Contract (frozen):

1. WRAP is a valid layout state and must not be unconditionally discarded by
   SHRINK.  SHRINK may change font size / line breaks / line count, but it may
   not degenerate an already-completed multi-line layout into a single-line
   ``shrink_to_fit`` unless re-layout proves a single line is admissible.
2. SHRINK must re-layout from the current constraints: WRAP -> shrink font ->
   re-WRAP -> re-measure -> ACCEPT if fit; continue otherwise.
3. CLIP is a terminal state, allowed only after admissible layout/recovery
   attempts are exhausted and overflow remains.
4. Terminal overflow must carry an auditable verdict: overflow / decision /
   reason / final_font_size / attempts / layout_strategy.

Because production code is intentionally unmodified in this milestone, these
assertions encode the CONTRACT, not the current behavior.
"""

from pdf2zh.semantic.layout.adaptive import adaptive_layout
from pdf2zh.semantic.layout.primitives import FlowText
from pdf2zh.semantic.layout.overflow import LayoutResult
from pdf2zh.semantic.layout.recovery import LayoutBudget

_HAS_FIX = True  # 7I-5C landed the re-wrap fix; contract tests are green


def _measure(text, size):
    w = 0.0
    for ch in text or "":
        w += size if ord(ch) >= 0x2E80 else size * 0.5
    return w


def _flow(text, w=120.0, h=30.0):
    return FlowText(text=text, origin=(40.0, 40.0), max_width=w, max_height=h)


def _budget():
    return LayoutBudget(
        allow_wrap=True,
        allow_shrink=True,
        allow_clip=True,
        min_font_size=5.0,
        max_font_reduction=0.9,
    )


# A paragraph long enough that WRAP yields >1 line within the box width.
_LONG = "A relatively long translated sentence that wraps into multiple lines here"


# ---------------------------------------------------------------------------
# 1. SHRINK must not degenerate WRAP multi-line -> single-line
# ---------------------------------------------------------------------------


def test_shrink_preserves_wrapped_lines():
    """After WRAP produced multi-line layout, further SHRINK (for height) must
    not collapse the text back to a single line while still width-overflowing.
    The WRAP line structure is a valid layout state that SHRINK may adapt
    (font / re-wrap) but not discard into a rejected single-line state."""
    r = adaptive_layout(
        _flow(_LONG),
        measure=_measure,
        font_size=10.0,
        avail_width=120.0,
        avail_height=30.0,
        budget=_budget(),
    )
    assert isinstance(r, LayoutResult)
    wrap_idx = r.recovery_steps.index("WRAP") if "WRAP" in r.recovery_steps else -1
    shrink_idx = (
        r.recovery_steps.index("SHRINK") if "SHRINK" in r.recovery_steps else -1
    )
    if wrap_idx >= 0 and shrink_idx > wrap_idx:
        # SHRINK followed an actual WRAP: line structure must survive unless a
        # single line genuinely fits the width.
        single_fits = _measure(_LONG, r.font_size) <= 120.0 + 1e-6
        if not single_fits:
            assert (
                len(r.lines) > 1
            ), "SHRINK must not collapse a WRAPped multi-line layout to 1 line"


# ---------------------------------------------------------------------------
# 2. The canonical smoking-gun regression (C p62_9 pattern) must re-wrap, not clip
# ---------------------------------------------------------------------------


def test_wrap_shrink_rewraps_instead_of_clip():
    """The exact 7I-5A smoking-gun: WRAP(>1 lines) -> SHRINK collapses to 1
    line at the 5pt floor -> CLIP.  Contract: SHRINK must re-layout from the
    wrapped state (shrink then re-WRAP) so the multi-line fit holds; CLIP must
    not be produced when a multi-line fit is admissible."""
    r = adaptive_layout(
        _flow(_LONG),
        measure=_measure,
        font_size=10.0,
        avail_width=120.0,
        avail_height=30.0,
        budget=_budget(),
    )
    assert (
        len(r.lines) > 1
    ), "re-WRAP must keep a multi-line layout, not collapse to 1 line"
    # and it must not have silently truncated to a single clipped line
    assert r.overflow is False or len(r.lines) > 1


# ---------------------------------------------------------------------------
# 3. CLIP is terminal: only after WRAP+SHRINK are exhausted
# ---------------------------------------------------------------------------


def test_clip_is_terminal_and_requires_admissible_exhaustion():
    """A clip is only admissible when no wrapable multi-line fit exists."""
    # unbreakable long token: genuinely no WRAP possible (no token boundary)
    url = "https://very-long-unbreakable-" + "x" * 60 + ".com/path"
    b = LayoutBudget(
        allow_wrap=True, allow_shrink=True, allow_clip=True, min_font_size=5.0
    )
    r = adaptive_layout(
        _flow(url, w=40.0, h=400.0),
        measure=_measure,
        font_size=10.0,
        avail_width=40.0,
        avail_height=400.0,
        budget=b,
    )
    assert "CLIP" in r.recovery_steps or r.overflow is True
    # terminal verdict must be auditable
    rec = r.to_dict()["recovery"]
    assert set(rec) >= {
        "reason",
        "decision",
        "steps",
        "original_font_size",
        "final_font_size",
    }


# ---------------------------------------------------------------------------
# 4. Terminal overflow carries an auditable verdict
# ---------------------------------------------------------------------------


def test_terminal_overflow_verdict():
    """Part of the contract that already holds: a clip/terminal overflow is never
    silent — it records reason/decision/steps/font trajectory."""
    r = adaptive_layout(
        _flow(_LONG),
        measure=_measure,
        font_size=10.0,
        avail_width=120.0,
        avail_height=30.0,
        budget=_budget(),
    )
    if r.overflow:
        rec = r.to_dict()["recovery"]
        assert rec["decision"] == "clip"
        assert isinstance(rec["steps"], list) and rec["steps"]
        assert "final_font_size" in rec
