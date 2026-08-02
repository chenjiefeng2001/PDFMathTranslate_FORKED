"""Module: V4/V5 Runtime Kernel — unified kernel for all runtimes.

V5 additions (deferred imports for zero-cost backward compatibility):
  - RuntimeContext: extracted context from Kernel (knowledge, memory, telemetry, plugins, config, cache)
  - WorkflowEngine: upgraded TaskGraph with conditions, parallel, merge, loops
  - ExecutionGraph: execution state tracking, separate from DocumentGraph
  - RuntimeSupervisor: Resource Manager, Recovery Manager, health monitoring
  - CausalDiagnosticGraph: root cause analysis on diagnostics
  - Tracer: distributed span-based tracing for performance analysis
"""
from __future__ import annotations
import copy, logging, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set, Type
from pdf2zh.v3.service import ServiceRegistry, ServiceInterface
logger = logging.getLogger(__name__)



class EventType(str, Enum):
    NODE_ADDED = "node_added"; NODE_REMOVED = "node_removed"; NODE_UPDATED = "node_updated"
    EDGE_ADDED = "edge_added"; EDGE_REMOVED = "edge_removed"; EDGE_UPDATED = "edge_updated"
    TRANSLATION_STARTED = "translation_started"; TRANSLATION_COMPLETED = "translation_completed"
    TRANSLATION_FAILED = "translation_failed"; TRANSLATION_CACHED = "translation_cached"
    LAYOUT_STARTED = "layout_started"; LAYOUT_COMPLETED = "layout_completed"
    LAYOUT_INVALIDATED = "layout_invalidated"; COLLISION_DETECTED = "collision_detected"
    QUALITY_EVALUATED = "quality_evaluated"; ISSUE_DETECTED = "issue_detected"
    REPAIR_STARTED = "repair_started"; REPAIR_COMPLETED = "repair_completed"
    RENDER_STARTED = "render_started"; RENDER_COMPLETED = "render_completed"
    KERNEL_INITIALIZED = "kernel_initialized"; KERNEL_SHUTDOWN = "kernel_shutdown"
    ERROR_OCCURRED = "error_occurred"

class PriorityLevel(Enum):
    LOW = 0; NORMAL = 1; HIGH = 2; CRITICAL = 3

@dataclass
class DeadLetterRecord:
    event: Event; error: str; timestamp: float = 0.0
    def __post_init__(self):
        if not self.timestamp: self.timestamp = time.time()

@dataclass
class Event:
    type: EventType; source: str = ""; timestamp: float = 0.0; data: dict = field(default_factory=dict); event_id: str = ""
    def __post_init__(self):
        if not self.timestamp: self.timestamp = time.time()
        if not self.event_id: self.event_id = uuid.uuid4().hex[:12]

EventHandler = Callable[[Event], None]

class EventBus:
    def __init__(self):
        self._subscribers: Dict[EventType, List[EventHandler]] = {}
        self._wildcard: List[EventHandler] = []
        self._history: List[Event] = []; self._max_history = 1000; self._total = 0
        self._dead_letter: List[DeadLetterRecord] = []
        self._max_dead_letter = 100
    def subscribe(self, t: EventType, h: EventHandler):
        self._subscribers.setdefault(t, []).append(h)
    def subscribe_priority(self, t: EventType, h: EventHandler, priority: PriorityLevel = PriorityLevel.NORMAL):
        self._subscribers.setdefault(t, []).append(h)
    def unsubscribe(self, t: EventType, h: EventHandler):
        if t in self._subscribers: self._subscribers[t] = [x for x in self._subscribers[t] if x is not h]
    def subscribe_all(self, h: EventHandler): self._wildcard.append(h)
    def unsubscribe_all(self, h: EventHandler): self._wildcard = [x for x in self._wildcard if x is not h]
    def emit(self, t: EventType, **kw) -> Event:
        e = Event(type=t, data=kw); self._dispatch(e); return e
    def emit_event(self, e: Event): self._dispatch(e)
    def _dispatch(self, e: Event):
        self._total += 1; self._history.append(e)
        if len(self._history) > self._max_history: self._history.pop(0)
        for h in self._subscribers.get(e.type, []):
            try: h(e)
            except Exception as ex:
                logger.error("Handler failed for %s: %s", e.type.value, ex)
                self._dead_letter.append(DeadLetterRecord(event=e, error=str(ex)))
                if len(self._dead_letter) > self._max_dead_letter: self._dead_letter.pop(0)
        for h in self._wildcard:
            try: h(e)
            except Exception as ex:
                logger.error("Wildcard failed: %s", ex)
                self._dead_letter.append(DeadLetterRecord(event=e, error=str(ex)))
                if len(self._dead_letter) > self._max_dead_letter: self._dead_letter.pop(0)
    @property
    def event_count(self) -> int: return self._total
    def get_history(self, limit=10): return list(reversed(self._history[-limit:]))
    def get_history_by_type(self, t: EventType, limit=10):
        return [x for x in reversed(self._history) if x.type == t][:limit]
    def clear_history(self): self._history.clear()
    def subscriber_count(self, t=None):
        if t: return len(self._subscribers.get(t, []))
        return sum(len(v) for v in self._subscribers.values())
    @property
    def dead_letter_count(self) -> int: return len(self._dead_letter)
    def get_dead_letters(self, limit=10) -> List[DeadLetterRecord]:
        return list(reversed(self._dead_letter[-limit:]))
    def retry_dead_letters(self, max_retry=5) -> int:
        retried = 0
        dl = list(self._dead_letter)
        self._dead_letter.clear()
        for rec in dl[:max_retry]:
            try:
                self._dispatch(rec.event)
                retried += 1
            except Exception:
                self._dead_letter.append(rec)
        return retried
    def stats(self) -> dict:
        return {"total_events": self._total, "history_size": len(self._history),
                "subscribers_by_type": {k.value: len(v) for k,v in self._subscribers.items()},
                "wildcard_subscribers": len(self._wildcard),
                "dead_letter_count": len(self._dead_letter)}

