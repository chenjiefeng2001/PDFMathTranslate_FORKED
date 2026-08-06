"""Module: Exports — Phase 6.8 多输出（同一 Document 导出多种格式）。

    export_markdown(doc)  → Markdown（# 标题 / - 目录 / $公式$ / 代码围栏 / 表格）
    export_html(doc)      → 最小 HTML（块级结构保持）
    export_text(doc)      → 纯文本（翻译优先）

渲染只读 Document（含 translated/kind），无 PDF 依赖。
"""
from __future__ import annotations

import html as _html

from pdf2zh.v3.document_model import DocumentModel


def _text_of(block) -> str:
    return (block.metadata.get("translated")
            or block.text or "").strip() or (block.text or "")


def export_markdown(doc: DocumentModel) -> str:
    out: list = []
    for page in doc.pages:
        for block in page.blocks:
            kind = block.kind
            text = _text_of(block)
            if not text:
                continue
            if kind == "heading":
                level = min(6, max(1, int(block.metadata.get(
                    "heading_level", 1) or 1)))
                out.append(f"{'#' * level} {text}")
            elif kind == "toc":
                num = block.metadata.get("toc_number", "")
                page_no = block.metadata.get("toc_page", "")
                title = block.metadata.get("toc_title", text)
                out.append(f"- {num} {title} {'....' if page_no else ''} {page_no}".strip())
            elif kind == "formula":
                out.append(f"$${text}$$")
            elif kind == "code":
                out.append(f"```\n{text}\n```")
            elif kind == "table":
                out.append("| " + " | ".join(text.replace("|", "/").split())
                           + " |")
            elif kind == "caption":
                out.append(f"*{text}*")
            else:
                out.append(text)
            out.append("")
    return "\n".join(out).strip() + "\n"


def export_html(doc: DocumentModel) -> str:
    body: list = []
    for page in doc.pages:
        body.append(f'<section data-page="{page.page_num}">')
        for block in page.blocks:
            text = _html.escape(_text_of(block))
            if not text:
                continue
            kind = block.kind
            if kind == "heading":
                body.append(f"<h{min(6, max(1, int(block.metadata.get('heading_level', 1) or 1)))}>{text}</h{min(6, max(1, int(block.metadata.get('heading_level', 1) or 1)))}>")
            elif kind == "toc":
                body.append(f'<div class="toc">{text}</div>')
            elif kind == "formula":
                body.append(f'<span class="formula">{text}</span>')
            elif kind == "code":
                body.append(f"<pre><code>{text}</code></pre>")
            elif kind == "table":
                body.append(f"<div class=\"table\">{text}</div>")
            elif kind == "caption":
                body.append(f"<figcaption>{text}</figcaption>")
            else:
                body.append(f"<p>{text}</p>")
        body.append("</section>")
    return ("<!DOCTYPE html><html><head><meta charset='utf-8'>"
            "<title>export</title></head><body>"
            + "\n".join(body) + "</body></html>")


def export_text(doc: DocumentModel) -> str:
    parts = []
    for page in doc.pages:
        for block in page.blocks:
            text = _text_of(block)
            if text:
                parts.append(text)
    return "\n".join(parts)


__all__ = ["export_markdown", "export_html", "export_text"]