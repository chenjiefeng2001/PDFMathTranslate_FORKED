"""Module: V5 Execution Graph."""
from __future__ import annotations
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Set

logger = logging.getLogger(__name__)

class ExecutionNodeState(Enum):
    NEW = "new"
    ANALYZED = "analyzed"
    PLANNED = "planned"
    TRANSLATED = "translated"
    LAYOUTED = "layoued"
    RENDERED = "rendered"
    VERIFIED = "verified"
    DONE = "done"
    FAILED = "failed"

@dataclass
class ExecutionNode:
    node_id: str
    label: str = ""
    state: ExecutionNodeState = ExecutionNodeState.NEW
    dirty: bool = True
    depends_on: Set[str] = field(default_factory=set)
    dependents: Set[str] = field(default_factory=set)
    metadata: dict = field(default_factory=dict)
    def mark_dirty(self) -> None:
        self.dirty = True
    def mark_clean(self) -> None:
        self.dirty = False
    def advance(self, new_state: ExecutionNodeState) -> None:
        self.state = new_state

class ExecutionGraph:
    def __init__(self) -> None:
        self._nodes: Dict[str, ExecutionNode] = {}
    def add_node(self, node_id: str, label: str = "", depends_on: Optional[List[str]] = None) -> ExecutionNode:
        if node_id in self._nodes:
            raise ValueError(f"ExecutionNode '{node_id}' already exists")
        n = ExecutionNode(node_id=node_id, label=label)
        n.dirty = True
        self._nodes[node_id] = n
        if depends_on:
            for dep in depends_on:
                if dep in self._nodes:
                    n.depends_on.add(dep)
                    self._nodes[dep].dependents.add(node_id)
        return n
    def remove_node(self, node_id: str) -> None:
        n = self._nodes.pop(node_id, None)
        if n:
            for dep in n.depends_on:
                if dep in self._nodes:
                    self._nodes[dep].dependents.discard(node_id)
            for dep_id in list(n.dependents):
                if dep_id in self._nodes:
                    self._nodes[dep_id].depends_on.discard(node_id)
    def get_node(self, node_id: str) -> Optional[ExecutionNode]:
        return self._nodes.get(node_id)
    def has_node(self, node_id: str) -> bool:
        return node_id in self._nodes
    def mark_dirty(self, node_id: str, cascade: bool = True) -> None:
        n = self._nodes.get(node_id)
        if n is None:
            raise KeyError(f"ExecutionNode '{node_id}' not found")
        n.mark_dirty()
        if cascade:
            for dep_id in n.dependents:
                self.mark_dirty(dep_id, cascade=True)
    def mark_clean(self, node_id: str) -> None:
        n = self._nodes.get(node_id)
        if n:
            n.mark_clean()
    def set_state(self, node_id: str, state: ExecutionNodeState) -> None:
        n = self._nodes.get(node_id)
        if n is None:
            raise KeyError(f"ExecutionNode '{node_id}' not found")
        n.advance(state)
    def get_execution_order(self) -> List[ExecutionNode]:
        in_deg = {nid: 0 for nid in self._nodes}
        for nid, n in self._nodes.items():
            for d in n.depends_on:
                if d in in_deg:
                    in_deg[nid] = in_deg.get(nid, 0) + 1
        q = [nid for nid, d in in_deg.items() if d == 0]
        ordered = []
        while q:
            nid = q.pop(0)
            ordered.append(self._nodes[nid])
            for oid, on in self._nodes.items():
                if nid in on.depends_on:
                    in_deg[oid] -= 1
                    if in_deg[oid] == 0:
                        q.append(oid)
        return ordered
    def get_ready_nodes(self) -> List[ExecutionNode]:
        ready = []
        for n in self._nodes.values():
            if not n.dirty:
                continue
            deps_ok = all(
                self._nodes.get(d) is not None and not self._nodes[d].dirty
                and self._nodes[d].state != ExecutionNodeState.NEW
                for d in n.depends_on
            )
            if deps_ok:
                ready.append(n)
        return ready
    def get_dirty_nodes(self) -> List[ExecutionNode]:
        return [n for n in self._nodes.values() if n.dirty]
    def get_nodes_by_state(self, state: ExecutionNodeState) -> List[ExecutionNode]:
        return [n for n in self._nodes.values() if n.state == state]
    def reset_all(self) -> None:
        for n in self._nodes.values():
            n.state = ExecutionNodeState.NEW
            n.dirty = True
    @property
    def node_count(self) -> int:
        return len(self._nodes)
    @property
    def dirty_count(self) -> int:
        return sum(1 for n in self._nodes.values() if n.dirty)
    def stats(self) -> dict:
        by_state = {}
        for s in ExecutionNodeState:
            by_state[s.value] = sum(1 for n in self._nodes.values() if n.state == s)
        return {"total": self.node_count, "dirty": self.dirty_count, "by_state": by_state}

__all__ = ["ExecutionNodeState", "ExecutionNode", "ExecutionGraph"]
