"""Invariant engine — rules consume flight-recorder events.

The 7N-8 machine audit (``doc/7n8_mp2e_audit.py``) worked on dumped
plan/mono artifacts; every check had to *guess* what a number meant.  With a
flight-recorder trace each number already declares its semantics
(``Coord.meaning``), so these rules are pure ``Rule → trace fields →
predicate → severity → action`` — they run over **every real translation**,
not over hand-written fixtures.

Severity / grading mirrors 7N-8: HIGH → page D, MEDIUM → page C,
LOW/recovery → B, clean → A.

Beyond single-rule verdicts, ``annotate_first_divergence`` is the per-block
post-pass that answers "which layer first produced the error": across all
FAILs of one ``trace_id``, the earliest pipeline stage is the root cause
(``first_divergence``) and later-stage FAILs are downstream symptoms.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

#: 阈值与 7N-8 审计一致（LARGE_SHIFT_PT / BBOX_JUMP_PT / decoupled 0.5）。
LARGE_SHIFT_PT = 60.0
BBOX_JUMP_PT = 100.0
DECOUPLED_TOL = 0.5
BASELINE_TOL = 0.5

TOKEN_RE = re.compile(r"<b\d+>|</b\d+>|<[a-z]+_\d+>")

#: 严重度 → 页面分级。
SEVERITY_DEFECT = "HIGH"
SEVERITY_SUSPICIOUS = "MEDIUM"
SEVERITY_INFO = "LOW"
GRADE_BY_SEVERITY = {SEVERITY_DEFECT: "D", SEVERITY_SUSPICIOUS: "C", SEVERITY_INFO: "B"}

#: 生产链路阶段顺序 —— first_divergence 的判定依据：同一块多个 FAIL 中，
#: pipeline 顺序最靠前的阶段是根因（first divergence），其后阶段的 FAIL
#: 是同一根因的连锁症状（downstream），避免把一处缺陷在多个阶段重复计数。
#:
#: 已扩展到全生命周期（ingestion 计划 v1.1）：ingest 是新的第一站 —— 解析后端
#: （pdfminer / Marker / MinerU）产出的块若在 ingest 即 FAIL，
#: ``annotate_first_divergence`` 会把它标成 ``first_divergence = ingest`` 而不是
#: 让问题一直落回 render。规则见 pdf2zh/v3/ingestion/rules.py。
PIPELINE_STAGES = [
    "ingest",
    "normalize",
    "translate",
    "plan",
    "fixup",
    "layout",
    "render",
    "erase",
    "raster",
]


@dataclass
class RuleResult:
    """One rule verdict for one block (only FAILs are emitted)."""

    rule: str
    status: str  # "FAIL"
    severity: str
    block_id: str
    page: int
    trace_id: str
    evidence: Dict[str, Any] = field(default_factory=dict)
    action: str = ""
    stage: str = ""
    #: 该块最早 FAIL 的生产阶段（annotate_first_divergence 填充）；
    #: downstream=True 表示本 FAIL 是 first divergence 之后的连锁症状。
    first_divergence: str = ""
    downstream: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule": self.rule,
            "status": self.status,
            "severity": self.severity,
            "page": self.page,
            "block_id": self.block_id,
            "trace_id": self.trace_id,
            "stage": self.stage,
            "first_divergence": self.first_divergence,
            "downstream": self.downstream,
            "action": self.action,
            "evidence": self.evidence,
        }


# ── block assembly ──────────────────────────────────────────────────────


def group_by_block(events: Sequence[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """``trace_id → [events]`` (all stages, one block)."""
    out: Dict[str, List[Dict[str, Any]]] = {}
    for ev in events:
        pno = int(ev.get("page") or -1)
        bid = ev.get("block_id") or "?"
        if bid == "*":
            continue
        tid = ev.get("trace_id") or f"{pno}/{bid}"
        out.setdefault(tid, []).append(ev)
    return out


def _first(events: List[Dict[str, Any]], *names: str) -> Optional[Dict[str, Any]]:
    for ev in events:
        if ev.get("event") in names:
            return ev
    return None


def _payload(ev: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return (ev or {}).get("payload") or {}


def _num(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _block_record(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """One block's assembled facts (plan + render + raster views)."""
    plan = _first(events, "plan.flow", "plan.block")
    fix = _first(
        events, "plan.shift_down", "plan.keep", "plan.preserve", "plan.overflowed"
    )
    rnd = _first(events, "render.flow", "render.block", "render.wrapped")
    er = _first(events, "render.erase")
    rast = _first(events, "raster.ink")
    pp = _payload(plan)
    fp = _payload(fix)
    rp = _payload(rnd)
    return {
        "page": int(events[0].get("page") or -1),
        "block_id": events[0].get("block_id"),
        "kind": pp.get("kind") or rp.get("kind"),
        "text": pp.get("text") or "",
        "translated": pp.get("translated") or "",
        "render_path": pp.get("render_path") or fp.get("render_path"),
        "fixup": fp.get("fixup") or pp.get("render_fixup"),
        "src_box": pp.get("src_box") or fp.get("src_box"),
        "dst_box": fp.get("dst_box") or pp.get("dst_box") or pp.get("src_box"),
        "font_size": pp.get("font_size"),
        "recovery": pp.get("recovery") or {},
        "rp_overflow": pp.get("overflow"),
        "layout_ok": pp.get("layout_ok"),
        "lines": pp.get("lines") or [],
        "commands": pp.get("commands") or [],
        "first_cmd_y": fp.get("first_cmd_y"),
        "render": rp,
        "erase": _payload(er)
        or ({"erase_rect": rp["erase_rect"]} if rp.get("erase_rect") else {}),
        "raster": _payload(rast),
    }


