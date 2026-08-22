"""Module: Translation Runtime — V4 Epic A.
Provides the core translation pipeline that replaces the legacy Translator.

Upgrades for Phase 2 (70% target):
  - LLMProvider abstraction (OpenAI/Google/DeepSeek/mock)
  - PostProcessor (translation refinement, consistency checks)
  - Streaming support in TranslationSession
  - Batch translation with retry/fallback and statistics
"""
from __future__ import annotations
import hashlib, json, logging, time, uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple, Generator
from pdf2zh.v3.graph import DocumentGraph, DocumentNode, NodeType
from pdf2zh.v3.memory import DocumentMemory
from pdf2zh.v3.planner import TranslationPlan, TranslationPlanner, PlannerConfig
logger = logging.getLogger(__name__)

DEFAULT_MODEL_MAP = {
    NodeType.PARAGRAPH: "gpt-4o",
    NodeType.HEADING: "gpt-4o-mini",
    NodeType.CAPTION: "gpt-4o-mini",
    NodeType.ABSTRACT: "gpt-4o",
    NodeType.FORMULA: "gpt-4o-mini",
    NodeType.REFERENCE: "gpt-4o-mini",
    NodeType.CODE: "gpt-4o-mini",
    NodeType.FOOTNOTE: "gpt-4o-mini",
    NodeType.TABLE: "gpt-4o",
}
DEFAULT_TEMPERATURE_MAP = {
    NodeType.FORMULA: 0.0, NodeType.CODE: 0.0, NodeType.REFERENCE: 0.0,
    NodeType.HEADING: 0.1, NodeType.ABSTRACT: 0.1,
    NodeType.CAPTION: 0.2, NodeType.PARAGRAPH: 0.3,
}


# -- LLMProvider (Phase 2) --


@dataclass
class LLMResponse:
    text: str
    model: str
    provider: str = "mock"
    latency_ms: float = 0.0
    token_count: int = 0
    finish_reason: str = "stop"


class LLMProvider(ABC):
    @abstractmethod
    def complete(self, messages, **kwargs):
        pass
    def stream(self, messages, **kwargs):
        yield self.complete(messages, **kwargs)


class MockLLMProvider(LLMProvider):
    def __init__(self, delay_ms=0.0):
        self._delay_ms = delay_ms
    def complete(self, messages, **kwargs):
        if self._delay_ms > 0:
            time.sleep(self._delay_ms / 1000.0)
        last = messages[-1]["content"] if messages else ""
        if "Text to translate:" in last:
            parts = last.split("Text to translate:")
            text = parts[-1].strip().rstrip("`") if len(parts) > 1 else last[:200]
        else:
            text = last[:200]
        return LLMResponse(
            text="[%s] %s" % (kwargs.get("model", "mock"), text),
            model=kwargs.get("model", "mock"),
            provider="mock", finish_reason="stop",
        )

@dataclass

class OpenAIProvider(LLMProvider):
    def __init__(self, api_key=None, model="gpt-4o"):
        self._api_key = api_key
        self._default_model = model
        self._client = None
    def _lazy_init(self):
        if self._client is None:
            import openai
            self._client = openai.OpenAI(api_key=self._api_key)
    def complete(self, messages, **kwargs):
        self._lazy_init()
        start = time.time()
        model = kwargs.get("model", self._default_model)
        resp = self._client.chat.completions.create(
            model=model, messages=messages,
            temperature=kwargs.get("temperature", 0.3),
            max_tokens=kwargs.get("max_tokens", 4096),
        )
        choice = resp.choices[0]
        elapsed = (time.time() - start) * 1000
        return LLMResponse(text=choice.message.content or "", model=model,
            provider="openai", latency_ms=round(elapsed, 1),
            token_count=resp.usage.total_tokens if resp.usage else 0,
            finish_reason=choice.finish_reason or "stop")


