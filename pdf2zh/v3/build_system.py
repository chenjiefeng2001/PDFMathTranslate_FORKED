"""Module: BuildSystem — Phase 6.5 增量构建系统（编译器式）。

修改一个节点只重跑受影响阶段：

    Paragraph 52
      └─ translation → layout → render   （依赖链）
      字体资源变更 → 涉及块 layout/render 失效

``DependencyGraph`` 记录 node → 依赖阶段；``BuildSystem.build`` 给出
每阶段的 rebuilt/cached 集合（整合 Phase 4.5 IncrementalEngine 与
Phase 6.4 分层缓存）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set

from pdf2zh.v3.document_model import DocumentModel, block_id

STAGE_ORDER = ("parse", "semantic", "translation", "layout", "render")


@dataclass
class BuildPlan:
    stages: Dict[str, Dict[str, List[str]]] = field(default_factory=dict)
    # {stage: {"rebuilt": [...], "cached": [...]}}

    def to_dict(self) -> dict:
        return {stage: {"rebuilt": list(self.stages.get(stage, {}).get("rebuilt", [])),
                        "cached": list(self.stages.get(stage, {}).get("cached", []))}
                for stage in STAGE_ORDER}

    def summary(self) -> str:
        return " | ".join(
            f"{s}:{len(self.stages.get(s, {}).get('rebuilt', []))}Δ"
            for s in STAGE_ORDER)


class DependencyGraph:
    """node_id → 影响它的节点（dependents）+ 该节点涉及的阶段。

    ``add_dependency(dep, node)`` 表示 node 依赖 dep（dep 变更 → node 受影响）。
    """

    def __init__(self) -> None:
        self._dependents: Dict[str, Set[str]] = {}  # dep -> {受影响节点}
        self._stage_map: Dict[str, Set[str]] = {}   # node -> {阶段}

    def add_dependency(self, depends_on: str, node_id: str) -> None:
        self._dependents.setdefault(depends_on, set()).add(node_id)

    def register(self, node_id: str, stages: Sequence[str]) -> None:
        self._stage_map.setdefault(node_id, set()).update(stages)

    def register_block(self, node_id: str, is_translatable: bool = True) -> None:
        stages = {"semantic", "layout", "render"}
        if is_translatable:
            stages.add("translation")
        self.register(node_id, stages)

    def stages_of(self, node_id: str) -> Set[str]:
        return set(self._stage_map.get(node_id, set()))

    def closure(self, changed_ids: Sequence[str]) -> Set[str]:
        """变更节点 → 全受影响闭包（沿 dependents 传播）。"""
        out: Set[str] = set()
        stack = list(changed_ids or [])
        while stack:
            cur = stack.pop()
            if cur in out:
                continue
            out.add(cur)
            for dep in self._dependents.get(cur, set()):
                if dep not in out:
                    stack.append(dep)
        return out

    def from_model(self, model: DocumentModel) -> "DependencyGraph":
        for page in model.pages:
            for i, block in enumerate(page.blocks):
                pol = block.metadata.get("translation_policy") or {}
                self.register_block(block_id(page.page_num, i),
                                    is_translatable=pol.get("translate", True))
        return self


class BuildSystem:
    """增量构建：changed_ids → 每阶段 rebuilt/cached。"""

    def __init__(self, cache=None, graph: Optional[DependencyGraph] = None,
                 incremental=None) -> None:
        self.cache = cache
        self.graph = graph or DependencyGraph()
        self.incremental = incremental  # Phase 4.5 IncrementalEngine

    def build(self, model: DocumentModel,
              changed_ids: Optional[Sequence[str]] = None) -> BuildPlan:
        plan = BuildPlan()
        if changed_ids is None:
            if self.incremental is not None:
                diff = self.incremental.update(model)
                changed_ids = diff["dirty"] + diff["added"]
            else:
                changed_ids = []
        changed = set(changed_ids or [])
        affected = self.graph.closure(changed)
        pages = {int(bid.split("_")[0][1:]) for bid in affected
                 if bid.startswith("p")}
        for stage in STAGE_ORDER:
            if stage in ("parse", "render"):
                rebuilt = [f"page_{p}" for p in sorted(pages)] if affected else []
            else:
                rebuilt = sorted(
                    nid for nid in affected if stage in self.graph.stages_of(nid))
            plan.stages[stage] = {"rebuilt": rebuilt, "cached": []}
        return plan


__all__ = ["STAGE_ORDER", "BuildPlan", "DependencyGraph", "BuildSystem"]