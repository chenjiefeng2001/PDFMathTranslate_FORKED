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


__all__ = ["inspect", "inspect_all", "inspect_toc"]