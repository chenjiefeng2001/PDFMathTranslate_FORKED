"""Module: V5 Runtime Supervisor."""
from __future__ import annotations
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple
from pdf2zh.v3.causal_graph import CausalDiagnosticGraph, CausalNode, Severity

logger = logging.getLogger(__name__)

RecoveryStrategy = Callable[[CausalNode], Optional[str]]

@dataclass
class ResourceUsage:
    operation: str
    elapsed: float
    timestamp: float = 0.0
    memory_delta: int = 0
    def __post_init__(self):
        if not self.timestamp: self.timestamp = time.time()

@dataclass
class ResourceReport:
    total_operations: int = 0
    total_time: float = 0.0
    avg_time: float = 0.0
    max_time: float = 0.0
    min_time: float = 0.0
    operation_counts: Dict[str, int] = field(default_factory=dict)
    operation_times: Dict[str, float] = field(default_factory=dict)
    error_count: int = 0

class ResourceManager:
    def __init__(self):
        self._usage = []
        self._errors = 0
        self._max_history = 10000
    def track(self, operation, elapsed, memory_delta=0):
        self._usage.append(ResourceUsage(operation=operation, elapsed=elapsed, memory_delta=memory_delta))
        if len(self._usage) > self._max_history: self._usage = self._usage[-self._max_history:]
    def track_error(self): self._errors += 1
    def get_resource_report(self):
        rpt = ResourceReport()
        rpt.error_count = self._errors
        if not self._usage: return rpt
        rpt.total_operations = len(self._usage)
        op_counts = {}; op_times = {}; times = []
        for u in self._usage:
            op_counts[u.operation] = op_counts.get(u.operation, 0) + 1
            op_times[u.operation] = op_times.get(u.operation, 0.0) + u.elapsed
            times.append(u.elapsed)
            rpt.total_time += u.elapsed
        rpt.operation_counts = op_counts; rpt.operation_times = op_times
        rpt.avg_time = rpt.total_time / len(times) if times else 0.0
        rpt.max_time = max(times) if times else 0.0
        rpt.min_time = min(times) if times else 0.0
        rpt.error_count = self._errors
        return rpt
    def clear(self): self._usage.clear(); self._errors = 0
    def avg_time_for(self, operation):
        times = [u.elapsed for u in self._usage if u.operation == operation]
        return sum(times) / len(times) if times else 0.0

class RecoveryManager:
    def __init__(self):
        self._strategies = {}
        self._history = []
    def register(self, error_type, strategy):
        self._strategies[error_type] = strategy
    def unregister(self, error_type):
        self._strategies.pop(error_type, None)
    def attempt_recovery(self, diagnostic):
        strategy = self._strategies.get(diagnostic.module) or self._strategies.get("*")
        if strategy is None: return None
        try:
            result = strategy(diagnostic)
            success = result is not None
            self._history.append((diagnostic.id, result or "", success))
            return result
        except Exception as e:
            self._history.append((diagnostic.id, str(e), False))
            return None
    def get_history(self): return list(self._history)
    def clear_history(self): self._history.clear()

class RuntimeSupervisor:
    def __init__(self, diagnostic_graph=None):
        self.resource_manager = ResourceManager()
        self.recovery_manager = RecoveryManager()
        self.diagnostic_graph = diagnostic_graph or CausalDiagnosticGraph()
        self._started = time.time()
        self._healthy = True
        self._health_log = []
    @property
    def uptime(self): return time.time() - self._started
    def check_health(self):
        rpt = self.resource_manager.get_resource_report()
        if rpt.error_count > rpt.total_operations * 0.3 and rpt.total_operations > 10:
            self._healthy = False
            self._health_log.append((time.time(), False, "Error rate > 30%"))
        else: self._healthy = True
        return self._healthy
    def record_operation(self, operation, elapsed, success=True):
        self.resource_manager.track(operation, elapsed)
        if not success: self.resource_manager.track_error()
    def auto_diagnose(self, label, *, severity=Severity.WARNING, module="", details="", repair_hint="", cause_ids=None):
        return self.diagnostic_graph.add_diagnostic(label=label, severity=severity, module=module, details=details, repair_hint=repair_hint, cause_ids=cause_ids)
    def auto_repair(self):
        repaired = 0
        for d in self.diagnostic_graph.get_unresolved():
            result = self.recovery_manager.attempt_recovery(d)
            if result is not None:
                self.diagnostic_graph.mark_repaired(d.id, result)
                repaired += 1
            else: self.diagnostic_graph.mark_failed(d.id, "No strategy")
        return repaired
    def stats(self):
        rpt = self.resource_manager.get_resource_report()
        return {"uptime": round(self.uptime, 2), "healthy": self._healthy, "resources": {"total_ops": rpt.total_operations, "total_time": round(rpt.total_time, 3), "avg_time": round(rpt.avg_time, 4), "errors": rpt.error_count}, "recovery": {"strategies": len(self.recovery_manager._strategies), "history": len(self.recovery_manager.get_history())}, "diagnostic_graph": self.diagnostic_graph.stats()}

__all__ = ["ResourceUsage", "ResourceReport", "ResourceManager", "RecoveryManager", "RuntimeSupervisor"]
