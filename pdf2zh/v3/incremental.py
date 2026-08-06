"""Module: Incremental — Phase 4.5 增量重建（编译器式性能层）。

修改单个段落不应整份 PDF 重生成：IncrementalEngine 缓存每个块的
内容哈希（kind/text/policy/translated），``update`` 只把变化的块标为
dirty，其余命中缓存 —— 语义节点 → 布局 → 渲染只重跑脏节点。

    engine = IncrementalEngine(model)
    plan = engine.update(model, "p10_3", new_text="...")
    # plan = {"dirty": ["p10_3"], "cached": [...], "added": [...], "removed": [...]}
"""
from __future__ import annotations

import hashlib
from typing import Dict, List, Optional

from pdf2zh.v3.document_model import DocumentModel, block_id


def node_hash(block) -> str:
    """块内容稳定哈希（kind/text/policy/translated）。"""
    pol = block.metadata.get("translation_policy") or {}
    payload = "|".join([
        block.kind or "",
        (block.text or ""),
        str(pol.get("translate")),
        str(block.metadata.get("translated", "")),
    ])
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


class IncrementalEngine:
    """块级缓存：register → update → {dirty, cached, added, removed}。"""

    def __init__(self) -> None:
        self._cache: Dict[str, str] = {}  # block_id -> hash

    def register(self, model: DocumentModel) -> int:
        count = 0
        for page in model.pages:
            pno = page.page_num
            for i, block in enumerate(page.blocks):
                self._cache[block_id(pno, i)] = node_hash(block)
                count += 1
        return count

    def update(self, model: DocumentModel) -> dict:
        """对比当前模型与缓存，返回增量计划（dirty 才需重建）。"""
        current: Dict[str, str] = {}
        for page in model.pages:
            pno = page.page_num
            for i, block in enumerate(page.blocks):
                current[block_id(pno, i)] = node_hash(block)
        dirty = [bid for bid, h in current.items()
                 if self._cache.get(bid) != h]
        cached = [bid for bid, h in current.items()
                  if self._cache.get(bid) == h]
        added = [bid for bid in current if bid not in self._cache]
        removed = [bid for bid in self._cache if bid not in current]
        for bid, h in current.items():
            self._cache[bid] = h
        for bid in removed:
            self._cache.pop(bid, None)
        return {"dirty": sorted(dirty), "cached": sorted(cached),
                "added": sorted(added), "removed": sorted(removed)}

    def rebuild_plan(self, model: DocumentModel,
                     dirty_ids: Optional[List[str]] = None) -> List[dict]:
        """只对脏节点生成重建计划（render_plan 子集）。"""
        dirty = set(dirty_ids) if dirty_ids is not None else None
        plan: List[dict] = []
        for page in model.pages:
            pno = page.page_num
            for i, block in enumerate(page.blocks):
                bid = block_id(pno, i)
                if dirty is not None and bid not in dirty:
                    continue
                plan.append({
                    "block_id": bid,
                    "page": pno,
                    "kind": block.kind,
                    "text": block.text,
                    "translated": block.metadata.get("translated",
                                                     block.text),
                    "src_box": [round(v, 2) for v in block.bbox],
                })
        return plan

    @property
    def cache_size(self) -> int:
        return len(self._cache)


__all__ = ["node_hash", "IncrementalEngine"]