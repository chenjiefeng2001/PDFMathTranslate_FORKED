"""Module: V7.4 Operator Result Cache — cache-aside 算子结果级缓存.

Iteration feedback (doc/v7_operator_runtime_report.md): the operator DAG
already provides *sub-graph* reuse through ``prune_from`` / incremental
execution, but every re-run still re-executes the operators in the affected
set. The next level of reuse is **result-level caching**: an operator whose
inputs have not changed since its last run should not be executed at all —
its previously produced outputs are simply restored (cache-aside pattern).

    run(ctx)            → operator executes, result stored  [cache miss]
    run(ctx')  ctx==ctx' → operator skipped, result applied  [cache hit]

Each builtin operator declares the *paths* of ``OperatorContext`` it reads
(inputs) and writes (outputs). The cache key is a content digest of the input
view plus the operator name/version, so any upstream change (a mutated block,
a new translation, a glossary update) changes the key and invalidates the
stale entry naturally — there is no explicit invalidation list to maintain.

Usage::

    from pdf2zh.v3.operator_cache import OperatorResultCache
    from pdf2zh.v3.operators import OperatorGraph, OperatorContext

    cache = OperatorResultCache(max_entries=128)
    ctx = graph.run(ctx, cache=cache)            # miss → execute
    ctx2 = graph.run(OperatorContext(document=...), cache=cache)  # hit → restore
    print(cache.stats())
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ── Path access helpers ──────────────────────────────────────────────

def _get_path(ctx: Any, path: str) -> Any:
    """Read a dotted path from an OperatorContext.

    ``"extra.manifest"`` → ``ctx.extra["manifest"]``;
    ``"document_graph"`` → ``ctx.document_graph``;
    ``"metrics.nodes"`` → ``ctx.metrics["nodes"]``.
    """
    parts = path.split(".")
    obj: Any = ctx
    for i, part in enumerate(parts):
        if i == 0:
            obj = getattr(obj, part, None)
        elif isinstance(obj, dict):
            obj = obj.get(part)
        else:
            obj = getattr(obj, part, None)
        if obj is None:
            return None
    return obj


def _set_path(ctx: Any, path: str, value: Any) -> None:
    """Write a dotted path into an OperatorContext (mirror of ``_get_path``)."""
    parts = path.split(".")
    if len(parts) == 1:
        setattr(ctx, parts[0], value)
        return
    obj: Any = ctx
    for part in parts[:-1]:
        obj = getattr(obj, part) if not isinstance(obj, dict) else obj[part]
    if isinstance(obj, dict):
        obj[parts[-1]] = value
    else:
        setattr(obj, parts[-1], value)


def _provider_signature(provider: Any) -> str:
    """Stable, compact identity of a translation provider.

    The raw provider object is not JSON-stable (module refs, caches, ...), so
    the cache key uses this signature instead — two providers with the same
    class + name + model + target_lang are treated as interchangeable.
    """
    if provider is None:
        return "<none>"
    parts = [f"{type(provider).__module__}.{type(provider).__qualname__}"]
    for attr in ("name", "model", "target_lang"):
        value = getattr(provider, attr, None)
        if value is not None and not callable(value):
            parts.append(f"{attr}={value}")
    return "|".join(parts)


def _copy_value(value: Any) -> Any:
    """Deepcopy cached values so a caller can never mutate a cache entry."""
    try:
        return copy.deepcopy(value)
    except Exception:  # pragma: no cover - defensive
        return value


# ── Cache specification ──────────────────────────────────────────────

@dataclass(frozen=True)
class OperatorCacheSpec:
    """Which ctx paths an operator reads (inputs) and writes (outputs)."""

    inputs: Tuple[str, ...]
    outputs: Tuple[str, ...]


# NOTE: "provider" is special-cased in the input view (see _provider_signature).
CACHE_SPECS: Dict[str, OperatorCacheSpec] = {
    "parse": OperatorCacheSpec(
        inputs=("document",),
        outputs=("document_graph", "graphs.document", "metrics.nodes")),
    "analyze": OperatorCacheSpec(
        inputs=("document_graph",),
        outputs=("extra.analysis", "metrics.entities")),
    "plan": OperatorCacheSpec(
        inputs=("config",),
        outputs=("extra.planner", "metrics.glossary_terms")),
    "translate": OperatorCacheSpec(
        inputs=("document_graph", "config", "translations",
                "extra.incremental_ids", "provider"),
        outputs=("translations", "metrics.translated",
                 "extra.session_summary",
                 # translate mutates the graph (translated_text on nodes), so
                 # the mutated graph is part of its observable output — caching
                 # it keeps review's input signature coherent across sessions.
                 "document_graph", "graphs.document")),
    "review": OperatorCacheSpec(
        inputs=("translations", "config", "document_graph"),
        outputs=("translations", "metrics.quality_score",
                 "metrics.review_errors", "extra.review")),
    "layout": OperatorCacheSpec(
        inputs=("document_graph", "config", "translations"),
        outputs=("extra.manifest", "metrics.layout_blocks")),
    "render": OperatorCacheSpec(
        inputs=("extra.manifest", "translations", "config"),
        outputs=("outputs", "metrics.formats")),
}


@dataclass
class OperatorCacheEntry:
    """One stored operator result."""

    key: str
    operator: str
    version: str
    outputs: Dict[str, Any]
    created_at: float = field(default_factory=time.time)
    hits: int = 0

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "operator": self.operator,
            "version": self.version,
            "hits": self.hits,
            "created_at": round(self.created_at, 3),
        }


def build_cache_key(op_name: str, op_version: str,
                    input_view: Dict[str, Any]) -> str:
    """Content-addressed cache key from an operator's input view."""
    from pdf2zh.v3.operators import _as_jsonable
    payload = json.dumps(_as_jsonable(input_view), sort_keys=True,
                         ensure_ascii=False, default=str)
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()
    return f"{op_name}:{op_version}:{digest}"