# ── rule implementations ────────────────────────────────────────────────


def _fail(rule, severity, rec, evidence, action="", stage=""):
    return RuleResult(
        rule=rule,
        status="FAIL",
        severity=severity,
        block_id=rec["block_id"],
        page=rec["page"],
        trace_id=f"{rec['page']}/{rec['block_id']}",
        evidence=evidence,
        action=action,
        stage=stage,
    )


def rule_token_leak(rec) -> Optional[RuleResult]:
    if TOKEN_RE.findall(rec["translated"] or ""):
        return _fail(
            "TOKEN_LEAK",
            SEVERITY_SUSPICIOUS,
            rec,
            {"translated": (rec["translated"] or "")[:60]},
            action="translation",
            stage="plan",
        )
    return None


def rule_empty_translation(rec) -> Optional[RuleResult]:
    text = (rec["text"] or "").strip()
    translated = (rec["translated"] or "").strip()
    path = rec["render_path"]
    if text and not translated and path in ("translate_refit", "shift_down"):
        return _fail(
            "EMPTY_TRANSLATION",
            SEVERITY_DEFECT,
            rec,
            {"text": text[:60]},
            action="translation",
            stage="plan",
        )
    return None


def rule_clip_readability(rec) -> Optional[RuleResult]:
    recov = rec["recovery"] or {}
    if recov.get("decision") == "clip":
        return _fail(
            "CLIP_READABILITY",
            SEVERITY_SUSPICIOUS,
            rec,
            {
                "decision": "clip",
                "steps": recov.get("steps") or [],
                "final_font": recov.get("final_font_size"),
            },
            action="FIX-1",
            stage="layout",
        )
    return None


def rule_one_line_collapse(rec) -> Optional[RuleResult]:
    steps = rec["recovery"].get("steps") or []
    if "SHRINK" not in steps:
        return None
    final_lines = len(rec["lines"] or [])
    src_lines = max(1, len((rec["text"] or "").splitlines()))
    if final_lines <= 1 and src_lines >= 3 and len((rec["translated"] or "")) >= 20:
        return _fail(
            "ONE_LINE_COLLAPSE",
            SEVERITY_SUSPICIOUS,
            rec,
            {"final_lines": final_lines, "src_lines": src_lines},
            action="FIX-1",
            stage="layout",
        )
    return None


def rule_residual_overflow(rec) -> Optional[RuleResult]:
    if rec["rp_overflow"] is True and (rec["recovery"] or {}).get("decision") in (
        "clip",
        None,
    ):
        return _fail(
            "RESIDUAL_OVERFLOW",
            SEVERITY_SUSPICIOUS,
            rec,
            {"overflow": True, "decision": (rec["recovery"] or {}).get("decision")},
            action="FIX-1",
            stage="layout",
        )
    return None


def rule_shift_direction(rec) -> Optional[RuleResult]:
    """7N-FIX-3: v3 y-up — "shift down" must be −Δy.  +Δy puts the box onto
    the line above (MECH-4: 153/153 old shift blocks moved UP)."""
    if rec["fixup"] != "shift_down":
        return None
    src = rec["src_box"]
    dst = rec["dst_box"]
    if not src or not dst or len(src) != 4 or len(dst) != 4:
        return None
    dy = _num(dst[3]) - _num(src[3])
    if dy >= -1e-6:
        return _fail(
            "SHIFT_DIRECTION",
            SEVERITY_DEFECT,
            rec,
            {
                "src_y1": src[3],
                "dst_y1": dst[3],
                "delta_y": round(dy, 2),
                "space": "v3",
            },
            action="FIX-3",
            stage="plan",
        )
    if abs(dy) > LARGE_SHIFT_PT:
        return _fail(
            "LARGE_SHIFT",
            SEVERITY_SUSPICIOUS,
            rec,
            {"delta_y": round(dy, 2), "threshold": LARGE_SHIFT_PT},
            action="investigate",
            stage="plan",
        )
    return None


