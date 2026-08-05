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
            f"📄 **{B('diag_graph')}**: {B('diag_node_heading')} {node_overview.get('pages', 0)}"
        ]
        if node_overview.get('paragraphs'):
            overview_items.append(f"{B('diag_paragraphs')} {node_overview.get('paragraphs', 0)}")
        if node_overview.get('headings'):
            overview_items.append(f"{B('diag_headings')} {node_overview.get('headings', 0)}")
        if node_overview.get('figures'):
            overview_items.append(f"{B('diag_figures')} {node_overview.get('figures', 0)}")
        if node_overview.get('formulas'):
            overview_items.append(f"{B('diag_formulas')} {node_overview.get('formulas', 0)}")
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
        prefix = "\u2705" if "passed" in diagnostic_summary.lower() else "\u26a0\ufe0f"
        safe_summary = "".join(c if ord(c) < 0xD800 or ord(c) > 0xDFFF else "\ufffd" for c in diagnostic_summary)
        parts.append(f"\n\n{prefix} {B('diag_diagnosis')}: {safe_summary}")

    return "\n\n".join(parts) if parts else f"*{B('diag_no_task')}*"


def create_diagnostic_panel() -> dict:
    """Create the V4 document intelligence diagnostic panel.

    Diagnostics are collapsed into the side rail (progressive disclosure):
    the graph overview, quality scores and the self-healing dashboard appear
    only on demand.

    Returns:
        dict of Gradio component references
    """
    with gr.Group(elem_classes="panel-card"):
        gr.Markdown(f"## 🧠 {B('section_diagnostics')}", elem_classes="section-header")

        with gr.Accordion(f"📊 {B('diag_graph')}", open=False):
            node_overview = gr.Markdown(
                value=f"*{B('diag_graph_idle')}*",
                elem_classes="diagnostic-overview",
            )

        with gr.Accordion(f"🎯 {B('diag_quality')}", open=False):
            quality_scores = gr.Markdown(
                value=f"*{B('diag_quality_idle')}*",
                elem_classes="quality-scores",
            )

        with gr.Accordion(f"🩹 {B('diag_healing')}", open=False):
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
]
