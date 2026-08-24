"""Module: V5 Tracer — distributed tracing for performance analysis.

Extends TelemetryCollector with span-based distributed tracing.
Supports nested spans, operation timing, and export to trace trees.

Usage::
    from pdf2zh.v3.tracing import Tracer

    tracer = Tracer()
    with tracer.span("translate"):
        with tracer.span("llm_call"):
            pass
    tree = tracer.export()
"""

from __future__ import annotations
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from pdf2zh.v3.runtime_kernel import TelemetryCollector

logger = logging.getLogger(__name__)


@dataclass
class TraceSpan:
    operation: str
    start_time: float = 0.0
    end_time: float = 0.0
    span_id: str = ""
    parent_id: Optional[str] = None
    attributes: Dict[str, Any] = field(default_factory=dict)
    children: List["TraceSpan"] = field(default_factory=list)

    def __post_init__(self):
        if not self.span_id:
            self.span_id = uuid.uuid4().hex[:12]
        if not self.start_time:
            self.start_time = time.time()

    @property
    def duration(self) -> float:
        if self.end_time > 0:
            return self.end_time - self.start_time
        return time.time() - self.start_time

    @property
    def is_completed(self) -> bool:
        return self.end_time > 0


class Tracer:
    """Distributed tracer with nested span support."""

    def __init__(self, telemetry: Optional[TelemetryCollector] = None) -> None:
        self._telemetry = telemetry
        self._spans: Dict[str, TraceSpan] = {}
        self._stack: List[str] = []
        self._root_spans: List[str] = []
        self._enabled: bool = True

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def telemetry(self):
        return self._telemetry

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled

    def start_span(
        self, operation: str, attributes: Optional[Dict[str, Any]] = None
    ) -> TraceSpan:
        if not self._enabled:
            span = TraceSpan(operation=operation, span_id="disabled")
            return span
        parent_id = self._stack[-1] if self._stack else None
        span = TraceSpan(
            operation=operation,
            parent_id=parent_id,
            attributes=attributes or {},
        )
        self._spans[span.span_id] = span
        if parent_id is None:
            self._root_spans.append(span.span_id)
        else:
            parent = self._spans.get(parent_id)
            if parent:
                parent.children.append(span)
        self._stack.append(span.span_id)
        return span

    def end_span(self, span_id: Optional[str] = None) -> None:
        if not self._enabled:
            return
        sid = span_id or (self._stack[-1] if self._stack else None)
        if sid is None or sid not in self._spans:
            return
        span = self._spans[sid]
        span.end_time = time.time()
        if self._stack and self._stack[-1] == sid:
            self._stack.pop()
        if self._telemetry:
            self._telemetry.record(span.operation, span.duration)

    def span(
        self, operation: str, attributes: Optional[Dict[str, Any]] = None
    ) -> "_SpanContext":
        return _SpanContext(self, operation, attributes)

    def get_span(self, span_id: str) -> Optional[TraceSpan]:
        return self._spans.get(span_id)

    def get_trace_tree(self) -> List[TraceSpan]:
        roots = []
        for rid in self._root_spans:
            span = self._spans.get(rid)
            if span:
                roots.append(span)
        return roots

    def export(self) -> List[dict]:
        def _serialize(span: TraceSpan) -> dict:
            return {
                "operation": span.operation,
                "span_id": span.span_id,
                "parent_id": span.parent_id,
                "duration_ms": round(span.duration * 1000, 2),
                "is_completed": span.is_completed,
                "attributes": dict(span.attributes),
                "children": [_serialize(c) for c in span.children],
            }

        return [_serialize(r) for r in self.get_trace_tree()]

    def clear(self) -> None:
        self._spans.clear()
        self._stack.clear()
        self._root_spans.clear()

    @property
    def active_span_count(self) -> int:
        return len(self._stack)

    @property
    def total_span_count(self) -> int:
        return len(self._spans)


class _SpanContext:
    """Context manager for tracing spans."""

    def __init__(
        self,
        tracer: Tracer,
        operation: str,
        attributes: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._tracer = tracer
        self._operation = operation
        self._attributes = attributes
        self._span_id: Optional[str] = None

    def __enter__(self) -> TraceSpan:
        span = self._tracer.start_span(self._operation, self._attributes)
        self._span_id = span.span_id
        return span

    def __exit__(self, *args: Any) -> None:
        if self._span_id:
            self._tracer.end_span(self._span_id)


__all__ = [
    "TraceSpan",
    "Tracer",
    "_SpanContext",
]
