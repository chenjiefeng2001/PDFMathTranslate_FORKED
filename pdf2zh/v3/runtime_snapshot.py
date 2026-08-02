"""Module: V7.2 State Snapshot — Checkpoint 升级为完整状态快照.

Iteration feedback: the V6 checkpoint only saves ``graph + translations +
outputs``. True ``rollback()`` (WAL / event-sourcing style) needs a full
*state snapshot* covering every runtime component::

    RuntimeSnapshot
      ├── graphs        — every unified graph view (document/execution/...)
      ├── knowledge     — glossary / entity / concept / citation knowledge
      ├── cache         — prompt cache / context cache
      ├── memory        — DocumentMemory snapshot
      ├── workflow      — workflow / task states
      ├── telemetry     — metrics & counters
      ├── diagnostics   — issues / health
      ├── plugins       — plugin states
      ├── queue         — execution queue
      └── artifacts     — translations / outputs / metrics

``RuntimeSnapshot.capture()`` and ``restore_into()`` make rollback a real
operation instead of a partial restore; ``save()/load()`` give the
persistence layer a JSON format; ``diff()`` yields the SnapshotDiff used by
the incremental engine.

Usage::

    snap = RuntimeSnapshot.capture(session, label="v1")
    snap.save(path)
    other = RuntimeSnapshot.load(path)
    snap.restore_into(session)          # true rollback
    d = snap.diff(other)                # structural diff
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_COMPONENTS = (
    "graphs", "knowledge", "cache", "memory", "workflow", "telemetry",
    "diagnostics", "plugins", "queue", "translations", "outputs", "metrics",
)


def _serialize(obj: Any, depth: int = 0) -> Any:
    """JSON-safe conversion for snapshot storage."""
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if depth > 8:
        return repr(obj)[:200]
    if isinstance(obj, dict):
        return {str(k): _serialize(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_serialize(v, depth + 1) for v in obj]
    for attr in ("to_dict", "to_json"):
        method = getattr(obj, attr, None)
        if callable(method):
            try:
                return _serialize(method(), depth + 1)
            except Exception:
                pass
    if hasattr(obj, "value") and not callable(getattr(obj, "value")):
        return obj.value
    if hasattr(obj, "__dict__"):
        return _serialize(vars(obj), depth + 1)
    return str(obj)


@dataclass
class RuntimeSnapshot:
    """A complete, serializable snapshot of one document session."""

    label: str
    session_id: str = ""
    state: str = ""
    timestamp: float = 0.0
    snapshot_id: str = ""

    graphs: Dict[str, Any] = field(default_factory=dict)
    knowledge: Dict[str, Any] = field(default_factory=dict)
    cache: Dict[str, Any] = field(default_factory=dict)
    memory: Dict[str, Any] = field(default_factory=dict)
    workflow: Dict[str, Any] = field(default_factory=dict)
    telemetry: Dict[str, Any] = field(default_factory=dict)
    diagnostics: Dict[str, Any] = field(default_factory=dict)
    plugins: Dict[str, Any] = field(default_factory=dict)
    queue: List[Any] = field(default_factory=list)

    translations: Dict[str, str] = field(default_factory=dict)
    outputs: Dict[str, Any] = field(default_factory=dict)
    metrics: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = time.time()
        if not self.snapshot_id:
            self.snapshot_id = uuid.uuid4().hex[:12]

    # ── Capture / restore ─────────────────────────────────────────────

    @classmethod
    def capture(cls, session: Any, label: str = "snapshot",
                state: Optional[str] = None) -> "RuntimeSnapshot":
        """Capture a full state snapshot from a DocumentSession (or any
        object exposing the same component attributes)."""
        state = state or getattr(session, "state", None)
        state_value = state.value if hasattr(state, "value") else (
            state if isinstance(state, str) else "unknown")
        return cls(
            label=label,
            session_id=getattr(session, "session_id", ""),
            state=state_value,
            graphs=dict(getattr(session, "graphs", {})),
            knowledge=_serialize(getattr(session, "knowledge", {})),
            cache=_serialize(getattr(session, "cache", {})),
            memory=_serialize(getattr(session, "memory", {})),
            workflow=_serialize(getattr(session, "workflow", {})),
            telemetry=_serialize(getattr(session, "telemetry", {})),
            diagnostics=_serialize(getattr(session, "diagnostics", {})),
            plugins=_serialize(getattr(session, "plugins", {})),
            queue=_serialize(getattr(session, "queue", [])),
            translations=dict(getattr(session, "translations", {})),
            outputs=dict(getattr(session, "outputs", {})),
            metrics=dict(getattr(session, "metrics", {})),
        )

    def restore_into(self, session: Any) -> None:
        """Restore every captured component into a session — a real
        rollback() instead of a partial state restore."""
        session.graphs = {k: v for k, v in self.graphs.items()}
        session.knowledge = dict(self.knowledge)
        session.cache = dict(self.cache)
        session.memory = dict(self.memory)
        session.workflow = dict(self.workflow)
        session.telemetry = dict(self.telemetry)
        session.diagnostics = dict(self.diagnostics)
        session.plugins = dict(self.plugins)
        session.queue = list(self.queue)
        session.translations = dict(self.translations)
        session.outputs = dict(self.outputs)
        session.metrics = dict(self.metrics)


    # ── Serialization ─────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "session_id": self.session_id,
            "state": self.state,
            "timestamp": self.timestamp,
            "snapshot_id": self.snapshot_id,
            "translations": dict(self.translations),
            "outputs": _serialize(self.outputs),
            "metrics": _serialize(self.metrics),
            "knowledge": _serialize(self.knowledge),
            "cache": _serialize(self.cache),
            "memory": _serialize(self.memory),
            "workflow": _serialize(self.workflow),
            "telemetry": _serialize(self.telemetry),
            "diagnostics": _serialize(self.diagnostics),
            "plugins": _serialize(self.plugins),
            "queue": _serialize(self.queue),
            "graphs": _serialize_graphs(self.graphs),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RuntimeSnapshot":
        return cls(
            label=data.get("label", "snapshot"),
            session_id=data.get("session_id", ""),
            state=data.get("state", ""),
            timestamp=float(data.get("timestamp", time.time())),
            snapshot_id=data.get("snapshot_id", ""),
            graphs=dict(data.get("graphs", {})),
            knowledge=dict(data.get("knowledge", {})),
            cache=dict(data.get("cache", {})),
            memory=dict(data.get("memory", {})),
            workflow=dict(data.get("workflow", {})),
            telemetry=dict(data.get("telemetry", {})),
            diagnostics=dict(data.get("diagnostics", {})),
            plugins=dict(data.get("plugins", {})),
            queue=list(data.get("queue", [])),
            translations=dict(data.get("translations", {})),
            outputs=dict(data.get("outputs", {})),
            metrics=dict(data.get("metrics", {})),
        )

    def save(self, path: str) -> str:
        """Persist the snapshot as JSON. Returns the written path."""
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, ensure_ascii=False, indent=2)
        logger.info("Snapshot '%s' saved to %s", self.label, path)
        return path

    @classmethod
    def load(cls, path: str) -> "RuntimeSnapshot":
        with open(path, "r", encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))

    # ── Diff ──────────────────────────────────────────────────────────

    def diff(self, other: "RuntimeSnapshot") -> "SnapshotDiff":
        return SnapshotDiff.between(self, other)

    @property
    def components(self) -> List[str]:
        return [name for name in _COMPONENTS if getattr(self, name, None)]

    @property
    def component_count(self) -> int:
        return len(self.components)

    @property
    def size(self) -> int:
        return len(json.dumps(self.to_dict(), ensure_ascii=False))


def _serialize_graphs(graphs: Dict[str, Any]) -> Dict[str, Any]:
    """Serialize graph views; adapt() for base-graph-style objects."""
    from pdf2zh.v3.base_graph import adapt

    result: Dict[str, Any] = {}
    for kind, graph in graphs.items():
        try:
            if isinstance(graph, dict):
                result[kind] = graph
                continue
            if hasattr(graph, "to_dict"):
                result[kind] = graph.to_dict()
                continue
            result[kind] = adapt(graph).to_dict()
        except Exception as exc:  # pragma: no cover - defensive
            result[kind] = {"kind": str(kind), "error": str(exc)}
    return result


# ═══════════════════════════════════════════════════════════════════
# SnapshotDiff
# ═══════════════════════════════════════════════════════════════════


@dataclass
class SnapshotDiff:
    """Structural difference between two state snapshots, component-wise."""

    before: str = ""
    after: str = ""
    updated_components: List[str] = field(default_factory=list)
    added_components: List[str] = field(default_factory=list)
    removed_components: List[str] = field(default_factory=list)

    @classmethod
    def between(cls, before: RuntimeSnapshot,
                after: RuntimeSnapshot) -> "SnapshotDiff":
        d = cls(before=before.label, after=after.label)
        for name in _COMPONENTS:
            if getattr(before, name) != getattr(after, name):
                d.updated_components.append(name)
        return d

    @property
    def is_empty(self) -> bool:
        return not (self.updated_components or self.added_components
                    or self.removed_components)

    @property
    def changed(self) -> bool:
        return not self.is_empty

    def to_dict(self) -> dict:
        return {
            "before": self.before,
            "after": self.after,
            "updated_components": self.updated_components,
            "added_components": self.added_components,
            "removed_components": self.removed_components,
        }

    def summary(self) -> dict:
        return {
            "changed": self.changed,
            "updated": len(self.updated_components),
            "added": len(self.added_components),
            "removed": len(self.removed_components),
            "components": self.updated_components,
        }


__all__ = [
    "RuntimeSnapshot", "SnapshotDiff", "_serialize",
]
