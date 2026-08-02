"""Module: V6.1 Document Runtime — Runtime-First document lifecycle.

Implements the "Document is a Runtime, not a one-shot input" evolution:

    runtime.open(document)
    runtime.execute()
    runtime.pause()
    runtime.resume()
    runtime.rollback()
    runtime.diff()
    runtime.snapshot()

A document stays alive inside a `DocumentSession`. The `DocumentRuntime` owns
every session, drives the V6 TransformationPipeline, keeps checkpoints, and
exposes a unified graph view (`BaseGraph`) over DocumentGraph / ExecutionGraph /
ConstraintGraph so that all graphs share one traversal / serialization / diff /
snapshot backbone.

Usage::

    from pdf2zh.v3.document_runtime import DocumentRuntime

    runtime = DocumentRuntime()
    session = runtime.open({"pages": [{"blocks": [...]}]})
    runtime.execute(session.session_id)
    runtime.pause(session.session_id)
    runtime.resume(session.session_id)
    runtime.snapshot(session.session_id, label="v1")
    runtime.diff(session.session_id, before="v1", after="v2")
    runtime.close(session.session_id)
"""

from __future__ import annotations

import copy
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from pdf2zh.v3.base_graph import BaseGraph, GraphKind, adapt
from pdf2zh.v3.graph import DocumentGraph, EdgeType, NodeType
from pdf2zh.v3.transformation_pipeline import (
    PipelineConfig, PipelineOutput, TransformationPipeline,
)

logger = logging.getLogger(__name__)


class SessionState(str, Enum):
    """Lifecycle states of a document session."""

    CREATED = "created"
    OPENED = "opened"
    READY = "ready"
    EXECUTING = "executing"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"
    CLOSED = "closed"


# Legal state transitions of the session state machine.
TRANSITIONS: Dict[SessionState, set] = {
    SessionState.CREATED: {SessionState.OPENED},
    SessionState.OPENED: {SessionState.READY, SessionState.CLOSED},
    SessionState.READY: {SessionState.EXECUTING, SessionState.PAUSED,
                         SessionState.CLOSED},
    SessionState.EXECUTING: {SessionState.PAUSED, SessionState.COMPLETED,
                             SessionState.FAILED},
    SessionState.PAUSED: {SessionState.EXECUTING, SessionState.ROLLED_BACK,
                          SessionState.CLOSED},
    SessionState.COMPLETED: {SessionState.PAUSED, SessionState.ROLLED_BACK,
                             SessionState.CLOSED},
    SessionState.FAILED: {SessionState.ROLLED_BACK, SessionState.EXECUTING,
                          SessionState.CLOSED},
    SessionState.ROLLED_BACK: {SessionState.EXECUTING, SessionState.CLOSED},
    SessionState.CLOSED: set(),
}


@dataclass
class RuntimeCheckpoint:
    """A point-in-time snapshot of a document session."""

    label: str
    session_id: str
    state: SessionState
    graph_snapshot: dict  # BaseGraph.to_dict() — serializable
    graph_object: Optional[Any]  # deep-copied DocumentGraph — in-memory restore
    translations: Dict[str, str] = field(default_factory=dict)
    outputs: Dict[str, str] = field(default_factory=dict)
    metrics: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = 0.0

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = time.time()

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "session_id": self.session_id,
            "state": self.state.value,
            "graph_snapshot": self.graph_snapshot,
            "translations": self.translations,
            "outputs": list(self.outputs.keys()),
            "metrics": self.metrics,
            "timestamp": self.timestamp,
        }


