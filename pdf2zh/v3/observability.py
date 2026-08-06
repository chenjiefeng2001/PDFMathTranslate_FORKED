"""Module: Observability — Phase D0/D1/D5/D6 文档可观测框架（编译器 Debug 层）。

以「编译器 + Render Graph 式」思路让每次翻译可观察、可复现、可差分：

    D0  TraceContext    —— DocumentID + NodeID 层次（所有日志只引用 NodeID，不写裸字符串）
    D1  SnapshotSystem  —— 每个 Pass 之间可落 JSON/Binary 的不可变快照链
    D5  DecisionLog     —— 每个决策记录「为什么」：evidence 各项得分 + 融合 confidence
    D6  DiagnosticEngine—— 编译器式 warning/error（loc 指向 Page，引用 node_id）

快照从统一文档模型（``PageModel`` / ``DocumentModel``）或已有 dict 生成，
与 `canonical_page` / `document_model` 的节点身份对齐（''P{pno}::B{i}''），
保证既有 `document_inspector` 的 block_id（``p{pno}_{i}``）可互查。

Usage::

    from pdf2zh.v3.observability import (
        TraceContext, DocumentID, NodeID,
        capture_snapshot, SnapshotStore,
        DecisionLog, DecisionRecord, DiagnosticEngine,
        ObsSession,
    )

    trace = TraceContext()
    page_node = trace.node("page", 1)
    block_node = trace.node("block", 0, parent=page_node)
    store = SnapshotStore()
    store.add("layout", capture_snapshot(model, "layout", trace))
    decisions = DecisionLog()
    decisions.record(block_node, "translate:on",
                     evidence={"structure": 0.9, "formula": 0.2},
                     source="toc_gate", stage="semantic")
"""
from __future__ import annotations

import gzip
import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from pdf2zh.v3.evidence import fuse_evidence

# 统一渲染阶段（与 legacy 主链路六大阶段对齐）
STAGES = ("parse", "semantic", "translation", "layout", "render")

# Block kind → 角色（D3 Overlay 用同一口径；heading 绿 / toc 蓝 / formula 黄 /
# image 红 / caption 青 / table 紫 / 其余灰）
ROLE_COLORS = {
    "heading": "#2e7d32",
    "title": "#2e7d32",
    "toc": "#1565c0",
    "formula": "#f9a825",
    "equation": "#f9a825",
    "image": "#c62828",
    "figure": "#c62828",
    "caption": "#00838f",
    "table": "#6a1b9a",
    "header": "#757575",
    "footer": "#757575",
    "footnote": "#5d4037",
    "code": "#37474f",
    "paragraph": "#9e9e9e",
}
_DEFAULT_COLOR = "#9e9e9e"

# 进入快照 payload 的 metadata 白名单（其余忽略，保持快照紧凑、可差分）
_MD_KEYS = (
    "role", "role_confidence", "confidence", "confidence_source",
    "uncertainty", "translate", "translated", "toc_number",
    "toc_confidence", "toc_scan", "formula_density", "reading_order",
    "anomaly", "render_path", "translation_policy",
    "typography", "fonts", "multifont",
)


def new_document_id(prefix: str = "DOC") -> str:
    """生成确定性文档 ID（``prefix_`` + 12 hex）。"""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@dataclass(frozen=True)
class DocumentID:
    """文档级唯一的可序列化身份（= trace_id 根）。"""

    value: str = ""

    def __post_init__(self) -> None:
        if not self.value:
            object.__setattr__(self, "value", new_document_id())

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class NodeID:
    """层次化节点 ID：``DOC_x::P1::B2::L0``。

    全引用统一走 ``str(node_id)``，禁止在可观测记录里用裸字符串指代节点。
    """

    doc_id: str
    path: Tuple[Tuple[str, int], ...]
    text: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", tuple(self.path))
        if not self.doc_id:
            object.__setattr__(self, "doc_id", new_document_id())

    @property
    def full(self) -> str:
        part = "::".join(f"{k}{n}" for k, n in self.path)
        return f"{self.doc_id}::{part}" if part else self.doc_id

    def __str__(self) -> str:
        return self.full

    def __repr__(self) -> str:
        return f"NodeID({self.full})"

    @property
    def parent(self) -> Optional["NodeID"]:
        if not self.path:
            return None
        return NodeID(self.doc_id, self.path[:-1])

    @property
    def kind(self) -> str:
        return self.path[-1][0] if self.path else "document"