class OpenCodeProvider(LLMProvider):
    """LLMProvider backed by the opencode CLI / `opencode serve` HTTP API.

    Delegates to legacy OpenCodeTranslator for transport (subprocess JSONL
    parsing + serve-mode session lifecycle), so both modes stay in sync.
    """

    def __init__(self, model=None, envs=None):
        self._default_model = model
        self._envs = envs
        self._translator = None

    def _lazy_init(self):
        if self._translator is None:
            from pdf2zh.translator import OpenCodeTranslator

            self._translator = OpenCodeTranslator(
                "auto", "auto", self._default_model,
                envs=self._envs, ignore_cache=True,
            )

    def complete(self, messages, **kwargs):
        self._lazy_init()
        start = time.time()
        text = self._translator.complete_raw(list(messages))
        elapsed = (time.time() - start) * 1000
        return LLMResponse(text=text,
            model=kwargs.get("model", self._default_model or "opencode"),
            provider="opencode", latency_ms=round(elapsed, 1),
            finish_reason="stop")


@dataclass
class PostProcessResult:
    text: str
    issues: list = None
    def __post_init__(self):
        if self.issues is None:
            self.issues = []


class PostProcessor:
    def __init__(self, memory=None):
        self._memory = memory
    def process(self, text, node=None):
        issues = []
        result = text.replace("\r\n", "\n").strip()
        result = result.replace(" ,", ",").replace(" .", ".")
        result = result.replace("( ", "(").replace(" )", ")")
        if self._memory is not None and node is not None:
            for entry in self._memory.get_all_glossary():
                if entry.source.lower() in node.text.lower():
                    if entry.target.lower() not in result.lower():
                        issues.append("term_mismatch:" + entry.source)
        return PostProcessResult(text=result, issues=issues)
    def process_batch(self, pairs):
        return [self.process(t, n) for t, n in pairs]


@dataclass
class TranslationStats:
    total_nodes: int = 0
    translated: int = 0
    cached: int = 0
    failed: int = 0
    total_latency_ms: float = 0.0
    total_tokens: int = 0
    def merge(self, other):
        self.total_nodes += other.total_nodes
        self.translated += other.translated
        self.cached += other.cached
        self.failed += other.failed
        self.total_latency_ms += other.total_latency_ms
        self.total_tokens += other.total_tokens
    def to_dict(self):
        return {
            "total_nodes": self.total_nodes,
            "translated": self.translated,
            "cached": self.cached,
            "failed": self.failed,
            "total_latency_ms": round(self.total_latency_ms, 1),
            "total_tokens": self.total_tokens,
            "avg_latency_ms": round(self.total_latency_ms / max(self.translated, 1), 1),
        }


@dataclass
class ModelRoute:
    model: str
    temperature: float
    max_tokens: int = 4096

class ModelRouter:
    def __init__(self, model_map=None, temperature_map=None, default_model="gpt-4o-mini"):
        self._model_map = dict(model_map or DEFAULT_MODEL_MAP)
        self._temperature_map = dict(temperature_map or DEFAULT_TEMPERATURE_MAP)
        self._default_model = default_model
    def route(self, node):
        model = self._model_map.get(node.node_type, self._default_model)
        temp = self._temperature_map.get(node.node_type, 0.3)
        mt = 8192 if node.node_type in (NodeType.PARAGRAPH, NodeType.ABSTRACT, NodeType.TABLE) else 2048
        return ModelRoute(model=model, temperature=temp, max_tokens=mt)
    def register_route(self, node_type, model, temperature=0.3):
        self._model_map[node_type] = model
        self._temperature_map[node_type] = temperature
    @property
    def model_count(self):
        return len(set(self._model_map.values()))
    def get_routes(self):
        routes = {}
        for nt, m in self._model_map.items():
            routes.setdefault(m, []).append(nt.value)
        return routes
@dataclass
class ComposedPrompt:
    messages: List[Dict[str, str]]
    model: str
    temperature: float
    max_tokens: int
    plan: TranslationPlan
    node_id: str