class NodeLifecycleState(str, Enum):
    NEW = "new"; PARSED = "parsed"; NORMALIZED = "normalized"
    ANALYZED = "analyzed"; PLANNED = "planned"; TRANSLATED = "translated"
    LAYOUTED = "layouted"; RENDERED = "rendered"; VERIFIED = "verified"
    ARCHIVED = "archived"; ERROR = "error"
    @property
    def is_terminal(self):
        return self in (NodeLifecycleState.ARCHIVED, NodeLifecycleState.ERROR)
    @property
    def is_error(self):
        return self == NodeLifecycleState.ERROR

_TRANSITIONS = {
    NodeLifecycleState.NEW: {NodeLifecycleState.PARSED, NodeLifecycleState.ERROR},
    NodeLifecycleState.PARSED: {NodeLifecycleState.NORMALIZED, NodeLifecycleState.ERROR},
    NodeLifecycleState.NORMALIZED: {NodeLifecycleState.ANALYZED, NodeLifecycleState.ERROR},
    NodeLifecycleState.ANALYZED: {NodeLifecycleState.PLANNED, NodeLifecycleState.ERROR},
    NodeLifecycleState.PLANNED: {NodeLifecycleState.TRANSLATED, NodeLifecycleState.ERROR},
    NodeLifecycleState.TRANSLATED: {NodeLifecycleState.LAYOUTED, NodeLifecycleState.ERROR},
    NodeLifecycleState.LAYOUTED: {NodeLifecycleState.RENDERED, NodeLifecycleState.ERROR},
    NodeLifecycleState.RENDERED: {NodeLifecycleState.VERIFIED, NodeLifecycleState.ERROR},
    NodeLifecycleState.VERIFIED: {NodeLifecycleState.ARCHIVED, NodeLifecycleState.ERROR},
    NodeLifecycleState.ERROR: {NodeLifecycleState.NEW, NodeLifecycleState.PARSED,
        NodeLifecycleState.NORMALIZED, NodeLifecycleState.ANALYZED,
        NodeLifecycleState.PLANNED, NodeLifecycleState.TRANSLATED,
        NodeLifecycleState.LAYOUTED, NodeLifecycleState.RENDERED,
        NodeLifecycleState.VERIFIED, NodeLifecycleState.ERROR},
}

StateChangeCallback = Callable[[str, NodeLifecycleState, NodeLifecycleState], None]

class NodeStateMachine:
    def __init__(self):
        self._states: Dict[str, NodeLifecycleState] = {}
        self._callbacks: List[StateChangeCallback] = []
        self._tc = 0
    def set_state(self, nid, s): self._states[nid] = s
    def get_state(self, nid): return self._states.get(nid)
    def initialize(self, nid): self._states[nid] = NodeLifecycleState.NEW
    def transition(self, nid, to_state):
        cur = self._states.get(nid, NodeLifecycleState.NEW)
        if cur == to_state: return True
        if to_state not in _TRANSITIONS.get(cur, set()): return False
        old = cur; self._states[nid] = to_state; self._tc += 1
        for cb in self._callbacks:
            try: cb(nid, old, to_state)
            except Exception as e: logger.error("State cb failed: %s", e)
        return True
    def can_transition(self, nid, to_state):
        cur = self._states.get(nid, NodeLifecycleState.NEW)
        if cur == to_state: return True
        return to_state in _TRANSITIONS.get(cur, set())
    def get_nodes_in_state(self, s): return [n for n, st in self._states.items() if st == s]
    def get_all_states(self): return dict(self._states)
    @property
    def node_count(self): return len(self._states)
    @property
    def transition_count(self): return self._tc
    def on_state_change(self, cb): self._callbacks.append(cb)
    def reset(self): self._states.clear(); self._tc = 0



