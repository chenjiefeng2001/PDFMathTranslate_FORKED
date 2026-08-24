"""Module: V7.5 Cross-Session Knowledge Graph — 增量传播与术语一致性.

Iteration feedback (doc/v7_operator_runtime_report.md §六): state snapshots
(V7.2) are *per session*; when a second document is translated there is no
way to reuse the terminology / entity knowledge the first session already
learned. V7.5 adds a **shared, cross-session knowledge graph**:

- every completed session incrementally *propagates* its extracted knowledge
  (entities / concepts / citations / glossary) into the shared graph;
- a new session *pulls* the accumulated glossary back into its config, so the
  planner produces terminology-consistent translations across documents;
- the graph is mergeable and serializable (``save`` / ``load``), which is the
  persistence side of "动态术语库与翻译记忆" (roadmap stage five).

        Session A ──► KnowledgeGraph (shared) ◄── Session B
          entities / concepts / citations / glossary   │
                                                       └─► prepare_config()
                                                              │
                      Session C ── plan/translate ───────────────┘
                          (sees A+B terminology)

Usage::

    from pdf2zh.v3.knowledge_graph import KnowledgeGraph, KnowledgePropagator

    graph = KnowledgeGraph("shared")
    propagator = KnowledgePropagator(graph)
    report = propagator.propagate(ctx, session_id="s1")
    config = propagator.prepare_config(config)   # glossary pulled in
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Union

logger = logging.getLogger(__name__)


def _normalize(name: str) -> str:
    return str(name).strip().lower().replace(" ", "_").replace("-", "_")


def _now() -> float:
    return time.time()


# ── Records ──────────────────────────────────────────────────────────


@dataclass
class KnowledgeEntity:
    """A named entity accumulated across sessions."""

    name: str
    canonical_name: str = ""
    entity_type: str = "unknown"
    aliases: List[str] = field(default_factory=list)
    definitions: List[str] = field(default_factory=list)
    occurrence_count: int = 1
    sessions: List[str] = field(default_factory=list)
    first_seen: float = field(default_factory=_now)
    last_seen: float = field(default_factory=_now)


@dataclass
class GlossaryTerm:
    """A source → target terminology mapping shared across sessions."""

    source: str
    target: str
    confidence: float = 1.0
    sessions: List[str] = field(default_factory=list)


@dataclass
class ConceptRecord:
    """A concept (heading/subject) with its hierarchy."""

    name: str
    parent: Optional[str] = None
    children: List[str] = field(default_factory=list)
    sessions: List[str] = field(default_factory=list)


@dataclass
class CitationRecord:
    """A citation reference accumulated across sessions."""

    citation_key: str
    page: int = 0
    sessions: List[str] = field(default_factory=list)


@dataclass
class PropagationReport:
    """What one session contributed to the shared knowledge graph."""

    session_id: str = ""
    entities_added: int = 0
    entities_updated: int = 0
    glossary_added: int = 0
    glossary_updated: int = 0
    concepts_added: int = 0
    citations_added: int = 0
    total_entities: int = 0
    total_glossary: int = 0
    total_concepts: int = 0
    total_citations: int = 0

    def merge(self, other: "PropagationReport") -> "PropagationReport":
        """Fold ``other``'s counters into this report (used to combine the
        analysis report and the glossary report of one propagation)."""
        for name in (
            "entities_added",
            "entities_updated",
            "glossary_added",
            "glossary_updated",
            "concepts_added",
            "citations_added",
        ):
            setattr(self, name, getattr(self, name) + getattr(other, name))
        for name in (
            "total_entities",
            "total_glossary",
            "total_concepts",
            "total_citations",
        ):
            setattr(self, name, max(getattr(self, name), getattr(other, name)))
        return self

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


class KnowledgeGraph:
    """Cross-session shared knowledge: entities / glossary / concepts /
    citations with mergeable, incrementally-propagated updates."""

    def __init__(self, name: str = "shared") -> None:
        self.name = name
        self._entities: Dict[str, KnowledgeEntity] = {}
        self._glossary: Dict[str, GlossaryTerm] = {}
        self._concepts: Dict[str, ConceptRecord] = {}
        self._citations: Dict[str, CitationRecord] = {}
        self._propagations: List[dict] = []
        self._lock = threading.Lock()

    # ── Entity propagation ───────────────────────────────────────────

    def _upsert_entity(
        self, name: str, info: dict, session_id: str, report: PropagationReport
    ) -> None:
        key = _normalize(name)
        aliases = [str(a) for a in (info.get("aliases") or [])]
        definitions = [str(d) for d in (info.get("definitions") or [])]
        occurrences = max(1, int(info.get("occurrences", 1) or 1))
        canonical = str(info.get("canonical") or info.get("canonical_name") or name)
        entity_type = str(info.get("type") or info.get("entity_type") or "unknown")
        existing = self._entities.get(key)
        if existing is not None:
            existing.occurrence_count += occurrences
            existing.last_seen = _now()
            if not existing.canonical_name or (
                existing.canonical_name == name and canonical != name
            ):
                existing.canonical_name = canonical
            for a in aliases:
                if a and a not in existing.aliases:
                    existing.aliases.append(a)
            for d in definitions:
                if d and d not in existing.definitions:
                    existing.definitions.append(d)
            if session_id and session_id not in existing.sessions:
                existing.sessions.append(session_id)
            report.entities_updated += 1
        else:
            self._entities[key] = KnowledgeEntity(
                name=name,
                canonical_name=canonical,
                entity_type=entity_type,
                aliases=aliases,
                definitions=definitions,
                occurrence_count=occurrences,
                sessions=[session_id] if session_id else [],
                first_seen=_now(),
                last_seen=_now(),
            )
            report.entities_added += 1
        report.total_entities = len(self._entities)

    # ── Glossary propagation ─────────────────────────────────────────

    def _upsert_glossary(
        self,
        source: str,
        target: str,
        session_id: str,
        report: PropagationReport,
        confidence: float = 1.0,
    ) -> None:
        key = str(source).strip()
        if not key or not target:
            return
        existing = self._glossary.get(key)
        if existing is not None:
            existing.confidence = max(existing.confidence, confidence)
            if existing.target != target and confidence >= existing.confidence:
                existing.target = target
            if session_id and session_id not in existing.sessions:
                existing.sessions.append(session_id)
            report.glossary_updated += 1
        else:
            self._glossary[key] = GlossaryTerm(
                source=key,
                target=str(target),
                confidence=confidence,
                sessions=[session_id] if session_id else [],
            )
            report.glossary_added += 1
        report.total_glossary = len(self._glossary)

    # ── Concept / citation propagation ───────────────────────────────

    def _upsert_concept(
        self, name: str, info: dict, session_id: str, report: PropagationReport
    ) -> None:
        key = _normalize(name)
        parent = info.get("parent") if isinstance(info, dict) else None
        children = (
            [str(c) for c in (info.get("children") or [])]
            if isinstance(info, dict)
            else []
        )
        existing = self._concepts.get(key)
        if existing is not None:
            existing.parent = parent or existing.parent
            for c in children:
                if c not in existing.children:
                    existing.children.append(c)
            if session_id and session_id not in existing.sessions:
                existing.sessions.append(session_id)
        else:
            self._concepts[key] = ConceptRecord(
                name=name,
                parent=parent,
                children=children,
                sessions=[session_id] if session_id else [],
            )
            report.concepts_added += 1
        report.total_concepts = len(self._concepts)

    def _upsert_citation(
        self, citation_key: str, info: dict, session_id: str, report: PropagationReport
    ) -> None:
        key = str(citation_key).strip()
        if not key:
            return
        page = int(info.get("page", 0) or 0) if isinstance(info, dict) else 0
        existing = self._citations.get(key)
        if existing is not None:
            if page and not existing.page:
                existing.page = page
            if session_id and session_id not in existing.sessions:
                existing.sessions.append(session_id)
        else:
            self._citations[key] = CitationRecord(
                citation_key=key, page=page, sessions=[session_id] if session_id else []
            )
            report.citations_added += 1
        report.total_citations = len(self._citations)

    # ── Public merge APIs ────────────────────────────────────────────

    def merge_analysis(
        self, analysis: Dict[str, Any], session_id: str
    ) -> PropagationReport:
        """Incrementally merge the ``analysis`` dict produced by
        ``AnalyzeOperator`` (entity / concept / citation views) into the
        shared graph."""
        report = PropagationReport(session_id=session_id)
        analysis = analysis or {}

        entity_view = analysis.get("entity") or {}
        entity_map = (
            entity_view.get("entities", entity_view) or {}
            if isinstance(entity_view, dict)
            else {}
        )
        for name, info in (entity_map or {}).items():
            self._upsert_entity(name, info or {}, session_id, report)

        concept_view = analysis.get("concept") or {}
        concept_map = (
            concept_view.get("concepts", concept_view) or {}
            if isinstance(concept_view, dict)
            else {}
        )
        for name, info in (concept_map or {}).items():
            self._upsert_concept(name, info or {}, session_id, report)

        citation_view = analysis.get("citation") or {}
        citation_map = (
            citation_view.get("citations", citation_view) or {}
            if isinstance(citation_view, dict)
            else {}
        )
        for key, info in (citation_map or {}).items():
            self._upsert_citation(key, info or {}, session_id, report)

        self._propagations.append(
            {
                "session_id": session_id,
                "at": _now(),
                "analysis": report.to_dict(),
            }
        )
        return report

    def merge_glossary(
        self, glossary: Any, session_id: str, confidence: float = 1.0
    ) -> PropagationReport:
        """Merge a glossary source into the shared graph.

        Accepts a ``{source: target}`` dict, an iterable of ``(source,
        target)`` pairs or entry dicts, or a ``GlossaryManager``-like object
        exposing ``to_pairs()`` / ``get_all_entries()``.
        """
        report = PropagationReport(session_id=session_id)
        pairs: List[Tuple[str, str]] = []
        if isinstance(glossary, dict):
            pairs = [(str(s), str(t)) for s, t in glossary.items()]
        elif callable(getattr(glossary, "to_pairs", None)):
            pairs = [(str(s), str(t)) for s, t in glossary.to_pairs()]
        elif callable(getattr(glossary, "get_all_entries", None)):
            for e in glossary.get_all_entries():
                pairs.append(
                    (
                        str(getattr(e, "source_term", "")),
                        str(getattr(e, "target_term", "")),
                    )
                )
        elif isinstance(glossary, (list, tuple)):
            for item in glossary:
                if isinstance(item, dict):
                    pairs.append(
                        (str(item.get("source", "")), str(item.get("target", "")))
                    )
                else:
                    pairs.append((str(item[0]), str(item[1])))
        for source, target in pairs:
            self._upsert_glossary(
                source, target, session_id, report, confidence=confidence
            )
        return report

    def merge(self, other: "KnowledgeGraph") -> PropagationReport:
        """Fold another knowledge graph into this one."""
        report = PropagationReport(session_id="merge")
        for ent in other._entities.values():
            self._upsert_entity(
                ent.name,
                {
                    "canonical": ent.canonical_name,
                    "type": ent.entity_type,
                    "aliases": ent.aliases,
                    "definitions": ent.definitions,
                    "occurrences": ent.occurrence_count,
                },
                "|".join(ent.sessions) or "merge",
                report,
            )
        for term in other._glossary.values():
            self._upsert_glossary(
                term.source,
                term.target,
                "|".join(term.sessions) or "merge",
                report,
                confidence=term.confidence,
            )
        for c in other._concepts.values():
            self._upsert_concept(
                c.name,
                {"parent": c.parent, "children": c.children},
                "|".join(c.sessions) or "merge",
                report,
            )
        for cit in other._citations.values():
            self._upsert_citation(
                cit.citation_key,
                {"page": cit.page},
                "|".join(cit.sessions) or "merge",
                report,
            )
        return report

    def clear(self) -> None:
        with self._lock:
            self._entities.clear()
            self._glossary.clear()
            self._concepts.clear()
            self._citations.clear()
            self._propagations.clear()

    # ── Accessors ────────────────────────────────────────────────────

    @property
    def entities(self) -> List[KnowledgeEntity]:
        with self._lock:
            return list(self._entities.values())

    @property
    def glossary_terms(self) -> List[GlossaryTerm]:
        with self._lock:
            return list(self._glossary.values())

    @property
    def concepts(self) -> List[ConceptRecord]:
        with self._lock:
            return list(self._concepts.values())

    @property
    def citations(self) -> List[CitationRecord]:
        with self._lock:
            return list(self._citations.values())

    @property
    def propagation_history(self) -> List[dict]:
        with self._lock:
            return [dict(p) for p in self._propagations]

    def session_ids(self) -> List[str]:
        """Distinct sessions that have contributed knowledge."""
        sessions = set()
        for rec in (
            *self._entities.values(),
            *self._glossary.values(),
            *self._concepts.values(),
            *self._citations.values(),
        ):
            sessions.update(rec.sessions)
        return sorted(sessions)

    def get_entity(self, name: str) -> Optional[KnowledgeEntity]:
        return self._entities.get(_normalize(name))

    def get_glossary_term(self, source: str) -> Optional[GlossaryTerm]:
        return self._glossary.get(str(source).strip())

    def glossary_map(self) -> Dict[str, str]:
        """source → target for prompt / config injection."""
        return {t.source: t.target for t in self._glossary.values()}

    def glossary_prompt(self, max_terms: int = 50) -> str:
        """Human-readable terminology block for shared prompt context."""
        items = list(self.glossary_map().items())[:max_terms]
        if not items:
            return ""
        return "\n".join(f"- {s} → {t}" for s, t in items)

    def stats(self) -> dict:
        return {
            "name": self.name,
            "entities": len(self._entities),
            "glossary": len(self._glossary),
            "concepts": len(self._concepts),
            "citations": len(self._citations),
            "sessions": len(self.session_ids()),
            "propagations": len(self._propagations),
        }

    def __len__(self) -> int:
        return (
            len(self._entities)
            + len(self._glossary)
            + len(self._concepts)
            + len(self._citations)
        )

    def __bool__(self) -> bool:
        return len(self) > 0

    # ── Serialization / persistence ──────────────────────────────────

    def to_dict(self) -> dict:
        with self._lock:
            return {
                "name": self.name,
                "entities": [asdict(e) for e in self._entities.values()],
                "glossary": [asdict(g) for g in self._glossary.values()],
                "concepts": [asdict(c) for c in self._concepts.values()],
                "citations": [asdict(c) for c in self._citations.values()],
                "propagations": [dict(p) for p in self._propagations],
            }

    @classmethod
    def from_dict(cls, data: dict) -> "KnowledgeGraph":
        graph = cls(name=str(data.get("name") or "shared"))
        for ent in data.get("entities", []):
            graph._entities[_normalize(ent["name"])] = KnowledgeEntity(
                **{
                    k: v
                    for k, v in ent.items()
                    if k in KnowledgeEntity.__dataclass_fields__
                }
            )
        for term in data.get("glossary", []):
            graph._glossary[term["source"]] = GlossaryTerm(
                **{
                    k: v
                    for k, v in term.items()
                    if k in GlossaryTerm.__dataclass_fields__
                }
            )
        for c in data.get("concepts", []):
            graph._concepts[_normalize(c["name"])] = ConceptRecord(
                **{
                    k: v
                    for k, v in c.items()
                    if k in ConceptRecord.__dataclass_fields__
                }
            )
        for cit in data.get("citations", []):
            graph._citations[cit["citation_key"]] = CitationRecord(
                **{
                    k: v
                    for k, v in cit.items()
                    if k in CitationRecord.__dataclass_fields__
                }
            )
        graph._propagations = [dict(p) for p in data.get("propagations", [])]
        return graph

    def save(self, path: str) -> str:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, ensure_ascii=False, indent=2)
        return path

    @classmethod
    def load(cls, path: str, name: Optional[str] = None) -> "KnowledgeGraph":
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        graph = cls.from_dict(data)
        if name:
            graph.name = name
        return graph


class KnowledgePropagator:
    """Session ↔ shared-graph bridge.

    ``propagate()`` pushes one executed session's knowledge into the shared
    ``KnowledgeGraph``; ``prepare_config()`` pulls the accumulated glossary
    back into a (cloned) config so the next session translates with
    terminology-consistent terms.
    """

    def __init__(self, graph: KnowledgeGraph) -> None:
        self.graph = graph

    # ── Capture side ─────────────────────────────────────────────────

    def capture_analysis(self, ctx: Any) -> Dict[str, Any]:
        return dict((ctx.extra or {}).get("analysis") or {})

    def capture_glossary(self, ctx: Any) -> Dict[str, str]:
        """Extract glossary from ctx.config and the planner built by
        ``PlanOperator`` (config first, planner fills in the rest)."""
        merged: Dict[str, str] = {}
        config = getattr(ctx, "config", None)
        if config is not None:
            for src, tgt in (getattr(config, "glossary", {}) or {}).items():
                merged.setdefault(str(src), str(tgt))
        planner = (ctx.extra or {}).get("planner")
        glossary = getattr(planner, "glossary", None)
        to_pairs = getattr(glossary, "to_pairs", None)
        if callable(to_pairs):
            for src, tgt in to_pairs():
                merged.setdefault(str(src), str(tgt))
        return merged

    def propagate(self, ctx: Any, session_id: str) -> PropagationReport:
        """Incrementally push a session's knowledge into the shared graph."""
        report = self.graph.merge_analysis(self.capture_analysis(ctx), session_id)
        glossary = self.capture_glossary(ctx)
        if glossary:
            report.merge(self.graph.merge_glossary(glossary, session_id))
        return report

    # ── Pull side ────────────────────────────────────────────────────

    def prepare_config(self, config: Any) -> Any:
        """Return a config clone whose glossary includes the shared terms.

        Uses ``dataclasses.replace`` so the shared config object is never
        mutated — each session sees the same terminology snapshot at build
        time. The caller's explicit glossary entries win over accumulated
        shared terms (user intent > learned terminology).
        """
        if config is None or not self.graph:
            return config
        base = dict(getattr(config, "glossary", {}) or {})
        merged = dict(self.graph.glossary_map())
        merged.update(base)
        if merged == base and len(merged) == len(base):
            return config
        from dataclasses import replace

        try:
            return replace(config, glossary=merged)
        except TypeError:  # pragma: no cover - non-dataclass config
            return config


__all__ = [
    "KnowledgeEntity",
    "GlossaryTerm",
    "ConceptRecord",
    "CitationRecord",
    "PropagationReport",
    "KnowledgeGraph",
    "KnowledgePropagator",
]