class TraceContext:
    """D0 — 文档级跟踪上下文：为每次翻译建立一个唯一 DocumentID/TraceID，
    负责节点注册、父子关系与祖先链查询。自身只产生结构化记录，不发日志。"""

    def __init__(self, doc_id: Optional[str] = None) -> None:
        self.doc_id: str = doc_id or new_document_id()
        self.trace_id: str = f"T-{uuid.uuid4().hex[:10]}"
        self._nodes: Dict[str, NodeID] = {}
        self._children: Dict[str, List[str]] = {}
        self._root = NodeID(self.doc_id, (), )

    def node_id(self, kind: str, seq: int = 0,
                parent: Optional[NodeID] = None, text: str = "") -> NodeID:
        """创建/复用子节点 ID：kind 简写（P/B/L/S/H/T/G）+ 序号。"""
        parent = parent or self._root
        node = NodeID(self.doc_id, parent.path + ((kind, seq),), text=text)
        self._nodes[node.full] = node
        self._children.setdefault(parent.full, [])
        if node.full not in self._children[parent.full]:
            self._children[parent.full].append(node.full)
        return node

    def register(self, node: NodeID) -> NodeID:
        self._nodes[node.full] = node
        if node.parent:
            self._children.setdefault(node.parent.full, [])
            if node.full not in self._children[node.parent.full]:
                self._children[node.parent.full].append(node.full)
        return node

    def ancestors(self, node: NodeID) -> List[NodeID]:
        """自根到父链，供 Inspector 画生命周期。"""
        chain: List[NodeID] = []
        cur = node.parent
        while cur is not None:
            chain.append(cur)
            cur = cur.parent
        chain.reverse()
        return chain

    def children_of(self, node: NodeID) -> List[NodeID]:
        return [self._nodes[k] for k in self._children.get(node.full, [])]

    def known(self) -> List[NodeID]:
        return [
            n for n in self._nodes.values()
            if n.full.startswith(self.doc_id)
        ]

    def to_dict(self) -> Dict:
        return {"doc_id": self.doc_id, "trace_id": self.trace_id,
                "nodes": sorted(self._nodes),
                "children": self._children}


# ── D1: SnapshotSystem ─────────────────────────────────────────────────


def _block_snapshot(block, doc_id: str) -> Dict[str, Any]:
    lines: Dict[str, Any] = {}
    for j, line in enumerate(block.lines):
        lines[f"L{j}"] = {
            "text": line.text or "",
            "baseline": round(float(line.baseline or 0.0), 2),
            "x0": round(float(line.x0 or 0.0), 2),
            "y0": round(float(line.y0 or 0.0), 2),
            "x1": round(float(line.x1 or 0.0), 2),
            "y1": round(float(line.y1 or 0.0), 2),
            "spans": [
                {"text": (span.text or ""), "size": round(float(span.size or 0.0), 2)}
                for span in line.spans[:32]
            ],
        }
    return {
        "kind": block.kind,
        "text": block.text or "",
        "bbox": [round(v, 2) for v in block.bbox],
        "font_size": round(float(block.font_size or 0.0), 2),
        "metadata": _dup_filtered(block.metadata),
        "lines": lines,
    }


def _dup_filtered(metadata: Dict) -> Dict:
    return {k: v for k, v in (metadata or {}).items() if k in _MD_KEYS}