class DiagnosticSeverity(str, Enum):
    TRACE = "trace"; DEBUG = "debug"; INFO = "info"
    WARNING = "warning"; ERROR = "error"; CRITICAL = "critical"

@dataclass
class Diagnostic:
    severity: DiagnosticSeverity; module: str; message: str
    node_id: str = ""; fix_hint: str = ""
    timestamp: float = 0.0; diagnostic_id: str = ""
    def __post_init__(self):
        if not self.timestamp: self.timestamp = time.time()
        if not self.diagnostic_id: self.diagnostic_id = uuid.uuid4().hex[:12]
    def to_dict(self):
        return {"diagnostic_id": self.diagnostic_id, "severity": self.severity.value,
                "module": self.module, "message": self.message, "node_id": self.node_id,
                "fix_hint": self.fix_hint, "timestamp": self.timestamp}

class DiagnosticCenter:
    def __init__(self):
        self._diags: List[Diagnostic] = []; self._max = 10000
    def report(self, severity, module, message, *, node_id="", fix_hint=""):
        sev = DiagnosticSeverity(severity.lower())
        d = Diagnostic(sev, module, message, node_id=node_id, fix_hint=fix_hint)
        self._diags.append(d)
        if len(self._diags) > self._max: self._diags = self._diags[-self._max:]
        return d
    def debug(self, m, msg, **kw): return self.report("debug", m, msg, **kw)
    def info(self, m, msg, **kw): return self.report("info", m, msg, **kw)
    def warning(self, m, msg, **kw): return self.report("warning", m, msg, **kw)
    def error(self, m, msg, **kw): return self.report("error", m, msg, **kw)
    def critical(self, m, msg, **kw): return self.report("critical", m, msg, **kw)
    def get_all(self): return list(self._diags)
    def get_by_severity(self, s):
        sev = DiagnosticSeverity(s.lower()); return [d for d in self._diags if d.severity == sev]
    def get_by_module(self, m): return [d for d in self._diags if d.module == m]
    def get_by_node(self, nid): return [d for d in self._diags if d.node_id == nid]
    def get_errors(self):
        return [d for d in self._diags if d.severity in (DiagnosticSeverity.ERROR, DiagnosticSeverity.CRITICAL)]
    def get_warnings(self):
        return [d for d in self._diags if d.severity == DiagnosticSeverity.WARNING]
    def count(self, severity=None):
        if severity:
            sev = DiagnosticSeverity(severity.lower())
            return sum(1 for d in self._diags if d.severity == sev)
        return len(self._diags)
    def summary(self):
        r = {}
        for sev in DiagnosticSeverity:
            cnt = sum(1 for d in self._diags if d.severity == sev)
            if cnt: r[sev.value] = cnt
        r["total"] = len(self._diags); return r
    def to_dict_list(self): return [d.to_dict() for d in self._diags]
    def clear(self): self._diags.clear()

class MemoryCenter:
    def __init__(self, mb=None, cb=None, pb=None):
        self._mem = mb; self._cache = cb; self._per = pb
        self._lk = 0; self._mh = 0; self._ch = 0; self._ph = 0; self._ms = 0
    @property
    def memory(self): return self._mem
    @memory.setter
    def memory(self, v): self._mem = v
    @property
    def cache(self): return self._cache
    @cache.setter
    def cache(self, v): self._cache = v
    @property
    def persistent(self): return self._per
    @persistent.setter
    def persistent(self, v): self._per = v
    def put(self, key, value):
        for b in (self._mem, self._cache, self._per):
            if b is not None:
                try: b.put(key, value)
                except Exception: pass
    def get(self, key):
        self._lk += 1
        for b, attr in [(self._mem, "_mh"), (self._cache, "_ch"), (self._per, "_ph")]:
            if b is not None:
                try:
                    r = b.get(key)
                    if r is not None:
                        setattr(self, attr, getattr(self, attr) + 1); return r
                except Exception: pass
        self._ms += 1; return None
    def has(self, key): return self.get(key) is not None
    def delete(self, key):
        for b in (self._mem, self._cache, self._per):
            if b is not None:
                try: b.delete(key)
                except Exception: pass
    def clear_all(self):
        for b in (self._mem, self._cache, self._per):
            if b is not None:
                try: b.clear()
                except Exception: pass
    @property
    def hit_rate(self):
        if self._lk == 0: return 1.0
        return (self._mh + self._ch + self._ph) / self._lk
    def stats(self):
        return {"lookups": self._lk, "memory_hits": self._mh, "cache_hits": self._ch,
                "persistent_hits": self._ph, "misses": self._ms, "hit_rate": round(self.hit_rate, 4)}


class PluginState(str, Enum):
    REGISTERED = "registered"; INITIALIZED = "initialized"
    STARTED = "started"; STOPPED = "stopped"; ERROR = "error"

