"""Preview and download panel for the Gradio UI.

Provides PDF preview, dual/mono output-mode switching, and download buttons.

NOTE: PDF preview uses an <iframe> via gr.HTML to avoid Gradio 5's
file-serving limitations with local paths. A custom /pdf-preview/ endpoint
is registered in app.py to serve output files.
"""

from __future__ import annotations

import gradio as gr


def create_preview_panel() -> dict:
    """Create the preview and download panel.

    Returns:
        dict of Gradio component references
    """
    with gr.Group(elem_classes="panel-card"):
        gr.Markdown("## 👁️ 预览与下载 / Preview & Download", elem_classes="section-header")

        # PDF preview — use gr.HTML with an iframe; the /pdf-preview/ endpoint
        # is registered at app level to serve the file.
        pdf_preview = gr.HTML(
            value="<div class='preview-empty'>等待翻译完成后显示预览</div>",
            elem_classes="pdf-preview",
        )

        # Dual / mono output-mode switcher + download toolbar.
        with gr.Row(elem_classes="preview-toolbar"):
            result_selector = gr.Radio(
                choices=[],
                label="输出模式 / Output Mode",
                interactive=True,
                elem_classes="result-select",
            )
            download_btn = gr.Button("📥 下载 / Download", variant="secondary")
            download_all_btn = gr.Button("📦 下载全部 (ZIP)", variant="secondary")

        # Use gr.File with visible=True; sync_status will set value when done.
        download_single = gr.File(label="下载选中的文件", visible=False)
        download_zip = gr.File(label="下载 ZIP 压缩包", visible=False)

    return {
        "pdf_preview": pdf_preview,
        "result_selector": result_selector,
        "download_btn": download_btn,
        "download_all_btn": download_all_btn,
        "download_single": download_single,
        "download_zip": download_zip,
    }