def capture_snapshot(source, stage: str,
                     trace: TraceContext) -> Dict[str, Any]:
    """从统一文档模型捕获节点级快照（不可变 dict，可直接 JSON/Binary 落盘）。

    ``source`` 可为 ``DocumentModel`` 或 ``PageModel``（含无 glyph 的空页）。
    节点 ID 层级对齐既有生态：``DOC_x::P{pno}`` 块 ``::B{i}`` 行 ``::B{i}::L{j}``，
    块级与 ``block_id(pno, i)``（``p{pno}_{i}``）语义等价。
    """
    if source is None:
        payload: Dict[str, Any] = {"nodes": {}, "stats": {
            "pages": 0, "blocks": 0, "lines": 0}}
    elif hasattr(source, "blocks") and not hasattr(source, "pages"):
        payload = _page_snapshot(source, trace)
    else:
        payload = _document_snapshot(source, trace)
    payload["doc_id"] = trace.doc_id
    payload["trace_id"] = trace.trace_id
    payload["stage"] = stage
    payload["timestamp"] = round(time.time(), 3)
    return payload


def _page_snapshot(page, trace: TraceContext) -> Dict[str, Any]:
    pno = int(getattr(page, "page_num", 0) or 0)
    page_node = trace.register(trace.node_id("P", pno, parent=trace._root))
    nodes: Dict[str, Any] = {page_node.full: {
        "kind": "page", "text": "", "bbox": [
            round(float(getattr(page, "width", 0.0) or 0.0), 2),
            round(float(getattr(page, "height", 0.0) or 0.0), 2)],
        "metadata": {"page_num": pno}}}
    blocks = 0
    for i, block in enumerate(page.blocks):
        bnode = trace.node_id("B", i, parent=page_node)
        nodes[bnode.full] = _block_snapshot(block, trace.doc_id)
        blocks += 1
    nodes[page_node.full]["metadata"]["blocks"] = blocks
    return {"nodes": nodes,
            "stats": {"pages": 1, "blocks": blocks,
                      "lines": sum(len(n["lines"]) for n in nodes.values()
                                   if "lines" in n)}}


def _document_snapshot(model, trace: TraceContext) -> Dict[str, Any]:
    doc_node = trace.node_id("DOC", 0)
    nodes: Dict[str, Any] = {doc_node.full: {"kind": "document", "text": "",
                                             "bbox": [], "metadata": {}}}
    pages_n = blocks_n = lines_n = 0
    for page in getattr(model, "pages", []) or []:
        psnap = _page_snapshot(page, trace)
        nodes.update(psnap["nodes"])
        pages_n += 1
        blocks_n += psnap["stats"]["blocks"]
        lines_n += psnap["stats"]["lines"]
    nodes[doc_node.full]["metadata"] = dict(getattr(model, "metadata", {}))
    nodes[doc_node.full]["stats"] = {"pages": pages_n,
                                     "blocks": blocks_n, "lines": lines_n}
    return {"nodes": nodes,
            "stats": {"pages": pages_n, "blocks": blocks_n, "lines": lines_n}}


