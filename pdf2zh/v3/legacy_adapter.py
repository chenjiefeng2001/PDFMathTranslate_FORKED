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
    """Wraps 24+ legacy translator engines for V3 Runtime compatibility.

    Bridges all translator engines from ``pdf2zh.translator`` (Google, DeepL,
    OpenAI, DeepSeek, Gemini, Grok, Ollama, etc.) into the V3
    ``DocumentGraph``-based pipeline.  When ``engine_name="mock"`` (default),
    falls back to the V3 ``Translator`` with ``MockLLMProvider``.

    Usage::

        adapter = LegacyTranslatorAdapter(engine_name="google")
        adapter.translate(graph, source_lang="en", target_lang="zh")

        # mock fallback (no real API call)
        adapter = LegacyTranslatorAdapter()
        adapter.translate(graph)
    """

    _ENGINES: Dict[str, Any] = {}

    @classmethod
    def _discover_engines(cls) -> Dict[str, Any]:
        """Lazily import and index all 24 legacy translator engines."""
        if cls._ENGINES:
            return cls._ENGINES
        try:
            from pdf2zh.translator import (
                GoogleTranslator, BingTranslator, DeepLTranslator,
                DeepLXTranslator, OllamaTranslator, XinferenceTranslator,
                AzureOpenAITranslator, OpenAITranslator, ZhipuTranslator,
                ModelScopeTranslator, SiliconTranslator, GeminiTranslator,
                AzureTranslator, TencentTranslator, DifyTranslator,
                AnythingLLMTranslator, ArgosTranslator, GrokTranslator,
                GroqTranslator, DeepseekTranslator, MiniMaxTranslator,
                OpenAIlikedTranslator, QwenMtTranslator, X302AITranslator,
            )
            cls._ENGINES = {
                "google": GoogleTranslator, "bing": BingTranslator,
                "deepl": DeepLTranslator, "deeplx": DeepLXTranslator,
                "ollama": OllamaTranslator, "xinference": XinferenceTranslator,
                "azure-openai": AzureOpenAITranslator,
                "openai": OpenAITranslator, "zhipu": ZhipuTranslator,
                "modelscope": ModelScopeTranslator,
                "silicon": SiliconTranslator, "gemini": GeminiTranslator,
                "azure": AzureTranslator, "tencent": TencentTranslator,
                "dify": DifyTranslator, "anythingllm": AnythingLLMTranslator,
                "argos": ArgosTranslator, "grok": GrokTranslator,
                "groq": GroqTranslator, "deepseek": DeepseekTranslator,
                "minimax": MiniMaxTranslator,
                "openailiked": OpenAIlikedTranslator,
                "qwen-mt": QwenMtTranslator, "x302ai": X302AITranslator,
            }
        except ImportError:
            pass
        return cls._ENGINES

    def __init__(self, engine_name: str = "mock"):
        super().__init__(name=f"LegacyTranslatorAdapter({engine_name})")
        self._engine_name = engine_name
        self._engine_cls = None
        if engine_name != "mock":
            engines = self._discover_engines()
            if engine_name not in engines:
                avail = list(engines.keys()) if engines else ["(none)"]
                raise ValueError(
                    f"Unknown engine {engine_name!r}. Available: {avail}"
                )
            self._engine_cls = engines[engine_name]

    def translate(self, graph: DocumentGraph, source_lang: str = "auto",
                  target_lang: str = "zh", **kwargs) -> DocumentGraph:
        """Translate all text nodes, delegating to the selected engine."""
        from pdf2zh.v3.translator import (
            TranslationSession, Translator as V3Translator,
        )
        from pdf2zh.v3.planner import PlannerConfig, TranslationPlanner

        self._stats.nodes_processed = len(graph.nodes)
        planner = kwargs.get("planner")
        if planner is None:
            planner = TranslationPlanner(PlannerConfig(
                source_lang=source_lang, target_lang=target_lang,
            ))
        memory = kwargs.get("memory")
        session = TranslationSession(
            graph=graph, planner=planner, memory=memory,
        )

        if self._engine_cls is None:
            translator = V3Translator(session)
            translator.translate_all()
        else:
            self._translate_via_engine(
                session, source_lang, target_lang, **kwargs,
            )

        self._stats.nodes_adapted = len(session.results)
        session.apply_results_to_graph()
        return graph

    def _translate_via_engine(
        self, session, source_lang, target_lang, **kwargs
    ):
        engine_cls = self._engine_cls
        model = kwargs.get("model")
        envs = kwargs.get("envs", {})
        prompt = kwargs.get("prompt")
        ignore_cache = kwargs.get("ignore_cache", False)

        try:
            engine = engine_cls(
                lang_in=source_lang, lang_out=target_lang,
                model=model, envs=envs, prompt=prompt,
                ignore_cache=ignore_cache,
            )
        except Exception as e:
            logger.error(
                "Failed to instantiate legacy engine %s: %s",
                self._engine_name, e,
            )
            self._stats.errors += 1
            return

        plans = kwargs.get("plans")
        if plans is None:
            plans = session.planner.plan_all(session.graph)

        for nid in plans:
            node = session.graph.get_node(nid)
            if node is None or not node.text or node.translated_text:
                continue
            try:
                translated = engine.translate(node.text)
                session.record_result(nid, translated)
                node.translated_text = translated
                self._stats.nodes_adapted += 1
            except Exception as e:
                logger.error(
                    "Legacy engine %s failed on node %s: %s",
                    self._engine_name, nid, e,
                )
                self._stats.errors += 1
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


