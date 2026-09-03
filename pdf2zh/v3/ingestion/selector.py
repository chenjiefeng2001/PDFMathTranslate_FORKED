"""Ingestion selector — decide which PDF-understanding backend serves a file.

Production goal: not "the system used Marker" but "the system can explain why
it decided to use Marker".  :func:`decide` turns the requested backend
(``auto`` / ``mineru`` / ``marker``) plus the primary run's quality evidence
into one :class:`IngestionDecision`; callers persist it with
``ingest.select`` (``base.emit_ingest_selection``) so ``trace_audit explain``
answers "why was Marker selected?" from the trace alone — no guessing.

Quality-gate principle (same as ``ingestion/rules``): only **canonical ingest
invariants** gate the fallback (geometry declared / normalized into v3 ...).
Per-backend quality heuristics (Marker block-count > N, table confidence <
0.8, ...) are backend policy and never live here or in ``trace_rules``.

``decide`` is pure and deliberately tiny: the caller runs the primary backend,
feeds its events to :func:`gate_quality`, and asks for the next step.  After
a fallback run completes, the caller may update ``decision.quality`` with the
selected backend's own gate result before emitting ``ingest.select``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from pdf2zh.v3.ingestion.base import BACKEND_MARKER, BACKEND_MINERU
from pdf2zh.v3.ingestion.rules import run_ingest_rules
from pdf2zh.v3.trace_rules import SEVERITY_DEFECT

#: 请求语义：``mineru``/``marker`` 强制指定；``auto`` = selector 决策。
REQUEST_AUTO = "auto"
REQUEST_MINERU = BACKEND_MINERU
REQUEST_MARKER = BACKEND_MARKER
REQUEST_CHOICES = (REQUEST_AUTO, REQUEST_MINERU, REQUEST_MARKER)

#: 候选顺序（primary 在前）—— 回退总是尝试下一个候选。
DEFAULT_CANDIDATES = (BACKEND_MINERU, BACKEND_MARKER)

#: gate 结果。
QUALITY_PASS = "PASS"
QUALITY_FAIL = "FAIL"

#: decision reason 码（写进 trace 的 ``ingest.select`` / audit explain）。
REASON_FORCED = "forced_backend"
REASON_PRIMARY_OK = "primary_ingest_pass"
REASON_PRIMARY_QUALITY_FAIL = "primary_ingest_quality_fail"
REASON_PRIMARY_PARSE_FAIL = "primary_ingest_parse_fail"
REASON_FALLBACK_UNAVAILABLE = "primary_failed_no_fallback"
REASON_FALLBACK_RUN_FAILED = "fallback_ingest_failed"


def normalize_requested(raw: Optional[str]) -> str:
    """``raw`` → one of auto/mineru/marker (unknown → auto)."""
    value = (raw or "").strip().lower()
    return value if value in REQUEST_CHOICES else REQUEST_AUTO


@dataclass
class IngestionDecision:
    """Why a backend was (or was not) selected — recorded as ``ingest.select``.

    ``quality`` is the *selected* backend's own gate result; before the
    selected run completes it mirrors the primary quality that triggered the
    fallback.  ``failed_rules`` names every canonical invariant the primary
    run broke (the answer to "why fall back?").

    ``fallback_attempted`` / ``fallback_succeeded`` separate the *decision*
    from the *run outcome* so telemetry never confuses a fallback attempt
    with a fallback success: ``decide`` sets ``fallback_attempted=True``
    when it selects a non-primary candidate; the caller updates
    ``fallback_succeeded`` after the selected run finishes (and flips
    ``selected_backend`` back to the primary + ``reason=fallback_ingest_failed``
    when the fallback run itself failed).
    """

    requested_backend: str
    selected_backend: str
    candidates: List[str] = field(default_factory=list)
    fallback: bool = False
    fallback_attempted: bool = False
    fallback_succeeded: bool = False
    reason: str = ""
    quality: str = QUALITY_PASS
    primary_backend: str = ""
    failed_rules: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "requested_backend": self.requested_backend,
            "selected_backend": self.selected_backend,
            "candidates": list(self.candidates),
            "fallback": bool(self.fallback),
            "fallback_attempted": bool(self.fallback_attempted),
            "fallback_succeeded": bool(self.fallback_succeeded),
            "reason": self.reason,
            "quality": self.quality,
            "primary_backend": self.primary_backend,
            "failed_rules": list(self.failed_rules),
        }


@dataclass
class GateResult:
    """Quality gate output over one backend run's ``ingest.*`` events."""

    quality: str = QUALITY_PASS
    failed_rules: List[str] = field(default_factory=list)
    by_rule: Dict[str, str] = field(default_factory=dict)  # rule -> severity

    def to_dict(self) -> Dict[str, Any]:
        return {
            "quality": self.quality,
            "failed_rules": list(self.failed_rules),
            "by_rule": dict(self.by_rule),
        }


