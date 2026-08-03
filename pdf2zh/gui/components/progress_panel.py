"""Progress and status panel for the Gradio UI.

Replaces the 13-tuple sync pattern with targeted component updates.
Supports V4 Diagnostic summary display and a 4-stage StepBar pipeline
(上传 -> 版面分析 -> 翻译 -> 渲染) rendered from runtime task state.
"""

from __future__ import annotations

import gradio as gr

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

#: Human-readable labels for running stages (progress bar / badge)
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
    green.

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
        label = zh if zh else en
        nodes.append(
            f'<div class="{cls}"><span class="step-dot">{i + 1}</span>'
            f'<span class="step-label">{label}</span></div>'
        )
    parts = [nodes[0]]
    for i in range(1, len(nodes)):
        conn_done = done or (i <= step_idx and not error) or (error and i <= step_idx)
        conn_cls = "step-connector done" if conn_done else "step-connector"
        parts.append(f'<div class="{conn_cls}"></div>')
        parts.append(nodes[i])
    return f'<div class="stepbar">{"".join(parts)}</div>'


def build_progress_bar_html(stage: str, pct: float, msg: str) -> str:
    """Render the token-driven progress bar HTML.

    Replaces the legacy inline ``<div style=...>`` markup so the progress
    indicator re-skins automatically in dark mode.
    """
    if stage == "completed" and pct >= 100:
        cls = "progress-active progress-done"
        stage_label = "完成 / Complete"
    elif stage in ("failed", "cancelled"):
        cls = "progress-active progress-error"
        stage_label = "已取消" if stage == "cancelled" else "失败 / Failed"
    else:
        cls = "progress-active"
        stage_label = BADGE_LABELS.get(stage, stage or "运行中")
    safe_msg = "".join(
        c if ord(c) < 0xD800 or ord(c) > 0xDFFF else "\ufffd" for c in (msg or "")
    )
    pct_clamped = max(0.0, min(100.0, float(pct)))
    return (
        f'<div class="{cls}">'
        '<div class="progress-head"><span>'
        f"{stage_label}</span><span class='pct'>{pct_clamped:.1f}%</span></div>"
        '<div class="progress-track">'
        f'<div class="progress-fill" style="width:{pct_clamped:.1f}%"></div>'
        "</div>"
        f'<div class="progress-msg">{safe_msg}</div></div>'
    )



def create_progress_panel() -> dict:
    """Create the progress and status monitoring panel.

    Returns:
        dict of Gradio component references
    """
    with gr.Group(elem_classes="panel-card"):
        gr.Markdown("## 📊 执行状态 / Execution Status", elem_classes="section-header")

        control_row = gr.Row()
        with control_row:
            translate_btn = gr.Button("🚀 开始翻译 / Translate", variant="primary")
            pause_btn = gr.Button("⏸ 暂停 / Pause")
            resume_btn = gr.Button("▶️ 恢复 / Resume")
            skip_btn = gr.Button("⏭ 跳过文件 / Skip")
            cancel_btn = gr.Button("⏹ 停止 / Cancel", variant="stop")

        progress_bar = gr.HTML(
            value=build_progress_bar_html("", 0.0, ""),
            elem_classes="progress-bar",
        )
        status_badge = gr.HTML(
            value=build_status_badge_html("idle"),
            elem_classes="status-badge-box",
        )
        status_markdown = gr.Markdown(
            value="**状态**: 就绪 / Ready",
            elem_classes="status-text",
        )

        with gr.Accordion("📋 详细日志 / Detailed Logs", open=False):
            log_output = gr.HTML(
                value="<pre class='log-output'>[系统就绪]</pre>",
                elem_classes="log-output",
            )

    return {
        "translate_btn": translate_btn,
        "pause_btn": pause_btn,
        "resume_btn": resume_btn,
        "skip_btn": skip_btn,
        "cancel_btn": cancel_btn,
        "progress_bar": progress_bar,
        "status_badge": status_badge,
        "status_markdown": status_markdown,
        "log_output": log_output,
    }