class LegacyCompatAdapter:
    """Thin compatibility shim wrapping RuntimeFacade for TranslateConverter.

    Phase 4, Step 4.2: Allows TranslateConverter to delegate to V4
    RuntimeFacade while maintaining backward compatibility with
    legacy callers.

    Usage:
        adapter = LegacyCompatAdapter()
        output = adapter.convert(pdf_path, ...)
    """

    def __init__(self, use_feature_flags: bool = True):
        from pdf2zh.v3.feature_flags import FeatureFlags, get_feature_flags
        self._flags = get_feature_flags() if use_feature_flags else FeatureFlags()
        self._facade = None
        self._stats = {"calls": 0, "load": 0, "translate": 0,
                       "layout": 0, "render": 0}

    def _ensure_facade(self, config: Optional[dict] = None):
        if self._facade is None:
            from pdf2zh.v3.runtime import RuntimeFacade
            self._facade = RuntimeFacade(
                config=config or {},
                feature_flags=self._flags,
            )
        return self._facade

    def load(self, path: str, config: Optional[dict] = None):
        """Load a PDF into V4 runtime."""
        facade = self._ensure_facade(config)
        facade.load(path)
        self._stats["calls"] += 1
        self._stats["load"] += 1
        return facade.graph

    def translate_graph(self, graph, config: Optional[dict] = None):
        """Translate a DocumentGraph using V4 pipeline."""
        facade = self._ensure_facade(config)
        facade.graph = graph
        facade.analyze()
        facade.plan()
        facade.translate()
        self._stats["translate"] += 1
        return facade.graph

    def layout_and_render(self, graph, output_path: str = "",
                           fmt: str = "pdf",
                           config: Optional[dict] = None):
        """Layout and render a translated graph."""
        facade = self._ensure_facade(config)
        facade.graph = graph
        facade.layout()
        self._stats["layout"] += 1
        result = facade.render(fmt=fmt)
        self._stats["render"] += 1

        if output_path and result:
            import pathlib
            pathlib.Path(output_path).write_bytes(result)

        return result

    def run_pipeline(self, path: str, fmt: str = "pdf",
                     config: Optional[dict] = None) -> bytes:
        """Run the full V4 pipeline and return rendered output."""
        facade = self._ensure_facade(config)
        result = facade.pipeline(path, fmt=fmt)
        self._stats["calls"] += 1
        return result

    @property
    def stats(self) -> dict:
        return dict(self._stats)

    @property
    def facade(self):
        return self._facade

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


class V4PipelineRunner:
    """V4 Pipeline Runner — Unified entry point for the full V4 pipeline.

    Flow: load(path) -> analyze() -> plan() -> translate() -> layout() -> render() -> evaluate()
    """

    def __init__(self, config: dict = None):
        from pdf2zh.v3.runtime import RuntimeFacade
        self._config = config or {}
        self._facade = RuntimeFacade(config=self._config)
        self._timings: dict = {}
        self._current_path: str = None

    @property
    def facade(self):
        return self._facade

    @property
    def timings(self) -> dict:
        return dict(self._timings)

    def run(self, path: str, output_format: str = "pdf") -> bytes:
        import time
        self._current_path = path
        self._timings = {}
        t0 = time.time(); self._facade.load(path); self._timings["load"] = time.time() - t0
        t0 = time.time(); self._facade.analyze(); self._timings["analyze"] = time.time() - t0
        t0 = time.time(); self._facade.plan(); self._timings["plan"] = time.time() - t0
        t0 = time.time(); self._facade.translate(); self._timings["translate"] = time.time() - t0
        t0 = time.time(); self._facade.layout(); self._timings["layout"] = time.time() - t0
        t0 = time.time(); result = self._facade.render(fmt=output_format); self._timings["render"] = time.time() - t0
        t0 = time.time(); self._facade.evaluate(); self._timings["evaluate"] = time.time() - t0
        return result

    def summary(self) -> dict:
        t = self._facade.summary() if hasattr(self._facade, 'summary') else {}
        t["timings"] = self._timings
        return t


class TranslateConverterStrangler:
    """Strangler adapter: wraps V4PipelineRunner for legacy TranslateConverter API."""

    def __init__(self, engine: str = "mock", lang_in: str = "", lang_out: str = "", **kwargs):
        config = {"engine": engine, "lang_in": lang_in, "lang_out": lang_out, **kwargs}
        from pdf2zh.v3.runtime import RuntimeFacade
        self._runner = V4PipelineRunner(config=config)
        self._engine = engine
        self._stats = {"calls": 0, "pages": 0}

    @property
    def runner(self):
        return self._runner

    def convert(self, path: str, output_path: str = None) -> bytes:
        self._stats["calls"] += 1
        fmt = "pdf"
        if output_path and output_path.endswith(".md"): fmt = "md"
        elif output_path and output_path.endswith(".html"): fmt = "html"
        result = self._runner.run(path, output_format=fmt)
        if output_path and result:
            with open(output_path, "wb") as f: f.write(result)
        return result




__all__ = ["AdapterStats", "BaseAdapter", "LegacyTranslatorAdapter",
           "LegacyLayoutAdapter", "LegacyRendererAdapter", "LegacyConverterBridge",
           "V4PipelineRunner", "TranslateConverterStrangler"]