class Plugin:
    plugin_id: str = ""; version: str = "1.0.0"; description: str = ""
    def __init__(self):
        self.state = PluginState.REGISTERED; self.kernel = None
    def initialize(self, kernel):
        self.kernel = kernel; self.state = PluginState.INITIALIZED
    def start(self): self.state = PluginState.STARTED
    def stop(self): self.state = PluginState.STOPPED
    def on_error(self, ex):
        self.state = PluginState.ERROR; logger.error("Plugin %s error: %s", self.plugin_id, ex)

class Capability(str, Enum):
    PARSER = "parser"; NORMALIZER = "normalizer"; ANALYZER = "analyzer"
    PLANNER = "planner"; TRANSLATOR = "translator"; LAYOUT = "layout"
    RENDERER = "renderer"; MEMORY = "memory"; PLUGIN = "plugin"

class CapabilityPlugin(Plugin):
    capabilities: Set[Capability] = field(default_factory=set)
    capability_priority: int = 50

class PluginManager:
    def __init__(self):
        self._plugins: Dict[str, Plugin] = {}; self._started = False
    def register(self, p: Plugin) -> str:
        if not p.plugin_id: raise ValueError("Plugin must have non-empty plugin_id")
        if p.plugin_id in self._plugins: raise ValueError(f"Plugin '{p.plugin_id}' already registered")
        self._plugins[p.plugin_id] = p; return p.plugin_id
    def unregister(self, pid):
        p = self._plugins.pop(pid, None)
        if p and self._started:
            try: p.stop()
            except Exception: pass
    def get(self, pid): return self._plugins.get(pid)
    def get_all(self): return list(self._plugins.values())
    def get_by_state(self, s): return [p for p in self._plugins.values() if p.state == s]
    def initialize_all(self, kernel):
        for pid, p in self._plugins.items():
            try: p.initialize(kernel)
            except Exception as e: logger.error("Failed to init plugin %s: %s", pid, e); p.on_error(e)
    def start_all(self):
        for pid, p in self._plugins.items():
            if p.state == PluginState.INITIALIZED:
                try: p.start()
                except Exception as e: logger.error("Failed to start plugin %s: %s", pid, e); p.on_error(e)
        self._started = True
    def stop_all(self):
        for pid, p in self._plugins.items():
            if p.state == PluginState.STARTED:
                try: p.stop()
                except Exception: pass
        self._started = False
    @property
    def plugin_count(self): return len(self._plugins)
    @property
    def running_count(self): return sum(1 for p in self._plugins.values() if p.state == PluginState.STARTED)
    def list_plugins(self):
        return [{"id": p.plugin_id, "version": p.version, "description": p.description, "state": p.state.value}
                for p in self._plugins.values()]
    def find_by_capability(self, cap: Capability) -> List[Plugin]:
        return [p for p in self._plugins.values() if isinstance(p, CapabilityPlugin) and cap in p.capabilities]
    def get_best_for_capability(self, cap: Capability) -> Optional[Plugin]:
        candidates = self.find_by_capability(cap)
        if not candidates: return None
        return max(candidates, key=lambda p: p.capability_priority if isinstance(p, CapabilityPlugin) else 0)

# ═══ RuntimeTransaction ═══

@dataclass
class TransactionSnapshot:
    state: dict; memory: dict; diagnostics: List[dict]

class RuntimeTransaction:
    def __init__(self, kernel: RuntimeKernel):
        self._kernel = kernel; self._snapshot: Optional[TransactionSnapshot] = None; self._active = False
    @property
    def active(self) -> bool: return self._active
    def begin(self) -> None:
        if self._active: raise RuntimeError("Transaction already active")
        mc = self._kernel.memory_center
        mem_snap = {}
        for attr in ("_mem", "_cache", "_per"):
            b = getattr(mc, attr, None)
            if b is not None and hasattr(b, "_data"):
                mem_snap[attr] = dict(b._data)
        self._snapshot = TransactionSnapshot(
            state=copy.deepcopy(self._kernel.state_machine._states),
            memory=mem_snap,
            diagnostics=[d.to_dict() for d in self._kernel.diagnostic_center._diags],
        )
        self._active = True
    def commit(self) -> dict:
        if not self._active: raise RuntimeError("No active transaction")
        self._active = False; result = {"nodes": len(self._snapshot.state) if self._snapshot else 0}
        self._snapshot = None; return result
    def rollback(self) -> int:
        if not self._active: raise RuntimeError("No active transaction")
        if self._snapshot is None: raise RuntimeError("No snapshot to restore")
        self._kernel.state_machine._states = copy.deepcopy(self._snapshot.state)
        mc = self._kernel.memory_center
        for attr, data in (self._snapshot.memory or {}).items():
            b = getattr(mc, attr, None)
            if b is not None and hasattr(b, "_data"):
                b._data = dict(data)
        self._kernel.diagnostic_center._diags = [
            Diagnostic(**d) if "module" in d else Diagnostic(severity=DiagnosticSeverity.INFO, module="", message="")
            for d in self._snapshot.diagnostics
        ]
        self._active = False; self._snapshot = None
        return len(self._kernel.state_machine._states)

