"""Progress and status panel for the Gradio UI.

Replaces the 13-tuple sync pattern with targeted component updates.
Supports V4 Diagnostic summary display.
"""

from __future__ import annotations

import gradio as gr


def create_progress_panel() -> dict:
    """Create the progress and status monitoring panel.

    Returns:
        dict of Gradio component references
    """
    with gr.Group():
        gr.Markdown("## 📊 执行状态 / Execution Status", elem_classes="section-header")

        control_row = gr.Row()
        with control_row:
            translate_btn = gr.Button("🚀 开始翻译 / Translate", variant="primary")
            pause_btn = gr.Button("⏸ 暂停 / Pause")
            resume_btn = gr.Button("▶️ 恢复 / Resume")
            skip_btn = gr.Button("⏭ 跳过文件 / Skip")
            cancel_btn = gr.Button("⏹ 停止 / Cancel", variant="stop")

        progress_bar = gr.HTML(
            value="<div class='progress-idle'>等待任务...</div>",
            elem_classes="progress-bar",
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
        "status_markdown": status_markdown,
        "log_output": log_output,
    }
