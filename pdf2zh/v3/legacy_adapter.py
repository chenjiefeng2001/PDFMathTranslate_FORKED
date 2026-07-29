"""Module: Legacy Adapter — bridge between V3 Runtime and Legacy TranslateConverter.

Each adapter wraps a legacy component and exposes a V3 Runtime-compatible interface.
"""

from __future__ import annotations
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from pdf2zh.v3.graph import DocumentGraph, DocumentNode, NodeType

logger = logging.getLogger(__name__)


@dataclass
class AdapterStats:
    nodes_processed: int = 0
    nodes_adapted: int = 0
    errors: int = 0
    adapter_name: str = ""

    def to_dict(self) -> dict:
        return {"adapter": self.adapter_name, "processed": self.nodes_processed,
                "adapted": self.nodes_adapted, "errors": self.errors}


class BaseAdapter:
    def __init__(self, name: str = "base"):
        self._stats = AdapterStats(adapter_name=name)

    @property
    def stats(self) -> AdapterStats:
        return self._stats

    def reset_stats(self) -> None:
        self._stats = AdapterStats(adapter_name=self._stats.adapter_name)


class LegacyTranslatorAdapter(BaseAdapter):
    """Wraps legacy translator engines for V3 Runtime compatibility."""

    def __init__(self):
        super().__init__(name="LegacyTranslatorAdapter")
        self._legacy_translator = None
        self._try_load_legacy()

    def _try_load_legacy(self) -> None:
        try:
            from pdf2zh.translator import Translator as LegacyTranslator
            self._legacy_translator = LegacyTranslator
        except ImportError:
            pass

    def translate(self, graph: DocumentGraph, source_lang: str = "auto",
                  target_lang: str = "zh", **kwargs) -> DocumentGraph:
        from pdf2zh.v3.translator import TranslationSession, Translator
        self._stats.nodes_processed = len(graph.nodes)
        session = TranslationSession(graph=graph, planner=kwargs.get("planner"))
        if self._legacy_translator and kwargs.get("use_legacy", False):
            self._translate_via_legacy(session, source_lang, target_lang)
        else:
            translator = Translator(session)
            translator.translate_all()
        self._stats.nodes_adapted = len(session.results)
        session.apply_results_to_graph()
        return graph

    def _translate_via_legacy(self, session, source_lang, target_lang):
        try:
            engine = self._legacy_translator(source_lang=source_lang, target_lang=target_lang)
            for node in session.graph.nodes:
                if node.text and not node.translated_text:
                    translated = engine.translate(node.text)
                    session.record_result(node.id, translated)
                    node.translated_text = translated
                    self._stats.nodes_adapted += 1
        except Exception as e:
            logger.error(f"Legacy translation failed: {e}")



class LegacyLayoutAdapter(BaseAdapter):
    """Wraps legacy layout modules for V3 VisualTree compatibility."""

    def __init__(self):
        super().__init__(name="LegacyLayoutAdapter")
        self._legacy_paragraph = None
        self._try_load_legacy()

    def _try_load_legacy(self) -> None:
        try:
            from pdf2zh.paragraph_style import ParagraphLayout as LP
            self._legacy_paragraph = LP
        except ImportError:
            pass

    def layout(self, graph):
        from pdf2zh.v3.layout import LayoutEngine
        if self._legacy_paragraph:
            return self._layout_via_legacy(graph)
        return LayoutEngine().layout(graph)

    def _layout_via_legacy(self, visual_tree):
        result = []
        for page in visual_tree.pages:
            for para in page.children:
                try:
                    pl = self._legacy_paragraph(para)
                    result.append(pl.layout())
                    self._stats.nodes_adapted += 1
                except Exception as e:
                    logger.warning(f"Legacy layout failed: {e}")
                    self._stats.errors += 1
        return result


class LegacyRendererAdapter(BaseAdapter):
    """Wraps legacy PDF renderer for V3 VisualTree compatibility."""

    def __init__(self):
        super().__init__(name="LegacyRendererAdapter")
        self._legacy_renderer = None
        self._try_load_legacy()

    def _try_load_legacy(self) -> None:
        try:
            from pdf2zh.overlay_renderer import overlay_renderer as LR
            self._legacy_renderer = LR
        except ImportError:
            pass

    def render(self, visual_tree, output_path: str, **kwargs) -> str:
        from pdf2zh.v3.renderer import PDFRenderer
        if self._legacy_renderer:
            return self._render_via_legacy(visual_tree, output_path, **kwargs)
        result = PDFRenderer().render(visual_tree)
        return result.decode('utf-8') if isinstance(result, bytes) else str(result)

    def _render_via_legacy(self, visual_tree, output_path: str, **kwargs) -> str:
        try:
            result = self._legacy_renderer(visual_tree, output_path, **kwargs)
            self._stats.nodes_adapted += 1
            return result
        except Exception as e:
            logger.error(f"Legacy render failed: {e}")
            self._stats.errors += 1
            return ""


class LegacyConverterBridge:
    """High-level bridge wrapping TranslateConverter pipeline for V3 Runtime."""

    def __init__(self, use_legacy_parser: bool = True):
        self._stats = {"calls": 0, "parser": 0, "translator": 0, "layout": 0, "renderer": 0}
        self._translator_adapter = LegacyTranslatorAdapter()
        self._layout_adapter = LegacyLayoutAdapter()
        self._renderer_adapter = LegacyRendererAdapter()
        self._use_legacy_parser = use_legacy_parser

    def _graph_to_visual_tree(self, graph):
        from pdf2zh.v3.visual_tree import VisualTree, Page, Paragraph, BoundingBox
        tree = VisualTree()
        page = Page(id="page_0", width=612, height=792, page_num=0)
        for node in graph.nodes:
            if hasattr(node, 'bbox') and node.bbox:
                bbox = node.bbox
                if hasattr(bbox, '__len__') and len(bbox) == 4:
                    bb = BoundingBox(bbox[0], bbox[1], bbox[2]-bbox[0], bbox[3]-bbox[1])
                else:
                    bb = BoundingBox(0, 0, 100, 20)
                para = Paragraph(id=node.id, bbox=bb)
                page.add_child(para)
        tree.add_page(page)
        return tree

    def convert(self, graph: DocumentGraph, **kwargs) -> DocumentGraph:
        self._stats["calls"] += 1
        self._stats["translator"] += 1
        graph = self._translator_adapter.translate(graph, **kwargs)
        self._stats["layout"] += 1
        from pdf2zh.v3.visual_tree import VisualTree
        visual_tree = self._graph_to_visual_tree(graph)
        self._layout_adapter.layout(graph)
        self._stats["renderer"] += 1
        output_path = kwargs.get("output_path", "output.pdf")
        self._renderer_adapter.render(visual_tree, output_path)
        return graph

    @property
    def stats(self) -> dict:
        return dict(self._stats)

    def get_adapter_stats(self):
        return {"translator": self._translator_adapter.stats.to_dict(),
                "layout": self._layout_adapter.stats.to_dict(),
                "renderer": self._renderer_adapter.stats.to_dict()}


__all__ = ["AdapterStats", "BaseAdapter", "LegacyTranslatorAdapter",
           "LegacyLayoutAdapter", "LegacyRendererAdapter", "LegacyConverterBridge"]