def input_view_of(ctx: Any, spec: OperatorCacheSpec) -> Dict[str, Any]:
    """Extract the JSON-safe input view an operator reads from ctx."""
    view: Dict[str, Any] = {}
    for path in spec.inputs:
        if path == "provider":
            view[path] = _provider_signature(getattr(ctx, "provider", None))
        else:
            view[path] = _get_path(ctx, path)
    return view


def output_view_of(ctx: Any, spec: OperatorCacheSpec) -> Dict[str, Any]:
    """Extract the output paths an operator wrote into ctx."""
    return {path: _get_path(ctx, path) for path in spec.outputs}


def apply_outputs(ctx: Any, outputs: Dict[str, Any]) -> None:
    """Restore a cached output view back into ctx (cache-aside hit path)."""
    for path, value in outputs.items():
        _set_path(ctx, path, _copy_value(value))



class OperatorResultCache:
    """In-memory LRU cache of operator results (cache-aside).

    Thread-safe via a lock; evicts the least-recently-used entry once the
    entry budget is exceeded. Values are deep-copied on both store and apply
    so the cache never shares mutable state with an executing session.
    """

    def __init__(self, max_entries: int = 256,
                 deepcopy_outputs: bool = True) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be >= 1")
        self.max_entries = max_entries
        self.deepcopy_outputs = deepcopy_outputs
        self._entries: "OrderedDict[str, OperatorCacheEntry]" = OrderedDict()
        self._hits = 0
        self._misses = 0
        self._skips = 0
        self._lock = threading.Lock()

    # ── Cache-aside core ─────────────────────────────────────────────

    def key_for(self, ctx: Any, op: Any) -> Optional[str]:
        """Content key for ``op`` given the current ctx input state.

        Returns ``None`` for operators without a declared spec — those are
        never cached (executed every run).
        """
        spec = CACHE_SPECS.get(getattr(op, "name", ""))
        if spec is None:
            with self._lock:
                self._skips += 1
            return None
        view = input_view_of(ctx, spec)
        return build_cache_key(getattr(op, "name", "?"),
                               getattr(op, "version", "?"), view)

    def get(self, key: Optional[str]) -> Optional[OperatorCacheEntry]:
        if key is None:
            return None
        with self._lock:
            entry = self._entries.pop(key, None)
            if entry is None:
                self._misses += 1
                return None
            self._entries[key] = entry  # LRU: move to back
            entry.hits += 1
            self._hits += 1
            return entry

    def put(self, key: str, ctx: Any, op: Any) -> OperatorCacheEntry:
        spec = CACHE_SPECS[getattr(op, "name", "")]
        outputs = output_view_of(ctx, spec)
        if self.deepcopy_outputs:
            outputs = _copy_value(outputs)
        entry = OperatorCacheEntry(
            key=key,
            operator=getattr(op, "name", "?"),
            version=getattr(op, "version", "?"),
            outputs=outputs,
        )
        with self._lock:
            self._entries[key] = entry
            self._entries.move_to_end(key)
            self._evict_locked()
        return entry

    def apply(self, entry: OperatorCacheEntry, ctx: Any) -> None:
        """Restore a cache hit into ctx without executing the operator."""
        apply_outputs(ctx, entry.outputs)

    # ── Administration ───────────────────────────────────────────────

    def _evict_locked(self) -> None:
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    def contains(self, key: str) -> bool:
        with self._lock:
            return key in self._entries

    def keys(self) -> List[str]:
        with self._lock:
            return list(self._entries.keys())

    def entries(self) -> List[OperatorCacheEntry]:
        with self._lock:
            return list(self._entries.values())

    def stats(self) -> dict:
        with self._lock:
            total = self._hits + self._misses
            return {
                "entries": len(self._entries),
                "max_entries": self.max_entries,
                "hits": self._hits,
                "misses": self._misses,
                "skips": self._skips,
                "hit_rate": round(self._hits / total, 4) if total else 0.0,
            }

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._hits = 0
            self._misses = 0
            self._skips = 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


def get_operator_cache_key(ctx: Any, op: Any) -> Optional[str]:
    """Convenience helper: compute the content key for ``op`` on ``ctx``."""
    return OperatorResultCache().key_for(ctx, op)


__all__ = [
    "OperatorCacheSpec", "OperatorCacheEntry", "OperatorResultCache",
    "CACHE_SPECS", "build_cache_key", "input_view_of", "output_view_of",
    "apply_outputs", "get_operator_cache_key",
]