def rule_decoupled(rec) -> Optional[RuleResult]:
    """MECH-2 / FIX-2: after shift_down, first_cmd_y == dst_box.y1.

    Prefers the fixup event's ``first_cmd_y`` (the **post-fixup** co-shifted
    command y — authoritative); falls back to the plan.flow commands (the
    pre-fixup view) when the fixup declared none, which surfaces a shift
    that failed to co-shift its payload commands.
    """
    if rec["fixup"] != "shift_down":
        return None
    dst = rec["dst_box"] or []
    if len(dst) != 4:
        return None
    first_y = rec.get("first_cmd_y")
    if first_y is None:
        cmds = rec["commands"] or []
        if not cmds:
            return None
        first_y = _num(cmds[0].get("y"))
    if abs(_num(first_y) - _num(dst[3])) > DECOUPLED_TOL:
        return _fail(
            "DECOUPLED",
            SEVERITY_DEFECT,
            rec,
            {
                "first_cmd_y": round(_num(first_y), 2),
                "dst_box_y1": round(_num(dst[3]), 2),
            },
            action="FIX-2",
            stage="plan",
        )
    return None


def rule_bbox_anomaly(rec) -> Optional[RuleResult]:
    if rec["fixup"] == "shift_down":
        return None  # shift 块由 SHIFT_DIRECTION/LARGE_SHIFT 负责
    src = rec["src_box"]
    dst = rec["dst_box"]
    if not src or not dst or len(src) != 4 or len(dst) != 4:
        return None
    jump = abs(_num(dst[3]) - _num(src[3]))
    if jump > BBOX_JUMP_PT:
        return _fail(
            "BBOX_ANOMALY",
            SEVERITY_SUSPICIOUS,
            rec,
            {"src_y1": src[3], "dst_y1": dst[3], "jump": round(jump, 2)},
            action="investigate",
            stage="plan",
        )
    bw = _num(dst[2]) - _num(dst[0])
    bh = _num(dst[3]) - _num(dst[1])
    if bw <= 1.0 or bh < 2.0:
        return _fail(
            "SUSPICIOUS_BBOX",
            SEVERITY_INFO,
            rec,
            {"dst_box": dst},
            action="investigate",
            stage="plan",
        )
    return None


def rule_flow_baseline_semantics(rec) -> Optional[RuleResult]:
    """MECH-4 / FIX-3A — the invariant that catches a renderer regressing to
    "baseline == box top" on ANY real run.

    The plan declares the flow command y meaning ("box_top"); the render
    event must agree and must place the baseline at box_top + 0.85*fs.
    """
    rnd = rec["render"]
    if not rnd or not rnd.get("commands"):
        return None
    first = rnd["commands"][0]
    y_meaning = first.get("y_meaning") or (first.get("y") or {}).get("meaning")
    if y_meaning not in ("box_top", "top", "box_top_anchor"):
        return _fail(
            "FLOW_BASELINE_SEMANTICS",
            SEVERITY_DEFECT,
            rec,
            {"y_meaning": y_meaning, "command_y": first.get("y")},
            action="FIX-3",
            stage="render",
        )
    # actual（renderer 实际使用）vs expected（独立按 box_top → +0.85em 推导）
    baseline = first.get("actual_baseline")
    expected = first.get("expected_baseline")
    if baseline is None and rnd.get("baseline") is not None:
        baseline = rnd.get("baseline")
    if expected is None and rnd.get("expected_baseline") is not None:
        expected = rnd.get("expected_baseline")
    if baseline is not None and expected is not None:
        diff = abs(_num(baseline) - _num(expected))
        if diff > BASELINE_TOL:
            return _fail(
                "FLOW_BASELINE_MISMATCH",
                SEVERITY_DEFECT,
                rec,
                {
                    "baseline": round(_num(baseline), 2),
                    "expected_baseline": round(_num(expected), 2),
                    "delta": round(diff, 2),
                },
                action="FIX-3",
                stage="render",
            )
    return None


