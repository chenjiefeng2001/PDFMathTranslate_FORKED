"""Module: Query — Phase 6.3 Document Query API（类数据库查询）。

    document.query().kind("formula").execute()
    document.query().confidence_below(0.8).translated("pending").execute()
    document.query().page(3).ids()

Fluent 条件（全部可组合，AND 语义）：
    kind(*kinds) / page(pno) / where(predicate)
    translated(status: done|pending|preserved|none)
    confidence_below(threshold) / has_replacement()
"""
from __future__ import annotations

from typing import Callable, List, Optional, Sequence

from pdf2zh.v3.document_model import DocumentModel, block_id


def _translation_status(block) -> str:
    pol = block.metadata.get("translation_policy") or {}
    if pol.get("translate") is False:
        return "preserved"
    if not block.metadata.get("translate"):
        return "pending"
    if block.metadata.get("translated_same"):
        return "pending"
    return "done"


class DocumentQuery:
    """文档查询器：条件叠加，execute() 出结果。"""

    def __init__(self, model: DocumentModel) -> None:
        self.model = model
        self._kinds: Optional[set] = None
        self._page: Optional[int] = None
        self._predicates: List[Callable[[object, str], bool]] = []
        self._status: Optional[str] = None
        self._conf_below: Optional[float] = None
        self._has_replacement: Optional[bool] = None

    def kind(self, *kinds: str) -> "DocumentQuery":
        self._kinds = set(kinds)
        return self

    def page(self, pno: int) -> "DocumentQuery":
        self._page = pno
        return self

    def where(self, predicate: Callable[[object, str], bool]) -> "DocumentQuery":
        self._predicates.append(predicate)
        return self

    def translated(self, status: str = "done") -> "DocumentQuery":
        self._status = status
        return self

    def confidence_below(self, threshold: float) -> "DocumentQuery":
        self._conf_below = threshold
        return self

    def has_replacement(self, flag: bool = True) -> "DocumentQuery":
        self._has_replacement = flag
        return self

    def _matches(self, block, pno: int, i: int) -> bool:
        bid = block_id(pno, i)
        if self._kinds is not None and block.kind not in self._kinds:
            return False
        if self._page is not None and pno != self._page:
            return False
        if self._status is not None and \
                _translation_status(block) != self._status:
            return False
        if self._conf_below is not None and \
                float(block.metadata.get("confidence", 1.0) or 1.0) >= \
                self._conf_below:
            return False
        if self._has_replacement is not None:
            has = any(g.decode != "ok" for l in block.lines
                      for s in l.spans for g in s.glyphs)
            if has != self._has_replacement:
                return False
        for pred in self._predicates:
            if not pred(block, bid):
                return False
        return True

    def execute(self) -> List[dict]:
        out: List[dict] = []
        for page in self.model.pages:
            pno = page.page_num
            for i, block in enumerate(page.blocks):
                if not self._matches(block, pno, i):
                    continue
                out.append({
                    "block_id": block_id(pno, i),
                    "page": pno,
                    "kind": block.kind,
                    "text": (block.text or "")[:120],
                    "confidence": block.metadata.get("confidence"),
                    "translation_status": _translation_status(block),
                    "translated": block.metadata.get("translated"),
                })
        return out

    def ids(self) -> List[str]:
        return [r["block_id"] for r in self.execute()]

    def count(self) -> int:
        return len(self.execute())


def query(model: DocumentModel) -> DocumentQuery:
    return DocumentQuery(model)


__all__ = ["DocumentQuery", "query"]