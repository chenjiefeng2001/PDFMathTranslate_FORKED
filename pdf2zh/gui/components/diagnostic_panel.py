"""V4 document intelligence diagnostic panel for the Gradio UI."""

from __future__ import annotations

import logging
import gradio as gr
from typing import Dict, Optional

from pdf2zh.gui.i18n import B

logger = logging.getLogger(__name__)


def build_diagnostic_markdown(
    quality_scores: Optional[Dict[str, float]] = None,
    diagnostic_summary: str = "",
    node_overview: Optional[Dict[str, int]] = None,
) -> str:
    """Build V4 Document Intelligence markdown from runtime data."""
    parts = []

    # Node overview
    if node_overview:
        overview_items = [
            f"**{B('diag_graph')}**: {B('diag_node_heading')} {node_overview.get('pages', 0)}"
        ]
        if node_overview.get("paragraphs"):
            overview_items.append(
                f"{B('diag_paragraphs')} {node_overview.get('paragraphs', 0)}"
            )
        if node_overview.get("headings"):
            overview_items.append(
                f"{B('diag_headings')} {node_overview.get('headings', 0)}"
            )
        if node_overview.get("figures"):
            overview_items.append(
                f"{B('diag_figures')} {node_overview.get('figures', 0)}"
            )
        if node_overview.get("formulas"):
            overview_items.append(
                f"{B('diag_formulas')} {node_overview.get('formulas', 0)}"
            )
        parts.append(" | ".join(overview_items))

    # Quality scores
    if quality_scores:
        score_bars = []
        for cat, score in sorted(quality_scores.items()):
            bar_len = int(score / 10)
            bar = "\u2588" * bar_len + "\u2591" * (10 - bar_len)
            score_bars.append(f"{cat}: {bar} {score:.0f}/100")
        parts.append("\n\n".join(score_bars))

    # Diagnostic summary
    if diagnostic_summary:
        passed = "passed" in diagnostic_summary.lower()
        marker = "OK" if passed else "WARN"
        safe_summary = "".join(
            c if ord(c) < 0xD800 or ord(c) > 0xDFFF else "\ufffd"
            for c in diagnostic_summary
        )
        parts.append(f"\n\n**[{marker}] {B('diag_diagnosis')}**: {safe_summary}")

    return "\n\n".join(parts) if parts else f"*{B('diag_no_task')}*"


def build_healing_markdown(
    diagnostic_report: Optional[dict] = None,
    heal_status: Optional[dict] = None,
    repair_records: Optional[list] = None,
    confidence_stats: Optional[dict] = None,
    diagnostic_summary: str = "",
) -> str:
    """Build the Diagnostic & Self-Healing dashboard markdown.

    Renders the structured diagnostic report (legacy errors/warnings/admissible
    or V4 evaluator pass-rate), the per-issue healing actions, the before/after
    healing summary and the document confidence statistics. Returns the idle
    placeholder when no data is available.
    """
    import re as _re

    parts: list = []

    def _sanitize(text: str) -> str:
        safe = _re.sub(r"[\x00-\x1f\x7f]", " ", str(text or ""))
        return "".join(
            c if ord(c) < 0xD800 or ord(c) > 0xDFFF else "\ufffd" for c in safe
        )

    has_report = False
    if isinstance(diagnostic_report, dict) and diagnostic_report:
        has_report = True
        if "errors" in diagnostic_report:
            errs = int(diagnostic_report.get("errors") or 0)
            warns = int(diagnostic_report.get("warnings") or 0)
            admissible = bool(diagnostic_report.get("admissible"))
            marker = "OK" if admissible else "ERROR"
            parts.append(
                f"**[{marker}] {B('diag_diagnosis')}**: "
                f"errors={errs} warnings={warns} admissible={admissible}"
            )
        elif "records" in diagnostic_report or "pass_rate" in diagnostic_report:
            total = int(diagnostic_report.get("total") or 0)
            failed = int(diagnostic_report.get("failed") or 0)
            rate = float(diagnostic_report.get("pass_rate") or 0.0)
            marker = "OK" if failed == 0 else "WARN"
            parts.append(
                f"**[{marker}] {B('diag_diagnosis')}**: "
                f"pass_rate={rate:.1f}% failed={failed}/{total}"
            )
        else:
            parts.append(
                f"**[WARN] {B('diag_diagnosis')}**: {_sanitize(diagnostic_report)}"
            )
    elif diagnostic_summary:
        passed = "passed" in diagnostic_summary.lower()
        parts.append(
            f"**[{'OK' if passed else 'WARN'}] {B('diag_diagnosis')}**: "
            f"{_sanitize(diagnostic_summary)}"
        )

    if repair_records:
        lines = [f"**{B('diag_healing_actions')}**:", ""]
        sev_ok = ("error", "major")
        for rec in list(repair_records)[:8]:
            sev = str(rec.get("severity") or "warning")
            mark = "ERROR" if sev.lower() in sev_ok else "WARN"
            node = f"p{rec.get('page')}" if rec.get("page") else ""
            lines.append(
                f"- `[{mark}]` {node} · {_sanitize(rec.get('code') or '?')} → "
                f"{_sanitize(rec.get('action') or '人工复核')} ({rec.get('status')})"
            )
        if len(repair_records) > 8:
            lines.append(f"- … 共 {len(repair_records)} 项")
        parts.append("\n".join(lines))

    if isinstance(heal_status, dict):
        if heal_status.get("ran"):
            parts.append(
                f"**{B('diag_heal_summary')}**: "
                f"before={heal_status.get('before_errors')} "
                f"after={heal_status.get('after_errors')} "
                f"iterations={heal_status.get('iterations')} "
                f"improved={heal_status.get('improved')}"
            )
        else:
            err = heal_status.get("error")
            parts.append(
                f"**{B('diag_heal_summary')}**: "
                f"{_sanitize(err) if err else B('diag_healing_idle')}"
            )

    if isinstance(confidence_stats, dict) and confidence_stats:
        cparts_list = []
        if confidence_stats.get("annotated") is not None:
            cparts_list.append(f"annotated={confidence_stats.get('annotated')}")
        for key in ("avg", "min", "max"):
            val = confidence_stats.get(key)
            if isinstance(val, (int, float)):
                cparts_list.append(f"{key}={val:.2f}")
        if cparts_list:
            parts.append(f"**{B('diag_confidence')}**: {' | '.join(cparts_list)}")

    # V1.23 Layout Inspector：逐段落排版证据（Font 来源 / 对齐 / 段拆）
    if isinstance(diagnostic_report, dict):
        layout = diagnostic_report.get("layout")
        if isinstance(layout, dict) and layout.get("paragraphs"):
            parts.append(_build_layout_section(layout))

    return "\n\n".join(parts) if parts else f"*{B('diag_healing_idle')}*"


