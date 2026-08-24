"""Module: V5 Workflow Engine."""

from __future__ import annotations
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set
from pdf2zh.v3.scheduler import Task, TaskStatus, TaskGraph

logger = logging.getLogger(__name__)


class WorkflowNodeType(Enum):
    TASK = "task"
    CONDITION = "condition"
    PARALLEL = "parallel"
    MERGE = "merge"
    LOOP = "loop"


ConditionPredicate = Callable[[Dict[str, Any]], bool]


@dataclass
class WorkflowNode:
    id: str
    name: str
    node_type: WorkflowNodeType = WorkflowNodeType.TASK
    handler: Optional[Callable] = None
    priority: int = 50
    max_retries: int = 2
    predicate: Optional[ConditionPredicate] = None
    if_true: str = ""
    if_false: str = ""
    parallel_branches: List[str] = field(default_factory=list)
    loop_body: List[str] = field(default_factory=list)
    loop_condition: Optional[ConditionPredicate] = None
    max_iterations: int = 10
    dependencies: Set[str] = field(default_factory=set)
    status: TaskStatus = TaskStatus.PENDING
    retry_count: int = 0
    result: Any = None
    error: Optional[str] = None

    def depends_on(self, dep_id: str) -> None:
        self.dependencies.add(dep_id)


class WorkflowEngine:
    def __init__(self) -> None:
        self._nodes: Dict[str, WorkflowNode] = {}
        self._name_index: Dict[str, str] = {}

    def _register(self, node):
        if node.id in self._nodes:
            raise ValueError(f"WorkflowNode '{node.id}' already exists")
        self._nodes[node.id] = node
        return node

    def add_task(
        self,
        node_id,
        name,
        *,
        handler=None,
        priority=50,
        max_retries=2,
        dependencies=None,
    ):
        n = WorkflowNode(
            id=node_id,
            name=name,
            node_type=WorkflowNodeType.TASK,
            handler=handler,
            priority=priority,
            max_retries=max_retries,
        )
        if dependencies:
            for d in dependencies:
                n.depends_on(d)
        return self._register(n)

    def add_condition(
        self, node_id, predicate, *, if_true="", if_false="", dependencies=None
    ):
        n = WorkflowNode(
            id=node_id,
            name=f"cond:{node_id}",
            node_type=WorkflowNodeType.CONDITION,
            predicate=predicate,
            if_true=if_true,
            if_false=if_false,
        )
        if dependencies:
            for d in dependencies:
                n.depends_on(d)
        return self._register(n)

    def add_parallel(self, node_id, *, branches, dependencies=None):
        n = WorkflowNode(
            id=node_id,
            name=f"parallel:{node_id}",
            node_type=WorkflowNodeType.PARALLEL,
            parallel_branches=list(branches),
        )
        if dependencies:
            for d in dependencies:
                n.depends_on(d)
        return self._register(n)

    def add_merge(self, node_id, *, dependencies=None):
        n = WorkflowNode(
            id=node_id, name=f"merge:{node_id}", node_type=WorkflowNodeType.MERGE
        )
        if dependencies:
            for d in dependencies:
                n.depends_on(d)
        return self._register(n)

    def add_loop(
        self, node_id, *, body, condition, max_iterations=10, dependencies=None
    ):
        n = WorkflowNode(
            id=node_id,
            name=f"loop:{node_id}",
            node_type=WorkflowNodeType.LOOP,
            loop_body=list(body),
            loop_condition=condition,
            max_iterations=max_iterations,
        )
        if dependencies:
            for d in dependencies:
                n.depends_on(d)
        return self._register(n)

    def get_node(self, node_id):
        return self._nodes.get(node_id)

    def has_node(self, node_id):
        return node_id in self._nodes

    def remove_node(self, node_id):
        self._nodes.pop(node_id, None)

    @property
    def node_count(self):
        return len(self._nodes)

    def get_ready_nodes(self):
        ready = []
        for n in self._nodes.values():
            if n.status in (TaskStatus.DONE, TaskStatus.RETRY):
                continue
            deps_ok = all(
                self._nodes.get(d, WorkflowNode("_", "_")).status == TaskStatus.DONE
                for d in n.dependencies
            )
            if deps_ok:
                ready.append(n)
        return ready

    def topological_sort(self):
        in_deg = {nid: 0 for nid in self._nodes}
        for nid, n in self._nodes.items():
            for d in n.dependencies:
                if d in in_deg:
                    in_deg[nid] = in_deg.get(nid, 0) + 1
        q = [nid for nid, d in in_deg.items() if d == 0]
        ordered = []
        while q:
            nid = q.pop(0)
            ordered.append(self._nodes[nid])
            for oid, on in self._nodes.items():
                if nid in on.dependencies:
                    in_deg[oid] -= 1
                    if in_deg[oid] == 0:
                        q.append(oid)
        return ordered

    def get_execution_plan(self):
        plan = []
        for n in self.topological_sort():
            e = {
                "id": n.id,
                "name": n.name,
                "type": n.node_type.value,
                "priority": n.priority,
                "max_retries": n.max_retries,
                "dependencies": list(n.dependencies),
            }
            if n.node_type == WorkflowNodeType.CONDITION:
                e["if_true"] = n.if_true
                e["if_false"] = n.if_false
            elif n.node_type == WorkflowNodeType.PARALLEL:
                e["branches"] = n.parallel_branches
            elif n.node_type == WorkflowNodeType.LOOP:
                e["body"] = n.loop_body
                e["max_iterations"] = n.max_iterations
            plan.append(e)
        return plan

    def to_task_graph(self):
        tg = TaskGraph()
        for n in self._nodes.values():
            t = Task(
                id=n.id,
                name=n.name,
                module="workflow",
                priority=n.priority,
                max_retries=n.max_retries,
            )
            for d in n.dependencies:
                t.depends_on(d)
            tg.add_task(t)
        return tg

    def get_node_by_name(self, name):
        for n in self._nodes.values():
            if n.name == name:
                return n
        return None

    def stats(self):
        by_type = {}
        for t in WorkflowNodeType:
            by_type[t.value] = sum(1 for n in self._nodes.values() if n.node_type == t)
        return {"node_count": self.node_count, "by_type": by_type}


__all__ = ["WorkflowNodeType", "WorkflowNode", "WorkflowEngine", "ConditionPredicate"]