class DocumentSession:
    """A single document living inside the runtime.

    The session is the *unit of lifecycle*: it owns the document, its graphs,
    its translations, its checkpoints and its history trace.
    """

    def __init__(self, document: Any, document_id: Optional[str] = None,
                 target_lang: str = "zh-CN",
                 checkpoint: Optional["RuntimeCheckpoint"] = None) -> None:
        self.session_id = uuid.uuid4().hex[:12]
        self.document_id = document_id or uuid.uuid4().hex[:8]
        self.document = document
        self.target_lang = target_lang
        self.provider: Any = None
        self.state = SessionState.CREATED
        self.graph: Optional[DocumentGraph] = None
        self.base_graph: Optional[BaseGraph] = None
        self.translations: Dict[str, str] = {}
        self.outputs: Dict[str, str] = {}
        self.metrics: Dict[str, Any] = {}
        self.checkpoints: List[RuntimeCheckpoint] = (
            [checkpoint] if checkpoint is not None else [])
        self.graphs: Dict[str, BaseGraph] = {}  # unified graph views
        self.history: List[Dict[str, Any]] = []
        # ── V7 state components (RuntimeSnapshot capture/restore) ──────
        self.knowledge: Dict[str, Any] = {}
        self.cache: Dict[str, Any] = {}
        self.memory: Dict[str, Any] = {}
        self.workflow: Dict[str, Any] = {}
        self.telemetry: Dict[str, Any] = {}
        self.diagnostics: Dict[str, Any] = {}
        self.plugins: Dict[str, Any] = {}
        self.queue: List[Any] = []
        self.last_active: float = time.time()
        self.created_at = time.time()
        self.updated_at = self.created_at
        self.closed_at: Optional[float] = None
        self.history.append({"state": self.state.value,
                             "timestamp": self.created_at, "event": "created"})

    @property
    def document_graph(self) -> Optional[DocumentGraph]:
        """Alias used by the V7 operator runtime (same object as ``graph``)."""
        return self.graph

    @document_graph.setter
    def document_graph(self, value: Optional[DocumentGraph]) -> None:
        self.graph = value

    # ── State machine ──────────────────────────────────────────────────

    def can_transition(self, target: SessionState) -> bool:
        return target in TRANSITIONS[self.state]

    def transition(self, target: SessionState, event: str = "") -> "DocumentSession":
        if not self.can_transition(target):
            raise RuntimeError(
                f"Illegal transition {self.state.value} -> {target.value}"
            )
        self.state = target
        self.updated_at = time.time()
        self.history.append({
            "state": target.value,
            "timestamp": self.updated_at,
            "event": event,
        })
        return self

    @property
    def is_alive(self) -> bool:
        return self.state not in (SessionState.CLOSED,)

    def state_trace(self) -> List[str]:
        return [h["state"] for h in self.history]


