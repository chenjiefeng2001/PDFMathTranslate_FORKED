"""Semantic renderers — preserve original geometry, mutate only text."""

from pdf2zh.semantic.renderer.list import (
    ListRenderer,
    RenderCommand,
    build_page_list_plan,
)
from pdf2zh.semantic.renderer.toc import (
    TocRenderer,
    build_page_toc_plan,
)

__all__ = [
    "ListRenderer",
    "RenderCommand",
    "build_page_list_plan",
    "TocRenderer",
    "build_page_toc_plan",
]
