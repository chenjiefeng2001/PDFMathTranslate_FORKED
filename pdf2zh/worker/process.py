"""process_page — 将 translate_patch 结果转换为 PageProcessOutput。

Phase 1.1 核心：
    process_page(task, doc_zh, obj_patch, page_xref_map) -> List[PageProcessOutput]

这个函数在 worker 内部调用，将 translate_patch 的 obj_patch 结果
转换为 PageProcessOutput，供两个 adapter 使用。

注意：
    这个函数不修改 doc_zh 或 obj_patch。
    它只是读取结果并构建 PageProcessOutput。
"""

from __future__ import annotations

import hashlib
from typing import Dict, List, Optional, Tuple

from pdf2zh.assembler.base import (
    FontIdentity,
    FontRequirement,
    PageMetrics,
    PageSize,
    ResourceRequirement,
    SCHEMA_VERSION,
)
from pdf2zh.worker.page_output import PageProcessOutput


def process_page(
    page_index: int,
    doc_zh: object,
    obj_patch: Dict[int, str],
    page_xref_map: Optional[Dict[int, int]],
) -> PageProcessOutput:
    """将 translate_patch 结果转换为 PageProcessOutput。

    Args:
        page_index: 页号
        doc_zh: pymupdf.Document (已应用 obj_patch)
        obj_patch: {xref: content_stream_string} dict
        page_xref_map: {page_number: xref} 映射

    Returns:
        PageProcessOutput (worker 内部 ABI)
    """
    import pymupdf as _fitz

    # 获取页面信息
    page = doc_zh[page_index]
    rect = page.rect
    page_size = PageSize(width=rect.width, height=rect.height)

    # 计算 source_page_hash (基于页面内容)
    source_hash = hashlib.sha256(f"page_{page_index}".encode()).hexdigest()[:16]

    # 收集该页面相关的 xrefs
    page_xrefs = _get_page_xrefs(page_index, page_xref_map, obj_patch)

    # 合并该页面的 content streams
    content_stream = _merge_page_content(page_xrefs, obj_patch)

    # 收集字体信息
    resources = _collect_resources(doc_zh, page)

    # 收集注释和链接
    annotations = _collect_annotations(page)
    links = _collect_links(page)

    # 构建 metrics
    metrics = _build_metrics(page)

    output = PageProcessOutput(
        page_index=page_index,
        source_page_hash=source_hash,
        content_stream=content_stream,
        resources=resources,
        annotations=annotations,
        links=links,
        page_size=page_size,
        metrics=metrics,
    )
    output.validate()
    return output


def _get_page_xrefs(
    page_index: int,
    page_xref_map: Optional[Dict[int, int]],
    obj_patch: Dict[int, str],
) -> List[int]:
    """获取该页面相关的 xref IDs。"""
    xrefs = []

    # 从 page_xref_map 获取页面主 xref
    if page_xref_map and page_index in page_xref_map:
        main_xref = page_xref_map[page_index]
        if main_xref in obj_patch:
            xrefs.append(main_xref)

    # Phase 1.2: 只返回该页面的主 xref
    # 不再包含其他页面的 xrefs
    return xrefs


def _merge_page_content(
    page_xrefs: List[int],
    obj_patch: Dict[int, str],
) -> bytes:
    """合并该页面的 content streams。"""
    parts = []
    for xref in page_xrefs:
        if xref in obj_patch:
            content = obj_patch[xref]
            if isinstance(content, str):
                parts.append(content.encode())
            elif isinstance(content, bytes):
                parts.append(content)
    return b"\n".join(parts)


def _collect_resources(doc_zh: object, page: object) -> ResourceRequirement:
    """收集页面资源信息。"""
    fonts = []

    # 从页面 Resources 中提取字体
    try:
        xref = page.xref
        # 获取页面的字体列表
        font_list = doc_zh.get_page_fonts(xref)
        for font_info in font_list:
            # font_info 格式: (xref, name, type, encoding, ...)
            if len(font_info) >= 2:
                font_xref = font_info[0]
                font_name = font_info[1]
                fonts.append(
                    FontIdentity(
                        name=font_name,
                        obj_ref=font_xref,
                        encoding="Identity-H",
                    )
                )
    except Exception:
        pass

    return ResourceRequirement(
        fonts=[FontRequirement(identity=f) for f in fonts],
    )


def _collect_annotations(page: object) -> List[dict]:
    """收集页面注释。"""
    annotations = []
    try:
        for annot in page.annots():
            annotations.append(
                {
                    "type": annot.type[0] if annot.type else "unknown",
                    "rect": list(annot.rect),
                }
            )
    except Exception:
        pass
    return annotations


def _collect_links(page: object) -> List[dict]:
    """收集页面链接。"""
    links = []
    try:
        for link in page.get_links():
            links.append(
                {
                    "kind": link.get("kind", 0),
                    "from": list(link.get("from", (0, 0, 0, 0))),
                    "to": list(link.get("to", (0, 0, 0, 0))),
                }
            )
    except Exception:
        pass
    return links


def _build_metrics(page: object) -> PageMetrics:
    """构建页面指标。"""
    return PageMetrics(
        elapsed=0.0,
        translation_chars=0,
        translation_api_calls=0,
        layout_regions=0,
    )