class DocumentRuntime:
    """Runtime-first orchestrator: documents stay alive as sessions.

    Wraps the V6 TransformationPipeline behind a full document lifecycle so a
    document can be opened, executed, paused, resumed, rolled back, diffed,
    snapshotted and finally closed — all while keeping every graph (document /
    execution / constraint) unified behind BaseGraph.
    """

    def __init__(self, pipeline: Optional[TransformationPipeline] = None,
                 config: Optional[PipelineConfig] = None,
                 kernel: Any = None) -> None:
        self.pipeline = pipeline or TransformationPipeline(config or PipelineConfig())
        self.kernel = kernel
        self.sessions: Dict[str, DocumentSession] = {}
        self._active_session_id: Optional[str] = None

    # ── Session management ─────────────────────────────────────────────

    def _resolve(self, session_id: Optional[str] = None) -> DocumentSession:
        sid = session_id or self._active_session_id
        if not sid or sid not in self.sessions:
            raise KeyError(f"Unknown session '{sid}' — call open() first")
        return self.sessions[sid]

    def open(self, document: Any, *, document_id: Optional[str] = None,
             target_lang: str = "zh-CN") -> DocumentSession:
        """Open a document and keep it alive inside the runtime."""
        session = DocumentSession(document, document_id=document_id,
                                  target_lang=target_lang)
        session.transition(SessionState.OPENED, event="open")
        session.transition(SessionState.READY, event="ready")
        self.sessions[session.session_id] = session
        self._active_session_id = session.session_id
        logger.info("Document session %s opened (%s)", session.session_id,
                    session.document_id)
        return session

    def close(self, session_id: Optional[str] = None) -> dict:
        """Close a session; its checkpoints stay queryable."""
        session = self._resolve(session_id)
        session.transition(SessionState.CLOSED, event="close")
        session.closed_at = time.time()
        if self._active_session_id == session.session_id:
            self._active_session_id = None
        return {"session_id": session.session_id, "state": session.state.value,
                "checkpoints": len(session.checkpoints)}

    def list_sessions(self) -> List[dict]:
        return [{"session_id": s.session_id, "document_id": s.document_id,
                 "state": s.state.value, "checkpoints": len(s.checkpoints),
                 "alive": s.is_alive} for s in self.sessions.values()]

    def status(self, session_id: Optional[str] = None) -> dict:
        session = self._resolve(session_id)
        return {
            "session_id": session.session_id,
            "document_id": session.document_id,
            "state": session.state.value,
            "trace": session.state_trace(),
            "checkpoints": [c.label for c in session.checkpoints],
            "metrics": dict(session.metrics),
            "graphs": list(session.graphs.keys()),
            "created_at": session.created_at,
            "updated_at": session.updated_at,
        }


    # ── Lifecycle ──────────────────────────────────────────────────────

    def execute(self, session_id: Optional[str] = None, *,
                provider: Any = None,
                page_width: float = 612.0,
                page_height: float = 792.0,
                event: str = "execute") -> PipelineOutput:
        """Run the whole pipeline and leave the document alive with results."""
        session = self._resolve(session_id)
        if session.state not in (SessionState.READY, SessionState.PAUSED,
                                 SessionState.ROLLED_BACK, SessionState.FAILED):
            raise RuntimeError(
                f"Cannot execute from state '{session.state.value}'")
        session.transition(SessionState.EXECUTING, event=event)
        session.last_active = time.time()
        blocks = self._prepare_blocks(session.document)
        self.snapshot(session.session_id, label=f"{event}_start")

        started = time.time()
        try:
            output = self.pipeline.run(blocks, page_width=page_width,
                                       page_height=page_height,
                                       provider=provider)
        except Exception as exc:  # pragma: no cover - defensive
            session.transition(SessionState.FAILED,
                               event=f"execute_error:{type(exc).__name__}")
            raise
        elapsed_ms = (time.time() - started) * 1000.0

        session.graph = output.graph
        session.translations = dict(output.translations)
        session.outputs = dict(output.rendered or {})
        stats = output.stats
        session.metrics.update({
            "elapsed_ms": getattr(stats, "elapsed_ms", elapsed_ms),
            "total_nodes": getattr(stats, "total_nodes", 0),
            "translated": getattr(stats, "translated", 0),
            "quality_score": getattr(stats, "quality_score", 1.0),
            "resume_count": session.metrics.get("resume_count", 0),
        })

        # Build auxiliary graphs and unify them behind BaseGraph.
        session.execution_graph = _build_execution_graph(output.graph)
        session.constraint_graph = _build_constraint_graph(output.graph)
        session.graphs["document"] = adapt(output.graph, GraphKind.DOCUMENT)
        session.graphs["execution"] = adapt(session.execution_graph)
        session.graphs["constraint"] = adapt(session.constraint_graph)

        self.snapshot(session.session_id, label=f"{event}_end")
        session.transition(SessionState.COMPLETED, event=f"{event}_done")

        if self.kernel is not None and hasattr(self.kernel, "telemetry"):
            self.kernel.telemetry.record("document_runtime.execute",
                                         elapsed_ms, success=True)
        return output

    def pause(self, session_id: Optional[str] = None) -> dict:
        """Pause the document mid-lifecycle, keeping a resumable checkpoint."""
        session = self._resolve(session_id)
        if session.state not in (SessionState.EXECUTING, SessionState.READY,
                                 SessionState.COMPLETED):
            raise RuntimeError(
                f"Cannot pause from state '{session.state.value}'")
        label = f"pause_{len(session.checkpoints) + 1}"
        self.snapshot(session.session_id, label=label)
        session.transition(SessionState.PAUSED, event="pause")
        return {"session_id": session.session_id, "state": session.state.value,
                "checkpoint": label}

    def resume(self, session_id: Optional[str] = None, *,
               provider: Any = None) -> PipelineOutput:
        """Resume a paused document from its last checkpoint."""
        session = self._resolve(session_id)
        if session.state != SessionState.PAUSED:
            raise RuntimeError(
                f"Cannot resume from state '{session.state.value}'")
        session.metrics["resume_count"] = session.metrics.get("resume_count", 0) + 1
        return self.execute(session.session_id, provider=provider,
                            event="resume")


    def snapshot(self, session_id: Optional[str] = None, *,
                 label: Optional[str] = None) -> RuntimeCheckpoint:
        """Explicitly capture a checkpoint of the current session state."""
        session = self._resolve(session_id)
        label = label or f"checkpoint_{len(session.checkpoints) + 1}"
        if session.graph is not None:
            snapshot_data = adapt(session.graph, GraphKind.DOCUMENT).to_dict()
            graph_object = copy.deepcopy(session.graph)
        else:
            snapshot_data = {"kind": GraphKind.DOCUMENT.value,
                             "name": session.document_id, "nodes": [], "edges": []}
            graph_object = None
        checkpoint = RuntimeCheckpoint(
            label=label,
            session_id=session.session_id,
            state=session.state,
            graph_snapshot=snapshot_data,
            graph_object=graph_object,
            translations=dict(session.translations),
            outputs=dict(session.outputs),
            metrics=dict(session.metrics),
        )
        session.checkpoints.append(checkpoint)
        return checkpoint

    def rollback(self, session_id: Optional[str] = None, *,
                 checkpoint_label: Optional[str] = None) -> dict:
        """Roll the document back to a checkpoint (default: latest)."""
        session = self._resolve(session_id)
        if not session.checkpoints:
            raise RuntimeError("No checkpoints available to roll back to")
        checkpoint = self._find_checkpoint(session, checkpoint_label)
        session.graph = copy.deepcopy(checkpoint.graph_object) \
            if checkpoint.graph_object is not None else None
        session.translations = dict(checkpoint.translations)
        session.outputs = dict(checkpoint.outputs)
        session.metrics = dict(checkpoint.metrics)
        session.base_graph = BaseGraph.from_dict(checkpoint.graph_snapshot) \
            if checkpoint.graph_snapshot else None
        idx = session.checkpoints.index(checkpoint)
        session.checkpoints = session.checkpoints[:idx + 1]
        session.transition(SessionState.ROLLED_BACK,
                           event=f"rollback_to:{checkpoint.label}")
        return {"session_id": session.session_id,
                "rollback_to": checkpoint.label,
                "state": session.state.value,
                "translations_restored": len(checkpoint.translations)}

    def diff(self, session_id: Optional[str] = None, *,
             before: Optional[str] = None,
             after: Optional[str] = None) -> dict:
        """Structural diff between two checkpoints of the same session."""
        session = self._resolve(session_id)
        if len(session.checkpoints) < 2:
            raise RuntimeError("At least two checkpoints are required for a diff")
        if before is None and after is None:
            first, second = session.checkpoints[-2], session.checkpoints[-1]
        else:
            first = self._find_checkpoint(session, before)
            second = self._find_checkpoint(session, after)
        gb = BaseGraph.from_dict(first.graph_snapshot)
        ga = BaseGraph.from_dict(second.graph_snapshot)
        diff = gb.diff(ga)
        return {"before": first.label, "after": second.label,
                "diff": diff.to_dict(), "summary": diff.summary()}

    # ── V7.2 state snapshots (full rollback, not just graph restore) ──

    def snapshot_state(self, session_id: Optional[str] = None, *,
                       label: str = "snapshot") -> Any:
        """Capture a full V7 state snapshot (every runtime component)."""
        from pdf2zh.v3.runtime_snapshot import RuntimeSnapshot

        session = self._resolve(session_id)
        return RuntimeSnapshot.capture(session, label=label)

    def rollback_state(self, session_id: Optional[str] = None,
                       snapshot: Any = None) -> Any:
        """True rollback: restore the complete session state from a snapshot."""
        from pdf2zh.v3.runtime_snapshot import RuntimeSnapshot

        session = self._resolve(session_id)
        target = snapshot
        if target is None:
            raise ValueError("rollback_state() requires a RuntimeSnapshot")
        if not isinstance(target, RuntimeSnapshot):
            raise TypeError("snapshot must be a RuntimeSnapshot")
        target.restore_into(session)
        session.transition(SessionState.ROLLED_BACK,
                           event=f"state_rollback:{target.label}")
        return target

    def persist_state(self, session_id: Optional[str] = None, *,
                      label: str = "snapshot", directory: Optional[str] = None
                      ) -> str:
        """Capture and persist a V7 state snapshot. Returns the file path."""
        from pdf2zh.v3.runtime_service import PersistenceLayer

        snapshot = self.snapshot_state(session_id, label=label)
        layer = PersistenceLayer(directory=directory)
        return layer.save_snapshot(snapshot)

    def restore_state(self, session_id: Optional[str] = None,
                      path: str = "") -> Any:
        """Load a persisted snapshot and restore it into the session."""
        from pdf2zh.v3.runtime_service import PersistenceLayer

        snapshot = PersistenceLayer().load_snapshot(path)
        return self.rollback_state(session_id, snapshot=snapshot)

    def diff_snapshots(self, before: Any, after: Any) -> dict:
        """Structural diff between two RuntimeSnapshot objects."""
        d = before.diff(after)
        return {"before": before.label, "after": after.label,
                "diff": d.to_dict(), "summary": d.summary()}


    # ── Unified graph views ────────────────────────────────────────────

    def register_graph(self, session_id: Optional[str], kind: GraphKind,
                       graph: Any) -> BaseGraph:
        """Register any concrete graph as a unified BaseGraph view."""
        session = self._resolve(session_id)
        bg = graph if isinstance(graph, BaseGraph) else adapt(graph, kind=kind)
        session.graphs[kind.value] = bg
        return bg

    def graphs(self, session_id: Optional[str] = None) -> Dict[str, BaseGraph]:
        session = self._resolve(session_id)
        return dict(session.graphs)

    # ── Helpers ────────────────────────────────────────────────────────

    def _find_checkpoint(self, session: DocumentSession,
                         label: Optional[str]) -> RuntimeCheckpoint:
        if label is None:
            return session.checkpoints[-1]
        for cp in reversed(session.checkpoints):
            if cp.label == label:
                return cp
        raise KeyError(f"Checkpoint '{label}' not found in session")

    @staticmethod
    def _prepare_blocks(document: Any) -> List[dict]:
        """Normalize a session document into pipeline block dicts."""
        if isinstance(document, DocumentGraph):
            return [{
                "id": n.id, "text": n.text,
                "type": n.node_type.value if hasattr(n.node_type, "value")
                        else str(n.node_type),
                "x": n.x0, "y": n.y0,
                "w": max(n.width, 1.0), "h": max(n.height, 1.0),
                "page": n.page_num, "font_size": n.font_size,
            } for n in document.nodes
                if n.node_type not in (NodeType.PAGE, NodeType.DOCUMENT)]
        if isinstance(document, list) and document and isinstance(document[0], dict):
            return document
        if isinstance(document, dict):
            blocks = document.get("blocks") or (
                document.get("pages") or [{}])[0].get("blocks", [])
            if blocks:
                return blocks
        raise TypeError(
            "Unsupported document: expected blocks list, dict, or DocumentGraph")


