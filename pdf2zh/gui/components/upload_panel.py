"""File upload panel component for the Gradio UI.

Provides:
  - File upload area (drag-and-drop)
  - URL link input
  - Multi-file support
  - Live file summary chips (name / size) rendered from the selection
"""

from __future__ import annotations

import os
from typing import Any, List

import gradio as gr

from pdf2zh.gui.i18n import B

#: Pairs of display names -> extensions. Kept here so the panel stays
#: self-contained; the worker validates the real file type later.
ACCEPTED_EXTENSIONS = [".pdf", ".docx", ".doc", ".PDF", ".DOCX", ".DOC"]


def _human_size(num_bytes: float) -> str:
    """Format a byte count into a compact human-readable string."""
    try:
        size = float(num_bytes)
    except (TypeError, ValueError):
        return "?"
    if size <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB"]
    idx = 0
    while size >= 1024 and idx < len(units) - 1:
        size /= 1024
        idx += 1
    return f"{size:.1f} {units[idx]}"


def build_file_summary_html(files: Any) -> str:
    """Render file-selection summary chips from a gr.File value.

    Args:
        files: ``gr.File`` value -- a dict (single) or a list of dicts
            (multi). Each dict has ``name`` / ``path`` and usually ``size``.

    Returns:
        HTML string (empty when no files are selected).
    """
    items: List[dict] = []
    if isinstance(files, dict):
        items = [files]
    elif isinstance(files, (list, tuple)):
        items = [f for f in files if isinstance(f, dict)]
    if not items:
        return ""
    chips = []
    for item in items:
        name = str(item.get("name") or item.get("path") or "unknown")
        # gr.File prefixes "tmp/" when a file is uploaded through the browser
        base = os.path.basename(name.replace("\\", "/"))
        size = item.get("size")
        label = _human_size(size) if size is not None else "?"
        chips.append(
            f'<span class="file-summary-item">{base} <em>({label})</em></span>'
        )
    total = len(items)
    plural = "" if total == 1 else f" × {total}"
    return (
        f'<div class="file-summary"><strong>{B("upload_summary_selected")}</strong>'
        f"{plural}: {' '.join(chips)}</div>"
    )


def create_upload_panel() -> dict:
    """Create the file upload UI panel.

    Returns:
        dict of Gradio component references
    """
    with gr.Group(elem_classes="panel-card"):
        gr.Markdown(f"## 📂 {B('section_upload')}", elem_classes="section-header")

        with gr.Tabs():
            with gr.Tab(f"📁 {B('upload_tab_local')}"):
                file_input = gr.File(
                    label=B("upload_label_file"),
                    file_types=ACCEPTED_EXTENSIONS,
                    file_count="multiple",
                    elem_classes="upload-area",
                )
                file_summary = gr.HTML(
                    value="",
                    visible=True,
                    elem_classes="file-summary",
                )

                def _on_files_changed(files: Any) -> str:
                    return build_file_summary_html(files)

                file_input.change(
                    fn=_on_files_changed,
                    inputs=[file_input],
                    outputs=[file_summary],
                )

            with gr.Tab(f"🔗 {B('upload_tab_url')}"):
                link_input = gr.Textbox(
                    label=B("upload_label_url"),
                    placeholder="https://example.com/paper.pdf",
                    lines=1,
                )
                gr.Markdown(f"_{B('upload_url_hint')}_")

    return {
        "file_input": file_input,
        "link_input": link_input,
        "file_summary": file_summary,
    }
