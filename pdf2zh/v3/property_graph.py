"""Module: V7.0 Property Graph — BaseGraph 升级为 Graph Database.

Iteration feedback (V6.2/V7 roadmap): BaseGraph today is a generic *graph
container* (a NetworkX/Boost.Graph style backbone). A Document Intelligence
Runtime needs a *Property Graph* — i.e. a graph database:

    Node ──► typed by Schema
    Edge ──► typed relation
    Property ──► validated key-value facets on both nodes and edges
    Schema ──► declares node types and their property fields
    Query ──► Cypher-style MATCH / WHERE / ORDER BY / LIMIT
    Traversal ──► index-accelerated reachability / nearest / range lookups
    Index ──► inverted index + sorted range index for fast WHERE resolution

This prevents the *Graph Explosion*: instead of growing into LayoutGraph /
PromptGraph / ContextGraph ... each owning its own clone/serialize/query
methods, any domain graph is a PropertyGraph whose node types, fields and
indexes are declared in a schema, and queried with one unified language.

Usage::

    from pdf2zh.v3.property_graph import PropertyGraph, PropertyField

    pg = PropertyGraph(name="document")
    pg.define_schema("paragraph", [
        PropertyField("page", type="int", required=True, indexed=True),
        PropertyField("y", type="float", indexed=True),
    ])
    pg.add_node(GraphNode("p1", properties={"page": 3, "y": 120.0}),
                node_type="paragraph")
    result = pg.query("MATCH paragraph WHERE page == 3 ORDER BY y LIMIT 10")
"""

from __future__ import annotations

import bisect
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from pdf2zh.v3.base_graph import BaseGraph, GraphEdge, GraphKind, GraphNode

logger = logging.getLogger(__name__)


class PropertySchemaError(ValueError):
    """Raised when a node violates its declared property schema."""


class GraphQueryError(ValueError):
    """Raised when a Cypher-style query cannot be parsed or executed."""


_TYPE_HINTS = {"int", "float", "str", "bool", "list", "any"}


@dataclass
class PropertyField:
    """One property field of a node type in the schema."""

    name: str
    type: str = "any"
    required: bool = False
    indexed: bool = False
    description: str = ""

    def validate(self, value: Any) -> bool:
        if value is None:
            return not self.required
        if self.type == "any":
            return True
        if self.type == "int":
            return isinstance(value, int) and not isinstance(value, bool)
        if self.type == "float":
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        if self.type == "str":
            return isinstance(value, str)
        if self.type == "bool":
            return isinstance(value, bool)
        if self.type == "list":
            return isinstance(value, (list, tuple, set))
        return True

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "type": self.type,
            "required": self.required,
            "indexed": self.indexed,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PropertyField":
        return cls(
            name=data["name"],
            type=data.get("type", "any"),
            required=bool(data.get("required", False)),
            indexed=bool(data.get("indexed", False)),
            description=data.get("description", ""),
        )


