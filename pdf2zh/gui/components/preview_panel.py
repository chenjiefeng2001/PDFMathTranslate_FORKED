"""Preview and download panel for the Gradio UI.

Provides PDF preview, output-file selection, and download buttons.

NOTE: PDF preview uses an <iframe> via gr.HTML to avoid Gradio 5's
file-serving limitations with local paths. A custom /pdf-preview/ endpoint
is registered in app.py to serve output files.
"""

from __future__ import annotations

import gradio as gr

from pdf2zh.gui.i18n import B


def create_preview_panel() -> dict:
    """Create the preview and download panel.

    Returns:
        dict of Gradio component references
    """
    with gr.Group(elem_classes="panel-card"):
        gr.Markdown(f"## 👁️ {B('section_preview')}", elem_classes="section-header")

        # PDF preview — use gr.HTML with an iframe; the /pdf-preview/ endpoint
        # is registered at app level to serve the file.
        pdf_preview = gr.HTML(
            value=f"<div class='preview-empty'>{B('preview_empty')}</div>",
            elem_classes="pdf-preview",
        )

        # Output file selector (dual/mono results) + download toolbar.
        with gr.Row(elem_classes="preview-toolbar"):
            result_selector = gr.Radio(
                choices=[],
                label=B("preview_output"),
                interactive=True,
                elem_classes="result-select",
            )
            download_btn = gr.Button(f"📥 {B('preview_download')}", variant="secondary")
            download_all_btn = gr.Button(
                f"📦 {B('preview_download_all')}", variant="secondary"
            )

        # Use gr.File with visible=True; sync_status will set value when done.
        download_single = gr.File(label=B("preview_download_label"), visible=False)
        download_zip = gr.File(label=B("preview_zip_label"), visible=False)

    return {
        "pdf_preview": pdf_preview,
        "result_selector": result_selector,
        "download_btn": download_btn,
        "download_all_btn": download_all_btn,
        "download_single": download_single,
        "download_zip": download_zip,
    }