def gate_quality(events: Sequence[Dict[str, Any]]) -> GateResult:
    """Run the canonical ingest invariants over ``ingest.block`` events.

    FAIL only on a HIGH (defect) verdict: geometry lost at the ingestion
    boundary poisons every downstream stage.  MEDIUM/LOW verdicts still show
    up in ``failed_rules`` / ``by_rule`` as decision evidence but do not by
    themselves force a fallback.  Empty/absent events → PASS (no evidence of
    an ingest defect is not a defect).
    """
    fails = run_ingest_rules(events)
    failed: List[str] = []
    by_rule: Dict[str, str] = {}
    for r in fails:
        by_rule[r.rule] = r.severity
        if r.rule not in failed:
            failed.append(r.rule)
    quality = (
        QUALITY_FAIL
        if any(r.severity == SEVERITY_DEFECT for r in fails)
        else QUALITY_PASS
    )
    return GateResult(quality=quality, failed_rules=failed, by_rule=by_rule)


def decide(
    requested: str,
    *,
    primary: str = BACKEND_MINERU,
    primary_quality: str = QUALITY_PASS,
    primary_failed_rules: Sequence[str] = (),
    fallback_available: bool = False,
    candidates: Sequence[str] = DEFAULT_CANDIDATES,
) -> IngestionDecision:
    """Pure decision: which backend should serve this file?

    - forced (``mineru``/``marker``): that backend is selected, never
      fallback (``reason=forced_backend``);
    - ``auto`` + primary PASS: primary is selected
      (``reason=primary_ingest_pass``);
    - ``auto`` + primary FAIL + fallback available: first candidate after the
      primary is selected (``fallback=True``,
      ``reason=primary_ingest_quality_fail``, ``failed_rules`` carried);
    - ``auto`` + primary FAIL + no fallback: primary still selected
      (``reason=primary_failed_no_fallback``) — the gate result stays visible
      in the trace so the failure is observable, not silent.

    ``fallback_succeeded`` is left to the caller: run the selected fallback,
    then overwrite ``decision.quality`` with its own ``gate_quality`` result
    and set ``fallback_succeeded=True``; if the fallback run itself failed,
    flip ``selected_backend`` back to the primary with
    ``reason=fallback_ingest_failed`` (the attempt stays
    ``fallback_attempted=True`` so telemetry counts attempts and successes
    separately).
    """
    req = normalize_requested(requested)
    cands = [c for c in (list(candidates) or DEFAULT_CANDIDATES)]
    failed = list(primary_failed_rules or [])

    if req == REQUEST_AUTO:
        if primary_quality == QUALITY_FAIL and fallback_available:
            for backend in cands:
                if backend != primary:
                    return IngestionDecision(
                        requested_backend=req,
                        selected_backend=backend,
                        candidates=cands,
                        fallback=True,
                        fallback_attempted=True,
                        reason=REASON_PRIMARY_QUALITY_FAIL,
                        quality=primary_quality,
                        primary_backend=primary,
                        failed_rules=failed,
                    )
        return IngestionDecision(
            requested_backend=req,
            selected_backend=(
                primary if primary in cands else (cands[0] if cands else primary)
            ),
            candidates=cands,
            fallback=False,
            reason=(
                REASON_PRIMARY_OK
                if primary_quality == QUALITY_PASS
                else REASON_FALLBACK_UNAVAILABLE
            ),
            quality=primary_quality,
            primary_backend=primary,
            failed_rules=failed,
        )
    # forced backend
    selected = (
        req
        if req in cands
        else (primary if primary in cands else (cands[0] if cands else req))
    )
    return IngestionDecision(
        requested_backend=req,
        selected_backend=selected,
        candidates=cands,
        fallback=False,
        reason=REASON_FORCED,
        quality=primary_quality,
        primary_backend=primary,
        failed_rules=failed,
    )


__all__ = [
    "REQUEST_AUTO",
    "REQUEST_MINERU",
    "REQUEST_MARKER",
    "REQUEST_CHOICES",
    "DEFAULT_CANDIDATES",
    "QUALITY_PASS",
    "QUALITY_FAIL",
    "REASON_FORCED",
    "REASON_PRIMARY_OK",
    "REASON_PRIMARY_QUALITY_FAIL",
    "REASON_PRIMARY_PARSE_FAIL",
    "REASON_FALLBACK_UNAVAILABLE",
    "REASON_FALLBACK_RUN_FAILED",
    "IngestionDecision",
    "GateResult",
    "normalize_requested",
    "gate_quality",
    "decide",
]
