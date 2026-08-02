# -*- coding: utf-8 -*-
"""V7.0 Property Graph - Node / Edge / Property / Schema / Query / Traversal / Index.

The PropertyGraph is the Graph-Database-style evolution of the V6.1 BaseGraph
"graph container": typed nodes indexed by node_type and property values, so
pattern queries (MATCH Paragraph WHERE page == 3) resolve via the index first.
It stays duck-type compatible with pdf2zh.v3.base_graph.adapt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Set


@dataclass
class PropertySchema:
    """Declarative schema of node types and their allowed properties."""

    node_types: Dict[str, Set[str]] = field(default_factory=dict)
    edge_relations: Set[str] = field(default_factory=set)
    strict: bool = False

    def validate(self, node_type: str, props: Dict[str, Any]) -> None:
        allowed = self.node_types.get(node_type)
        if not self.strict or allowed is None:
            return
        unknown = set(props) - allowed
        if unknown:
            raise ValueError(
                f"PropertyGraph schema violation: type {node_type!r} does "
                f"not declare {sorted(unknown)}")


@dataclass
class PropertyEdge:
    source: str
    target: str
    relation: str = "related"
    props: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"source": self.source, "target": self.target,
                "relation": self.relation, "props": dict(self.props)}


class PropertyQuery:
    """Declarative pattern query over a PropertyGraph (MATCH ... WHERE ...)."""

    def __init__(self, graph: "PropertyGraph") -> None:
        self._graph = graph
        self._type: Optional[str] = None
        self._filters: List[Callable[[dict], bool]] = []

    def where_type(self, node_type: str) -> "PropertyQuery":
        self._type = node_type
        return self

    def where(self, **filters: Any) -> "PropertyQuery":
        def match(node: dict) -> bool:
            return all(node.get(k) == v for k, v in filters.items())
        self._filters.append(match)
        return self

    def _candidates(self) -> Iterable[dict]:
        if self._type is not None:
            return self._graph._nodes_by_type.get(self._type, set())
        return set(self._graph._nodes.keys())

    def collect(self) -> List[dict]:
        """Return matching nodes (resolved from the type index first)."""
        matches = []
        for nid in self._candidates():
            node = self._graph._nodes[nid]
            if all(f(node) for f in self._filters):
                matches.append(node)
        return matches

    def ids(self) -> List[str]:
        return [n["id"] for n in self.collect()]

    def count(self) -> int:
        return len(self.collect())

    def out(self, node_id: str, relation: Optional[str] = None) -> Set[str]:
        return self._graph.neighbors(node_id, direction="out", relation=relation)

    def in_(self, node_id: str, relation: Optional[str] = None) -> Set[str]:
        return self._graph.neighbors(node_id, direction="in", relation=relation)

    def select(self, *props: str) -> List[dict]:
        return [{p: n.get(p) for p in props} for n in self.collect()]

class PropertyGraph:
    """Graph-Database-style property graph with per-type / per-value indexes."""

    def __init__(self, name: str = "property_graph",
                 schema: Optional[PropertySchema] = None) -> None:
        self.name = name
        self.schema = schema or PropertySchema()
        self._nodes: Dict[str, dict] = {}
        self._edges: List[PropertyEdge] = []
        self._nodes_by_type: Dict[str, Set[str]] = {}
        self._prop_index: Dict[str, Dict[Any, Set[str]]] = {}
        self._out_adj: Dict[str, Dict[str, Set[str]]] = {}
        self._in_adj: Dict[str, Dict[str, Set[str]]] = {}

    @property
    def nodes(self) -> Dict[str, dict]:
        return self._nodes

    @property
    def edges(self) -> List[PropertyEdge]:
        return self._edges

    def add_node(self, node_id: str, node_type: str = "Node",
                 label: str = "", **props: Any) -> dict:
        """Insert a typed node; updates per-type and per-value indexes."""
        if node_id in self._nodes:
            raise KeyError(f"Node {node_id!r} already exists - use upsert_node")
        merged = {"id": node_id, "type": node_type,
                  "label": label or node_id}
        merged.update(props)
        self.schema.validate(node_type, merged)
        self._nodes[node_id] = merged
        self._nodes_by_type.setdefault(node_type, set()).add(node_id)
        for key, value in merged.items():
            if key in ("id", "type", "label"):
                continue
            self._prop_index.setdefault(key, {}).setdefault(value, set()).add(node_id)
        return merged

    def upsert_node(self, node_id: str, node_type: Optional[str] = None,
                    **props: Any) -> dict:
        """Insert or merge a node; removes stale property-index entries."""
        if node_id not in self._nodes:
            return self.add_node(node_id, node_type or "Node", **props)
        existing = self._nodes[node_id]
        if node_type:
            self._nodes_by_type[existing["type"]].discard(node_id)
            existing["type"] = node_type
            self._nodes_by_type.setdefault(node_type, set()).add(node_id)
        for key, value in props.items():
            old = existing.get(key)
            if key in ("id", "type", "label"):
                continue
            if key in self._prop_index and old in self._prop_index[key]:
                self._prop_index[key][old].discard(node_id)
            existing[key] = value
            self._prop_index.setdefault(key, {}).setdefault(value, set()).add(node_id)
        return existing

    def get_node(self, node_id: str) -> Optional[dict]:
        return self._nodes.get(node_id)

    def has_node(self, node_id: str) -> bool:
        return node_id in self._nodes

    def add_edge(self, source: str, target: str,
                 relation: str = "related", **props: Any) -> PropertyEdge:
        """Insert an edge and maintain both adjacency maps."""
        if source not in self._nodes or target not in self._nodes:
            missing = source if source not in self._nodes else target
            raise KeyError(f"Edge endpoints must exist first: {missing!r}")
        edge = PropertyEdge(source=source, target=target, relation=relation,
                            props=dict(props))
        self._edges.append(edge)
        self._out_adj.setdefault(source, {}).setdefault(relation, set()).add(target)
        self._in_adj.setdefault(target, {}).setdefault(relation, set()).add(source)
        return edge

    def node_types(self) -> List[str]:
        return sorted(self._nodes_by_type)

    def ids_of_type(self, node_type: str) -> List[str]:
        return sorted(self._nodes_by_type.get(node_type, set()))

    def lookup(self, prop: str, value: Any) -> List[str]:
        """Resolve node ids by property value via the value index."""
        return sorted(self._prop_index.get(prop, {}).get(value, set()))

    def neighbors(self, node_id: str, direction: str = "out",
                  relation: Optional[str] = None) -> Set[str]:
        adj = self._out_adj if direction == "out" else self._in_adj
        rels = adj.get(node_id, {})
        if relation is not None:
            return set(rels.get(relation, set()))
        return set().union(*rels.values()) if rels else set()

    def query(self) -> PropertyQuery:
        return PropertyQuery(self)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "nodes": [dict(n) for n in self._nodes.values()],
            "edges": [e.to_dict() for e in self._edges],
        }

    @classmethod
    def from_dict(cls, data: dict,
                  schema: Optional[PropertySchema] = None) -> "PropertyGraph":
        pg = cls(name=data.get("name", "property_graph"), schema=schema)
        for node in data.get("nodes", []):
            node = dict(node)
            nid = node.pop("id")
            pg.add_node(nid, node_type=node.pop("type", "Node"),
                        label=node.pop("label", ""), **node)
        for edge in data.get("edges", []):
            pg.add_edge(edge["source"], edge["target"],
                        relation=edge.get("relation", "related"),
                        **edge.get("props", {}))
        return pg

    def __len__(self) -> int:
        return len(self._nodes)

def create_property_graph_from_document(document_graph: Any,
                                        name: str = "doc_property") -> PropertyGraph:
    """Index a DocumentGraph as a PropertyGraph with per-type / per-page data."""
    pg = PropertyGraph(name=name)
    for node in document_graph.nodes:
        ntype = node.node_type.value if hasattr(node.node_type, "value") \
            else str(node.node_type)
        if ntype.lower() in ("page", "document"):
            continue
        bbox = getattr(node, "bbox", (0.0, 0.0, 0.0, 0.0)) or (0, 0, 0, 0)
        pg.add_node(
            node.id, node_type=ntype, label=(node.text or "")[:80],
            page=getattr(node, "page_num", 0),
            bbox=(bbox[0], bbox[1], bbox[2], bbox[3]),
            font_size=getattr(node, "font_size", 0.0),
            confidence=getattr(node, "confidence", 1.0),
            language=getattr(node, "language", ""),
        )
    for edge in document_graph.edges:
        etype = edge.edge_type.value if hasattr(edge.edge_type, "value") \
            else str(edge.edge_type)
        try:
            pg.add_edge(edge.source_id, edge.target_id, relation=etype)
        except KeyError:
            continue
    return pg


__all__ = [
    "PropertySchema", "PropertyEdge", "PropertyQuery", "PropertyGraph",
    "create_property_graph_from_document",
]