# ═══ KnowledgeCenter ═══

@dataclass
class KnowledgeEntry:
    entity: str; aliases: Set[str] = field(default_factory=set)
    definition: str = ""; cross_refs: Set[str] = field(default_factory=set); confidence: float = 1.0

class KnowledgeCenter:
    def __init__(self):
        self._entries: Dict[str, KnowledgeEntry] = {}; self._alias_index: Dict[str, str] = {}
    def learn(self, entity: str, definition: str = "", *, aliases: Optional[Set[str]] = None,
              cross_refs: Optional[Set[str]] = None, confidence: float = 1.0) -> None:
        key = entity.lower()
        if key in self._entries:
            entry = self._entries[key]; entry.definition = definition or entry.definition
            entry.confidence = max(entry.confidence, confidence)
        else:
            entry = KnowledgeEntry(entity=entity, definition=definition, confidence=confidence)
            self._entries[key] = entry
        if aliases:
            entry.aliases.update(a.lower() for a in aliases)
            for a in aliases: self._alias_index[a.lower()] = key
        if cross_refs: entry.cross_refs.update(c.lower() for c in cross_refs)
    def query(self, name: str) -> Optional[KnowledgeEntry]:
        key = name.lower()
        if key in self._entries: return self._entries[key]
        canonical = self._alias_index.get(key)
        return self._entries.get(canonical) if canonical else None
    def query_cross_references(self, name: str) -> List[str]:
        entry = self.query(name); return list(entry.cross_refs) if entry else []
    def resolve_alias(self, alias: str) -> Optional[str]:
        canonical = self._alias_index.get(alias.lower())
        return self._entries[canonical].entity if canonical and canonical in self._entries else None
    def entry_count(self) -> int: return len(self._entries)
    def clear(self) -> None: self._entries.clear(); self._alias_index.clear()

# ═══ DiagnosticGraph ═══

@dataclass
class DiagnosticNode:
    diagnostic_id: str; severity: DiagnosticSeverity; module: str; message: str
    cause: str = ""; fix_hint: str = ""; repaired: bool = False; result: str = ""
    node_id: str = ""; timestamp: float = 0.0
    def __post_init__(self):
        if not self.timestamp: self.timestamp = time.time()

class DiagnosticGraph:
    def __init__(self):
        self._nodes: Dict[str, DiagnosticNode] = {}; self._edges: List[tuple] = []
    def add_diagnostic(self, d: Diagnostic) -> DiagnosticNode:
        import uuid as _uid; nid = _uid.uuid4().hex[:12]
        node = DiagnosticNode(diagnostic_id=nid, severity=d.severity, module=d.module, message=d.message,
                              fix_hint=d.fix_hint, node_id=d.node_id)
        self._nodes[nid] = node; return node
    def add_fix(self, diagnostic_id: str, fix_applied: str, success: bool) -> None:
        node = self._nodes.get(diagnostic_id)
        if node: node.repaired = success; node.result = fix_applied
    def add_edge(self, source_id: str, target_id: str, relation: str = "causes") -> None:
        if source_id in self._nodes and target_id in self._nodes:
            self._edges.append((source_id, target_id, relation))
    def get_chain(self, diagnostic_id: str) -> List[Dict]:
        chain = []; node = self._nodes.get(diagnostic_id)
        if node:
            chain.append({"id": node.diagnostic_id, "severity": node.severity.value, "message": node.message,
                         "cause": node.cause, "fix_hint": node.fix_hint, "repaired": node.repaired, "result": node.result})
            for src, tgt, rel in self._edges:
                if src == diagnostic_id and tgt in self._nodes:
                    chain.append({"relation": rel, "target": self._nodes[tgt].message})
        return chain
    def get_unresolved(self) -> List[DiagnosticNode]:
        return [n for n in self._nodes.values() if not n.repaired
                and n.severity in (DiagnosticSeverity.ERROR, DiagnosticSeverity.CRITICAL)]
    def resolve_count(self) -> int: return sum(1 for n in self._nodes.values() if n.repaired)
    def total_count(self) -> int: return len(self._nodes)
    def clear(self) -> None: self._nodes.clear(); self._edges.clear()

# ═══ TelemetryCollector ═══

@dataclass
class TelemetrySample:
    operation: str; duration_ms: float; success: bool; timestamp: float = 0.0
    def __post_init__(self):
        if not self.timestamp: self.timestamp = time.time()

class _TimerContext:
    def __init__(self, collector: TelemetryCollector, operation: str):
        self._collector = collector; self._operation = operation; self._start = 0.0
    def __enter__(self):
        self._start = time.time(); return self
    def __exit__(self, exc_type, exc_val, exc_tb):
        duration = (time.time() - self._start) * 1000
        self._collector.record(self._operation, duration, success=exc_type is None)

