"""Module: V5 Causal Diagnostic Graph."""
from __future__ import annotations
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple
logger = logging.getLogger(__name__)

class Severity(Enum):
    INFO = "info"
    WARNING = "warning"
    HIGH = "high"
    CRITICAL = "critical"

class RepairStatus(Enum):
    PENDING = "pending"
    APPLIED = "applied"
    FAILED = "failed"
    SKIPPED = "skipped"

@dataclass
class CausalNode:
    id: str
    label: str
    severity: Severity = Severity.WARNING
    module: str = ""
    details: str = ""
    cause_ids: Set[str] = field(default_factory=set)
    effect_ids: Set[str] = field(default_factory=set)
    repair_hint: str = ""
    repair_status: RepairStatus = RepairStatus.PENDING
    repair_result: str = ""
    metadata: dict = field(default_factory=dict)
    def add_cause(self, cause_id): self.cause_ids.add(cause_id)
    def add_effect(self, effect_id): self.effect_ids.add(effect_id)
    @property
    def is_root_cause(self): return len(self.cause_ids) == 0
    @property
    def depth(self): return len(self.cause_ids)

class CausalDiagnosticGraph:
    def __init__(self):
        self._nodes = {}
        self._counter = 0
    def add_diagnostic(self, label, *, severity=Severity.WARNING, module="", details="", cause_ids=None, repair_hint=""):
        self._counter += 1
        nid = f"diag_{self._counter}"
        n = CausalNode(id=nid, label=label, severity=severity, module=module, details=details, repair_hint=repair_hint)
        if cause_ids:
            for cid in cause_ids:
                if cid in self._nodes:
                    n.add_cause(cid)
                    self._nodes[cid].add_effect(nid)
        self._nodes[nid] = n
        return n
    def add_causal_chain(self, chain):
        nodes = []
        prev_id = None
        for label, severity, module in chain:
            n = self.add_diagnostic(label, severity=severity, module=module, cause_ids=[prev_id] if prev_id else None)
            nodes.append(n)
            prev_id = n.id
        return nodes
    def get_node(self, nid): return self._nodes.get(nid)
    def remove_node(self, nid):
        n = self._nodes.pop(nid, None)
        if n:
            for cid in list(n.cause_ids):
                if cid in self._nodes: self._nodes[cid].effect_ids.discard(nid)
            for eid in list(n.effect_ids):
                if eid in self._nodes: self._nodes[eid].cause_ids.discard(nid)
    def find_root_causes(self): return [n for n in self._nodes.values() if n.is_root_cause]
    def get_leaf_causes(self): return [n for n in self._nodes.values() if not n.effect_ids]
    def get_causal_chain(self, nid):
        chain = []
        visited = set()
        current = nid
        while current and current not in visited:
            visited.add(current)
            n = self._nodes.get(current)
            if n is None: break
            chain.append(n)
            causes = list(n.cause_ids)
            current = causes[0] if causes else None
        return chain
    def get_affected_nodes(self, nid):
        affected = []
        visited = set()
        queue = [nid]
        while queue:
            cid = queue.pop(0)
            if cid in visited: continue
            visited.add(cid)
            n = self._nodes.get(cid)
            if n:
                affected.append(n)
                for eid in n.effect_ids:
                    if eid not in visited: queue.append(eid)
        return affected
    def suggest_repairs(self):
        suggestions = []
        for n in self.find_root_causes():
            if n.repair_hint and n.repair_status == RepairStatus.PENDING:
                suggestions.append((n, n.repair_hint))
        return suggestions
    def mark_repaired(self, nid, result=""):
        n = self._nodes.get(nid)
        if n: n.repair_status = RepairStatus.APPLIED; n.repair_result = result
    def mark_failed(self, nid, result=""):
        n = self._nodes.get(nid)
        if n: n.repair_status = RepairStatus.FAILED; n.repair_result = result
    @property
    def node_count(self): return len(self._nodes)
    def get_unresolved(self): return [n for n in self._nodes.values() if n.repair_status != RepairStatus.APPLIED]
    def auto_repair_suggestions(self):
        suggestions = []
        for n in self.find_root_causes():
            if n.repair_hint:
                suggestions.append({"node_id": n.id, "label": n.label, "severity": n.severity.value, "module": n.module, "hint": n.repair_hint})
        return suggestions
    def stats(self):
        by_severity = {}
        by_module = {}
        for n in self._nodes.values():
            by_severity[n.severity.value] = by_severity.get(n.severity.value, 0) + 1
            if n.module: by_module[n.module] = by_module.get(n.module, 0) + 1
        return {"total": self.node_count, "root_causes": len(self.find_root_causes()), "unresolved": len(self.get_unresolved()), "by_severity": by_severity, "by_module": by_module}

__all__ = ["Severity", "RepairStatus", "CausalNode", "CausalDiagnosticGraph"]
