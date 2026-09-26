"""PipelineTrace — 全链路可观测性。

追踪每页的完整 pipeline 执行。

用法：
    trace = PipelineTrace()

    with trace.span("parse", page=0) as span:
        result = parse_page(pdf, 0)
        span.set("pages", len(result.pages))

    print(trace.summary())
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Span:
    """追踪 span。"""

    name: str = ""
    page: int = -1
    start_ms: float = 0.0
    end_ms: float = 0.0
    duration_ms: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_valid(self) -> bool:
        return self.end_ms > self.start_ms

    def set(self, key: str, value: Any) -> None:
        self.metadata[key] = value


@dataclass
class PageTrace:
    """单页追踪。"""

    page_index: int = 0
    spans: List[Span] = field(default_factory=list)
    total_ms: float = 0.0

    def get_span(self, name: str) -> Optional[Span]:
        for s in self.spans:
            if s.name == name:
                return s
        return None

    def summary(self) -> str:
        lines = [f"Page {self.page_index} ({self.total_ms:.0f}ms):"]
        for s in self.spans:
            lines.append(f"  {s.name:<20} {s.duration_ms:>8.0f}ms")
        return "\n".join(lines)


@dataclass
class PipelineTrace:
    """全链路追踪。"""

    pages: List[PageTrace] = field(default_factory=list)
    total_ms: float = 0.0

    def start_page(self, page_index: int) -> PageTrace:
        """开始追踪一页。"""
        trace = PageTrace(page_index=page_index)
        self.pages.append(trace)
        return trace

    @contextmanager
    def span(
        self,
        name: str,
        page: int = -1,
        **metadata,
    ):
        """追踪一个 span。"""
        s = Span(name=name, page=page)
        s.start_ms = time.perf_counter() * 1000
        for k, v in metadata.items():
            s.set(k, v)
        try:
            yield s
        finally:
            s.end_ms = time.perf_counter() * 1000
            s.duration_ms = s.end_ms - s.start_ms

    def add_span(self, span: Span) -> None:
        """添加 span。"""
        # 找到或创建 page trace
        page_trace = None
        for p in self.pages:
            if p.page_index == span.page:
                page_trace = p
                break
        if page_trace is None:
            page_trace = self.start_page(span.page)
        page_trace.spans.append(span)

    def summary(self) -> str:
        lines = [
            "Pipeline Trace",
            "=" * 60,
            f"Total pages: {len(self.pages)}",
            f"Total time: {self.total_ms:.0f}ms",
        ]

        # 阶段汇总
        stage_totals: Dict[str, float] = {}
        for page_trace in self.pages:
            for span in page_trace.spans:
                stage_totals[span.name] = (
                    stage_totals.get(span.name, 0) + span.duration_ms
                )

        lines.append("")
        lines.append("Stage Totals:")
        for name, total in sorted(stage_totals.items()):
            avg = total / len(self.pages) if self.pages else 0
            lines.append(f"  {name:<20} {total:>10.0f}ms (avg {avg:.0f}ms)")

        # Hotspot pages
        slow_pages = sorted(self.pages, key=lambda p: p.total_ms, reverse=True)[:5]
        lines.append("")
        lines.append("Top 5 Slowest Pages:")
        for pt in slow_pages:
            lines.append(f"  Page {pt.page_index}: {pt.total_ms:.0f}ms")

        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_ms": self.total_ms,
            "pages": [
                {
                    "page_index": pt.page_index,
                    "total_ms": pt.total_ms,
                    "spans": [
                        {
                            "name": s.name,
                            "duration_ms": s.duration_ms,
                            "metadata": s.metadata,
                        }
                        for s in pt.spans
                    ],
                }
                for pt in self.pages
            ],
        }
