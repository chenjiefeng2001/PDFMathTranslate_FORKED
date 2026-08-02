"""File upload panel component for the Gradio UI.

Provides:
  - File upload area (drag-and-drop)
  - URL link input
  - Multi-file support
"""

from __future__ import annotations

import gradio as gr


def create_upload_panel() -> dict:
    """Create the file upload UI panel.

    Returns:
        dict of Gradio component references
    """
    with gr.Group():
        gr.Markdown("## 📂 文件上传 / File Upload", elem_classes="section-header")

        with gr.Tabs():
            with gr.Tab("📁 本地上传 / Local File"):
                file_input = gr.File(
                    label="上传 PDF/DOCX 文件 / Upload PDF/DOCX",
                    file_types=[".pdf", ".docx", ".doc"],
                    file_count="multiple",
                    elem_classes="upload-area",
                )

            with gr.Tab("🔗 在线链接 / URL Link"):
                link_input = gr.Textbox(
                    label="输入 PDF 链接 / PDF URL",
                    placeholder="https://example.com/paper.pdf",
                    lines=1,
                )
                gr.Markdown(
                    "_提示：仅支持可直接下载的 PDF 文件链接。_"
                )

    return {
        "file_input": file_input,
        "link_input": link_input,
    }