class TelemetryCollector:
    def __init__(self, max_samples: int = 10000):
        self._samples: List[TelemetrySample] = []; self._max_samples = max_samples
        self._counters: Dict[str, int] = {}; self._errors: Dict[str, int] = {}
    def record(self, operation: str, duration_ms: float, success: bool = True) -> None:
        self._samples.append(TelemetrySample(operation=operation, duration_ms=duration_ms, success=success))
        if len(self._samples) > self._max_samples: self._samples.pop(0)
        self._counters[operation] = self._counters.get(operation, 0) + 1
        if not success: self._errors[operation] = self._errors.get(operation, 0) + 1
    def record_timed(self, operation: str) -> _TimerContext:
        return _TimerContext(self, operation)
    def summary(self) -> Dict[str, Any]:
        if not self._samples: return {"operations": {}, "total": 0, "error_rate": 0.0}
        ops = {}
        for s in self._samples:
            if s.operation not in ops:
                ops[s.operation] = {"count": 0, "total_ms": 0.0, "errors": 0, "min_ms": float('inf'), "max_ms": 0.0}
            o = ops[s.operation]; o["count"] += 1; o["total_ms"] += s.duration_ms
            o["min_ms"] = min(o["min_ms"], s.duration_ms); o["max_ms"] = max(o["max_ms"], s.duration_ms)
            if not s.success: o["errors"] += 1
        for op in ops:
            c = ops[op]["count"]; ops[op]["avg_ms"] = round(ops[op]["total_ms"] / c, 2) if c else 0
            durs = sorted(s.duration_ms for s in self._samples if s.operation == op)
            import math
            ops[op]["p50_ms"] = durs[max(0, min(int(math.ceil(50/100.0*len(durs)))-1, len(durs)-1))] if durs else 0.0
            ops[op]["p95_ms"] = durs[max(0, min(int(math.ceil(95/100.0*len(durs)))-1, len(durs)-1))] if durs else 0.0
        total_errors = sum(v["errors"] for v in ops.values())
        total_count = sum(v["count"] for v in ops.values())
        return {"operations": ops, "total": len(self._samples),
                "error_rate": round(total_errors / total_count, 4) if total_count else 0.0}
    def get_errors(self, operation: Optional[str] = None) -> Dict[str, int]:
        if operation: return {operation: self._errors.get(operation, 0)}
        return dict(self._errors)
    def clear(self) -> None: self._samples.clear(); self._counters.clear(); self._errors.clear()

