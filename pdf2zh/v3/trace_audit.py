"""Trace audit — qualification from a flight-recorder trace.

``audit trace events.jsonl`` or ``audit run --pdf out.pdf --trace t.jsonl``
runs the invariant rules (``trace_rules``) over the recorded events and
writes:

    audit/
    ├── summary.json        run/rule/grading/qualification summary
    ├── defect-ledger.csv   machine-generated ledger (trace + rules)
    ├── pages.json          per-page A/B/C/D grades
    ├── trace-index.json    per-block stage traversal index
    ├── qualification.md    human-readable report
    └── crops/              Level-2 raster evidence for FAIL blocks

With ``--pdf`` the auditor additionally performs Level-2 raster evidence on
flagged blocks: locates the translation ink near the command site on the
rendered PDF, measures overlap against foreign ink, emits ``raster.ink``
facts, and crops the page region around each defect — the trace's causal
chain is extended with the actual ink observation.

Every FAIL block is annotated with its **first divergence** — the earliest
pipeline stage (plan → fixup → layout → render → erase → raster) where a
rule failed, the root cause; rule FAILs at later stages on the same block
are marked as downstream symptoms.  ``summary.json`` / ``defect-ledger.csv``
carry the annotation, and ``qualification.md`` renders a per-block stage
tree, so an unfamiliar book needs no pre-known bug list: the audit answers
"which layer first produced the error" on its own.

``explain`` turns that annotation into a one-command diagnosis for a single
block (``trace_audit explain trace/events.jsonl 442/p442_4``): root-cause
stage → responsible module → violated invariant → fix action → downstream
symptoms → evidence, all derived from the recorded trace without re-running
the translation.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
from typing import Any, Dict, List, Optional, Sequence

from pdf2zh.v3.flight_recorder import build_trace_index, read_events, write_trace_index
from pdf2zh.v3.ingestion.rules import run_ingest_rules
from pdf2zh.v3.trace_rules import (
    PIPELINE_STAGES,
    RuleResult,
    annotate_first_divergence,
    grade_pages,
    group_by_block,
    run_rules,
)

_SEVERITY_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}


def _rule_results(events: Sequence[Dict[str, Any]]) -> List[RuleResult]:
    """plan/render/raster rules **and** ingestion invariants, one FAIL stream.

    Ingest rules (``INGEST_GEOMETRY_DECLARED`` / ``MARKER_GEOMETRY_NORMALIZED``
    in ``ingestion/rules``) consume the ``ingest.*`` events a backend emits
    when it runs; merging them here means a parser that lost coordinates is
    qualified at the ingest layer (``first_divergence = ingest``) instead of
    silently poisoning later stages.  Both sets emit the shared
    :class:`RuleResult`, so the merged list feeds
    ``annotate_first_divergence`` / ``grade_pages`` / reports unchanged.
    """
    return list(run_rules(events)) + list(run_ingest_rules(events))


def _cjk(s: str) -> bool:
    return any("\u4e00" <= c <= "\u9fff" for c in s)


def _area(bb) -> float:
    return max(0.0, float(bb[2]) - float(bb[0])) * max(0.0, float(bb[3]) - float(bb[1]))


def _inter(a, b) -> float:
    x0 = max(a[0], b[0])
    x1 = min(a[2], b[2])
    y0 = max(a[1], b[1])
    y1 = min(a[3], b[3])
    return max(0.0, x1 - x0) * max(0.0, y1 - y0)


# ── first divergence（根因定位）─────────────────────────────────────────


def _event_layer(ev: Dict[str, Any]) -> str:
    """Event → pipeline layer。fixup 决策事件（plan.shift/keep/preserve/…）
    归到 fixup 层，render.erase 单独成层，其余按 stage 字段兜底。"""
    name = ev.get("event") or ""
    if name == "render.erase":
        return "erase"
    if name.startswith("raster."):
        return "raster"
    if name.startswith("render."):
        return "render"
    if name.startswith(("plan.shift", "plan.keep", "plan.preserve", "plan.overflow")):
        return "fixup"
    if name.startswith("plan."):
        return "plan"
    return ev.get("stage") or ""


def _block_stage_lines(
    events: Sequence[Dict[str, Any]], results: Sequence[RuleResult], tid: str
) -> List[str]:
    """一个 FAIL 块的 pipeline 阶段树（plan → fixup → … → raster）。

    PASS = 该块实际遍历且无 FAIL；FAIL = 规则命中（首个 FAIL 阶段标注
    first divergence，其后阶段标注 downstream symptom）；- = 未遍历。
    """
    present: set = set()
    for ev in events:
        if (ev.get("trace_id") or f"{ev.get('page')}/{ev.get('block_id')}") != tid:
            continue
        layer = _event_layer(ev)
        if layer:
            present.add(layer)
    fails: Dict[str, List[str]] = {}
    for r in results:
        if r.trace_id != tid:
            continue
        fails.setdefault(r.stage, []).append(r.rule)
    first = next(
        (
            r.first_divergence
            for r in results
            if r.trace_id == tid and r.first_divergence
        ),
        None,
    )
    lines = [tid]
    for i, stage in enumerate(PIPELINE_STAGES):
        prefix = " └─ " if i == len(PIPELINE_STAGES) - 1 else " ├─ "
        if stage in fails:
            rules = ", ".join(sorted(set(fails[stage])))
            if stage == first:
                lines.append(f"{prefix}{stage:<8}FAIL  ← first divergence ({rules})")
            else:
                lines.append(f"{prefix}{stage:<8}FAIL  ← downstream symptom ({rules})")
        elif stage in present:
            lines.append(f"{prefix}{stage:<8}PASS")
        else:
            lines.append(f"{prefix}{stage:<8}-")
    return lines


# ── explain（单块诊断：根因阶段 → 模块 → 不变量 → 修复 → 下游症状）──────


#: pipeline 阶段 → 负责模块（first divergence 直接指向该模块）。
_STAGE_MODULE = {
    "ingest": "pdf2zh/v3/ingestion/",
    "normalize": "pdf2zh/v3/document_model.py",
    "translate": "pdf2zh/v3/translator.py",
    "plan": "pdf2zh/v3/document_model.py",
    "fixup": "pdf2zh/v3/render_takeover.py",
    "layout": "pdf2zh/v3/flow_sidechannel.py",
    "render": "pdf2zh/v3/magicpdf_renderer.py",
    "erase": "pdf2zh/v3/magicpdf_renderer.py",
    "raster": "pdf2zh/v3/trace_audit.py",
}


def _explain_details(
    block_events: Sequence[Dict[str, Any]],
) -> List[tuple]:
    """从该块已记录的事件提取各阶段事实（只输出 trace 里真实存在的字段）。"""

    def payload(ev):
        return (ev or {}).get("payload") or {}

    plan = next((e for e in block_events if e.get("event") == "plan.flow"), None)
    fix = next(
        (
            e
            for e in block_events
            if e.get("event", "").startswith("plan.")
            and e.get("event") != "plan.flow"
            and payload(e).get("fixup")
        ),
        None,
    )
    rnd = next((e for e in block_events if e.get("event") == "render.flow"), None)
    wrapped = next(
        (e for e in block_events if e.get("event") == "render.wrapped"), None
    )
    rast = next((e for e in block_events if e.get("event") == "raster.ink"), None)
    out: List[tuple] = []

    if plan:
        pp = payload(plan)
        rows = [
            ("kind", pp.get("kind")),
            ("text", (pp.get("text") or "")[:60]),
            ("translated", (pp.get("translated") or "")[:60]),
            ("render_path", pp.get("render_path")),
            ("src_box", pp.get("src_box")),
            ("dst_box", pp.get("dst_box")),
            ("dst y1 meaning", "box_top (v3 y-up)"),
            ("font_size", pp.get("font_size")),
        ]
        if pp.get("overflow") is not None:
            rows.append(("overflow", pp["overflow"]))
        if pp.get("layout_ok") is not None:
            rows.append(("layout_ok", pp["layout_ok"]))
        recov = pp.get("recovery") or {}
        if recov.get("decision"):
            rows.append(("recovery", recov.get("decision")))
        out.append(("Plan", [(k, v) for k, v in rows if v is not None]))

    if fix:
        fp = payload(fix)
        title = f"Fixup ({fp.get('fixup')})"
        out.append(
            (
                title,
                [
                    (k, v)
                    for k, v in [
                        ("delta_y", fp.get("delta_y")),
                        ("delta_y meaning", fp.get("delta_y_meaning")),
                        ("first_cmd_y", fp.get("first_cmd_y")),
                        ("dst_box", fp.get("dst_box")),
                    ]
                    if v is not None
                ],
            )
        )

    rp = payload(rnd or wrapped)
    if rp:
        cmd0 = (rp.get("commands") or [{}])[0]
        rows = [
            ("y", cmd0.get("y")),
            ("y_meaning", cmd0.get("y_meaning")),
            ("font_size", rp.get("font_size")),
            ("baseline", rp.get("baseline")),
            ("expected_baseline", rp.get("expected_baseline")),
            ("baseline delta", cmd0.get("baseline_delta")),
        ]
        rows = [(k, v) for k, v in rows if v is not None]
        if rp.get("erase_rect") is not None:
            rows.append(("erase_rect (v3)", rp.get("erase_rect")))
            rows.append(("erase_semantics", rp.get("erase_semantics")))
        out.append(("Renderer", rows))

    if rast:
        p = payload(rast)
        rows = [
            ("ink_bbox", p.get("ink_bbox")),
            ("foreign_overlap_pct", p.get("foreign_overlap_pct")),
            ("collides_with", (p.get("collides_with") or "")[:30]),
        ]
        out.append(("Raster", [(k, v) for k, v in rows if v is not None]))

    return out


def explain_block(
    events: Sequence[Dict[str, Any]],
    trace_id: str,
    *,
    pdf: Optional[str] = None,
    source: Optional[str] = None,
    out: Optional[str] = None,
    crop_max: int = 1,
) -> str:
    """One block's full diagnosis.  ``trace_audit explain`` CLI:

    根因阶段 → 负责模块 → 违反的不变量 → 修复动作 → 下游症状 → evidence。
    全部由该块已记录的 production trace 推导，不需要重跑翻译。
    """
    block_events = [
        ev
        for ev in events
        if (ev.get("trace_id") or f"{ev.get('page')}/{ev.get('block_id')}") == trace_id
    ]
    if not block_events:
        n_blocks = len({ev.get("trace_id") for ev in events if ev.get("trace_id")})
        return f"trace_id {trace_id!r} not found (trace has {n_blocks} blocks)"

    results = _rule_results(block_events)
    annotate_first_divergence(results)
    fails = [r for r in results if r.status == "FAIL"]

    lines = [f"Trace:       {trace_id}"]
    if not fails:
        lines.append("Status:      PASS")
        lines.append("")
        lines.extend(_block_stage_lines(events, results, trace_id))
        return "\n".join(lines)

    first_stage = next(
        (r.first_divergence for r in fails if r.first_divergence), fails[0].stage
    )
    first_rules = sorted(
        [r for r in fails if r.stage == first_stage and not r.downstream]
        or [r for r in fails if r.stage == first_stage],
        key=lambda r: (_SEVERITY_ORDER.get(r.severity, 9), r.rule),
    )
    worst = first_rules[0]
    downstream = sorted(
        [r for r in fails if r.downstream],
        key=lambda r: (_SEVERITY_ORDER.get(r.severity, 9), r.stage),
    )

    lines.append("Status:      FAIL")
    lines.append(f"First stage: {first_stage}")
    lines.append(f"Module:      {_STAGE_MODULE.get(first_stage, '?')}")
    lines.append(f"Severity:    {worst.severity}")
    lines.append(f"Rules:       {', '.join(sorted({r.rule for r in first_rules}))}")
    lines.append(f"Fix:         {worst.action or '-'}")
    if downstream:
        lines.append(
            "Downstream:  " + ", ".join(f"{r.rule} ({r.stage})" for r in downstream)
        )
    lines.append("")
    lines.extend(_block_stage_lines(events, results, trace_id))

    for title, rows in _explain_details(block_events):
        if rows:
            lines.append("")
            lines.append(f"{title}:")
            lines.extend(f"  {k:<18}= {v}" for k, v in rows)

    # ── evidence：优先用本次 --pdf 新生成的光栅证据，否则列出已有 crops ──
    pno = int(block_events[0].get("page") or -1)
    bid = block_events[0].get("block_id") or "?"
    crop_dir = os.path.join(out or "audit", "crops")
    crops: List[str] = []
    if pdf and os.path.exists(pdf) and fails:
        raster_evidence_for_blocks(
            fails,
            block_events,
            pdf,
            source_path=source,
            crop_max=crop_max,
            crop_dir=crop_dir,
        )
    if os.path.isdir(crop_dir):
        crops = sorted(
            fn for fn in os.listdir(crop_dir) if fn.startswith(f"p{pno}_{bid}")
        )
    if crops:
        lines.append("")
        lines.append("Evidence:")
        lines.extend(f"  crop        = {os.path.join(crop_dir, fn)}" for fn in crops)
    return "\n".join(lines)


# ── Level-2 raster evidence ─────────────────────────────────────────────


def raster_evidence_for_blocks(
    results: Sequence[RuleResult],
    events: Sequence[Dict[str, Any]],
    mono_path: str,
    *,
    source_path: Optional[str] = None,
    crop_max: int = 8,
    crop_dir: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """For FAIL blocks: locate translation ink on the rendered PDF, measure
    overlap against foreign ink, write crops.  Returns raster.ink facts.

    Command site comes from the recorded ``render.flow`` events (baseline =
    ``(page_h - cmd_y) + 0.85 * font`` — the *declared* semantics, no guessing).
    """
    import pymupdf

    facts: List[Dict[str, Any]] = []
    if not os.path.exists(mono_path):
        return facts
    try:
        doc = pymupdf.open(mono_path)
    except Exception:  # noqa: BLE001
        return facts

    src_doc = None
    if source_path and os.path.exists(source_path):
        try:
            src_doc = pymupdf.open(source_path)
        except Exception:  # noqa: BLE001
            src_doc = None

    # block → render.flow command facts (first command, declared semantics)
    cmd_facts: Dict[str, Dict[str, Any]] = {}
    for ev in events:
        if ev.get("event") != "render.flow":
            continue
        p = ev.get("payload") or {}
        cmds = p.get("commands") or []
        if not cmds:
            continue
        tid = ev.get("trace_id") or f"{ev.get('page')}/{ev.get('block_id')}"
        cmd_facts[tid] = {
            "page": int(ev.get("page") or -1),
            "commands": cmds,
            "payload": p,
        }

    os.makedirs(crop_dir or "", exist_ok=True) if crop_dir else None
    seen = 0
    for r in sorted(results, key=lambda x: _SEVERITY_ORDER.get(x.severity, 9)):
        if seen >= crop_max:
            break
        tid = r.trace_id
        cf = cmd_facts.get(tid)
        if cf is None:
            continue
        pno = cf["page"]
        if pno < 0 or pno >= len(doc):
            continue
        pg = doc[pno]
        h = float(pg.rect.height)
        first = cf["commands"][0]
        y_up = float(first.get("y") or 0.0)
        fs = float(first.get("font_size") or cf["payload"].get("font_size") or 0.0)
        x0 = float(first.get("x") or 0.0)
        baseline = h - y_up + 0.85 * fs

        d = pg.get_text("rawdict")
        spans = []
        for b in d["blocks"]:
            for ln in b.get("lines", []):
                for sp in ln.get("spans", []):
                    txt = "".join(ch["c"] for ch in sp.get("chars", []))
                    if txt.strip():
                        spans.append((txt, tuple(sp["bbox"])))

        ink = None
        for txt, bb in spans:
            if _cjk(txt) and abs(bb[3] - baseline) < 6 and abs(bb[0] - x0) < 25:
                ink = bb
                break
        if ink is None:
            facts.append(
                {
                    "trace_id": tid,
                    "page": pno,
                    "ink_bbox": None,
                    "foreign_overlap_pct": None,
                    "found": False,
                }
            )
            continue

        overlap = 0.0
        collides = None
        for txt, bb in spans:
            if _cjk(txt) or not txt.strip():
                continue
            ov = _inter(ink, bb)
            if ov > 0.10 * _area(ink):
                overlap = max(overlap, ov)
                collides = txt[:30]
        pct = round(100 * overlap / max(1e-6, _area(ink)), 1)
        facts.append(
            {
                "trace_id": tid,
                "page": pno,
                "ink_bbox": [round(v, 1) for v in ink],
                "ink_area": round(_area(ink), 1),
                "foreign_overlap_pct": pct,
                "collides_with": collides,
                "found": True,
            }
        )

        if crop_dir:
            pad = 6.0
            clip = pymupdf.Rect(
                max(0, ink[0] - pad),
                max(0, ink[1] - pad - 4 * fs),
                min(pg.rect.width, ink[2] + pad),
                min(pg.rect.height, ink[3] + pad + 2 * fs),
            )
            try:
                pm = pg.get_pixmap(clip=clip, dpi=150)
                pm.save(os.path.join(crop_dir, f"p{pno}_{r.block_id}_mono.png"))
            except Exception:  # noqa: BLE001
                pass
            if src_doc is not None and pno < len(src_doc):
                try:
                    spm = src_doc[pno].get_pixmap(clip=clip, dpi=150)
                    spm.save(os.path.join(crop_dir, f"p{pno}_{r.block_id}_src.png"))
                except Exception:  # noqa: BLE001
                    pass
        seen += 1

    if src_doc is not None:
        src_doc.close()
    doc.close()
    return facts


# ── report writers ──────────────────────────────────────────────────────


def write_outputs(
    out_dir: str,
    events: Sequence[Dict[str, Any]],
    results: List[RuleResult],
    facts: Sequence[Dict[str, Any]],
    *,
    mono_path: Optional[str] = None,
    trace_path: Optional[str] = None,
) -> Dict[str, Any]:
    os.makedirs(out_dir, exist_ok=True)

    # raster facts join the rule stream (INK_OVERLAP rule re-runs on them)
    from pdf2zh.v3.flight_recorder import TraceContext, Coord  # noqa: F401

    raster_events = [
        {
            "event": "raster.ink",
            "page": f["page"],
            "block_id": f["trace_id"].split("/")[-1],
            "trace_id": f["trace_id"],
            "stage": "raster",
            "payload": f,
        }
        for f in facts
        if f.get("found")
    ]
    all_results = results + run_rules(raster_events)
    # 去重（同一 block 同一 rule 只保留一条）
    dedup: Dict[tuple, RuleResult] = {}
    for r in all_results:
        dedup.setdefault((r.trace_id, r.rule), r)

    final = list(dedup.values())
    # first_divergence 标注必须在**完整结果集**（含 Level-2 raster facts
    # 复跑出的 INK_OVERLAP）去重之后：每个 FAIL 块的最早 FAIL 阶段是根因，
    # 其后阶段的 FAIL 是下游症状。
    first_map = annotate_first_divergence(final)

    pages_seen = sorted(
        {int(ev.get("page") or -1) for ev in events if ev.get("page") is not None}
    )
    grades = grade_pages(final, pages=pages_seen)
    by_sev: Dict[str, int] = {}
    by_rule: Dict[str, int] = {}
    for r in final:
        by_sev[r.severity] = by_sev.get(r.severity, 0) + 1
        by_rule[r.rule] = by_rule.get(r.rule, 0) + 1

    has_high = by_sev.get("HIGH", 0) > 0
    qualification = (
        "FAIL"
        if has_high
        else ("PASS_WITH_MEDIUM" if by_sev.get("MEDIUM", 0) else "PASS")
    )
    grade_hist = {g: sum(1 for v in grades.values() if v == g) for g in "ABCD"}

    idx = write_trace_index(
        list(events) + raster_events, os.path.join(out_dir, "trace-index.json")
    )

    summary = {
        "schema": "trace-audit-v1",
        "trace": os.path.abspath(trace_path) if trace_path else None,
        "mono_pdf": os.path.abspath(mono_path) if mono_path else None,
        "run_ids": idx["run_ids"],
        "books": idx["books"],
        "total_events": len(events) + len(raster_events),
        "pages": len(pages_seen),
        "page_grades": grade_hist,
        "grade_D_pages": [p for p in pages_seen if grades.get(p) == "D"],
        "grade_C_pages": [p for p in pages_seen if grades.get(p) == "C"],
        "rule_results": len(final),
        "by_severity": by_sev,
        "by_rule": by_rule,
        "qualification": qualification,
        "first_divergence_by_stage": {
            s: sum(1 for v in first_map.values() if v == s)
            for s in PIPELINE_STAGES
            if any(v == s for v in first_map.values())
        },
        "first_divergence_blocks": len(first_map),
        "downstream_symptoms": sum(1 for r in final if r.downstream),
        "rules": [
            {
                "rule": r.rule,
                "severity": r.severity,
                "page": r.page,
                "block_id": r.block_id,
                "trace_id": r.trace_id,
                "stage": r.stage,
                "first_divergence": r.first_divergence,
                "downstream": r.downstream,
                "action": r.action,
                "evidence": r.evidence,
            }
            for r in sorted(
                final, key=lambda x: (_SEVERITY_ORDER.get(x.severity, 9), x.trace_id)
            )
        ],
    }
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=1)

    with open(os.path.join(out_dir, "pages.json"), "w", encoding="utf-8") as fh:
        json.dump(
            [{"page": p, "grade": grades.get(p, "A")} for p in pages_seen],
            fh,
            ensure_ascii=False,
            indent=1,
        )

    ledger_path = os.path.join(out_dir, "defect-ledger.csv")
    with open(ledger_path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(
            [
                "page",
                "block",
                "defect",
                "stage",
                "severity",
                "action",
                "first_divergence",
                "downstream",
                "evidence",
            ]
        )
        for r in sorted(
            final, key=lambda x: (_SEVERITY_ORDER.get(x.severity, 9), x.trace_id)
        ):
            w.writerow(
                [
                    r.page,
                    r.block_id,
                    r.rule,
                    r.stage,
                    r.severity,
                    r.action,
                    r.first_divergence,
                    "1" if r.downstream else "0",
                    json.dumps(r.evidence, ensure_ascii=False),
                ]
            )

    _write_qualification_md(out_dir, summary, events, final)
    return summary


def _write_qualification_md(
    out_dir: str,
    summary: Dict[str, Any],
    events: Sequence[Dict[str, Any]],
    results: Sequence[RuleResult],
) -> None:
    lines = ["# Qualification (trace audit)\n"]
    lines.append(f"- qualification: **{summary['qualification']}**")
    lines.append(f"- pages: {summary['pages']}  grades: {summary['page_grades']}")
    lines.append(
        f"- events: {summary['total_events']}  rule FAILs: {summary['rule_results']}"
    )
    lines.append(
        f"- by severity: {summary['by_severity']}  by rule: {summary['by_rule']}"
    )
    lines.append(
        f"- first divergence: {summary.get('first_divergence_by_stage')}"
        f"  downstream symptoms: {summary.get('downstream_symptoms')}\n"
    )
    lines.append("## Defects\n")
    lines.append(
        "| Severity | Rule | Page | Block | Stage | First divergence | Action |"
    )
    lines.append("|---|---|---|---|---|---|---|")
    for r in summary["rules"]:
        lines.append(
            f"| {r['severity']} | {r['rule']} | {r['page']} | {r['block_id']} | "
            f"{r['stage']} | {r['first_divergence'] or '-'} | {r['action']} |"
        )
    lines.append("")
    if summary.get("grade_D_pages"):
        lines.append(f"D pages: {summary['grade_D_pages']}")
    if summary.get("grade_C_pages"):
        lines.append(f"C pages: {summary['grade_C_pages']}")

    # ── first divergence：每个 FAIL 块的 pipeline 阶段树 ────────────────
    fail_tids = sorted(
        {r.trace_id for r in results},
        key=lambda t: (
            int(t.split("/")[0]) if "/" in t and t.split("/")[0].isdigit() else 10**9,
            t,
        ),
    )
    if fail_tids:
        lines.append("")
        lines.append("## First divergence\n")
        lines.append("每个 FAIL 块按 pipeline 顺序列出各阶段：最靠前的 FAIL 阶段是")
        lines.append("first divergence（根因），其后阶段的 FAIL 是 downstream")
        lines.append("symptom（同一根因的连锁症状，不重复计数）。PASS = 该块实际")
        lines.append("遍历且无 FAIL；- = 未遍历。\n")
        for tid in fail_tids:
            lines.extend(_block_stage_lines(events, results, tid))
            lines.append("")
    with open(os.path.join(out_dir, "qualification.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


# ── CLI ─────────────────────────────────────────────────────────────────


def _run_explain(
    trace_path: str,
    trace_id: str,
    *,
    pdf: Optional[str] = None,
    source: Optional[str] = None,
    out: Optional[str] = None,
    crop_max: int = 1,
) -> int:
    events = list(read_events(trace_path))
    if not events:
        print(f"[trace-audit] empty or unreadable trace: {trace_path}")
        return 2
    print(
        explain_block(
            events, trace_id, pdf=pdf, source=source, out=out, crop_max=crop_max
        )
    )
    return 0


def _run_audit(
    trace_path: str,
    *,
    pdf: Optional[str] = None,
    source: Optional[str] = None,
    out: Optional[str] = None,
    crop_max: int = 8,
) -> int:
    events = list(read_events(trace_path))
    if not events:
        print(f"[trace-audit] empty or unreadable trace: {trace_path}")
        return 2
    results = _rule_results(events)
    print(
        f"[trace-audit] events={len(events)} blocks={len(group_by_block(events))} "
        f"rule_fails={len(results)}"
    )

    facts: List[Dict[str, Any]] = []
    if pdf:
        crop_dir = os.path.join(out or "audit", "crops")
        facts = raster_evidence_for_blocks(
            results,
            events,
            pdf,
            source_path=source,
            crop_max=crop_max,
            crop_dir=crop_dir,
        )
        print(f"[trace-audit] raster evidence: {len(facts)}")

    out_dir = out or "audit"
    summary = write_outputs(
        out_dir, events, results, facts, mono_path=pdf, trace_path=trace_path
    )
    print(
        f"[trace-audit] qualification={summary['qualification']} "
        f"grades={summary['page_grades']} by_severity={summary['by_severity']}"
    )
    print(f"[trace-audit] wrote {out_dir}/")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("trace", help="audit a flight-recorder trace")
    a.add_argument("trace", help="path to events.jsonl")
    a.add_argument("--pdf", help="rendered mono PDF for Level-2 raster evidence")
    a.add_argument("--source", help="original source PDF (for src crops)")
    a.add_argument("--out", default="audit", help="output directory")
    a.add_argument("--crop-max", type=int, default=8, help="max Level-2 crops")
    a.set_defaults(func=_run_audit)

    r = sub.add_parser("run", help="audit a full run (pdf + trace)")
    r.add_argument("--pdf", required=True)
    r.add_argument("--trace", required=True)
    r.add_argument("--source", default=None)
    r.add_argument("--out", default="audit")
    r.add_argument("--crop-max", type=int, default=8)
    r.set_defaults(func=_run_audit)

    x = sub.add_parser("explain", help="explain one block's failure from a trace")
    x.add_argument("trace", help="path to events.jsonl")
    x.add_argument("trace_id", help="block identity, e.g. 442/p442_4")
    x.add_argument(
        "--pdf", default=None, help="rendered mono PDF (Level-2 raster evidence)"
    )
    x.add_argument("--source", default=None)
    x.add_argument("--out", default="audit", help="audit directory (crops evidence)")
    x.add_argument("--crop-max", type=int, default=1)
    x.set_defaults(func=_run_explain)

    args = ap.parse_args(argv)
    if args.cmd == "run":
        return _run_audit(
            args.trace,
            pdf=args.pdf,
            source=args.source,
            out=args.out,
            crop_max=args.crop_max,
        )
    if args.cmd == "explain":
        return _run_explain(
            args.trace,
            args.trace_id,
            pdf=args.pdf,
            source=args.source,
            out=args.out,
            crop_max=args.crop_max,
        )
    return args.func(
        args.trace,
        pdf=args.pdf,
        source=args.source,
        out=args.out,
        crop_max=args.crop_max,
    )


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["raster_evidence_for_blocks", "write_outputs", "explain_block", "main"]