class PropertySchema:
    """Declares node types and validates properties against them."""

    def __init__(
        self,
        types: Optional[Dict[str, Dict[str, PropertyField]]] = None,
    ) -> None:
        # node_type -> {field_name -> PropertyField}
        self._types: Dict[str, Dict[str, PropertyField]] = {}
        if types:
            for node_type, fields in types.items():
                self.add_type(node_type, list(fields.values()))

    def add_type(self, node_type: str,
                 fields: List[PropertyField]) -> "PropertySchema":
        existing = self._types.setdefault(node_type, {})
        for f in fields:
            if f.type not in _TYPE_HINTS:
                raise PropertySchemaError(
                    f"Unknown type '{f.type}' for field '{f.name}'")
            existing[f.name] = f
        return self

    def has_type(self, node_type: str) -> bool:
        return node_type in self._types

    def get_fields(self, node_type: str) -> Dict[str, PropertyField]:
        return dict(self._types.get(node_type, {}))

    @property
    def types(self) -> Dict[str, Dict[str, PropertyField]]:
        return dict(self._types)

    @property
    def type_names(self) -> List[str]:
        return sorted(self._types.keys())

    def indexed_fields(self) -> List[str]:
        return sorted(
            f.name for fields in self._types.values() for f in fields.values()
            if f.indexed)

    def validate(self, node_type: str, properties: Dict[str, Any]) -> List[str]:
        """Return a list of schema violations (empty means valid)."""
        errors: List[str] = []
        fields = self._types.get(node_type)
        if fields is None:
            return errors
        for f in fields.values():
            if f.name not in properties:
                if f.required:
                    errors.append(f"missing required field '{f.name}'")
                continue
            if not f.validate(properties[f.name]):
                errors.append(
                    f"field '{f.name}' expects {f.type}, "
                    f"got {type(properties[f.name]).__name__}")
        return errors

    def is_valid(self, node_type: str, properties: Dict[str, Any]) -> bool:
        return not self.validate(node_type, properties)

    def to_dict(self) -> dict:
        return {
            node_type: [f.to_dict() for f in fields.values()]
            for node_type, fields in self._types.items()
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PropertySchema":
        schema = cls()
        for node_type, fields in data.items():
            schema.add_type(node_type, [PropertyField.from_dict(f) for f in fields])
        return schema


# ═══════════════════════════════════════════════════════════════════
# PropertyIndex — inverted index + sorted range index
# ═══════════════════════════════════════════════════════════════════

_COMPARABLE = (int, float, str)


class PropertyIndex:
    """Two-way index over one property: value → node ids, plus a sorted
    list for numeric range queries. Maintained incrementally by PropertyGraph.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self._inverted: Dict[Any, Set[str]] = {}
        self._node_value: Dict[str, Any] = {}
        self._sorted_num: List[Tuple[float, str]] = []  # numeric range index
        self._sorted_str: List[Tuple[str, str]] = []    # string range index

    def _key(self, value: Any) -> Any:
        try:
            hash(value)
            return value
        except TypeError:
            return repr(value)

    def add(self, node_id: str, value: Any) -> None:
        self._node_value[node_id] = value
        self._inverted.setdefault(self._key(value), set()).add(node_id)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            self._sorted_num.append((float(value), node_id))
            self._sorted_num.sort(key=lambda pair: (pair[0], pair[1]))
        elif isinstance(value, str):
            self._sorted_str.append((value, node_id))
            self._sorted_str.sort(key=lambda pair: (pair[0], pair[1]))

    def remove(self, node_id: str) -> None:
        value = self._node_value.pop(node_id, None)
        if value is None:
            return
        bucket = self._inverted.get(self._key(value))
        if bucket is not None:
            bucket.discard(node_id)
            if not bucket:
                self._inverted.pop(self._key(value), None)
        self._sorted_num = [(v, n) for v, n in self._sorted_num if n != node_id]
        self._sorted_str = [(v, n) for v, n in self._sorted_str if n != node_id]

    def update(self, node_id: str, value: Any) -> None:
        self.remove(node_id)
        self.add(node_id, value)

    def lookup(self, value: Any) -> Set[str]:
        """Exact-match lookup, O(1) via the inverted index."""
        return set(self._inverted.get(self._key(value), set()))

    def lookup_many(self, values: Iterable[Any]) -> Set[str]:
        ids: Set[str] = set()
        for v in values:
            ids |= self.lookup(v)
        return ids

    def range_query(self, lo: Any, hi: Any) -> Set[str]:
        """Range lookup via bisect over the matching sorted list."""
        if isinstance(lo, (int, float)) and not isinstance(lo, bool) or \
                isinstance(hi, (int, float)) and not isinstance(hi, bool):
            if not self._sorted_num:
                return set()
            start = bisect.bisect_left(self._sorted_num, (float(lo), ""))
            end = bisect.bisect_right(self._sorted_num, (float(hi), "\uffff"))
            return {nid for _, nid in self._sorted_num[start:end]}
        if not self._sorted_str:
            return set()
        start = bisect.bisect_left(self._sorted_str, (lo, ""))
        end = bisect.bisect_right(self._sorted_str, (hi, "\uffff"))
        return {nid for _, nid in self._sorted_str[start:end]}


    def contains_value(self, value: Any) -> bool:
        return bool(self._inverted.get(self._key(value)))

    @property
    def size(self) -> int:
        return len(self._node_value)

    @property
    def distinct_values(self) -> int:
        return len(self._inverted)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "size": self.size,
            "distinct_values": self.distinct_values,
        }


# ═══════════════════════════════════════════════════════════════════
# GraphQuery — Cypher-style query DSL + result
# ═══════════════════════════════════════════════════════════════════

_OPS = ("==", "!=", ">=", "<=", ">", "<", "=")

_COND_RE = re.compile(
    r"(\w+)\s*(==|!=|>=|<=|>|<|=)\s*"
    r"(?:\"([^\"]*)\"|'([^']*)'|([+-]?\d+(?:\.\d+)?))")


class GraphQueryResult:
    """Result of a PropertyGraph query, with the execution plan used."""

    def __init__(self, nodes: List[Any], plan: List[str],
                 elapsed_ms: float, total_matched: int) -> None:
        self.nodes = nodes
        self.plan = plan
        self.elapsed_ms = round(elapsed_ms, 4)
        self.total_matched = total_matched

    @property
    def count(self) -> int:
        return len(self.nodes)

    @property
    def ids(self) -> List[str]:
        return [getattr(n, "id", n) for n in self.nodes]

    @property
    def first(self) -> Optional[Any]:
        return self.nodes[0] if self.nodes else None

    def to_dict(self) -> dict:
        return {
            "count": self.count,
            "total_matched": self.total_matched,
            "elapsed_ms": self.elapsed_ms,
            "plan": self.plan,
            "ids": self.ids,
        }

    def __iter__(self):
        return iter(self.nodes)

    def __len__(self) -> int:
        return len(self.nodes)

    def __getitem__(self, item):
        return self.nodes[item]


class GraphQueryBuilder:
    """Chainable query builder: match → where → order_by → limit → run.

    Example::

        pg.match("paragraph").where("page", "==", 3) \\
          .order_by("y").limit(10).run()
    """

    def __init__(self, graph: "PropertyGraph") -> None:
        self._graph = graph
        self._kind: Optional[str] = None
        self._conditions: List[Tuple[str, str, Any]] = []
        self._order_by: Optional[Tuple[str, bool]] = None
        self._limit: Optional[int] = None
        self._select: Optional[List[str]] = None

    def match(self, kind: Optional[str] = None) -> "GraphQueryBuilder":
        self._kind = kind
        return self

    def where(self, prop: str, op: str, value: Any) -> "GraphQueryBuilder":
        if op not in _OPS:
            raise GraphQueryError(f"Unsupported operator '{op}'")
        self._conditions.append((prop, op, value))
        return self

    def order_by(self, prop: str, desc: bool = False) -> "GraphQueryBuilder":
        self._order_by = (prop, desc)
        return self

    def limit(self, n: int) -> "GraphQueryBuilder":
        if n < 0:
            raise GraphQueryError("LIMIT must be >= 0")
        self._limit = n
        return self

    def select(self, *props: str) -> "GraphQueryBuilder":
        self._select = list(props)
        return self

    # ── Execution ────────────────────────────────────────────────────

    def run(self) -> GraphQueryResult:
        return self._graph._execute_query(
            kind=self._kind, conditions=self._conditions,
            order_by=self._order_by, limit=self._limit, select=self._select)

    def count(self) -> int:
        return self.run().count

    def __len__(self) -> int:
        return self.run().count


def parse_cypher(query: str) -> dict:
    """Parse a small Cypher-like query into a parameter dict.

    Supported grammar::

        MATCH <kind> [WHERE <prop> <op> <value> [AND ...]]
                [ORDER BY <prop> [DESC|ASC]] [LIMIT <n>]
                [RETURN <prop, prop, ...>]

    ``<value>`` may be a quoted string or a number. Operators: ``== != >=
    <= > < =``. Returns ``{"kind", "conditions", "order_by", "limit",
    "select"}``.
    """
    m = re.search(r"MATCH\s+([a-zA-Z0-9_]+)", query, re.IGNORECASE)
    if not m:
        raise GraphQueryError("Query must start with 'MATCH <kind>'")
    kind = m.group(1)
    where_part = re.search(
        r"WHERE\s+(.+?)(?=\s+ORDER\s+BY|\s+LIMIT\s+|\s+RETURN\s+|$)",
        query, re.IGNORECASE | re.DOTALL)
    order_m = re.search(
        r"ORDER\s+BY\s+([a-zA-Z0-9_]+)(?:\s+(DESC|ASC))?",
        query, re.IGNORECASE)
    limit_m = re.search(r"LIMIT\s+(\d+)", query, re.IGNORECASE)
    return_m = re.search(r"RETURN\s+([a-zA-Z0-9_,\s]+)", query, re.IGNORECASE)

    conditions: List[Tuple[str, str, Any]] = []
    if where_part:
        conds = _COND_RE.findall(where_part.group(1))
        if not conds:
            raise GraphQueryError(
                f"Could not parse WHERE clause: '{where_part.group(1)}'")
        for prop, op, q1, q2, num in conds:
            if q1 is not None and q1 != "":
                value: Any = q1
            elif q2 is not None and q2 != "":
                value = q2
            else:
                value = float(num) if "." in num else int(num)
            conditions.append((prop, op, value))

    order_by = None
    if order_m:
        order_by = (order_m.group(1), bool(order_m.group(2) and
                                           order_m.group(2).upper() == "DESC"))
    select = None
    if return_m:
        select = [p.strip() for p in return_m.group(1).split(",") if p.strip()]
    return {
        "kind": kind,
        "conditions": conditions,
        "order_by": order_by,
        "limit": int(limit_m.group(1)) if limit_m else None,
        "select": select,
    }


# ═══════════════════════════════════════════════════════════════════
# PropertyGraph — BaseGraph upgraded into a property-graph database
# ═══════════════════════════════════════════════════════════════════


class PropertyGraph(BaseGraph):
    """A unified graph with declared schema, validated properties and
    index-accelerated Cypher-style queries.

    Everything a BaseGraph offers (DFS / BFS / topological / clone / merge /
    serialize / diff / snapshot) is inherited; PropertyGraph adds the
    *database* layer: typed node types, property validation, inverted +
    range indexes and a query DSL.

    Usage::

        pg = PropertyGraph(name="doc", kind=GraphKind.DOCUMENT)
        pg.define_schema("paragraph", [
            PropertyField("page", type="int", required=True, indexed=True),
            PropertyField("y", type="float", indexed=True),
            PropertyField("lang", type="str", indexed=True),
        ])
        pg.add_node(GraphNode("p1", properties={"page": 3, "y": 120.0,
                                                "lang": "en"}),
                    node_type="paragraph")
        hits = pg.query("MATCH paragraph WHERE page == 3 ORDER BY y")
    """

    def __init__(self, kind: GraphKind = GraphKind.CUSTOM, name: str = "",
                 schema: Optional[PropertySchema] = None) -> None:
        super().__init__(kind, name)
        self._schema = schema or PropertySchema()
        self._indexes: Dict[str, PropertyIndex] = {}
        self._type_index: Dict[str, Set[str]] = {}

    # ── Schema ───────────────────────────────────────────────────────

    @property
    def schema(self) -> PropertySchema:
        return self._schema

    def define_schema(self, node_type: str,
                      fields: List[PropertyField]) -> "PropertyGraph":
        self._schema.add_type(node_type, fields)
        for f in fields:
            if f.indexed:
                self._ensure_index(f.name)
        return self

    # ── Indexes ──────────────────────────────────────────────────────

    def _ensure_index(self, prop: str) -> PropertyIndex:
        if prop not in self._indexes:
            self._indexes[prop] = PropertyIndex(prop)
        return self._indexes[prop]

    def create_index(self, prop: str) -> "PropertyGraph":
        """Explicitly (re)build an index over one property."""
        idx = PropertyIndex(prop)
        for n in self.nodes:
            if prop in n.properties:
                idx.add(n.id, n.properties[prop])
        self._indexes[prop] = idx
        return self

    @property
    def indexes(self) -> Dict[str, PropertyIndex]:
        return dict(self._indexes)

    def index_stats(self) -> Dict[str, dict]:
        return {name: idx.to_dict() for name, idx in self._indexes.items()}

    # ── Node operations (override to keep schema + indexes in sync) ──

    def add_node(self, node: GraphNode,
                 node_type: Optional[str] = None,
                 validate: bool = True) -> "PropertyGraph":
        if node_type is not None:
            node.label = node_type
            node.properties.setdefault("type", node_type)
        if validate and node.label and self._schema.has_type(node.label):
            errors = self._schema.validate(node.label, node.properties)
            if errors:
                raise PropertySchemaError(
                    f"Node '{node.id}': {'; '.join(errors)}")
        for f in self._schema.get_fields(node.label or "").values():
            if f.indexed:
                self._ensure_index(f.name)
        super().add_node(node)
        self._type_index.setdefault(node.label, set()).add(node.id)
        for name, idx in self._indexes.items():
            if name in node.properties:
                idx.add(node.id, node.properties[name])
        return self

    def remove_node(self, node_id: str) -> bool:
        node = self.get_node(node_id)
        if node is None:
            return False
        for idx in self._indexes.values():
            idx.remove(node_id)
        bucket = self._type_index.get(node.label)
        if bucket is not None:
            bucket.discard(node_id)
        return super().remove_node(node_id)

    def set_property(self, node_id: str, key: str, value: Any) -> "PropertyGraph":
        node = self.get_node(node_id)
        if node is None:
            raise KeyError(f"Node '{node_id}' not found")
        if node.label and self._schema.has_type(node.label):
            errors = self._schema.validate(node.label,
                                           {**node.properties, key: value})
            if errors:
                raise PropertySchemaError(
                    f"Node '{node_id}': {'; '.join(errors)}")
        super().set_property(node_id, key, value)
        if key in self._indexes:
            self._indexes[key].update(node_id, value)
        return self

    def set_properties(self, node_id: str, props: Dict[str, Any]) -> "PropertyGraph":
        node = self.get_node(node_id)
        if node is None:
            raise KeyError(f"Node '{node_id}' not found")
        if node.label and self._schema.has_type(node.label):
            merged = {**node.properties, **props}
            errors = self._schema.validate(node.label, merged)
            if errors:
                raise PropertySchemaError(
                    f"Node '{node_id}': {'; '.join(errors)}")
        for k, v in props.items():
            super().set_property(node_id, k, v)
            if k in self._indexes:
                self._indexes[k].update(node_id, v)
        return self

    def clear(self) -> None:
        super().clear()
        self._type_index.clear()
        for idx in self._indexes.values():
            self._indexes[idx.name] = PropertyIndex(idx.name)


    # ── Query API ────────────────────────────────────────────────────

    def query(self, cypher: str) -> GraphQueryResult:
        """Run a Cypher-style query string against this graph."""
        params = parse_cypher(cypher)
        return self._execute_query(
            kind=params["kind"], conditions=params["conditions"],
            order_by=params["order_by"], limit=params["limit"],
            select=params["select"])

    def match(self, kind: Optional[str] = None) -> GraphQueryBuilder:
        """Start a chainable query builder."""
        return GraphQueryBuilder(self).match(kind)

    def find(self, prop: str, value: Any) -> List[GraphNode]:
        """Find all nodes whose property equals value (index-accelerated)."""
        idx = self._indexes.get(prop)
        ids = idx.lookup(value) if idx else {
            n.id for n in self.nodes if n.properties.get(prop) == value}
        return [self.get_node(i) for i in sorted(ids)]

    def find_by_property(self, prop: str, value: Any) -> List[GraphNode]:
        return self.find(prop, value)

    def range(self, prop: str, lo: Any, hi: Any) -> List[GraphNode]:
        """Find all nodes whose property lies in [lo, hi]."""
        idx = self._indexes.get(prop)
        ids = idx.range_query(lo, hi) if idx else {
            n.id for n in self.nodes
            if isinstance(n.properties.get(prop), _COMPARABLE)
            and lo <= n.properties[prop] <= hi}
        return [self.get_node(i) for i in sorted(ids)]

    def nearest(self, prop: str, value: Any, k: int = 1) -> List[GraphNode]:
        """Return the k nodes closest to ``value`` along a numeric property.

        Uses the sorted numeric index when available (binary search), falling
        back to a linear scan. Deterministic tie-breaking by node id.
        """
        idx = self._indexes.get(prop)
        if idx is not None and idx._sorted_num:
            values = idx._sorted_num
            pos = bisect.bisect_left(values, (float(value), ""))
            candidates: List[Tuple[float, str]] = []
            left, right = pos - 1, pos
            while left >= 0 or right < len(values):
                if right < len(values):
                    candidates.append(values[right])
                    right += 1
                if left >= 0:
                    candidates.append(values[left])
                    left -= 1
                if len(candidates) >= k * 2:
                    break
            ranked = sorted(candidates,
                            key=lambda p: (abs(p[0] - value), p[1]))
            return [self.get_node(nid) for _, nid in ranked[:k]]
        ranked_all = sorted(
            ((abs(n.properties[prop] - value), n.id) for n in self.nodes
             if isinstance(n.properties.get(prop), (int, float))
             and not isinstance(n.properties[prop], bool)),
            key=lambda p: (p[0], p[1]))
        return [self.get_node(nid) for _, nid in ranked_all[:k]]

    def nodes_of_type(self, node_type: str) -> List[GraphNode]:
        """All nodes of a given declared type (type-index accelerated)."""
        return [self.get_node(i) for i in sorted(
            self._type_index.get(node_type, set()))]

    def type_counts(self) -> Dict[str, int]:
        return {t: len(ids) for t, ids in sorted(self._type_index.items())}


    # ── Query engine ──────────────────────────────────────────────────

    def _execute_query(self, *, kind: Optional[str],
                       conditions: List[Tuple[str, str, Any]],
                       order_by: Optional[Tuple[str, bool]],
                       limit: Optional[int],
                       select: Optional[List[str]]) -> GraphQueryResult:
        started = time.time()
        plan: List[str] = []

        # 1. Candidate set — type index first.
        if kind is not None:
            candidates: Optional[Set[str]] = set(self._type_index.get(kind, set()))
            plan.append(f"type_index[{kind}]")
        else:
            candidates = set(self._nodes.keys())
            plan.append("full_scan")

        # 2. Narrow with property indexes where available.
        for prop, op, value in conditions:
            idx = self._indexes.get(prop)
            if idx is None:
                continue
            if op in ("==", "="):
                ids = idx.lookup(value)
                plan.append(f"index[{prop}=={value!r}]")
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                if op in (">=", ">"):
                    ids = idx.range_query(value, float("inf"))
                else:  # op in ("<=", "<")
                    ids = idx.range_query(float("-inf"), value)
                plan.append(f"index[{prop}{op}{value!r}]")
            else:
                continue
            candidates = ids if candidates is None else (candidates & ids)
            if not candidates:
                break

        # 3. Linear filter over remaining candidates.
        matched: List[str] = []
        for nid in candidates:
            node = self._nodes[nid]
            if all(self._matches(node, prop, op, value)
                   for prop, op, value in conditions):
                matched.append(nid)
        matched.sort()

        # 4. Order / limit / project.
        nodes: List[Any] = [self._nodes[nid] for nid in matched]
        if order_by is not None:
            prop, desc = order_by
            nodes.sort(key=lambda n: (n.properties.get(prop) is None,
                                      n.properties.get(prop)),
                       reverse=desc)
        total = len(nodes)
        if limit is not None:
            nodes = nodes[:limit]
        if select is not None:
            nodes = [{p: n.properties.get(p) for p in select}
                     for n in nodes]
        return GraphQueryResult(nodes, plan, (time.time() - started) * 1000,
                                total)

    @staticmethod
    def _matches(node: GraphNode, prop: str, op: str, value: Any) -> bool:
        actual = node.properties.get(prop)
        if op in ("==", "="):
            return actual == value
        if op == "!=":
            return actual != value
        try:
            if op == ">":
                return actual > value
            if op == ">=":
                return actual >= value
            if op == "<":
                return actual < value
            if op == "<=":
                return actual <= value
        except TypeError:
            return False
        return False

    # ── Serialization ─────────────────────────────────────────────────

    def to_dict(self) -> dict:
        data = super().to_dict()
        data["schema"] = self._schema.to_dict()
        data["indexes"] = [self._indexes[name].to_dict()
                           for name in sorted(self._indexes)]
        return data

    @classmethod
    def from_dict(cls, data: dict, kind: Optional[GraphKind] = None,
                  name: Optional[str] = None) -> "PropertyGraph":
        graph = cls(
            kind=kind or GraphKind(data.get("kind", GraphKind.CUSTOM.value)),
            name=name if name is not None else data.get("name", ""),
            schema=PropertySchema.from_dict(data.get("schema", {}))
            if data.get("schema") else None,
        )
        for nd in data.get("nodes", []):
            graph.add_node(GraphNode.from_dict(nd))
        for ed in data.get("edges", []):
            edge = GraphEdge.from_dict(ed)
            if graph.has_node(edge.source_id) and graph.has_node(edge.target_id):
                graph.add_edge(edge)
        for idx in data.get("indexes", []):
            if "name" in idx:
                graph.create_index(idx["name"])
        return graph

    def stats(self) -> dict:
        return {
            "kind": self.kind.value,
            "name": self.name,
            "nodes": self.node_count,
            "edges": self.edge_count,
            "types": self.type_counts(),
            "indexes": self.index_stats(),
        }


__all__ = [
    "PropertyField", "PropertySchema", "PropertySchemaError",
    "GraphQueryError", "PropertyIndex", "GraphQueryResult",
    "GraphQueryBuilder", "parse_cypher", "PropertyGraph",
]
