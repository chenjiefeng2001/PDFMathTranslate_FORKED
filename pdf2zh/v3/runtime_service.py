"""Module: V7.3 Runtime Service — DocumentRuntime 服务化 (DIR).

Iteration feedback: the runtime should move from a *library* to a long-lived
*runtime service* (V6.2+). ``RuntimeService`` composes every service
component into one Document Intelligence Runtime platform::

    RuntimeService
      ├── SessionManager         — document sessions, lifecycle, quotas
      ├── ExecutionScheduler     — operator DAG scheduling over ExecutionGraph
      ├── IncrementalEngine      — diff + dirty propagation (re-run only the
      │                            affected subgraph)
      ├── PersistenceLayer       — snapshot / knowledge / telemetry storage
      ├── ResourceManager        — CPU / memory / LLM concurrency quotas
      └── RuntimeNotificationBus — publish / subscribe across components

Execution is fully operator-based (no TransformationPipeline): the runtime
*is* the pipeline (Runtime → OperatorGraph → Scheduler → Operators).

Usage::

    service = RuntimeService(persistence_dir="...")
    session = service.open(blocks)
    output = service.execute(session.session_id)
    service.snapshot(session.session_id, label="v1")
    # ... change a few blocks and re-open ...
    plan = service.incremental.plan(before, after)
    output2 = service.execute(session.session_id,
                              changed_ids=plan.changed)
    service.rollback(session.session_id, label="v1")
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Set

from pdf2zh.v3.operators import (
    AnalyzeOperator, LayoutOperator, OperatorContext, OperatorGraph,
    OperatorRegistry, ParseOperator, PlanOperator, RenderOperator,
    ReviewOperator, TranslateOperator,
)

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# ResourceManager — concurrency & quota control
# ═══════════════════════════════════════════════════════════════════


class ResourceManager:
    """Token-bucket style resource controller for concurrent execution.

    Every resource type (``llm``, ``cpu``, ``memory``) has a limit and a
    semaphore; ``acquire()`` blocks up to ``timeout`` seconds and returns a
    permit handle that must be released. This is what keeps multi-session
    runtime execution bounded.
    """

    def __init__(self,
                 limits: Optional[Dict[str, int]] = None) -> None:
        self._limits: Dict[str, int] = {}
        self._sems: Dict[str, threading.BoundedSemaphore] = {}
        if limits:
            for name, limit in limits.items():
                self.register(name, limit)

    def register(self, name: str, limit: int) -> "ResourceManager":
        if limit < 1:
            raise ValueError(f"Resource '{name}' limit must be >= 1")
        self._limits[name] = limit
        self._sems[name] = threading.BoundedSemaphore(limit)
        return self

    def acquire(self, name: str, amount: int = 1,
                timeout: Optional[float] = None) -> bool:
        """Try to reserve ``amount`` units; returns True if granted."""
        sem = self._sems.get(name)
        if sem is None:
            raise KeyError(f"Unknown resource '{name}' — register() first")
        acquired = 0
        for _ in range(amount):
            if sem.acquire(timeout=timeout):
                acquired += 1
            else:
                break
        if acquired < amount:
            for _ in range(acquired):
                sem.release()
            return False
        return True

    def release(self, name: str, amount: int = 1) -> None:
        sem = self._sems.get(name)
        if sem is None:
            raise KeyError(f"Unknown resource '{name}'")
        for _ in range(amount):
            sem.release()

    def available(self, name: str) -> int:
        sem = self._sems.get(name)
        return sem._value if sem is not None else 0

    def used(self, name: str) -> int:
        return self._limits.get(name, 0) - self.available(name)

    def limits(self) -> Dict[str, int]:
        return dict(self._limits)

    def stats(self) -> dict:
        return {name: {"limit": self._limits.get(name, 0),
                       "used": self.used(name),
                       "available": self.available(name)}
                for name in sorted(self._limits)}

    def __enter__(self) -> "ResourceManager":
        return self

    def __exit__(self, *exc) -> None:
        pass


# ═══════════════════════════════════════════════════════════════════
# SessionManager — document sessions & lifecycle
# ═══════════════════════════════════════════════════════════════════


class SessionManager:
    """Owns DocumentSession instances: create / get / close / evict.

    Enforces a global session cap and supports idle eviction so the runtime
    can serve many documents without leaking state.
    """

    def __init__(self, max_sessions: int = 32,
                 on_evict: Optional[Callable[[str], None]] = None) -> None:
        if max_sessions < 1:
            raise ValueError("max_sessions must be >= 1")
        self._max = max_sessions
        self._sessions: Dict[str, Any] = {}
        self._lock = threading.Lock()
        #: Called with the session id after an idle eviction (create-at-cap
        #: auto-evict or explicit evict_idle) so owners can release
        #: per-session side data (snapshots, metrics, ...).
        self._on_evict = on_evict

    def list_ids(self) -> List[str]:
        """Return the currently live session ids (sorted)."""
        with self._lock:
            return sorted(self._sessions)

    def create(self, document: Any, document_id: Optional[str] = None,
               target_lang: str = "zh-CN") -> Any:
        """Create a new DocumentSession for ``document``.

        When the session cap is reached, idle sessions (untouched for
        ``evict_idle_seconds``) are evicted automatically — including their
        ``on_evict`` side-data cleanup — instead of failing outright. The
        cap is only a hard limit for *actively used* sessions.
        """
        from pdf2zh.v3.document_runtime import DocumentSession, \
            RuntimeCheckpoint, SessionState

        with self._lock:
            if len(self._sessions) >= self._max:
                self._evict_idle_locked()
            if len(self._sessions) >= self._max:
                raise RuntimeError(
                    f"Session limit reached ({self._max}) — all sessions are "
                    "actively in use; close a session first")
            session = DocumentSession(
                document=document, document_id=document_id,
                target_lang=target_lang,
                checkpoint=RuntimeCheckpoint(
                    label="initial", session_id="", state=SessionState.CREATED,
                    graph_snapshot={}, graph_object=None))
            self._sessions[session.session_id] = session
            return session

    def get(self, session_id: str) -> Any:
        with self._lock:
            if session_id not in self._sessions:
                raise KeyError(f"Unknown session '{session_id}'")
            return self._sessions[session_id]

    def has(self, session_id: str) -> bool:
        return session_id in self._sessions

    def close(self, session_id: str) -> bool:
        with self._lock:
            removed = self._sessions.pop(session_id, None) is not None
        if removed and self._on_evict is not None:
            self._notify_evict(session_id)
        return removed

    def evict_idle(self, max_idle_seconds: float = 600.0) -> List[str]:
        """Close sessions untouched for ``max_idle_seconds``."""
        cutoff = time.time() - max_idle_seconds
        evicted: List[str] = []
        with self._lock:
            for sid, session in list(self._sessions.items()):
                last = getattr(session, "last_active", 0.0) or 0.0
                if last < cutoff:
                    self._sessions.pop(sid, None)
                    evicted.append(sid)
        if evicted:
            logger.info("Evicted idle sessions: %s", evicted)
            for sid in evicted:
                self._notify_evict(sid)
        return evicted

    def _evict_idle_locked(self,
                           max_idle_seconds: float = 600.0) -> List[str]:
        """Idle-eviction variant for callers already holding ``_lock``.

        ``create()`` uses this at cap; must never re-acquire the (non-
        reentrant) lock.
        """
        cutoff = time.time() - max_idle_seconds
        evicted: List[str] = []
        for sid, session in list(self._sessions.items()):
            last = getattr(session, "last_active", 0.0) or 0.0
            if last < cutoff:
                self._sessions.pop(sid, None)
                evicted.append(sid)
        if evicted:
            logger.info("Auto-evicted idle sessions at cap: %s", evicted)
            for sid in evicted:
                self._notify_evict(sid)
        return evicted

    def _notify_evict(self, session_id: str) -> None:
        """Best-effort side-data cleanup callback (never blocks eviction)."""
        cb = self._on_evict
        if cb is None:
            return
        try:
            cb(session_id)
        except Exception:  # noqa: BLE001 -- cleanup must not break lifecycle
            logger.warning("on_evict callback failed for session %s",
                           session_id, exc_info=True)

    @property
    def sessions(self) -> List[str]:
        return list(self._sessions.keys())

    @property
    def count(self) -> int:
        return len(self._sessions)

    @property
    def active_count(self) -> int:
        return sum(1 for s in self._sessions.values()
                   if not getattr(s, "closed", False))

    @property
    def max_sessions(self) -> int:
        return self._max

    def stats(self) -> dict:
        return {"count": self.count, "active": self.active_count,
                "max": self._max, "sessions": self.sessions}


# ═══════════════════════════════════════════════════════════════════
# IncrementalEngine — diff + dirty propagation
# ═══════════════════════════════════════════════════════════════════


@dataclass
class IncrementalPlan:
    """What must be re-executed after a set of input changes."""

    changed: List[str] = field(default_factory=list)
    affected: List[str] = field(default_factory=list)

    @property
    def skipped(self) -> int:
        return max(0, len(self.affected) - len(self.changed))

    @property
    def ratio(self) -> float:
        """Fraction of the (potential) execution that is re-run."""
        if not self.affected:
            return 0.0
        return round(len(self.changed) / len(self.affected), 4)

    def to_dict(self) -> dict:
        return {"changed": self.changed, "affected": self.affected,
                "ratio": self.ratio, "skipped": self.skipped}


class IncrementalEngine:
    """Computes dirty sets from snapshot diffs and execution graphs.

    Dirty propagation follows the ExecutionGraph dependency DAG: when a node
    changes, every transitive dependent becomes dirty and must be re-run,
    while unrelated subgraphs are skipped entirely.
    """

    def propagate_dirty(self, execution_graph: Any,
                        changed_ids: Iterable[str]) -> Set[str]:
        """Mark changed nodes (cascade=True) and return the dirty set."""
        for node_id in changed_ids:
            if execution_graph.has_node(node_id):
                execution_graph.mark_dirty(node_id, cascade=True)
        return set(execution_graph.get_dirty_nodes())

    def affected_nodes(self, execution_graph: Any,
                       changed_ids: Iterable[str]) -> List[str]:
        """Dirty node ids in execution order, or [] if no graph available."""
        dirty = self.propagate_dirty(execution_graph, changed_ids)
        ordered = getattr(execution_graph, "get_execution_order", lambda: [])()
        return [node.node_id for node in ordered
                if node.node_id in dirty and node.dirty]

    def plan(self, doc_before: Any, doc_after: Any,
             execution_graph: Optional[Any] = None) -> IncrementalPlan:
        """Diff two documents and plan the affected subgraph.

        ``doc_before`` / ``doc_after`` may be snapshot dicts or objects with
        a ``to_dict()`` exposing ``blocks`` / ``graphs``; node ids of changed
        content nodes are returned as ``changed`` and propagated through the
        execution graph to obtain ``affected``.
        """
        before = self._node_ids(doc_before)
        after = self._node_ids(doc_after)
        changed = sorted(before.keys() ^ after.keys())
        changed += self._changed_properties(before, after)
        # A changed translation is also a dirty signal: the translated state
        # no longer matches the source, so dependent operators must re-run.
        before_tr = self._translations(doc_before)
        after_tr = self._translations(doc_after)
        for nid in before_tr.keys() & after_tr.keys():
            if before_tr.get(nid) != after_tr.get(nid):
                changed.append(nid)
        changed = sorted(set(changed))
        affected: List[str] = list(changed)
        if execution_graph is not None and changed:
            affected = self.affected_nodes(execution_graph, changed)
            changed = [nid for nid in affected if nid in changed]
        return IncrementalPlan(changed=changed, affected=affected)

    # ── helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _node_ids(doc: Any) -> Dict[str, Any]:
        if doc is None:
            return {}
        if isinstance(doc, list):
            data = {"blocks": doc}
        elif isinstance(doc, dict):
            data = doc
        elif hasattr(doc, "to_dict"):
            try:
                data = doc.to_dict()
            except Exception:
                data = {}
        else:
            data = {}
        ids: Dict[str, Any] = {}
        blocks = data.get("blocks", []) if isinstance(data, dict) else []
        for block in blocks:
            if isinstance(block, dict) and block.get("id"):
                ids[block["id"]] = block.get("text", "")
        if not ids and isinstance(data, dict):
            # RuntimeSnapshot shape: {"graphs": {kind: [node_dict, ...]}}
            for nodes in data.get("graphs", {}).values():
                for node in nodes or []:
                    if isinstance(node, dict) and node.get("id"):
                        ids[node["id"]] = node.get("text", "")
        return ids

    @staticmethod
    def _translations(doc: Any) -> Dict[str, str]:
        if doc is None:
            return {}
        if isinstance(doc, dict):
            data = doc
        elif hasattr(doc, "to_dict"):
            try:
                data = doc.to_dict()
            except Exception:
                data = {}
        else:
            data = {}
        translations = data.get("translations", {}) \
            if isinstance(data, dict) else {}
        return {str(k): str(v) for k, v in (translations or {}).items()}

    @staticmethod
    def _changed_properties(before: Dict[str, Any],
                            after: Dict[str, Any]) -> List[str]:
        return [nid for nid in before.keys() & after.keys()
                if before.get(nid) != after.get(nid)]


# ═══════════════════════════════════════════════════════════════════
# ExecutionScheduler — dependency scheduling over operator graphs
# ═══════════════════════════════════════════════════════════════════


class ExecutionScheduler:
    """Schedules operator execution with resource gating.

    Given an ``OperatorGraph`` (the execution DAG), ``run()`` executes the
    topological order while honoring resource quotas; ``run_incremental()``
    restricts execution to the operators marked dirty by the incremental
    engine (skipping clean subgraphs).
    """

    def __init__(self, resource_manager: Optional[ResourceManager] = None
                 ) -> None:
        self.resources = resource_manager or ResourceManager()
        self._runs: List[Dict[str, Any]] = []

    def plan(self, operator_graph: Any,
             filter_names: Optional[Iterable[str]] = None) -> List[str]:
        return operator_graph.order(filter_names=filter_names)

    def run(self, operator_graph: Any, ctx: Any,
            filter_names: Optional[Iterable[str]] = None,
            cache: Optional[Any] = None) -> Any:
        started = time.time()
        order = self.plan(operator_graph, filter_names)
        ctx = operator_graph.run(ctx, filter_names=order, cache=cache)
        self._runs.append({
            "operators": list(order),
            "elapsed_ms": round((time.time() - started) * 1000, 4),
            "incremental": False,
            "cached_operators": [t["operator"] for t in
                                 operator_graph.trace if t.get("cached")],
        })
        return ctx

    def run_incremental(self, operator_graph: Any, ctx: Any,
                        affected_operators: Iterable[str],
                        cache: Optional[Any] = None) -> Any:
        """Execute only the affected operators, preserving clean state."""
        started = time.time()
        affected = list(dict.fromkeys(affected_operators))
        order = self.plan(operator_graph, affected)
        ctx = operator_graph.run(ctx, filter_names=order, cache=cache)
        self._runs.append({
            "operators": list(order),
            "elapsed_ms": round((time.time() - started) * 1000, 4),
            "incremental": True,
            "cached_operators": [t["operator"] for t in
                                 operator_graph.trace if t.get("cached")],
        })
        return ctx

    @property
    def run_history(self) -> List[Dict[str, Any]]:
        return list(self._runs)

    def stats(self) -> dict:
        return {"runs": len(self._runs),
                "last": self._runs[-1] if self._runs else None}


# ═══════════════════════════════════════════════════════════════════
# PersistenceLayer — snapshot / knowledge / telemetry storage
# ═══════════════════════════════════════════════════════════════════


class PersistenceLayer:
    """JSON-file persistence for RuntimeSnapshots and runtime state.

    Snapshots are stored as ``snapshot_<session>_<label>_<ts>.json`` under
    ``directory``, keyed and retrievable by session and label.
    """

    def __init__(self, directory: Optional[str] = None) -> None:
        self.directory = directory or os.path.join(
            os.path.expanduser("~"), ".pdf2zh", "v7_snapshots")
        os.makedirs(self.directory, exist_ok=True)

    def _path(self, snapshot: Any) -> str:
        safe_label = snapshot.label.replace(" ", "_")
        return os.path.join(
            self.directory,
            f"snapshot_{snapshot.session_id or 'anon'}_{safe_label}_"
            f"{int(snapshot.timestamp)}.json")

    def save_snapshot(self, snapshot: Any) -> str:
        return snapshot.save(self._path(snapshot))

    def load_snapshot(self, path: str) -> Any:
        from pdf2zh.v3.runtime_snapshot import RuntimeSnapshot
        return RuntimeSnapshot.load(path)

    def list_snapshots(self, session_id: Optional[str] = None) -> List[str]:
        pattern = f"snapshot_{session_id}_" if session_id else "snapshot_"
        return sorted(
            os.path.join(self.directory, f)
            for f in os.listdir(self.directory)
            if f.startswith(pattern) and f.endswith(".json"))

    def delete_snapshot(self, path: str) -> bool:
        if not os.path.exists(path):
            return False
        os.remove(path)
        return True

    def find(self, session_id: str, label: str) -> Optional[str]:
        for path in self.list_snapshots(session_id):
            base = os.path.basename(path)
            if f"_{label}_" in base:
                return path
        return None

    def stats(self) -> dict:
        return {"directory": self.directory,
                "snapshots": len(self.list_snapshots())}


# ═══════════════════════════════════════════════════════════════════
# RuntimeNotificationBus — publish / subscribe across components
# ═══════════════════════════════════════════════════════════════════


class RuntimeNotificationBus:
    """A lightweight, component-agnostic event bus.

    Topics are free-form strings (``execute.started``, ``snapshot.saved``,
    ``session.closed`` ...). Every event is also kept in a bounded history so
    consumers can inspect recent activity without a live subscription.
    """

    def __init__(self, max_history: int = 500) -> None:
        self._subscribers: Dict[str, List[Callable[[dict], None]]] = {}
        self._history: List[dict] = []
        self._max_history = max_history
        self._lock = threading.Lock()

    def publish(self, topic: str, data: Optional[dict] = None) -> dict:
        event = {"topic": topic, "data": dict(data or {}),
                 "timestamp": time.time()}
        with self._lock:
            self._history.append(event)
            if len(self._history) > self._max_history:
                self._history = self._history[-self._max_history:]
            handlers = list(self._subscribers.get(topic, ()))
        for handler in handlers:
            try:
                handler(event)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Event handler for '%s' failed: %s",
                               topic, exc)
        return event

    def subscribe(self, topic: str,
                  handler: Callable[[dict], None]) -> "RuntimeNotificationBus":
        self._subscribers.setdefault(topic, []).append(handler)
        return self

    def unsubscribe(self, topic: str,
                    handler: Callable[[dict], None]) -> bool:
        handlers = self._subscribers.get(topic, [])
        if handler in handlers:
            handlers.remove(handler)
            return True
        return False

    def history(self, topic: Optional[str] = None) -> List[dict]:
        if topic is None:
            return list(self._history)
        return [e for e in self._history if e["topic"] == topic]

    def clear(self) -> None:
        with self._lock:
            self._history.clear()
            self._subscribers.clear()

    def stats(self) -> dict:
        return {
            "history": len(self._history),
            "topics": len(self._subscribers),
            "subscribers": sum(len(h) for h in self._subscribers.values()),
        }


# ═══════════════════════════════════════════════════════════════════
# RuntimeService — the Document Intelligence Runtime facade
# ═══════════════════════════════════════════════════════════════════


class RuntimeService:
    """Long-lived Document Intelligence Runtime service.

    Composes session management, operator scheduling, incremental execution,
    persistence, resource quotas and notifications — the V6.2/V7 target of
    moving DocumentRuntime from a library to a runtime service. Execution is
    fully operator-based (no TransformationPipeline).

    Defaults are rule-based (no LLM required), mirroring DocumentRuntime, so
    the service runs end-to-end out of the box.
    """

    def __init__(self, document_runtime: Any = None, *,
                 persistence_dir: Optional[str] = None,
                 max_sessions: int = 32,
                 max_concurrency: int = 4,
                 max_llm_concurrency: int = 2,
                 cache: Optional[Any] = None,
                 knowledge: Optional[Any] = None) -> None:
        from pdf2zh.v3.document_runtime import DocumentRuntime
        from pdf2zh.v3.knowledge_graph import KnowledgePropagator
        from pdf2zh.v3.operator_cache import OperatorResultCache

        self.runtime = document_runtime or DocumentRuntime()
        self.sessions = SessionManager(
            max_sessions=max_sessions,
            on_evict=self._release_session_side_data)
        self.resources = ResourceManager()
        self.resources.register("concurrency", max_concurrency)
        self.resources.register("llm", max_llm_concurrency)
        self.scheduler = ExecutionScheduler(self.resources)
        self.incremental = IncrementalEngine()
        self.persistence = PersistenceLayer(directory=persistence_dir)
        self.bus = RuntimeNotificationBus()
        self.operators = OperatorRegistry()
        self.operator_graph = self._build_operator_graph()
        # V7.4 cache-aside operator result cache (enabled by default; pass
        # cache=False to disable, or an OperatorResultCache to tune it).
        if cache is False:
            self.cache = None
        else:
            self.cache = cache if cache is not None else OperatorResultCache()
        # V7.5 cross-session knowledge graph + propagation bridge.
        self.knowledge = knowledge
        self.knowledge_propagator = KnowledgePropagator(knowledge) \
            if knowledge is not None else None
        self._snapshots: Dict[str, List[Any]] = {}
        self._metrics: Dict[str, Any] = {}

    # ── Operator DAG (the pipeline replacement) ───────────────────────

    def _build_operator_graph(self) -> OperatorGraph:
        g = OperatorGraph()
        g.add(ParseOperator())
        g.add(AnalyzeOperator(), depends_on=["parse"])
        g.add(PlanOperator(), depends_on=["parse"])
        g.add(TranslateOperator(), depends_on=["plan"])
        g.add(ReviewOperator(), depends_on=["translate"])
        g.add(LayoutOperator(), depends_on=["parse"])
        g.add(RenderOperator(), depends_on=["layout", "review"])
        return g

    # ── Session lifecycle ─────────────────────────────────────────────

    def open(self, document: Any, document_id: Optional[str] = None,
             target_lang: str = "zh-CN") -> Any:
        """Open a new document session managed by the runtime."""
        from pdf2zh.v3.document_runtime import SessionState

        session = self.sessions.create(document, document_id, target_lang)
        session.transition(SessionState.OPENED, event="open")
        session.transition(SessionState.READY, event="ready")
        session.last_active = time.time()
        self.bus.publish("session.opened",
                         {"session_id": session.session_id,
                          "document_id": document_id})
        return session

    def close(self, session_id: str) -> bool:
        closed = self.sessions.close(session_id)
        if closed:
            self._release_session_side_data(session_id)
            self.bus.publish("session.closed", {"session_id": session_id})
        return closed

    def _release_session_side_data(self, session_id: str) -> None:
        """Drop per-session in-memory side data (GC/leak guard).

        ``self._snapshots[session_id]`` previously grew without bound for the
        lifetime of the service — it is never read after close/eviction.
        """
        self._snapshots.pop(session_id, None)
        self._metrics.pop(session_id, None)

    # ── Execution ─────────────────────────────────────────────────────

    def execute(self, session_id: str, *,
                provider: Any = None,
                changed_ids: Optional[Iterable[str]] = None) -> Any:
        """Execute the operator DAG for a session.

        Full execution when ``changed_ids`` is None; otherwise only the
        operators affected by the given content-node changes are re-run
        (incremental execution).
        """
        session = self.sessions.get(session_id)
        session.last_active = time.time()
        incremental = bool(changed_ids)
        self.bus.publish("execute.started", {
            "session_id": session_id, "incremental": incremental,
            "changed": sorted(set(changed_ids or ())),
        })

        acquired = self.resources.acquire("concurrency", timeout=60.0)
        if not acquired:
            raise RuntimeError("Runtime busy: concurrency quota exhausted")
        try:
            if incremental:
                self._incremental_ids = list(changed_ids or ())
                affected = self._affected_operators(session, changed_ids)
                logger.info("Session %s: incremental re-run of %s",
                            session_id, affected)
                ctx = self.scheduler.run_incremental(
                    self.operator_graph,
                    self._build_context(session, provider),
                    affected_operators=affected,
                    cache=self.cache)
            else:
                self._incremental_ids = None
                ctx = self.scheduler.run(
                    self.operator_graph,
                    self._build_context(session, provider),
                    cache=self.cache)
        finally:
            self.resources.release("concurrency")

        self._commit(session, ctx)
        self._propagate_knowledge(session, ctx)
        self.bus.publish("execute.completed", {
            "session_id": session_id,
            "translated": ctx.metrics.get("translated", 0),
            "operators": [t["operator"] for t in self.operator_graph.trace],
            "cached_operators": [t["operator"] for t in
                                 self.operator_graph.trace
                                 if t.get("cached")],
        })
        return self._to_pipeline_output(session, ctx)

    def execute_incremental(self, session_id: str, changed_ids: Iterable[str],
                            provider: Any = None) -> Any:
        """Convenience wrapper for incremental re-execution."""
        return self.execute(session_id, provider=provider,
                            changed_ids=changed_ids)


    # ── Execution internals ───────────────────────────────────────────

    def _build_context(self, session: Any, provider: Any) -> OperatorContext:
        config = self.runtime.pipeline.config
        # V7.5: a new session sees the shared terminology accumulated by all
        # previously executed sessions (config is cloned, never mutated).
        if self.knowledge_propagator is not None:
            config = self.knowledge_propagator.prepare_config(config)
        ctx = OperatorContext(
            session_id=session.session_id,
            document=session.document,
            provider=provider or getattr(session, "provider", None),
            config=config,
            page_width=getattr(session, "page_width", 612.0),
            page_height=getattr(session, "page_height", 792.0),
        )
        # Carry over previously computed state so incremental (operator
        # subgraph) execution can reuse the document graph and translations
        # that were produced by operators skipped in this run.
        if session.document_graph is not None:
            ctx.document_graph = session.document_graph
            ctx.register_graph("document", session.document_graph)
        if session.translations:
            ctx.translations = dict(session.translations)
        if getattr(self, "_incremental_ids", None):
            ctx.extra["incremental_ids"] = list(self._incremental_ids)
        return ctx

    def _commit(self, session: Any, ctx: OperatorContext) -> None:
        """Write operator results back into the document session state."""
        from pdf2zh.v3.document_runtime import SessionState
        from pdf2zh.v3.graph_property import create_property_graph_from_document

        session.document_graph = ctx.document_graph
        session.translations = dict(ctx.translations)
        session.outputs = dict(ctx.outputs)
        session.metrics = dict(ctx.metrics)
        session.graphs = dict(ctx.graphs)
        # V7.0: keep a property-graph view of the document alongside the
        # document graph — indexed MATCH / traversal queries without O(N)
        # scans (e.g. "all Paragraph nodes on page 0").
        if session.document_graph is not None:
            try:
                property_graph = create_property_graph_from_document(
                    session.document_graph)
                session.graphs["property"] = property_graph
            except Exception:  # never break the pipeline on indexing errors
                pass
        if "analysis" in ctx.extra:
            session.knowledge.update(ctx.extra["analysis"])
        if "review" in ctx.extra:
            session.diagnostics["review"] = ctx.extra["review"]
        session.workflow["operators"] = list(self.operator_graph.trace)
        session.telemetry["scheduler"] = self.scheduler.stats()
        session.telemetry["metrics"] = dict(ctx.metrics)
        session.last_active = time.time()
        if session.can_transition(SessionState.EXECUTING):
            session.transition(SessionState.EXECUTING, event="operator_run")
        if session.can_transition(SessionState.COMPLETED):
            session.transition(SessionState.COMPLETED, event="execute_done")

    def _propagate_knowledge(self, session: Any, ctx: OperatorContext) -> Any:
        """V7.5: push a finished session's knowledge into the shared graph."""
        if self.knowledge_propagator is None:
            return None
        report = self.knowledge_propagator.propagate(ctx,
                                                     session.session_id)
        self.bus.publish("knowledge.propagated", report.to_dict())
        logger.info("Session %s → knowledge: +%d entities, +%d glossary",
                    session.session_id, report.entities_added,
                    report.glossary_added)
        return report

    def knowledge_stats(self) -> dict:
        if self.knowledge is None:
            return {"enabled": False}
        return {"enabled": True, **self.knowledge.stats()}

    def _affected_operators(self, session: Any,
                            changed_ids: Iterable[str]) -> List[str]:
        """Dirty propagation: content change → affected operators only."""
        from pdf2zh.v3.execution_graph import ExecutionGraph

        eg = ExecutionGraph()
        order = self.operator_graph.order()
        for op in order:
            deps = [d for d in order if d in self.operator_graph._deps[op]]
            eg.add_node(op, depends_on=deps)
        for block in self._content_blocks(session):
            eg.add_node(block, depends_on=[])
            eg.get_node("translate").depends_on.add(block)
            eg.get_node(block).dependents.add("translate")
            eg.get_node("layout").depends_on.add(block)
            eg.get_node(block).dependents.add("layout")

        dirty: Set[str] = set()
        stack = list(changed_ids)
        while stack:
            nid = stack.pop()
            if nid in dirty:
                continue
            dirty.add(nid)
            node = eg.get_node(nid)
            if node is None:
                continue
            stack.extend(node.dependents)
        affected = [op for op in order if op in dirty]
        # A content change always invalidates the source parse as well: the
        # DocumentGraph must be rebuilt from the (new) document text before
        # translation / layout can meaningfully re-run.
        if affected and "parse" not in affected:
            affected.insert(0, "parse")
        return affected

    @staticmethod
    def _content_blocks(session: Any) -> List[str]:
        doc = session.document
        if isinstance(doc, list):
            return [str(b.get("id")) for b in doc
                    if isinstance(b, dict) and b.get("id")]
        if hasattr(doc, "nodes"):
            return [getattr(n, "id", str(i))
                    for i, n in enumerate(doc.nodes)
                    if getattr(n, "text", "").strip()]
        return []

    def _to_pipeline_output(self, session: Any, ctx: OperatorContext) -> Any:
        from pdf2zh.v3.transformation_pipeline import PipelineOutput, \
            PipelineStats

        review = ctx.extra.get("review", {})
        return PipelineOutput(
            graph=session.document_graph,
            translations=dict(session.translations),
            review=review,
            manifest=ctx.extra.get("manifest", {}),
            rendered=dict(session.outputs),
            stats=PipelineStats(
                total_nodes=ctx.metrics.get("nodes", 0),
                translated=ctx.metrics.get("translated", 0),
                review_errors=review.get("errors", 0),
                quality_score=review.get("quality_score", 1.0),
                elapsed_ms=ctx.metrics.get("elapsed_ms", 0.0),
            ),
            session_summary=ctx.extra.get("session_summary", {}),
        )


    # ── State snapshot / rollback (V7.2) ──────────────────────────────

    def snapshot(self, session_id: str,
                 label: str = "snapshot") -> Any:
        """Capture a full state snapshot of a session (in-memory)."""
        from pdf2zh.v3.runtime_snapshot import RuntimeSnapshot

        session = self.sessions.get(session_id)
        snap = RuntimeSnapshot.capture(session, label=label)
        self._snapshots.setdefault(session_id, []).append(snap)
        self.bus.publish("snapshot.captured", {
            "session_id": session_id, "label": label,
            "snapshot_id": snap.snapshot_id,
        })
        return snap

    def rollback(self, session_id: str,
                 snapshot: Any = None) -> Any:
        """True rollback: restore the complete session state from a snapshot."""
        session = self.sessions.get(session_id)
        target = snapshot
        if target is None:
            snaps = self._snapshots.get(session_id, [])
            if not snaps:
                raise ValueError(
                    f"No in-memory snapshot for session '{session_id}'")
            target = snaps[-1]
        target.restore_into(session)
        session.last_active = time.time()
        self.bus.publish("session.rolled_back", {
            "session_id": session_id, "label": target.label,
        })
        return target

    def persist(self, session_id: str, label: str = "snapshot") -> str:
        """Capture AND persist a snapshot to disk. Returns the file path."""
        snap = self.snapshot(session_id, label=label)
        path = self.persistence.save_snapshot(snap)
        self.bus.publish("snapshot.saved", {
            "session_id": session_id, "label": label, "path": path,
        })
        return path

    def restore(self, session_id: str, path: str) -> Any:
        """Load a persisted snapshot and restore it into the session."""
        snap = self.persistence.load_snapshot(path)
        return self.rollback(session_id, snapshot=snap)

    def list_snapshots(self, session_id: str) -> List[Any]:
        return list(self._snapshots.get(session_id, []))

    def persisted(self, session_id: Optional[str] = None) -> List[str]:
        return self.persistence.list_snapshots(session_id)

    # ── Events / notifications ────────────────────────────────────────

    def notify(self, topic: str, data: Optional[dict] = None) -> dict:
        return self.bus.publish(topic, data)

    def on(self, topic: str,
           handler: Callable[[dict], None]) -> "RuntimeService":
        self.bus.subscribe(topic, handler)
        return self

    # ── Status / stats ────────────────────────────────────────────────

    def status(self, session_id: str) -> dict:
        session = self.sessions.get(session_id)
        return {
            "session_id": session_id,
            "state": getattr(session, "state", ""),
            "nodes": getattr(session, "metrics", {}).get("nodes", 0),
            "translated": len(getattr(session, "translations", {})),
            "formats": list(getattr(session, "outputs", {}).keys()),
            "snapshots": len(self._snapshots.get(session_id, [])),
            "last_active": getattr(session, "last_active", None),
        }

    def stats(self) -> dict:
        return {
            "sessions": self.sessions.stats(),
            "resources": self.resources.stats(),
            "scheduler": self.scheduler.stats(),
            "events": self.bus.stats(),
            "operators": self.operator_graph.stats(),
            "persistence": self.persistence.stats(),
            "cache": self.cache.stats() if self.cache is not None
            else {"enabled": False},
            "knowledge": self.knowledge_stats(),
        }


__all__ = [
    "ResourceManager", "SessionManager", "IncrementalPlan",
    "IncrementalEngine", "ExecutionScheduler", "PersistenceLayer",
    "RuntimeNotificationBus", "RuntimeService",
]
