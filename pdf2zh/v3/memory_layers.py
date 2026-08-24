"""Module: V6.0 Memory Layers — Style Memory, Reasoning Memory, Memory Hub.

The report (chapter 6) upgrades Memory from a "glossary database" to a
FOUR-LAYER memory system:

    Layer 1  Document Memory   — canonical term mappings   (v3/memory.py)
    Layer 2  Entity Memory     — numbered entities         (v3/document_intelligence.py EntityGraph)
    Layer 3  Style Memory      — tone / tense / style      (this module)
    Layer 4  Reasoning Memory  — domain / method / topics  (this module)

The layers form an inheritance chain: Reasoning decides "what this paper is
about" -> Style decides "which tone to use" -> Document/Entity decide "which
terms are pinned to a fixed translation". All downstream prompts directly
inherit these layers instead of being built from scratch every call.

This module provides the two missing layers plus a MemoryHub aggregator that
combines DocumentMemory (term layer) with Style + Reasoning layers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Style Memory ────────────────────────────────────────────────────────


@dataclass
class StyleEntry:
    """A single structured style rule (e.g. Academic Tone)."""

    key: str
    value: str
    source: str = "manual"  # manual | detected | inherited
    confidence: float = 1.0

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "value": self.value,
            "source": self.source,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "StyleEntry":
        return cls(
            key=data["key"],
            value=data.get("value", ""),
            source=data.get("source", "manual"),
            confidence=data.get("confidence", 1.0),
        )


class StyleMemory:
    """Structured style rules: tone, voice, tense, formality, reference style.

    Unlike DocumentMemory.language_style (a single string), StyleMemory keeps
    structured, queryable rules that the Planner injects into translation
    prompts as explicit constraints.
    """

    def __init__(self) -> None:
        self._entries: Dict[str, StyleEntry] = {}

    def set_rule(
        self, key: str, value: str, source: str = "manual", confidence: float = 1.0
    ) -> None:
        self._entries[key] = StyleEntry(
            key=key,
            value=value,
            source=source,
            confidence=confidence,
        )

    def get(self, key: str) -> Optional[str]:
        entry = self._entries.get(key)
        return entry.value if entry else None

    def get_entry(self, key: str) -> Optional[StyleEntry]:
        return self._entries.get(key)

    def has(self, key: str) -> bool:
        return key in self._entries

    def all_rules(self) -> Dict[str, str]:
        return {k: v.value for k, v in self._entries.items()}

    @property
    def entries(self) -> List[StyleEntry]:
        return list(self._entries.values())

    def merge(self, other: "StyleMemory", overwrite: bool = False) -> None:
        for key, entry in other._entries.items():
            if overwrite or key not in self._entries:
                self._entries[key] = entry

    def apply_defaults(self) -> None:
        """Preload sensible academic-document defaults (skippable)."""
        if not self.has("tone"):
            self.set_rule("tone", "academic", source="default")
        if not self.has("voice"):
            self.set_rule("voice", "passive", source="default")
        if not self.has("formality"):
            self.set_rule("formality", "formal", source="default")

    def to_dict(self) -> dict:
        return {"entries": [e.to_dict() for e in self._entries.values()]}

    @classmethod
    def from_dict(cls, data: dict) -> "StyleMemory":
        mem = cls()
        for ed in data.get("entries", []):
            entry = StyleEntry.from_dict(ed)
            mem._entries[entry.key] = entry
        return mem


# ── Reasoning Memory ────────────────────────────────────────────────────


@dataclass
class ReasoningEntry:
    """A domain/method/topic note that shapes professional context."""

    domain: str
    detail: str = ""
    confidence: float = 1.0
    source: str = "manual"

    def to_dict(self) -> dict:
        return {
            "domain": self.domain,
            "detail": self.detail,
            "confidence": self.confidence,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ReasoningEntry":
        return cls(
            domain=data["domain"],
            detail=data.get("detail", ""),
            confidence=data.get("confidence", 1.0),
            source=data.get("source", "manual"),
        )


class ReasoningMemory:
    """Domain / method / topic inheritance chain.

    Records "this document is about Diffusion + Machine Learning + Optimization"
    so ContextBuilder and PromptManager inherit the professional domain instead
    of reconstructing it per call.
    """

    def __init__(self) -> None:
        self._domains: Dict[str, ReasoningEntry] = {}
        self._topics: List[str] = []

    def record_domain(
        self,
        domain: str,
        detail: str = "",
        confidence: float = 1.0,
        source: str = "manual",
    ) -> None:
        existing = self._domains.get(domain)
        if existing is None or confidence >= existing.confidence:
            self._domains[domain] = ReasoningEntry(
                domain=domain,
                detail=detail,
                confidence=confidence,
                source=source,
            )

    def add_topic(self, topic: str) -> None:
        if topic and topic not in self._topics:
            self._topics.append(topic)

    def add_topics(self, topics: List[str]) -> None:
        for t in topics:
            self.add_topic(t)

    def has_domain(self, domain: str) -> bool:
        return domain in self._domains

    def get(self, domain: str) -> Optional[ReasoningEntry]:
        return self._domains.get(domain)

    @property
    def domains(self) -> List[str]:
        return list(self._domains.keys())

    @property
    def topics(self) -> List[str]:
        return list(self._topics)

    def primary_domain(self) -> Optional[str]:
        if not self._domains:
            return None
        best = max(self._domains.values(), key=lambda e: e.confidence)
        return best.domain

    def summary(self) -> str:
        """Human readable domain summary injected into prompts."""
        parts = []
        primary = self.primary_domain()
        if primary:
            parts.append(primary)
        if self._topics:
            parts.extend(self._topics[:5])
        return "; ".join(parts) if parts else ""

    def merge(self, other: "ReasoningMemory", overwrite: bool = False) -> None:
        for domain, entry in other._domains.items():
            if overwrite or domain not in self._domains:
                self._domains[domain] = entry
        for t in other._topics:
            self.add_topic(t)

    def to_dict(self) -> dict:
        return {
            "domains": [e.to_dict() for e in self._domains.values()],
            "topics": list(self._topics),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ReasoningMemory":
        mem = cls()
        for ed in data.get("domains", []):
            entry = ReasoningEntry.from_dict(ed)
            mem._domains[entry.domain] = entry
        mem._topics = list(data.get("topics", []))
        return mem


# ── Memory Hub ──────────────────────────────────────────────────────────


class MemoryHub:
    """Aggregates the four memory layers behind one query interface.

    Layers:
        document_memory : DocumentMemory        (terms / glossary / abbreviations)
        style_memory    : StyleMemory           (tone / voice / formality)
        reasoning_memory: ReasoningMemory       (domain / method / topics)
        entity_graph    : optional EntityGraph  (numbered entities)
    """

    def __init__(
        self,
        document_memory=None,
        style_memory: Optional[StyleMemory] = None,
        reasoning_memory: Optional[ReasoningMemory] = None,
        entity_graph=None,
    ) -> None:
        self.document_memory = document_memory
        self.style_memory = style_memory or StyleMemory()
        self.reasoning_memory = reasoning_memory or ReasoningMemory()
        self.entity_graph = entity_graph

    # ── Injection payload ─────────────────────────────────────────

    def glossary_pairs(self) -> Dict[str, str]:
        if self.document_memory is None:
            return {}
        getter = getattr(self.document_memory, "get_glossary_pairs", None)
        if getter is not None:
            return getter()
        return {}

    def entity_pairs(self) -> Dict[str, str]:
        if self.entity_graph is None:
            return {}
        # EntityGraph: nodes are EntityNode with .canonical_name / .aliases
        entities = {}
        nodes = getattr(self.entity_graph, "nodes", []) or []
        for node in nodes:
            name = getattr(node, "canonical_name", None) or getattr(node, "name", None)
            if name:
                entities[name] = name
        return entities

    def style_rules(self) -> Dict[str, str]:
        return self.style_memory.all_rules()

    def domain_summary(self) -> str:
        return self.reasoning_memory.summary()

    def build_prompt_context(self) -> dict:
        """Everything the Planner/PromptManager needs for one call."""
        return {
            "glossary": self.glossary_pairs(),
            "entities": self.entity_pairs(),
            "style": self.style_rules(),
            "domain": self.domain_summary(),
        }

    # ── Learning from graph ───────────────────────────────────────

    def learn_from_graph(self, graph) -> None:
        """Seed Style/Reasoning layers from graph content (keywords)."""
        text = " ".join(
            getattr(n, "text", "") or "" for n in getattr(graph, "nodes", []) or []
        )
        lower = text.lower()
        # Simple domain keyword heuristics
        domain_keywords = {
            "machine learning": "machine learning",
            "deep learning": "deep learning",
            "diffusion": "diffusion",
            "optimization": "optimization",
            "nlp": "nlp",
            "computer vision": "computer vision",
            "statistics": "statistics",
        }
        for keyword, domain in domain_keywords.items():
            if keyword in lower:
                self.reasoning_memory.record_domain(
                    domain,
                    detail="detected from graph",
                    confidence=0.6,
                    source="graph",
                )
        if self.document_memory is not None:
            learn = getattr(self.document_memory, "learn_from_graph", None)
            if learn is not None:
                learn(graph)

    # ── Snapshot / serialization ──────────────────────────────────

    def snapshot(self) -> dict:
        return {
            "style": self.style_memory.to_dict(),
            "reasoning": self.reasoning_memory.to_dict(),
        }

    def restore(self, data: dict) -> None:
        if "style" in data:
            self.style_memory = StyleMemory.from_dict(data["style"])
        if "reasoning" in data:
            self.reasoning_memory = ReasoningMemory.from_dict(data["reasoning"])


__all__ = [
    "StyleEntry",
    "StyleMemory",
    "ReasoningEntry",
    "ReasoningMemory",
    "MemoryHub",
]