@dataclass
class SnapshotStore:
    """D1 — 每 Pass 一个不可变快照的链式存储 + JSON/Binary 导出。"""

    doc_id: str = field(default_factory=new_document_id)
    snapshots: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    order: List[str] = field(default_factory=list)
    trace: Optional[TraceContext] = None

    def add(self, snapshot: Optional[Dict[str, Any]],
            stage: Optional[str] = None) -> str:
        if snapshot is None:
            return ""
        stage = stage or (snapshot or {}).get("stage", "unknown")
        if stage in self.snapshots:
            idx = self.order.index(stage)
            self.order.pop(idx)
            self.snapshots[stage] = snapshot
            self.order.insert(idx, stage)
        else:
            self.snapshots[stage] = snapshot
            self.order.append(stage)
        return stage

    def add_stage(self, source, stage: str, trace: Optional[TraceContext] = None) -> str:
        tr = trace or self.trace
        if tr is None:
            tr = TraceContext(self.doc_id)
            self.trace = tr
        return self.add(capture_snapshot(source, stage, tr), stage)

    def snapshot_for(self, stage: str) -> Optional[Dict[str, Any]]:
        return self.snapshots.get(stage)

    def latest(self) -> Optional[Dict[str, Any]]:
        return self.snapshots[self.order[-1]] if self.order else None

    def stages(self) -> List[str]:
        return list(self.order)

    def diff_stages(self, a: str, b: str) -> List["PassDiffEntry"]:
        from pdf2zh.v3.pass_diff import diff_snapshots
        return diff_snapshots(self.snapshots.get(a, {}),
                              self.snapshots.get(b, {}))

    def serialize(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    def to_dict(self) -> Dict[str, Any]:
        return {"doc_id": self.doc_id,
                "trace": self.trace.to_dict() if self.trace else None,
                "stages": self.order,
                "snapshots": self.snapshots}

    def to_binary(self) -> bytes:
        return gzip.compress(self.serialize().encode("utf-8"))

    def to_json_bytes(self, stage: str) -> bytes:
        return json.dumps(self.snapshots.get(stage, {}),
                          ensure_ascii=False).encode("utf-8")

    def save(self, path: str, binary: bool = False) -> None:
        data = self.to_binary() if binary else self.serialize().encode("utf-8")
        with open(path, "wb") as f:
            f.write(data)

    def digest(self) -> str:
        """内容哈希：只对快照内容敏感（doc_id/trace_id/timestamp 不入）。"""
        payload: Dict[str, Any] = {}
        for stage in self.order:
            snap = self.snapshots.get(stage) or {}
            payload[stage] = {k: v for k, v in snap.items()
                              if k not in ("timestamp", "trace_id")}
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True)
            .encode("utf-8")).hexdigest()[:16]

    @classmethod
    def load(cls, path: str) -> Optional["SnapshotStore"]:
        with open(path, "rb") as f:
            raw = f.read()
        try:
            text = gzip.decompress(raw).decode("utf-8")
        except OSError:
            text = raw.decode("utf-8")
        data = json.loads(text)
        store = SnapshotStore(data.get("doc_id", new_document_id()))
        store.snapshots = data.get("snapshots", {})
        store.order = data.get("stages", [])
        return store


# ── D5: DecisionLog ─────────────────────────────────────────────────────


@dataclass
class DecisionRecord:
    node_id: str = ""
    decision: str = ""
    evidence: Dict[str, float] = field(default_factory=dict)
    confidence: float = 0.5
    source: str = ""
    stage: str = ""
    message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"node_id": self.node_id, "decision": self.decision,
                "evidence": {k: round(float(v), 3) for k, v in self.evidence.items()},
                "confidence": round(float(self.confidence), 3),
                "source": self.source, "stage": self.stage,
                "message": self.message}


@dataclass
class DecisionLog:
    """D5 — 决策证据日志：每个节点每个决策记下「哪些信号 · 多可信 · 谁决定」。"""

    decisions: List[DecisionRecord] = field(default_factory=list)

    def record(self, node_id, decision: str, evidence=None,
               confidence: Optional[float] = None, source: str = "",
               stage: str = "", message: str = "") -> DecisionRecord:
        rec = DecisionRecord(
            node_id=str(node_id), decision=decision,
            evidence={k: float(v) for k, v in (evidence or {}).items()},
            confidence=float(confidence) if confidence is not None
            else fuse_evidence(evidence or {}),
            source=source, stage=stage, message=message)
        self.decisions.append(rec)
        return rec

    def for_node(self, node_id) -> List[DecisionRecord]:
        return [d for d in self.decisions if d.node_id == str(node_id)]

    def stage_records(self, stage: str) -> List[DecisionRecord]:
        return [d for d in self.decisions if d.stage == stage]

    def counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for d in self.decisions:
            out[d.decision] = out.get(d.decision, 0) + 1
        return out

    def to_dict(self) -> Dict[str, Any]:
        return {"counts": self.counts(),
                "records": [d.to_dict() for d in self.decisions]}

    def summary(self) -> str:
        return "DecisionLog " + " ".join(
            f"{k}={v}" for k, v in sorted(self.counts().items()))


