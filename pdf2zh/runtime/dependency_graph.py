"""DependencyGraph — Artifact 依赖图。

追踪 PDF 对象之间的依赖关系，支持精确失效。

用法：
    graph = DependencyGraph()
    graph.add_dependency("glossary:Transformer", "page:3:block:8")
    graph.invalidate("glossary:Transformer")
    # 只失效 3 个 block
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set


@dataclass
class DependencyEdge:
    """依赖边。"""

    source: str = ""
    target: str = ""
    dependency_type: str = "translation"  # translation, layout, glossary, policy
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class InvalidationResult:
    """失效结果。"""

    invalidated: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    total_affected: int = 0

    @property
    def efficiency(self) -> float:
        """失效效率（越低越好）。"""
        if self.total_affected == 0:
            return 1.0
        return len(self.invalidated) / self.total_affected


class DependencyGraph:
    """依赖图。"""

    def __init__(self) -> None:
        # 正向依赖：source → [targets]
        self._forward: Dict[str, List[str]] = defaultdict(list)
        # 反向依赖：target → [sources]
        self._backward: Dict[str, List[str]] = defaultdict(list)
        # 元数据
        self._metadata: Dict[str, Dict] = {}

    def add_dependency(
        self,
        source: str,
        target: str,
        dep_type: str = "translation",
        metadata: Optional[Dict] = None,
    ) -> None:
        """添加依赖。"""
        self._forward[source].append(target)
        self._backward[target].append(source)
        if metadata:
            self._metadata[f"{source}->{target}"] = metadata

    def invalidate(self, source: str) -> InvalidationResult:
        """失效依赖。"""
        result = InvalidationResult()
        visited: Set[str] = set()
        queue = [source]

        while queue:
            node = queue.pop(0)
            if node in visited:
                continue
            visited.add(node)

            # 失效当前节点
            if node != source:
                result.invalidated.append(node)

            # 继续传播
            for target in self._forward.get(node, []):
                if target not in visited:
                    queue.append(target)

        result.total_affected = len(visited) - 1
        return result

    def get_dependents(self, source: str) -> List[str]:
        """获取依赖项。"""
        return list(self._forward.get(source, []))

    def get_dependencies(self, target: str) -> List[str]:
        """获取被依赖项。"""
        return list(self._backward.get(target, []))

    def get_translation_deps(self, page: int) -> List[str]:
        """获取页面翻译依赖。"""
        prefix = f"page:{page}:"
        return [k for k in self._backward.keys() if k.startswith(prefix)]

    def add_glossary_deps(
        self,
        term: str,
        affected_blocks: List[str],
    ) -> None:
        """添加术语表依赖。"""
        source = f"glossary:{term}"
        for block in affected_blocks:
            self.add_dependency(source, block, "glossary")

    def add_policy_deps(
        self,
        policy_version: int,
        affected_blocks: List[str],
    ) -> None:
        """添加策略依赖。"""
        source = f"policy:v{policy_version}"
        for block in affected_blocks:
            self.add_dependency(source, block, "policy")

    def summary(self) -> str:
        sources = len(self._forward)
        targets = len(self._backward)
        edges = sum(len(v) for v in self._forward.values())
        return f"DependencyGraph: {sources} sources, {targets} targets, {edges} edges"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "forward": dict(self._forward),
            "backward": dict(self._backward),
        }