class RuntimeKernel:
    """The unified Runtime Kernel for V4 Document Intelligence Runtime."""

    def __init__(self):
        self.event_bus = EventBus()
        self.state_machine = NodeStateMachine()
        self._sr = None
        self.diagnostic_center = DiagnosticCenter()
        self.memory_center = MemoryCenter()
        self.plugin_manager = PluginManager()
        self.knowledge_center = KnowledgeCenter()
        self.diagnostic_graph = DiagnosticGraph()
        self.telemetry = TelemetryCollector()
        self._active_transaction: Optional[RuntimeTransaction] = None
        self._initialized = False
        self._kid = uuid.uuid4().hex[:8]
        self._st = 0.0

    @property
    def service_locator(self):
        if self._sr is None: self._sr = ServiceRegistry.get_instance()
        return self._sr

    @property
    def initialized(self): return self._initialized
    @property
    def kernel_id(self): return self._kid
    @property
    def uptime(self):
        if self._st == 0: return 0.0
        return time.time() - self._st

    def initialize(self):
        self._st = time.time()
        _ = self.service_locator
        self.plugin_manager.initialize_all(self)
        self._initialized = True
        self.event_bus.emit(EventType.KERNEL_INITIALIZED, kernel_id=self._kid)

    def start(self):
        if not self._initialized: self.initialize()
        self.plugin_manager.start_all()

    def shutdown(self):
        self.event_bus.emit(EventType.KERNEL_SHUTDOWN, kernel_id=self._kid, uptime=self.uptime)
        self.plugin_manager.stop_all()
        self._initialized = False

    def emit(self, t, **kw): return self.event_bus.emit(t, **kw)
    def on(self, t, h): self.event_bus.subscribe(t, h)
    def on_error(self, h): self.event_bus.subscribe(EventType.ERROR_OCCURRED, h)
    def initialize_node(self, nid): self.state_machine.initialize(nid)

    def transition_node(self, nid, state):
        ok = self.state_machine.transition(nid, state)
        if ok: self.event_bus.emit(EventType.NODE_UPDATED, node_id=nid, state=state.value)
        return ok

    def get_node_state(self, nid): return self.state_machine.get_state(nid)

    def diagnose(self, severity, module, message, *, node_id="", fix_hint=""):
        d = self.diagnostic_center.report(severity, module, message, node_id=node_id, fix_hint=fix_hint)
        if d.severity in (DiagnosticSeverity.WARNING, DiagnosticSeverity.ERROR, DiagnosticSeverity.CRITICAL):
            self.event_bus.emit(EventType.ISSUE_DETECTED, diagnostic=d.to_dict())
        return d

    def register_service(self, iface, impl, *, replace=False):
        self.service_locator.register(iface, impl, replace=replace)
    def get_service(self, iface): return self.service_locator.get(iface)

    # ── Transaction ────────────────────────────────────────────
    def begin_transaction(self) -> RuntimeTransaction:
        if self._active_transaction and self._active_transaction.active:
            raise RuntimeError("Transaction already in progress")
        self._active_transaction = RuntimeTransaction(self)
        self._active_transaction.begin()
        return self._active_transaction

    def commit_transaction(self) -> dict:
        if not self._active_transaction or not self._active_transaction.active:
            raise RuntimeError("No active transaction")
        return self._active_transaction.commit()

    def rollback_transaction(self) -> int:
        if not self._active_transaction or not self._active_transaction.active:
            raise RuntimeError("No active transaction")
        return self._active_transaction.rollback()

    # ── Knowledge ──────────────────────────────────────────────
    def learn(self, entity, definition="", *, aliases=None, cross_refs=None, confidence=1.0):
        self.knowledge_center.learn(entity, definition, aliases=aliases, cross_refs=cross_refs, confidence=confidence)
    def query_knowledge(self, name):
        return self.knowledge_center.query(name)
    def query_cross_references(self, name):
        return self.knowledge_center.query_cross_references(name)

    # ── Telemetry ──────────────────────────────────────────────
    def record_telemetry(self, operation, duration_ms, success=True):
        self.telemetry.record(operation, duration_ms, success=success)
    def timed(self, operation):
        return self.telemetry.record_timed(operation)

    # ── Diagnostic Graph ──────────────────────────────────────
    def diagnose_graph(self, severity, module, message, *, node_id="", fix_hint="", cause=""):
        d = self.diagnose(severity, module, message, node_id=node_id, fix_hint=fix_hint)
        return self.diagnostic_graph.add_diagnostic(d)

    def resolve_diagnostic(self, diagnostic_id, fix_applied, success=True):
        self.diagnostic_graph.add_fix(diagnostic_id, fix_applied, success)

    # ── Task Graph Integration ─────────────────────────────────
    def schedule(self, task_id, name, *, module="", handler=None, priority=50, dependencies=None, max_retries=2):
        from pdf2zh.v3.scheduler import Task
        task = Task(id=task_id, name=name, module=module, handler=handler, priority=priority, max_retries=max_retries)
        if dependencies:
            for dep in dependencies: task.depends_on(dep)
        self._task_graph = getattr(self, '_task_graph', None) or __import__('pdf2zh.v3.scheduler', fromlist=['TaskGraph']).TaskGraph()
        if not hasattr(self, '_task_graph') or self._task_graph is None:
            from pdf2zh.v3.scheduler import TaskGraph
            self._task_graph = TaskGraph()
        self._task_graph.add_task(task)
        return task

    # ── V5 Integration Properties ──────────────────────────────────

    @property
    def runtime_context(self):
        """Lazy access to RuntimeContext (knowledge, memory, telemetry, config, cache)."""
        if not hasattr(self, '_runtime_context') or self._runtime_context is None:
            from pdf2zh.v3.runtime_context import RuntimeContext, RuntimeConfig
            cfg = RuntimeConfig()
            cfg.telemetry_enabled = True
            self._runtime_context = RuntimeContext(
                config=cfg,
                knowledge=self.knowledge_center,
                memory=self.memory_center,
                telemetry=self.telemetry,
                plugins=self.plugin_manager,
                service_registry=self.service_locator,
            )
        return self._runtime_context

    @property
    def workflow_engine(self):
        """Lazy access to WorkflowEngine (upgraded TaskGraph)."""
        if not hasattr(self, '_workflow_engine') or self._workflow_engine is None:
            from pdf2zh.v3.workflow_engine import WorkflowEngine
            self._workflow_engine = WorkflowEngine()
        return self._workflow_engine

    @property
    def execution_graph(self):
        """Lazy access to ExecutionGraph (execution state tracking)."""
        if not hasattr(self, '_execution_graph') or self._execution_graph is None:
            from pdf2zh.v3.execution_graph import ExecutionGraph
            self._execution_graph = ExecutionGraph()
        return self._execution_graph

    @property
    def causal_graph(self):
        """Lazy access to CausalDiagnosticGraph (root cause analysis)."""
        if not hasattr(self, '_causal_graph') or self._causal_graph is None:
            from pdf2zh.v3.causal_graph import CausalDiagnosticGraph
            self._causal_graph = CausalDiagnosticGraph()
        return self._causal_graph

    @property
    def runtime_supervisor(self):
        """Lazy access to RuntimeSupervisor (resource manager, recovery, health)."""
        if not hasattr(self, '_runtime_supervisor') or self._runtime_supervisor is None:
            from pdf2zh.v3.runtime_supervisor import RuntimeSupervisor
            self._runtime_supervisor = RuntimeSupervisor(
                diagnostic_graph=self.causal_graph)
        return self._runtime_supervisor

    @property
    def tracer(self):
        """Lazy access to Tracer (distributed span-based tracing)."""
        if not hasattr(self, '_tracer') or self._tracer is None:
            from pdf2zh.v3.tracing import Tracer
            self._tracer = Tracer(telemetry=self.telemetry)
        return self._tracer

    def execute(self, task_ids=None):
        from pdf2zh.v3.scheduler import Executor
        tg = getattr(self, '_task_graph', None)
        if tg is None:
            from pdf2zh.v3.scheduler import TaskGraph
            tg = TaskGraph(); self._task_graph = tg
        executor = Executor(tg)
        if task_ids:
            return executor.run_selective(set(task_ids))
        return executor.run_all()

    def stats(self):
        s = {
            "kernel_id": self._kid, "initialized": self._initialized, "uptime": round(self.uptime, 2),
            "event_bus": self.event_bus.stats(),
            "state_machine": {"nodes": self.state_machine.node_count, "transitions": self.state_machine.transition_count},
            "diagnostic_center": self.diagnostic_center.summary(),
            "memory_center": self.memory_center.stats(),
            "plugin_manager": {"total": self.plugin_manager.plugin_count, "running": self.plugin_manager.running_count},
            "services": self.service_locator.list_services(),
            "knowledge_center": {"entries": self.knowledge_center.entry_count()},
            "diagnostic_graph": {"total": self.diagnostic_graph.total_count(), "resolved": self.diagnostic_graph.resolve_count()},
            "telemetry": self.telemetry.summary(),
        }
        try:
            tc = getattr(self, '_task_graph', None)
            s["task_graph"] = {
                "has_graph": tc is not None,
                "tasks": tc.task_count if tc and hasattr(tc, 'task_count') else 0,
            }
        except Exception:
            s["task_graph"] = {"has_graph": False, "tasks": 0}

        # V5 component stats (lazy, only if initialized)
        for attr, key in [
            ("_runtime_context", "runtime_context"),
            ("_workflow_engine", "workflow_engine"),
            ("_execution_graph", "execution_graph"),
            ("_causal_graph", "causal_graph"),
            ("_runtime_supervisor", "runtime_supervisor"),
        ]:
            try:
                obj = getattr(self, attr, None)
                if obj is not None:
                    s[key] = obj.stats()
            except Exception:
                s[key] = {"error": True}
        try:
            if hasattr(self, '_tracer') and self._tracer is not None:
                s["tracer"] = {
                    "total_spans": self._tracer.total_span_count,
                    "active_spans": self._tracer.active_span_count,
                }
        except Exception:
            s["tracer"] = {"error": True}
        return s
    def summary(self):
        s = self.stats()
        return (
            "RuntimeKernel [%s]\n  State: %s\n  Uptime: %ss\n  Events: %s\n  Nodes: %s (%s transitions)\n"
            "  Diagnostics: %s\n  Memory: %.1f%% hit rate\n  Plugins: %s (%s running)\n" % (
                self._kid, "RUNNING" if self._initialized else "STOPPED", s["uptime"],
                s["event_bus"]["total_events"], s["state_machine"]["nodes"],
                s["state_machine"]["transitions"], s["diagnostic_center"].get("total", 0),
                s["memory_center"]["hit_rate"] * 100,
                s["plugin_manager"]["total"], s["plugin_manager"]["running"],
            )
        )


__all__ = [
    "RuntimeKernel", "EventBus", "Event", "EventType",
    "PriorityLevel", "DeadLetterRecord",
    "NodeStateMachine", "NodeLifecycleState",
    "DiagnosticCenter", "Diagnostic", "DiagnosticSeverity",
    "MemoryCenter", "PluginManager", "Plugin", "PluginState",
    "Capability", "CapabilityPlugin",
    "TransactionSnapshot", "RuntimeTransaction",
    "KnowledgeEntry", "KnowledgeCenter",
    "DiagnosticNode", "DiagnosticGraph",
    "TelemetrySample", "TelemetryCollector",
]


# V5 symbols available via lazy properties on RuntimeKernel:
# - runtime_context  -> pdf2zh.v3.runtime_context.RuntimeContext
# - workflow_engine  -> pdf2zh.v3.workflow_engine.WorkflowEngine
# - execution_graph  -> pdf2zh.v3.execution_graph.ExecutionGraph
# - causal_graph     -> pdf2zh.v3.causal_graph.CausalDiagnosticGraph
# - runtime_supervisor -> pdf2zh.v3.runtime_supervisor.RuntimeSupervisor
# - tracer           -> pdf2zh.v3.tracing.Tracer