def _build_layout_section(report: dict) -> str:
    """渲染 Layout Inspector 段落（Font Resolution / 对齐 / Lv2 段拆）。"""
    lines = [
        f"**{B('diag_layout')}** ({B('diag_layout_paragraphs')}: "
        f"{report.get('stats', {}).get('blocks', len(report.get('paragraphs', [])))}"
        f", {B('diag_layout_issues')}: {report.get('stats', {}).get('issues', 0)})",
        "",
    ]
    for row in list(report.get("paragraphs", []))[:10]:
        size = row.get("font_size")
        ratio = row.get("font_size_ratio")
        max_sz = row.get("font_size_max")
        align = row.get("alignment") or "?"
        split_mark = "⟲" if row.get("layout_split") else " "
        lines.append(
            f"- `{split_mark} {row.get('block_id')}` [{row.get('kind')}] "
            f"{B('diag_layout_align')}={align} "
            f"size={size} max={max_sz} ratio={ratio} "
            f"lines={row.get('lines')} · {row.get('text', '')[:48]}"
        )
    for issue in list(report.get("issues", []))[:6]:
        mark = "WARN" if issue.get("kind") == "size_blend" else "INFO"
        lines.append(
            f"- `[{mark}] {issue.get('kind')}` {issue.get('node')} "
            f"{issue.get('why')}"
        )
    return "\n".join(lines)


def create_diagnostic_panel() -> dict:
    """Create the V4 document intelligence diagnostic panel.

    Diagnostics are collapsed into the side rail (progressive disclosure):
    the graph overview, quality scores and the self-healing dashboard appear
    only on demand.

    Returns:
        dict of Gradio component references
    """
    with gr.Group(elem_classes="panel-card"):
        gr.Markdown(f"## {B('section_diagnostics')}", elem_classes="section-header")

        with gr.Accordion(B("diag_graph"), open=False):
            node_overview = gr.Markdown(
                value=f"*{B('diag_graph_idle')}*",
                elem_classes="diagnostic-overview",
            )

        with gr.Accordion(B("diag_quality"), open=False):
            quality_scores = gr.Markdown(
                value=f"*{B('diag_quality_idle')}*",
                elem_classes="quality-scores",
            )

        with gr.Accordion(B("diag_healing"), open=False):
            diagnostic_status = gr.Markdown(
                value=f"*{B('diag_healing_idle')}*",
                elem_classes="diagnostic-status",
            )

    return {
        "node_overview": node_overview,
        "quality_scores": quality_scores,
        "diagnostic_status": diagnostic_status,
    }


__all__ = [
    "create_diagnostic_panel",
    "build_diagnostic_markdown",
    "build_healing_markdown",
]
