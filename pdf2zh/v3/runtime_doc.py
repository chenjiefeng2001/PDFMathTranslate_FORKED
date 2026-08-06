"""Module: RuntimeDoc — Phase 6.1/6.8 Document Runtime（DOM 固化 + Runtime API）。

文档从「Parser 中间产物」升级为长期存在、可查询、可修改、可增量更新的
运行环境：

    DocumentRuntime
     ├── model          （DocumentModel，唯一数据源）
     ├── versions       （VersionManager：每节点版本历史 / undo / diff）
     ├── resources      （ResourceManager：字体/图片注册表）
     ├── cache          （DocumentCache：五层缓存）
     ├── build          （BuildSystem：增量构建）
     └── plugins        （PluginRegistry：插件）

Runtime API（浏览器 DOM API 式）：
    runtime.open(model) / edit(node_id, text=...) / undo()
    runtime.query() / translate(fn) / build() / render_page(pno)
    runtime.export("markdown") / inspect(node_id)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

from pdf2zh.v3.cache import DocumentCache
from pdf2zh.v3.document_model import (
    DocumentModel, block_id,
)
from pdf2zh.v3.exports import export_html, export_markdown, export_text
from pdf2zh.v3.query import DocumentQuery, query
from pdf2zh.v3.resources import ResourceManager


@dataclass
class NodeRevision:
    version: int = 0
    field: str = ""
    value: object = None

    def to_dict(self) -> dict:
        return {"version": self.version, "field": self.field,
                "value": self.value}


class VersionManager:
    """每节点版本历史（Git 式）：edit/undo/diff/version。

    每条 revision 记录**被替换的旧值**（用于 undo 恢复与 diff 对比）。
    """

    def __init__(self) -> None:
        self.history: Dict[str, List[NodeRevision]] = {}
        self._counter = 0
        self._snapshot: Dict[str, Dict[str, object]] = {}

    def record(self, node_id: str, field: str, old_value: object) -> int:
        """记录一次变更：``old_value`` 为变更前的值（undo 时恢复它）。"""
        self._counter += 1
        rev = NodeRevision(self._counter, field, old_value)
        self.history.setdefault(node_id, []).append(rev)
        self._snapshot.setdefault(node_id, {})[field] = old_value
        return self._counter

    def snapshot(self, node_id: str) -> Dict[str, object]:
        return dict(self._snapshot.get(node_id, {}))

    def version(self, node_id: str) -> int:
        revs = self.history.get(node_id, [])
        return revs[-1].version if revs else 0

    def history_of(self, node_id: str) -> List[NodeRevision]:
        return list(self.history.get(node_id, []))

    def undo(self, node_id: str) -> Optional[NodeRevision]:
        revs = self.history.get(node_id)
        if not revs:
            return None
        last = revs.pop()
        self._snapshot[node_id].pop(last.field, None)
        return last

    def diff(self, node_id: str, v1: int, v2: int) -> Optional[dict]:
        """两个版本间该字段的变化（无历史 → None）。"""
        revs = self.history.get(node_id, [])
        vals = {r.version: (r.field, r.value) for r in revs}
        a = vals.get(v1)
        b = vals.get(v2)
        if a is None or b is None:
            return None
        return {"field": b[0], "before": a[1], "after": b[1]}


class DocumentRuntime:
    """文档运行时：长期存在、可查询、可修改、可增量更新。"""

    def __init__(self, model: Optional[DocumentModel] = None,
                 cache: Optional[DocumentCache] = None,
                 resources: Optional[ResourceManager] = None) -> None:
        self.model = model or DocumentModel()
        self.versions = VersionManager()
        self.cache = cache or DocumentCache()
        self.resources = resources or ResourceManager()
        self.resources.from_model(self.model)
        self._edits: Dict[str, Dict[str, object]] = {}

    # ── Runtime API ──────────────────────────────────────────────────

    def open(self, model: DocumentModel) -> "DocumentRuntime":
        """装载文档（登记资源 + 版本基线）。"""
        self.model = model
        self.resources = ResourceManager().from_model(model)
        for page in model.pages:
            for i, block in enumerate(page.blocks):
                self.versions.record(block_id(page.page_num, i),
                                     "text", block.text)
        return self

    def query(self) -> DocumentQuery:
        return query(self.model)

    def edit(self, node_id: str, **fields) -> dict:
        """版本化编辑：改 text/translated 等字段，记录历史 + 失效缓存。"""
        block = self._find_block(node_id)
        if block is None:
            return {"ok": False, "reason": "block not found"}
        for field, value in fields.items():
            if not hasattr(block, field):
                continue
            old = getattr(block, field)
            self.versions.record(node_id, field, old)
            setattr(block, field, value)
        page = self._page_of(node_id)
        if page is not None:
            self.cache.invalidate_page(page)
        self._edits[node_id] = dict(fields)
        return {"ok": True, "node_id": node_id, "fields": dict(fields),
                "version": self.versions.version(node_id)}

    def undo(self, node_id: str) -> Optional[dict]:
        """撤销该节点最近一次编辑（恢复字段值）。"""
        rev = self.versions.undo(node_id)
        if rev is None:
            return None
        block = self._find_block(node_id)
        if block is not None and hasattr(block, rev.field):
            setattr(block, rev.field, rev.value)
        page = self._page_of(node_id)
        if page is not None:
            self.cache.invalidate_page(page)
        return {"node_id": node_id, "reverted": rev.to_dict()}

    def translate(self, translate_fn: Callable[[str], str],
                  context_aware: bool = False) -> dict:
        """翻译（带缓存；context_aware 走上下文翻译）。"""
        if context_aware:
            from pdf2zh.v3.context_translation import (
                translate_document_context_aware,
            )
            return translate_document_context_aware(
                self.model, lambda t, c: self.cache.translate(t, translate_fn))
        from pdf2zh.v3.document_model import translate_document
        return translate_document(
            self.model, lambda t: self.cache.translate(t, translate_fn))

    def build(self, changed_ids: Optional[Sequence[str]] = None) -> dict:
        """增量构建计划（dirty 才重建）。"""
        from pdf2zh.v3.build_system import BuildSystem, DependencyGraph
        graph = DependencyGraph().from_model(self.model)
        system = BuildSystem(cache=self.cache, graph=graph)
        plan = system.build(self.model, changed_ids)
        return plan.to_dict()

    def render_page(self, pno: int) -> dict:
        """页渲染计划（layout+render 缓存键按内容哈希）。"""
        key = f"p{pno}"
        cached = self.cache.get("render", key)
        if cached is not None:
            return dict(cached, cached=True)
        plan = []
        for page in self.model.pages:
            if page.page_num != pno:
                continue
            for i, block in enumerate(page.blocks):
                plan.append({
                    "block_id": block_id(pno, i),
                    "kind": block.kind,
                    "text": block.metadata.get("translated", block.text),
                    "bbox": [round(v, 2) for v in block.bbox],
                    "render_path": block.metadata.get("render_path",
                                                      "translate_refit"),
                })
        result = {"page": pno, "blocks": plan, "cached": False}
        self.cache.set("render", key, result)
        return result

    def export(self, fmt: str = "markdown") -> str:
        """多输出：markdown / html / text。"""
        if fmt == "markdown":
            return export_markdown(self.model)
        if fmt == "html":
            return export_html(self.model)
        if fmt == "text":
            return export_text(self.model)
        raise ValueError(f"unsupported format: {fmt}")

    def inspect(self, node_id: str) -> Optional[dict]:
        """DevTools 式节点视图（Phase 6.9：含版本/缓存状态）。"""
        from pdf2zh.v3.document_inspector import inspect
        view = inspect(self.model, node_id)
        if view is None:
            return None
        view["version"] = self.versions.version(node_id)
        view["version_history"] = [
            r.to_dict() for r in self.versions.history_of(node_id)[-5:]]
        view["cached_pages"] = [
            {"layer": layer, "size": self.cache.layers[layer].size}
            for layer in self.cache.layers]
        view["resource_fonts"] = list(self.resources.fonts)
        return view

    # ── 内部 ─────────────────────────────────────────────────────────

    def _find_block(self, node_id: str):
        for page in self.model.pages:
            pno = page.page_num
            for i, block in enumerate(page.blocks):
                if block_id(pno, i) == node_id:
                    return block
        return None

    def _page_of(self, node_id: str):
        for page in self.model.pages:
            for i, block in enumerate(page.blocks):
                if block_id(page.page_num, i) == node_id:
                    return page.page_num
        return None

    def summary(self) -> str:
        return (f"DocumentRuntime pages={len(self.model.pages)} "
                f"versions={self.versions._counter} "
                f"cache={self.cache.stats().summary()}")


__all__ = ["NodeRevision", "VersionManager", "DocumentRuntime"]