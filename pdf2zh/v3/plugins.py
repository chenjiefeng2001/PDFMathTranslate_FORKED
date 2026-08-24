"""Module: Plugins — Phase 6.6 插件架构（DocumentPlugin.process(doc)）。

新增能力 = 注册插件，不改核心：

    PluginRegistry
     ├── PassPlugin（包装 DocumentPass：Semantic/Normalize/...）
     ├── TranslatePlugin（翻译后端包装）
     └── ExportPlugin（Markdown/HTML 导出）

所有插件只操作 DocumentModel（统一数据源）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from pdf2zh.v3.document_model import DocumentModel


class DocumentPlugin:
    """插件基类：process(doc) 只写 metadata/结构，返回 stats。"""

    name = "base"
    stage = "semantic"

    def process(self, doc: DocumentModel) -> dict:
        raise NotImplementedError


class PassPlugin(DocumentPlugin):
    """包装 DocumentPass（Phase 2 框架）为插件。"""

    def __init__(self, pass_obj, name: Optional[str] = None) -> None:
        self.pass_obj = pass_obj
        self.name = name or getattr(pass_obj, "name", "pass")
        self.stage = "semantic"

    def process(self, doc: DocumentModel) -> dict:
        return self.pass_obj.run(doc) or {}


class TranslatePlugin(DocumentPlugin):
    """翻译后端插件：translate_fn(text) -> str。"""

    def __init__(
        self, translate_fn: Callable[[str], str], name: str = "translate"
    ) -> None:
        self.translate_fn = translate_fn
        self.name = name
        self.stage = "translation"

    def process(self, doc: DocumentModel) -> dict:
        from pdf2zh.v3.document_model import translate_document

        return translate_document(doc, self.translate_fn)


class ExportPlugin(DocumentPlugin):
    """导出插件：process 把产物存进 self.output。"""

    def __init__(
        self, exporter: Callable[[DocumentModel], str], name: str = "export"
    ) -> None:
        self.exporter = exporter
        self.name = name
        self.stage = "export"
        self.output: Optional[str] = None

    def process(self, doc: DocumentModel) -> dict:
        self.output = self.exporter(doc)
        return {"bytes": len(self.output or "")}


class PluginRegistry:
    """插件注册表：register / run（按 stage）/ available。"""

    def __init__(self) -> None:
        self._plugins: List[DocumentPlugin] = []
        self.outputs: Dict[str, object] = {}

    def register(self, plugin: DocumentPlugin) -> "PluginRegistry":
        self._plugins.append(plugin)
        return self

    def run(self, doc: DocumentModel, stage: Optional[str] = None) -> dict:
        results = {}
        for plugin in self._plugins:
            if stage is not None and plugin.stage != stage:
                continue
            try:
                results[plugin.name] = plugin.process(doc) or {}
                if isinstance(plugin, ExportPlugin):
                    self.outputs[plugin.name] = plugin.output
            except Exception as e:  # noqa: BLE001 — 插件容错
                results[plugin.name] = {"error": str(e)[:120]}
        return results

    def available(self) -> List[str]:
        return [p.name for p in self._plugins]

    def get(self, name: str) -> Optional[DocumentPlugin]:
        for p in self._plugins:
            if p.name == name:
                return p
        return None


__all__ = [
    "DocumentPlugin",
    "PassPlugin",
    "TranslatePlugin",
    "ExportPlugin",
    "PluginRegistry",
]