# ── D6: DiagnosticEngine（编译器式 warning/error） ───────────────────────


@dataclass
class DiagnosticEngine:
    """D6 — 复用既有 `diagnostics.analyze_document`，输出编译器式行。

    ``error: Page 18: Formula may overlap page right margin?? ``——
    编译器风格消息：severity + location(page) + 消息 + 引用 node_id。
    """

    report: Optional[Any] = None

    def run(self, model) -> dict:
        from pdf2zh.v3.diagnostics import analyze_document
        self.report = analyze_document(model)
        return self.to_dict()

    def diagnostics_for(self, node_id: str) -> List[Dict[str, Any]]:
        if self.report is None:
            return []
        return [i.to_dict() for i in self.report.issues
                if i.node_id == str(node_id)]

    def format_issue(self, issue) -> str:
        loc = f"Page {issue.page}" if issue.page is not None else "doc"
        return (f"{issue.severity}: {loc}: {issue.message} "
                f"[{issue.node_id}]")

    def format_report(self, include_messages: bool = True) -> str:
        if self.report is None:
            return ""
        groups = {"error": [], "warning": []}
        for issue in self.report.issues:
            groups.get(issue.severity, groups["warning"]).append(issue)
        lines = [f"{issue.severity}: Page {issue.page}: {issue.message} "
                 f"[{issue.node_id}]"
                 for issue in groups["error"] + groups["warning"]]
        if include_messages and not lines:
            lines.append(
                f"{self.report.error_count} error(s), "
                f"{self.report.warning_count} warning(s)")
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        if self.report is None:
            return {"errors": 0, "warnings": 0, "issues": []}
        return self.report.to_dict()


# ── 主链路 side-channel 会话聚合 ────────────────────────────────────────


class ObsSession:
    """converter/high_level 可观测会话：一个文档一套 trace+snapshots+decisions。"""

    def __init__(self, doc_id: Optional[str] = None) -> None:
        self.trace = TraceContext(doc_id)
        self.snapshot_store = SnapshotStore(self.trace.doc_id, trace=self.trace)
        self.decisions = DecisionLog()
        self.diagnostics = DiagnosticEngine()
        self.page_dims: Dict[Any, Tuple[float, float]] = {}

    def capture(self, source, stage: str) -> str:
        return self.snapshot_store.add_stage(source, stage, self.trace)

    def record(self, node_id, decision, evidence=None, confidence=None,
               source="", stage="", message="") -> DecisionRecord:
        return self.decisions.record(node_id, decision, evidence, confidence,
                                     source, stage, message)

    def diagnose(self, document) -> Dict[str, Any]:
        return self.diagnostics.run(document)

    def bundle(self) -> Dict[str, Any]:
        return {
            "doc_id": self.trace.doc_id,
            "trace": self.trace.to_dict(),
            "snapshots": self.snapshot_store.to_dict(),
            "decisions": self.decisions.to_dict(),
            "diagnostics": self.diagnostics.to_dict(),
        }


def make_session(doc_id: Optional[str] = None) -> ObsSession:
    return ObsSession(doc_id)


__all__ = [
    "STAGES", "ROLE_COLORS", "new_document_id", "DocumentID", "NodeID",
    "TraceContext", "SnapshotStore", "capture_snapshot", "DecisionLog",
    "DecisionRecord", "DiagnosticEngine", "ObsSession", "make_session",
]