class PromptComposer:
    def __init__(self, planner, memory=None, router=None):
        self._planner = planner
        self._memory = memory
        self._router = router or ModelRouter()
    def compose(self, graph, node_id) -> ComposedPrompt:
        node = graph.get_node(node_id)
        if node is None:
            raise ValueError(f"Node {node_id!r} not found")
        plan = self._planner.plan(graph, node_id)
        route = self._router.route(node)
        plan_pairs = list(plan.glossary or [])
        pairs = []
        for g in plan_pairs:
            if isinstance(g, tuple):
                pairs.append((g[0], g[1]))
            else:
                pairs.append((g.source, g.target))
        if self._memory is not None:
            for g in self._memory.get_all_glossary():
                if g.source not in {ge[0] for ge in pairs}:
                    pairs.append((g.source, g.target))
        sys_msg = "You are a professional document translator.\nGlossary:\n"
        sys_msg += "\n".join(f"- {s} -> {t}" for s, t in pairs[:20]) if pairs else "(none)"
        msgs = [{"role": "system", "content": sys_msg}, {"role": "user", "content": plan.prompt}]
        return ComposedPrompt(messages=msgs, model=route.model, temperature=route.temperature,
                              max_tokens=route.max_tokens, plan=plan, node_id=node_id)

@dataclass
class CacheEntry:
    key: str; source_text: str; translated_text: str
    source_lang: str; target_lang: str; model: str
    timestamp: float; hit_count: int = 1

class TranslationCache:
    def __init__(self, max_size=10000):
        self._max_size = max_size; self._cache = {}
        self._access_order = []; self._stats = {"hits": 0, "misses": 0}
    def _make_key(self, src, sl, tl, m):
        return hashlib.sha256(f"{src}|{sl}|{tl}|{m}".encode()).hexdigest()[:32]
    def get(self, source_text, source_lang, target_lang, model):
        key = self._make_key(source_text, source_lang, target_lang, model)
        entry = self._cache.get(key)
        if entry is None:
            self._stats["misses"] += 1; return None
        entry.hit_count += 1; self._stats["hits"] += 1
        if key in self._access_order: self._access_order.remove(key)
        self._access_order.append(key)
        return entry.translated_text
    def put(self, source_text, translated_text, source_lang, target_lang, model):
        key = self._make_key(source_text, source_lang, target_lang, model)
        self._cache[key] = CacheEntry(key=key, source_text=source_text,
            translated_text=translated_text, source_lang=source_lang,
            target_lang=target_lang, model=model, timestamp=time.time())
        self._access_order.append(key)
        if len(self._cache) > self._max_size:
            self._cache.pop(self._access_order.pop(0), None)
        return key
    def clear(self):
        self._cache.clear(); self._access_order.clear()
        self._stats = {"hits": 0, "misses": 0}
    @property
    def size(self): return len(self._cache)
    @property
    def stats(self): return dict(self._stats)
    def contains(self, source_text, source_lang, target_lang, model):
        return self._make_key(source_text, source_lang, target_lang, model) in self._cache
class TranslationSession:
    def __init__(self, graph, memory=None, planner=None, cache=None, session_id=None,
                 provider=None, post_processor=None):
        self.session_id = session_id or uuid.uuid4().hex[:12]
        self.graph = graph
        self.memory = memory or DocumentMemory()
        self.planner = planner or TranslationPlanner(PlannerConfig())
        self.cache = cache or TranslationCache()
        from pdf2zh.v3.translator import MockLLMProvider, PostProcessor
        self._provider = provider or MockLLMProvider()
        self._post_processor = post_processor or PostProcessor(self.memory)
        self._stats = TranslationStats()
        self._results = {}; self._errors = {}
        self._started_at = None; self._finished_at = None
        self._status = "created"
        self._on_translate = None

    @property
    def provider(self):
        return self._provider

    @provider.setter
    def provider(self, value):
        self._provider = value

    @property
    def post_processor(self):
        return self._post_processor

    @post_processor.setter
    def post_processor(self, value):
        self._post_processor = value
    @property
    def status(self): return self._status
    @property
    def results(self): return dict(self._results)
    @property
    def errors(self): return dict(self._errors)
    @property
    def elapsed_seconds(self):
        if self._started_at is None: return None
        return (self._finished_at or time.time()) - self._started_at
    def start(self): self._started_at = time.time(); self._status = "running"
    def finish(self): self._finished_at = time.time(); self._status = "completed"
    def fail(self, error): self._finished_at = time.time(); self._status = "failed"; self._errors["_session"] = error
    def record_result(self, node_id, text): self._results[node_id] = text
    def record_error(self, node_id, error): self._errors[node_id] = error
    def get_result(self, node_id): return self._results.get(node_id)
    def has_result(self, node_id): return node_id in self._results
    def has_errors(self): return len(self._errors) > 0
    def on_translate(self, callback):
        self._on_translate = callback

    @property
    def stats(self):
        return self._stats

    def record_translated(self, node_id, latency_ms=0, tokens=0):
        self._stats.translated += 1
        self._stats.total_latency_ms += latency_ms
        self._stats.total_tokens += tokens

    def summary(self):
        return {"session_id": self.session_id, "status": self._status,
                "nodes_total": len(self.graph.nodes), "nodes_translated": len(self._results),
                "errors": len(self._errors), "elapsed": round(self.elapsed_seconds or 0, 2),
                "stats": self._stats.to_dict() if self._stats else {}}

    def apply_results_to_graph(self):
        for nid, txt in self._results.items():
            n = self.graph.get_node(nid)
            if n is not None: n.translated_text = txt

