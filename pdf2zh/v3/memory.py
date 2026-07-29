"""Module: Document Memory — V4.3 Knowledge Runtime.

Cross-page, cross-document memory for entities, terminology, and concepts.
"""
from __future__ import annotations
import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

logger = logging.getLogger(__name__)


@dataclass
class EntityEntry:
    """A named entity with metadata."""
    canonical_name: str
    aliases: Set[str] = field(default_factory=set)
    definition: str = ""
    source_lang: str = "en"
    target_lang: str = "zh-cn"
    translation: str = ""
    confidence: float = 1.0

    def add_alias(self, alias: str) -> None:
        self.aliases.add(alias.lower())


@dataclass
class GlossaryEntry:
    """A source->target term mapping with metadata."""
    source: str
    target: str
    context: str = ""
    confidence: float = 1.0
    is_case_sensitive: bool = False


@dataclass
class AbbreviationEntry:
    """An abbreviation with its expanded form."""
    short: str
    long: str
    source_lang: str = "en"


@dataclass
class DocumentMemorySnapshot:
    """Serializable snapshot of memory state."""
    entities: List[dict]
    glossary: List[dict]
    abbreviations: List[dict]
    topics: List[str]
    language_style: str = ""


class DocumentMemory:
    """Cross-page, cross-document memory for entities and terminology.
    Maintains Entity / Glossary / Abbreviation registries.
    All lookups case-insensitive by default.
    """

    def __init__(self) -> None:
        self._entities: Dict[str, EntityEntry] = {}
        self._alias_index: Dict[str, str] = {}
        self._glossary: Dict[str, GlossaryEntry] = {}
        self._abbreviations: Dict[str, AbbreviationEntry] = {}
        self._topics: List[str] = []
        self._language_style: str = ""

    # ── Entity Registry ──────────────────────────────────────────

    def remember_entity(
        self, canonical_name: str, *aliases: str,
        definition: str = "", translation: str = "",
        source_lang: str = "en", target_lang: str = "zh-cn",
        confidence: float = 1.0,
    ) -> None:
        key = canonical_name.lower()
        if key in self._entities:
            entry = self._entities[key]
            entry.definition = definition or entry.definition
            entry.translation = translation or entry.translation
        else:
            entry = EntityEntry(
                canonical_name=canonical_name, definition=definition,
                source_lang=source_lang, target_lang=target_lang,
                translation=translation, confidence=confidence,
            )
            self._entities[key] = entry
        for alias in aliases:
            entry.add_alias(alias)
            self._alias_index[alias.lower()] = key

    def get_entity(self, name: str) -> Optional[EntityEntry]:
        key = name.lower()
        if key in self._entities:
            return self._entities[key]
        canonical = self._alias_index.get(key)
        return self._entities.get(canonical) if canonical else None

    def has_entity(self, name: str) -> bool:
        return self.get_entity(name) is not None

    def get_all_entities(self) -> List[EntityEntry]:
        return list(self._entities.values())

    def entity_count(self) -> int:
        return len(self._entities)

    # ── Glossary Registry ────────────────────────────────────────

    def remember_glossary(
        self, source: str, target: str,
        context: str = "", confidence: float = 1.0,
        is_case_sensitive: bool = False,
    ) -> None:
        key = source if is_case_sensitive else source.lower()
        self._glossary[key] = GlossaryEntry(
            source=source, target=target, context=context,
            confidence=confidence, is_case_sensitive=is_case_sensitive,
        )

    def lookup_glossary(self, term: str) -> Optional[GlossaryEntry]:
        if term in self._glossary:
            return self._glossary[term]
        key = term.lower()
        if key in self._glossary:
            return self._glossary[key]
        candidates = []
        for k, entry in self._glossary.items():
            search_key = k if entry.is_case_sensitive else k.lower()
            if search_key in term.lower():
                candidates.append((len(search_key), entry))
        if candidates:
            candidates.sort(key=lambda x: -x[0])
            return candidates[0][1]
        return None

    def get_all_glossary(self) -> List[GlossaryEntry]:
        return list(self._glossary.values())

    def glossary_count(self) -> int:
        return len(self._glossary)

    def get_glossary_pairs(self) -> Dict[str, str]:
        return {e.source: e.target for e in self._glossary.values()}

    # ── Abbreviation Registry ────────────────────────────────────

    def remember_abbreviation(self, short: str, long: str, source_lang: str = "en") -> None:
        self._abbreviations[short.lower()] = AbbreviationEntry(short=short, long=long, source_lang=source_lang)

    def expand_abbreviation(self, short: str) -> Optional[str]:
        entry = self._abbreviations.get(short.lower())
        return entry.long if entry else None

    def get_all_abbreviations(self) -> List[AbbreviationEntry]:
        return list(self._abbreviations.values())

    def abbreviation_count(self) -> int:
        return len(self._abbreviations)

    # ── Topics & Style ───────────────────────────────────────────

    def set_topics(self, topics: List[str]) -> None:
        self._topics = topics

    def add_topic(self, topic: str) -> None:
        if topic not in self._topics:
            self._topics.append(topic)

    @property
    def topics(self) -> List[str]:
        return list(self._topics)

    def set_language_style(self, style: str) -> None:
        self._language_style = style

    @property
    def language_style(self) -> str:
        return self._language_style

    # ── Snapshot ─────────────────────────────────────────────────

    def take_snapshot(self) -> DocumentMemorySnapshot:
        return DocumentMemorySnapshot(
            entities=[{"canonical_name": e.canonical_name, "aliases": list(e.aliases),
                        "definition": e.definition, "translation": e.translation,
                        "confidence": e.confidence} for e in self._entities.values()],
            glossary=[{"source": g.source, "target": g.target,
                       "context": g.context, "confidence": g.confidence} for g in self._glossary.values()],
            abbreviations=[{"short": a.short, "long": a.long} for a in self._abbreviations.values()],
            topics=list(self._topics), language_style=self._language_style,
        )

    def restore_snapshot(self, snapshot: DocumentMemorySnapshot) -> None:
        self._entities.clear()
        self._alias_index.clear()
        self._glossary.clear()
        self._abbreviations.clear()
        self._topics = list(snapshot.topics)
        self._language_style = snapshot.language_style
        for ed in snapshot.entities:
            self.remember_entity(ed["canonical_name"], *ed.get("aliases", []),
                                 definition=ed.get("definition", ""),
                                 translation=ed.get("translation", ""),
                                 confidence=ed.get("confidence", 1.0))
        for gd in snapshot.glossary:
            self.remember_glossary(gd["source"], gd["target"],
                                   context=gd.get("context", ""),
                                   confidence=gd.get("confidence", 1.0))
        for ad in snapshot.abbreviations:
            self.remember_abbreviation(ad["short"], ad["long"])

    # ── Bulk import ──────────────────────────────────────────────

    def learn_from_graph(self, graph) -> None:
        for node in graph.nodes:
            text = node.text
            if not text:
                continue
            for match in re.finditer(r'\b([A-Z]{2,5})\b', text):
                abbr = match.group(1)
                if not self.expand_abbreviation(abbr):
                    self.remember_abbreviation(abbr, abbr)

    def clear(self) -> None:
        self._entities.clear()
        self._alias_index.clear()
        self._glossary.clear()
        self._abbreviations.clear()
        self._topics.clear()
        self._language_style = ""


__all__ = [
    "DocumentMemory", "DocumentMemorySnapshot",
    "EntityEntry", "GlossaryEntry", "AbbreviationEntry",
]