def _build_execution_graph(doc_graph: DocumentGraph) -> Any:
    """Derive an ExecutionGraph from the DocumentGraph reading order."""
    from pdf2zh.v3.execution_graph import ExecutionGraph, ExecutionNodeState

    eg = ExecutionGraph()
    content = [n for n in doc_graph.nodes
               if n.node_type not in (NodeType.PAGE, NodeType.DOCUMENT)]
    for n in content:
        deps = [e.source_id for e in doc_graph.edges
                if e.target_id == n.id and e.edge_type != EdgeType.CONTAINS]
        eg.add_node(n.id, label=(n.text[:40] or n.node_type.value), depends_on=deps)
    # Follow reading order and mark nodes as translated.
    for n in sorted(content, key=lambda x: (x.page_num, x.y0, x.x0)):
        if eg.has_node(n.id):
            eg.set_state(n.id, ExecutionNodeState.TRANSLATED)
            eg.mark_clean(n.id)
    return eg


def _build_constraint_graph(doc_graph: DocumentGraph) -> Any:
    """Derive a ConstraintGraph from the DocumentGraph layout relations."""
    from pdf2zh.v3.constraint_graph import (
        ConstraintGraph, build_constraint_graph_from_document,
    )

    cg = ConstraintGraph()
    build_constraint_graph_from_document(cg, doc_graph)
    return cg


__all__ = [
    "SessionState", "TRANSITIONS", "RuntimeCheckpoint", "DocumentSession",
    "DocumentRuntime",
]