class Translator:
    def __init__(self, session, composer=None, llm_handler=None):
        self.session = session
        self.composer = composer or PromptComposer(session.planner, session.memory)
        self._llm_handler = llm_handler or self._session_provider_handler

    def _session_provider_handler(self, messages, **kw):
        resp = self.session.provider.complete(messages, **kw)
        return resp.text
    def translate_node(self, node_id, force=False):
        node = self.session.graph.get_node(node_id)
        if node is None: raise ValueError(f"Node {node_id!r} not found")
        pl = self.session.planner
        if not force and self.session.cache.contains(node.text, pl.config.source_lang, pl.config.target_lang, "default"):
            cached = self.session.cache.get(node.text, pl.config.source_lang, pl.config.target_lang, "default")
            if cached is not None: self.session.record_result(node_id, cached); return cached
        composed = self.composer.compose(self.session.graph, node_id)
        try:
            result = self._llm_handler(composed.messages, model=composed.model, temperature=composed.temperature, max_tokens=composed.max_tokens)
        except Exception as e:
            self.session.record_error(node_id, str(e)); return None
        self.session.cache.put(node.text, result, pl.config.source_lang, pl.config.target_lang, composed.model)
        self.session.record_result(node_id, result)
        return result
    def translate_all(self, force=False):
        self.session.start()
        all_plans = list(self.session.planner.plan_all(self.session.graph))
        self.session._stats = TranslationStats(total_nodes=len(all_plans))
        for nid in all_plans:
            start_t = time.time()
            result = self.translate_node(nid, force=force)
            latency = (time.time() - start_t) * 1000
            if result is not None:
                node = self.session.graph.get_node(nid)
                pp_result = self.session.post_processor.process(result, node)
                self.session.record_result(nid, pp_result.text)
                self.session.record_translated(nid, latency, len(pp_result.text) // 4)
                if self.session._on_translate:
                    self.session._on_translate(nid, pp_result.text)
            else:
                self.session._stats.failed += 1
        self.session.finish()
        self.session.apply_results_to_graph()
        return self.session.results
    def translate_batch(self, node_ids):
        self.session.start()
        self.session._stats = TranslationStats(total_nodes=len(node_ids))
        for nid in node_ids:
            start_t = time.time()
            result = self.translate_node(nid)
            latency = (time.time() - start_t) * 1000
            if result is not None:
                node = self.session.graph.get_node(nid)
                pp_result = self.session.post_processor.process(result, node)
                self.session.record_result(nid, pp_result.text)
                self.session.record_translated(nid, latency, len(pp_result.text) // 4)
            else:
                self.session._stats.failed += 1
        self.session.finish()
        return {nid: self.session.get_result(nid) for nid in node_ids}

__all__ = ["ModelRoute", "ModelRouter", "ComposedPrompt", "PromptComposer",
           "CacheEntry", "TranslationCache", "TranslationSession", "Translator",
           "LLMResponse", "LLMProvider", "MockLLMProvider", "OpenAIProvider",
           "PostProcessResult", "PostProcessor", "TranslationStats"]
