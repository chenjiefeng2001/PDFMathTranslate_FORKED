
from __future__ import annotations

import logging
import gradio as gr
from typing import Any, Dict, Optional

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
        overview_items = [f"\U0001F4C4 **文档概况**: 页面 {node_overview.get('pages', 0)}"]
        if node_overview.get('paragraphs'):
            overview_items.append(f"段落 {node_overview.get('paragraphs', 0)}")
        if node_overview.get('headings'):
            overview_items.append(f"标题 {node_overview.get('headings', 0)}")
        if node_overview.get('figures'):
            overview_items.append(f"图表 {node_overview.get('figures', 0)}")
        if node_overview.get('formulas'):
            overview_items.append(f"公式 {node_overview.get('formulas', 0)}")
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
        parts.append(f"\n\n{prefix} 诊断: {safe_summary}")

    return "\n\n".join(parts) if parts else "*等待翻译任务开始...*"

# Original functions follow


def create_diagnostic_panel() -> dict:
    """Create the V4 document intelligence diagnostic panel.

    Diagnostics are collapsed into the side rail (progressive disclosure):
    the graph overview, five-dimension quality scores and the self-healing
    dashboard appear only on demand.

    Returns:
        dict of Gradio component references
    """
    with gr.Group(elem_classes="panel-card"):
        gr.Markdown("## \U0001F9E0 V4 \u6587\u6863\u667a\u80fd\u5206\u6790 / Document Intelligence", elem_classes="section-header")

        with gr.Accordion("\U0001F4CA \u6587\u6863\u56fe\u8282\u70b9\u6982\u51b5 / Document Graph Overview", open=False):
            node_overview = gr.Markdown(
                value="*\u7b49\u5f85\u7ffb\u8bd1\u4efb\u52a1\u5f00\u59cb...*",
                elem_classes="diagnostic-overview",
            )

        with gr.Accordion("\U0001F3AF \u4e94\u7ef4\u8d28\u91cf\u8bc4\u4f30 / Quality Assessment", open=False):
            quality_scores = gr.Markdown(
                value="*\u7ffb\u8bd1\u5b8c\u6210\u540e\u5c06\u663e\u793a\u8d28\u91cf\u8bc4\u5206*",
                elem_classes="quality-scores",
            )

        with gr.Accordion("\U0001FAB7 \u8bca\u65ad\u4e0e\u81ea\u6108\u770b\u677f / Diagnostic & Self-Healing", open=False):
            diagnostic_status = gr.Markdown(
                value="*\u5c1a\u672a\u8fd0\u884c\u8bca\u65ad\u5206\u6790*",
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
