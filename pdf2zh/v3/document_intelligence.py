"""Module: V6.2 Document Intelligence — Entity, Concept, Citation Graphs.

Document intelligence with:
- Entity graph (entities, aliases, definitions, abbreviations)
- Concept graph (concept hierarchy, relationships)
- Citation graph (references, citations, cross-references)
- Cross-page knowledge fusion

Builds on DocumentMemory to provide structured knowledge representation
that drives translation planning, prompt composition, and quality evaluation.

Usage:
    from pdf2zh.v3.document_intelligence import (
        EntityGraph, ConceptGraph, CitationGraph,
        KnowledgeFuser, DocumentIntelligence,
    )
    di = DocumentIntelligence(graph, memory)
    di.build()
    di.fuse()
    print(di.entity_graph.entities)
"""

from __future__ import annotations
import logging, re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple
from pdf2zh.v3.graph import DocumentGraph, DocumentNode, NodeType, Edge, EdgeType
from pdf2zh.v3.memory import DocumentMemory, EntityEntry, AbbreviationEntry

logger = logging.getLogger(__name__)


# ── Entity Graph ─────────────────────────────────────────────


@dataclass
class EntityNode:
    """A named entity in the document."""

    id: str
    name: str
    canonical_name: str = ""
    entity_type: str = "unknown"  # model, method, dataset, metric, etc.
    aliases: List[str] = field(default_factory=list)
    definitions: List[str] = field(default_factory=list)
    first_occurrence_page: int = 0
    occurrence_count: int = 1
    contexts: List[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


@dataclass
class EntityRelation:
    source_id: str
    target_id: str
    relation_type: str  # is_a, part_of, abbreviation_of, synonym_of, defined_by
    weight: float = 1.0


class EntityGraph:
    """Knowledge graph of entities found in the document.

    Extracts entities from text and builds relations between them
    (e.g., "LLM" -> abbreviation_of -> "Large Language Model").
    """

    def __init__(self) -> None:
        self._entities: Dict[str, EntityNode] = {}
        self._relations: List[EntityRelation] = []

    def add_entity(
        self,
        name: str,
        entity_type: str = "unknown",
        canonical_name: str = "",
        aliases: Optional[List[str]] = None,
        definition: str = "",
        page_num: int = 0,
    ) -> EntityNode:
        eid = self._make_id(name)
        if eid in self._entities:
            existing = self._entities[eid]
            existing.occurrence_count += 1
            if definition and definition not in existing.definitions:
                existing.definitions.append(definition)
            if page_num > 0 and (
                existing.first_occurrence_page == 0
                or page_num < existing.first_occurrence_page
            ):
                existing.first_occurrence_page = page_num
            if aliases:
                for a in aliases:
                    if a not in existing.aliases:
                        existing.aliases.append(a)
            return existing
        can = canonical_name or name
        node = EntityNode(
            id=eid,
            name=name,
            canonical_name=can,
            entity_type=entity_type,
            aliases=aliases or [],
            definitions=[definition] if definition else [],
            first_occurrence_page=page_num,
        )
        self._entities[eid] = node
        return node

    def get_entity(self, name: str) -> Optional[EntityNode]:
        eid = self._make_id(name)
        return self._entities.get(eid)

    def add_relation(
        self,
        source_name: str,
        target_name: str,
        relation_type: str,
        weight: float = 1.0,
    ) -> None:
        source = self.get_entity(source_name) or self.add_entity(source_name)
        target = self.get_entity(target_name) or self.add_entity(target_name)
        self._relations.append(
            EntityRelation(
                source_id=source.id,
                target_id=target.id,
                relation_type=relation_type,
                weight=weight,
            )
        )

    def find_related(self, name: str, relation_type: str = "") -> List[EntityNode]:
        eid = self._make_id(name)
        results = []
        for r in self._relations:
            if r.source_id == eid and (
                not relation_type or r.relation_type == relation_type
            ):
                target = self._entities.get(r.target_id)
                if target:
                    results.append(target)
            if r.target_id == eid and (
                not relation_type or r.relation_type == relation_type
            ):
                source = self._entities.get(r.source_id)
                if source:
                    results.append(source)
        return results

    def get_canonical(self, name: str) -> str:
        entity = self.get_entity(name)
        if entity:
            return entity.canonical_name
        return name

    def resolve_abbreviation(self, abbr: str) -> Optional[str]:
        entity = self.get_entity(abbr)
        if entity:
            for r in self._relations:
                if r.source_id == entity.id and r.relation_type == "abbreviation_of":
                    target = self._entities.get(r.target_id)
                    if target:
                        return target.canonical_name
        return None

    def extract_from_text(self, text: str, page_num: int = 0) -> List[EntityNode]:
        """Simple regex-based entity extraction."""
        found = []
        # Acronym pattern: ALL CAPS (2-8 chars)
        for match in re.finditer(r"\b([A-Z]{2,8})\b", text):
            abbr = match.group(1)
            entity = self.add_entity(
                abbr, entity_type="abbreviation", page_num=page_num
            )
            if entity not in found:
                found.append(entity)
        # Numbered entity pattern: e.g., "Transformer", "BERT", "GPT-4"
        for match in re.finditer(r"\b([A-Z][a-z]+(?:[-][A-Za-z0-9]+)?)\b", text):
            word = match.group(1)
            if len(word) >= 3 and word.lower() not in (
                "the",
                "this",
                "that",
                "with",
                "from",
                "have",
            ):
                entity = self.add_entity(word, entity_type="concept", page_num=page_num)
                if entity not in found:
                    found.append(entity)
        return found

    def build_from_graph(self, graph: DocumentGraph) -> None:
        """Populate entity graph from a DocumentGraph."""
        for node in graph.nodes:
            if node.text and len(node.text) > 3:
                self.extract_from_text(node.text, node.page_num)
        # Try to find abbreviation-definition pairs
        texts = [n.text for n in graph.nodes if n.text]
        for text in texts:
            # Pattern: "Long Form (LF)" or "LF (Long Form)"
            for match in re.finditer(
                r"([A-Z][a-zA-Z\s]{2,40})\s*\(([A-Z]{2,8})\)", text
            ):
                full = match.group(1).strip()
                abbr = match.group(2)
                self.add_entity(abbr, entity_type="abbreviation", canonical_name=abbr)
                self.add_entity(full, entity_type="concept", canonical_name=full)
                self.add_relation(abbr, full, "abbreviation_of")
            for match in re.finditer(r"([A-Z]{2,8})\s*\(([A-Za-z\s]{3,40})\)", text):
                abbr = match.group(1)
                full = match.group(2).strip()
                self.add_entity(abbr, entity_type="abbreviation", canonical_name=abbr)
                self.add_entity(full, entity_type="concept", canonical_name=full)
                self.add_relation(abbr, full, "abbreviation_of")

    @property
    def entities(self) -> List[EntityNode]:
        return list(self._entities.values())

    @property
    def entity_count(self) -> int:
        return len(self._entities)

    @property
    def relations(self) -> List[EntityRelation]:
        return list(self._relations)

    def to_dict(self) -> dict:
        return {
            "entities": {
                e.name: {
                    "type": e.entity_type,
                    "canonical": e.canonical_name,
                    "aliases": e.aliases,
                    "occurrences": e.occurrence_count,
                }
                for e in self._entities.values()
            },
            "relations": [
                {
                    "source": (
                        self._entities.get(r.source_id).name
                        if self._entities.get(r.source_id)
                        else r.source_id
                    ),
                    "target": (
                        self._entities.get(r.target_id).name
                        if self._entities.get(r.target_id)
                        else r.target_id
                    ),
                    "type": r.relation_type,
                }
                for r in self._relations
            ],
        }

    @staticmethod
    def _make_id(name: str) -> str:
        return name.lower().replace(" ", "_").replace("-", "_")


# ── Concept Graph ────────────────────────────────────────────


@dataclass
class ConceptNode:
    id: str
    name: str
    description: str = ""
    parent_id: str = ""
    children: List[str] = field(default_factory=list)
    related_pages: List[int] = field(default_factory=list)
    confidence: float = 1.0


class ConceptGraph:
    """Hierarchical concept graph extracted from document structure.

    Maps document sections/headings into a concept hierarchy.
    """

    def __init__(self) -> None:
        self._concepts: Dict[str, ConceptNode] = {}

    def add_concept(
        self,
        name: str,
        description: str = "",
        parent: Optional[str] = None,
        page_num: int = 0,
    ) -> ConceptNode:
        cid = self._make_id(name)
        if cid not in self._concepts:
            node = ConceptNode(
                id=cid,
                name=name,
                description=description,
                parent_id=parent and self._make_id(parent) or "",
                related_pages=[page_num] if page_num > 0 else [],
            )
            self._concepts[cid] = node
            if node.parent_id and node.parent_id in self._concepts:
                if node.id not in self._concepts[node.parent_id].children:
                    self._concepts[node.parent_id].children.append(node.id)
            return node
        existing = self._concepts[cid]
        if description and not existing.description:
            existing.description = description
        if page_num > 0 and page_num not in existing.related_pages:
            existing.related_pages.append(page_num)
        return existing

    def get_concept(self, name: str) -> Optional[ConceptNode]:
        cid = self._make_id(name)
        return self._concepts.get(cid)

    def get_children(self, name: str) -> List[ConceptNode]:
        cid = self._make_id(name)
        if cid not in self._concepts:
            return []
        return [
            self._concepts[c]
            for c in self._concepts[cid].children
            if c in self._concepts
        ]

    def get_parent(self, name: str) -> Optional[ConceptNode]:
        cid = self._make_id(name)
        if cid not in self._concepts:
            return None
        parent_id = self._concepts[cid].parent_id
        if parent_id and parent_id in self._concepts:
            return self._concepts[parent_id]
        return None

    def build_from_graph(self, graph: DocumentGraph) -> None:
        """Build concept hierarchy from section headings."""
        headings = sorted(
            [n for n in graph.nodes if n.node_type == NodeType.HEADING],
            key=lambda n: (n.page_num, n.y0),
        )
        stack: List[str] = []
        for h in headings:
            level = h.metadata.get("heading_level", 1)
            # Pop stack to correct level
            while stack and len(stack) >= level:
                stack.pop()
            parent = stack[-1] if stack else None
            concept = self.add_concept(
                h.text.strip(), page_num=h.page_num, parent=parent
            )
            stack.append(concept.id)

    @property
    def concepts(self) -> List[ConceptNode]:
        return list(self._concepts.values())

    @property
    def concept_count(self) -> int:
        return len(self._concepts)

    def to_dict(self) -> dict:
        return {
            "concepts": {
                c.name: {
                    "children": [
                        self._concepts[cid].name
                        for cid in c.children
                        if cid in self._concepts
                    ],
                    "parent": (
                        self._concepts[c.parent_id].name
                        if c.parent_id and c.parent_id in self._concepts
                        else None
                    ),
                    "pages": c.related_pages,
                }
                for c in self._concepts.values()
            },
        }

    @staticmethod
    def _make_id(name: str) -> str:
        return name.lower().replace(" ", "_").replace("-", "_")


# ── Citation Graph ───────────────────────────────────────────


@dataclass
class CitationNode:
    id: str
    citation_key: str  # e.g., "[1]", "Smith2023"
    title: str = ""
    authors: str = ""
    year: str = ""
    source_text: str = ""
    page_num: int = 0
    is_cited: bool = False
    metadata: dict = field(default_factory=dict)


@dataclass
class CitationRelation:
    source_id: str
    target_id: str
    relation_type: str  # cites, references, cross_ref
    page_num: int = 0


class CitationGraph:
    """Citation and cross-reference graph.

    Maps references, citations, figure/table cross-references.
    """

    def __init__(self) -> None:
        self._citations: Dict[str, CitationNode] = {}
        self._relations: List[CitationRelation] = []

    def add_citation(
        self,
        citation_key: str,
        title: str = "",
        authors: str = "",
        year: str = "",
        page_num: int = 0,
    ) -> CitationNode:
        cid = f"cit_{len(self._citations)}"
        node = CitationNode(
            id=cid,
            citation_key=citation_key,
            title=title,
            authors=authors,
            year=year,
            page_num=page_num,
        )
        self._citations[cid] = node
        return node

    def add_reference(self, ref_text: str, page_num: int = 0) -> CitationNode:
        cid = f"ref_{len(self._citations)}"
        node = CitationNode(
            id=cid,
            citation_key=ref_text[:40],
            source_text=ref_text,
            page_num=page_num,
            is_cited=False,
        )
        self._citations[cid] = node
        return node

    def add_cross_ref(self, source: str, target: str, page_num: int = 0) -> None:
        self._relations.append(
            CitationRelation(
                source_id=source,
                target_id=target,
                relation_type="cross_ref",
                page_num=page_num,
            )
        )

    def find_citations_in_text(self, text: str, page_num: int = 0) -> List[str]:
        """Find citation markers like [1], [2,3], [Smith2023]."""
        found = []
        for match in re.finditer(r"\[([^\]]+)\]", text):
            ref = match.group(1)
            if re.match(r"[\d\s,\-–]+$", ref) or re.match(r"[A-Za-z]+", ref):
                found.append(f"[{ref}]")
                self.add_reference(f"[{ref}]", page_num=page_num)
        # Also find "Figure N", "Table N", "Fig. N"
        for match in re.finditer(
            r"(?:Fig|Figure|Table|Tab|Section|Sec)\.?\s*(\d+(?:\.\d+)?)", text
        ):
            ref = match.group(0)
            found.append(ref)
        return found

    def build_from_graph(self, graph: DocumentGraph) -> None:
        """Extract citations and references from a DocumentGraph."""
        for node in graph.nodes:
            if node.node_type in (NodeType.REFERENCE, NodeType.BIBLIOGRAPHY):
                self.add_reference(node.text, page_num=node.page_num)
            if node.text:
                self.find_citations_in_text(node.text, node.page_num)
        # Connect citations to references via edges
        for edge in graph.edges:
            if edge.edge_type == EdgeType.REFERENCE:
                self.add_cross_ref(edge.source_id, edge.target_id)

    @property
    def citations(self) -> List[CitationNode]:
        return list(self._citations.values())

    @property
    def citation_count(self) -> int:
        return len(self._citations)

    def to_dict(self) -> dict:
        return {
            "citations": {
                c.citation_key: {"page": c.page_num, "is_cited": c.is_cited}
                for c in self._citations.values()
            },
            "cross_refs": [
                {"source": r.source_id, "target": r.target_id, "type": r.relation_type}
                for r in self._relations
            ],
        }


# ── Knowledge Fuser ──────────────────────────────────────────


class KnowledgeFuser:
    """Fuse knowledge from multiple sources into a unified representation.

    Combines EntityGraph, ConceptGraph, CitationGraph, and DocumentMemory
    into a single coherent knowledge base for translation planning.
    """

    def __init__(
        self,
        entity_graph: Optional[EntityGraph] = None,
        concept_graph: Optional[ConceptGraph] = None,
        citation_graph: Optional[CitationGraph] = None,
        memory: Optional[DocumentMemory] = None,
    ) -> None:
        self.entity_graph = entity_graph or EntityGraph()
        self.concept_graph = concept_graph or ConceptGraph()
        self.citation_graph = citation_graph or CitationGraph()
        self.memory = memory
        self._fused = False

    def fuse(self) -> None:
        """Fuse all knowledge sources together."""
        if self._fused:
            return
        # Sync entities from memory to entity graph
        if self.memory:
            for entry in self.memory.get_all_entities():
                self.entity_graph.add_entity(
                    entry.canonical_name,
                    entity_type="entity",
                    canonical_name=entry.canonical_name,
                )
            for entry in self.memory.get_all_abbreviations():
                entity = self.entity_graph.add_entity(
                    entry.short,
                    entity_type="abbreviation",
                    canonical_name=entry.short,
                )
                self.entity_graph.add_entity(
                    entry.long,
                    entity_type="concept",
                    canonical_name=entry.long,
                )
                self.entity_graph.add_relation(
                    entry.short,
                    entry.long,
                    "abbreviation_of",
                )
        self._fused = True

    def get_context_for_node(self, node: DocumentNode, window_size: int = 3) -> dict:
        """Build rich context for a node from all knowledge sources."""
        context: dict = {
            "node_id": node.id,
            "node_type": (
                node.node_type.value
                if hasattr(node.node_type, "value")
                else str(node.node_type)
            ),
            "text": node.text,
            "entities": [],
            "concepts": [],
            "citations": [],
            "glossary": [],
        }
        if not self._fused:
            self.fuse()
        # Find related entities
        for entity in self.entity_graph.entities:
            if entity.name.lower() in node.text.lower():
                context["entities"].append(
                    {
                        "name": entity.name,
                        "canonical": entity.canonical_name,
                        "type": entity.entity_type,
                    }
                )
        # Find related concepts
        for concept in self.concept_graph.concepts:
            if concept.name.lower() in node.text.lower():
                context["concepts"].append(
                    {
                        "name": concept.name,
                        "parent": (
                            self.concept_graph.get_parent(concept.name).name
                            if self.concept_graph.get_parent(concept.name)
                            else None
                        ),
                    }
                )
        # Find citations
        for cit in self.citation_graph.citations:
            if cit.citation_key.lower() in node.text.lower():
                context["citations"].append(
                    {
                        "key": cit.citation_key,
                        "page": cit.page_num,
                    }
                )
        # Add glossary from memory
        if self.memory:
            for entry in self.memory.get_all_glossary():
                if entry.source.lower() in node.text.lower():
                    context["glossary"].append(
                        {
                            "source": entry.source,
                            "target": entry.target,
                        }
                    )
        return context

    def build_all(self, graph: DocumentGraph) -> None:
        """Build all knowledge graphs from a DocumentGraph."""
        self.entity_graph.build_from_graph(graph)
        self.concept_graph.build_from_graph(graph)
        self.citation_graph.build_from_graph(graph)
        self.fuse()

    def summary(self) -> dict:
        return {
            "entities": self.entity_graph.entity_count,
            "concepts": self.concept_graph.concept_count,
            "citations": self.citation_graph.citation_count,
            "fused": self._fused,
        }


class DocumentIntelligence:
    """Top-level Document Intelligence Runtime.

    Usage:
        di = DocumentIntelligence(graph, memory)
        di.analyze()
        context = di.get_context("node_42")
        print(di.summary())
    """

    def __init__(
        self,
        graph: Optional[DocumentGraph] = None,
        memory: Optional[DocumentMemory] = None,
    ) -> None:
        self.graph = graph
        self.memory = memory
        self.fuser = KnowledgeFuser(memory=memory)
        self._analyzed = False

    def analyze(self) -> None:
        """Run full document intelligence analysis."""
        if self.graph is None:
            raise ValueError("DocumentGraph required for analysis")
        self.fuser.build_all(self.graph)
        self._analyzed = True

    def get_context(self, node_id: str, window_size: int = 3) -> dict:
        if not self._analyzed:
            self.analyze()
        node = self.graph.get_node(node_id) if self.graph else None
        if node is None:
            return {"node_id": node_id, "error": "node not found"}
        return self.fuser.get_context_for_node(node, window_size=window_size)

    def get_entity_context(self) -> dict:
        return self.fuser.entity_graph.to_dict()

    def get_concept_context(self) -> dict:
        return self.fuser.concept_graph.to_dict()

    def get_citation_context(self) -> dict:
        return self.fuser.citation_graph.to_dict()

    def summary(self) -> dict:
        return self.fuser.summary()


__all__ = [
    "EntityNode",
    "EntityRelation",
    "EntityGraph",
    "ConceptNode",
    "ConceptGraph",
    "CitationNode",
    "CitationRelation",
    "CitationGraph",
    "KnowledgeFuser",
    "DocumentIntelligence",
]
