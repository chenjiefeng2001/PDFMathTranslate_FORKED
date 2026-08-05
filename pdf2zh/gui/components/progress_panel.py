"""Progress and status panel for the Gradio UI.

Replaces the 13-tuple sync pattern with targeted component updates.
Supports V4 Diagnostic summary display and a 4-stage StepBar pipeline
(上传 -> 版面分析 -> 翻译 -> 渲染) rendered from runtime task state.

The StepBar is fully ARIA-annotated (``role="list"`` / ``role="listitem"`` /
``aria-current``) so screen readers can follow the pipeline position.
"""

from __future__ import annotations

import gradio as gr

from pdf2zh.gui.i18n import B, stage_text
from pdf2zh.gui.styles import build_status_badge_html

#: StepBar pipeline definition -- order matters and matches the worker stages.
STEPBAR_STAGES = [
    ("upload", "上传", "Upload"),
    ("layout", "版面分析", "Layout"),
    ("translate", "翻译", "Translate"),
    ("render", "渲染", "Render"),
]

#: Runtime task status -> (stepbar_index, is_error). Pending/parsing live on
#: step 1, analyzing/planning on step 2, translating on step 3 and
#: layouting/rendering/evaluating/repairing on step 4.
_STATUS_STEP_MAP = {
    "pending": (0, False),
    "parsing": (0, False),
    "normalizing": (0, False),
    "analyzing": (1, False),
    "planning": (1, False),
    "translating": (2, False),
    "layouting": (3, False),
    "rendering": (3, False),
    "evaluating": (3, False),
    "repairing": (3, False),
    "completed": (3, False),
    "failed": (0, True),
    "cancelled": (0, True),
}

#: Human-readable Chinese labels for running stages (kept for compatibility;
#: new code should use ``pdf2zh.gui.i18n.stage_text``).
BADGE_LABELS = {
    "pending": "排队中",
    "parsing": "解析中",
    "normalizing": "规范化",
    "analyzing": "版面分析",
    "planning": "规划中",
    "translating": "翻译中",
    "layouting": "排版中",
    "rendering": "渲染中",
    "evaluating": "质量评估",
    "repairing": "自动修复",
}


def build_stepbar_html(status: str, progress: float = 0.0) -> str:
    """Render the 4-stage StepBar from a runtime task status.

    The active stage is derived from ``_STATUS_STEP_MAP``; when a task is
    ``failed`` the *first* pipeline step is marked as an error so the user
    sees immediately where the flow broke. A completed task paints all steps
    green. The markup carries ARIA semantics (list / listitem / current).

    Args:
        status: runtime task status (e.g. ``parsing``, ``translating``).
        progress: numeric progress in [0, 100] -- used for the active step.

    Returns:
        HTML fragment for the StepBar rail.
    """
    step_idx, is_error = _STATUS_STEP_MAP.get(status, (0, False))
    done = status == "completed"
    error = is_error or status in ("failed", "cancelled")
    nodes = []
    for i, (_key, zh, en) in enumerate(STEPBAR_STAGES):
        if done:
            cls = "step-item done"
        elif error and i < step_idx:
            cls = "step-item done"
        elif error and i == step_idx:
            cls = "step-item error"
        elif i < step_idx:
            cls = "step-item done"
        elif i == step_idx:
            cls = "step-item active"
        else:
            cls = "step-item"
        current_attr = ' aria-current="step"' if i == step_idx and not done else ""
        label = zh if zh else en
        nodes.append(
            f'<div class="{cls}" role="listitem"{current_attr}>'
            f'<span class="step-dot" aria-hidden="true">{i + 1}</span>'
            f'<span class="step-label">{label}</span></div>'
        )
    parts = [nodes[0]]
    for i in range(1, len(nodes)):
        conn_done = done or (i <= step_idx and not error) or (error and i <= step_idx)
        conn_cls = "step-connector done" if conn_done else "step-connector"
        parts.append(f'<div class="{conn_cls}" role="presentation"></div>')
        parts.append(nodes[i])
    return (
        f'<div class="stepbar" role="list" '
        f'aria-label="{B("stepbar_aria")}">{"".join(parts)}</div>'
    )


def build_progress_bar_html(stage: str, pct: float, msg: str) -> str:
    """Render the token-driven progress bar HTML.

    Replaces the legacy inline ``<div style=...>`` markup so the progress
    indicator re-skins automatically in dark mode. Exposes a semantic
    ``role="progressbar"`` with ``aria-valuenow`` for assistive tech.
    """
    if stage == "completed" and pct >= 100:
        cls = "progress-active progress-done"
        stage_label = "✓ 完成 / Done"
    elif stage in ("failed", "cancelled"):
        cls = "progress-active progress-error"
        stage_label = stage_text(stage)
    else:
        cls = "progress-active"
        stage_label = stage_text(stage) if stage else B("status_running")
    safe_msg = "".join(
        c if ord(c) < 0xD800 or ord(c) > 0xDFFF else "\ufffd" for c in (msg or "")
    )
    pct_clamped = max(0.0, min(100.0, float(pct)))
    return (
        f'<div class="{cls}">'
        '<div class="progress-head"><span>'
        f"{stage_label}</span><span class='pct'>{pct_clamped:.1f}%</span></div>"
        '<div class="progress-track">'
        f'<div class="progress-fill" style="width:{pct_clamped:.1f}%" '
        f'role="progressbar" aria-valuemin="0" aria-valuemax="100" '
        f'aria-valuenow="{pct_clamped:.1f}" '
        f'aria-label="{B("progress_aria")}"></div>'
        "</div>"
        f'<div class="progress-msg">{safe_msg}</div></div>'
    )


def create_progress_panel() -> dict:
    """Create the progress and status monitoring panel.

    Returns:
        dict of Gradio component references
    """
    with gr.Group(elem_classes="panel-card"):
        gr.Markdown(f"## 📊 {B('section_progress')}", elem_classes="section-header")

        control_row = gr.Row(elem_classes="control-row")
        with control_row:
            translate_btn = gr.Button(f"🚀 {B('progress_translate')}", variant="primary")
            pause_btn = gr.Button(f"⏸ {B('progress_pause')}")
            resume_btn = gr.Button(f"▶️ {B('progress_resume')}")
            skip_btn = gr.Button(f"⏭ {B('progress_skip')}")
            retry_btn = gr.Button(f"🔁 {B('progress_retry')}", visible=False)
            cancel_btn = gr.Button(f"⏹ {B('progress_cancel')}", variant="stop")

        progress_bar = gr.HTML(
            value=build_progress_bar_html("", 0.0, ""),
            elem_classes="progress-bar",
        )
        status_badge = gr.HTML(
            value=build_status_badge_html("idle"),
            elem_classes="status-badge-box",
        )
        status_markdown = gr.Markdown(
            value=f"**{B('label_status')}**: {B('status_ready')}",
            elem_classes="status-text",
        )

        with gr.Accordion(f"📋 {B('progress_logs')}", open=False):
            log_output = gr.HTML(
                value=f"<pre class='log-output'>{B('progress_log_idle')}</pre>",
                elem_classes="log-output",
            )

    return {
        "translate_btn": translate_btn,
        "pause_btn": pause_btn,
        "resume_btn": resume_btn,
        "skip_btn": skip_btn,
        "retry_btn": retry_btn,
        "cancel_btn": cancel_btn,
        "progress_bar": progress_bar,
        "status_badge": status_badge,
        "status_markdown": status_markdown,
        "log_output": log_output,
    }
