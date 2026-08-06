"""Module: DocumentInspector — Phase 2.5 节点检查器（DevTools 式）。

给定 DocumentModel，按 block_id（``p{page}_{i}``）查看节点全貌：
Kind / BBox / Reading Order / Style（fonts/multifont）/ Translation Policy /
Font size / Relations（出边）/ Children（TOC 子节点）/ Metadata。

纯查询、无副作用；``inspect_all`` 输出整树摘要（诊断/CLI 用）。
"""
from __future__ import annotations

from typing import Dict, List, Optional

from pdf2zh.v3.document_model import (
    DocumentModel, block_id, toc_records_from_model,
)


def _find_block(doc: DocumentModel, bid: str):
    for page in doc.pages:
        pno = page.page_num
        for i, b in enumerate(page.blocks):
            if block_id(pno, i) == bid:
                return page, i, b
    return None, None, None


def _outgoing_relations(doc: DocumentModel, bid: str) -> List[dict]:
    return [{"type": r.type, "target": r.target}
            for r in doc.relations if r.source == bid]


def _children(doc: DocumentModel, bid: str) -> List[str]:
    return [r.target for r in doc.relations
            if r.type == "contains" and r.source == bid]


def inspect(doc: DocumentModel, bid: str) -> Optional[dict]:
    """查看单个节点的完整视图（找不到返回 None）。"""
    page, idx, block = _find_block(doc, bid)
    if block is None:
        return None
    md = block.metadata or {}
    return {
        "block_id": bid,
        "page": page.page_num,
        "index": idx,
        "kind": block.kind,
        "text": block.text,
        "bbox": [round(v, 2) for v in block.bbox],
        "font_size": round(block.font_size, 2),
        "reading_order": md.get("reading_order"),
        "role": md.get("role"),
        "role_confidence": md.get("role_confidence"),
        "style": {"fonts": md.get("fonts", {}),
                  "multifont": md.get("multifont", False)},
        "translation_policy": md.get("translation_policy"),
        "translated": md.get("translated"),
        "render_path": md.get("render_path"),
        "typography": md.get("typography"),
        "anomaly": md.get("anomaly"),
        "relations": _outgoing_relations(doc, bid),
        "children": _children(doc, bid),
        "metadata": {k: v for k, v in md.items()
                     if k not in ("fonts", "translation_policy",
                                  "translated", "render_path",
                                  "typography")},
    }


def inspect_all(doc: DocumentModel) -> List[dict]:
    """整树摘要（每节点一行）：id/kind/role/text 前 40 字符。"""
    out: List[dict] = []
    for page in doc.pages:
        pno = page.page_num
        for i, b in enumerate(page.blocks):
            out.append({
                "block_id": block_id(pno, i),
                "page": pno,
                "index": i,
                "kind": b.kind,
                "role": b.metadata.get("role"),
                "text": (b.text or "")[:40],
            })
    return out


def inspect_toc(doc: DocumentModel) -> List[dict]:
    """目录视图：模型 toc 块的层级摘要（块 id + 编号 + 页）。"""
    return [{
        "block_id": r["block_id"],
        "number": r["number"],
        "title": r["title"],
        "page": r["page"],
        "translated": r["translated_title"],
    } for r in toc_records_from_model(doc)]


def inspect_layout(doc: DocumentModel, page_num: Optional[int] = None
                   ) -> List[dict]:
    """Layout Inspector：逐 Paragraph 输出排版诊断行。

    每行给出 Source Blocks（序）、行明细（size/alignment）、Font 来源
    （major vs max）、字号比、对齐摘要与 Layout 切分 provenance ——
    直接支撑 L2/L3/L4 排查。纯查询，无副作用。
    """
    rows: List[dict] = []
    for page in doc.pages:
        if page_num is not None and page.page_num != page_num:
            continue
        for i, b in enumerate(page.blocks):
            md = b.metadata or {}
            rows.append({
                "block_id": block_id(page.page_num, i),
                "page": page.page_num,
                "index": i,
                "kind": b.kind,
                "role": md.get("role"),
                "role_confidence": md.get("role_confidence"),
                "text": (b.text or "")[:80],
                "bbox": [round(v, 2) for v in b.bbox],
                "lines": len(b.lines),
                "line_sizes": list(md.get("line_sizes") or []),
                "line_alignments": list(md.get("line_alignments") or []),
                "alignment": md.get("alignment"),
                "font_major": md.get("font_major"),
                "font_size": round(float(md.get("font_size") or b.font_size or 0.0), 2),
                "font_size_max": md.get("font_size_max"),
                "font_size_ratio": md.get("font_size_ratio"),
                "font_uniform": md.get("font_uniform"),
                "multifont": md.get("multifont"),
                "fonts": md.get("fonts"),
                "layout_split": bool(md.get("layout_split")),
                "layout_provenance": md.get("layout_provenance"),
                "render_path": md.get("render_path"),
                "translated": md.get("translated"),
                "node_confidence": md.get("node_confidence"),
            })
    return rows


def build_layout_report(doc: DocumentModel) -> dict:
    """Layout 巡检报告：逐 Paragraph 明细 + 问题清单（Inspector 取证）。

    问题条目：
      - ``split``       （Lv2 段内拆块 provenance，标注是哪个边界为何被拆）
      - ``size_blend``  （字号来源不设 major：ratio > 1.6，即引 max 污染）
      - ``align_mixed`` （段内对齐混杂，标题/正文曾被并入同段）
    无问题返回全空字段，供 GUI 渲染。
    """
    try:
        rows = inspect_layout(doc)
    except Exception:  # noqa: BLE001
        return None
    issues: List[dict] = []
    for r in rows:
        if r["layout_split"] and r.get("layout_provenance"):
            issues.append({
                "kind": "split", "node": r["block_id"], "page": r["page"],
                "why": r["layout_provenance"], "severity": "info",
            })
        ratio = r.get("font_size_ratio") or 0.0
        if isinstance(ratio, float) and ratio > 1.6:
            issues.append({
                "kind": "size_blend", "node": r["block_id"], "page": r["page"],
                "why": f"size={r['font_size']} max={r.get('font_size_max')} "
                       f"ratio={ratio:.2f}",
                "severity": "warning",
            })
        aligns = r.get("line_alignments") or []
        if aligns and len(set(aligns)) > 1:
            issues.append({
                "kind": "align_mixed", "node": r["block_id"], "page": r["page"],
                "why": " -> ".join(str(a) for a in aligns),
                "severity": "info",
            })
    return {
        "column_names": [
            "block_id", "kind", "role", "text", "lines", "font_size",
            "font_size_max", "font_size_ratio", "alignment",
            "line_alignments", "line_sizes", "font_major", "layout_split",
            "layout_provenance", "render_path", "node_confidence",
        ],
        "paragraphs": rows,
        "issues": issues,
        "stats": {
            "blocks": len(rows),
            "issues": len(issues),
            "splits": sum(1 for i in issues if i["kind"] == "split"),
            "size_blends": sum(1 for i in issues if i["kind"] == "size_blend"),
            "align_mixed": sum(1 for i in issues if i["kind"] == "align_mixed"),
        },
    }


__all__ = ["inspect", "inspect_all", "inspect_toc",
           "inspect_layout", "build_layout_report"]