def rule_erase_geometry(rec) -> Optional[RuleResult]:
    """MECH-4 / FIX-3B — the white erase rect must cover the SOURCE geometry,
    never the (possibly shifted) dst_box (which would wipe out neighbours)."""
    er = rec["erase"]
    if not er:
        return None
    src = rec["src_box"] or []
    dst = rec["dst_box"] or []
    erase = er.get("erase_rect") or []
    if len(erase) != 4:
        return None
    erase_is_src = len(src) == 4 and all(
        abs(_num(a) - _num(b)) <= 1.0 for a, b in zip(erase, src)
    )
    erase_is_dst = len(dst) == 4 and all(
        abs(_num(a) - _num(b)) <= 1.0 for a, b in zip(erase, dst)
    )
    shifted = len(src) == 4 and len(dst) == 4 and abs(_num(dst[3]) - _num(src[3])) > 0.5
    if shifted and erase_is_dst and not erase_is_src:
        return _fail(
            "ERASE_GEOMETRY",
            SEVERITY_DEFECT,
            rec,
            {"erase_rect": erase, "src_box": src, "dst_box": dst, "shifted": True},
            action="FIX-3",
            stage="render",
        )
    return None


def rule_ink_overlap(rec) -> Optional[RuleResult]:
    """Level-2 raster rule: translation ink vs foreign ink overlap > 10%
    (threshold from 7N-8 8B visual_overlap)."""
    rast = rec["raster"]
    if not rast:
        return None
    ov = rast.get("foreign_overlap_pct")
    if ov is None:
        return None
    if _num(ov) > 10.0:
        return _fail(
            "INK_OVERLAP",
            SEVERITY_DEFECT,
            rec,
            {
                "foreign_overlap_pct": round(_num(ov), 1),
                "ink_bbox": rast.get("ink_bbox"),
            },
            action="FIX-3/FIX-1",
            stage="raster",
        )
    return None


def _stage_order(stage: str) -> int:
    """Pipeline 顺序下标；未知阶段排在已知之后（防御：规则 stage 漂移）。"""
    return (
        PIPELINE_STAGES.index(stage)
        if stage in PIPELINE_STAGES
        else len(PIPELINE_STAGES)
    )


def annotate_first_divergence(results: Sequence[RuleResult]) -> Dict[str, str]:
    """Per block: 标定 first divergence（根因阶段）并标记下游症状。

    对每个 ``trace_id``，把该块全部 FAIL 按 pipeline 顺序排序，最早阶段即
    根因；所有结果写入 ``first_divergence``，晚于根因阶段的结果标记
    ``downstream=True``。返回 ``{trace_id: first_divergence_stage}``。

    必须在**合并完整结果集之后**调用（不能在单次 ``run_rules`` 内完成）：
    Level-2 光栅事实（``raster.ink``）在 trace_audit 中第二批复跑规则，
    若在第一次执行时标注，INK_OVERLAP 会被误标成自己的根因。
    """
    by_block: Dict[str, List[RuleResult]] = {}
    for r in results:
        by_block.setdefault(r.trace_id, []).append(r)
    first: Dict[str, str] = {}
    for tid, rs in by_block.items():
        rs.sort(key=lambda r: _stage_order(r.stage))
        first_stage = rs[0].stage
        first_order = _stage_order(first_stage)
        first[tid] = first_stage
        for r in rs:
            r.first_divergence = first_stage
            r.downstream = _stage_order(r.stage) > first_order
    return first


#: 规则执行顺序（稳定输出）。
ALL_RULES = [
    rule_token_leak,
    rule_empty_translation,
    rule_clip_readability,
    rule_one_line_collapse,
    rule_residual_overflow,
    rule_shift_direction,
    rule_decoupled,
    rule_bbox_anomaly,
    rule_flow_baseline_semantics,
    rule_erase_geometry,
    rule_ink_overlap,
]


def run_rules(events: Sequence[Dict[str, Any]]) -> List[RuleResult]:
    """Run every rule over every traced block; FAIL-only results."""
    results: List[RuleResult] = []
    for tid, evs in sorted(group_by_block(events).items()):
        rec = _block_record(evs)
        for rule in ALL_RULES:
            try:
                res = rule(rec)
            except Exception:  # noqa: BLE001 -- a broken rule never kills the audit
                res = None
            if res is not None:
                results.append(res)
    return results


def grade_pages(
    results: Sequence[RuleResult], pages: Optional[Sequence[int]] = None
) -> Dict[int, str]:
    """Page A/B/C/D: any HIGH → D, else any MEDIUM → C, else any LOW → B."""
    worst: Dict[int, str] = {}
    for r in results:
        g = GRADE_BY_SEVERITY.get(r.severity, "A")
        cur = worst.get(r.page, "A")
        order = {"A": 0, "B": 1, "C": 2, "D": 3}
        if order.get(g, 0) > order.get(cur, 0):
            worst[r.page] = g
    return worst


__all__ = [
    "RuleResult",
    "group_by_block",
    "run_rules",
    "grade_pages",
    "annotate_first_divergence",
    "PIPELINE_STAGES",
    "ALL_RULES",
]
