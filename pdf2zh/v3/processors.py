"""Module: Node Processors — 单一核心 IR 上的 AST-Pass 层（V9.0）。

架构决策（对应「单一核心 IR + 专用 Processor」方案）：

    - **核心 IR 只有一个**：``DocumentGraph``（``v3.graph`` 的
      ``DocumentNode`` / ``NodeType``）。图片 / 目录 / 公式 / 代码 /
      表格 **不是平行 IR**，只是 ``NodeType`` 不同的 Node —— 类比
      DOM / AST / ECS：类型是枚举成员，专用细节写进 ``metadata``。
    - **领域引擎不是 IR**：``image_engine`` / ``toc_semantics`` /
      ``content_preservation`` 各自的能力以 ``NodeProcessor`` 形式
      挂在图上 —— 读取 Node、改写 Node metadata（AST Pass 语义）。
    - **生命周期只是注解**：RAW → SEMANTIC → TRANSLATION → RENDER
      四个阶段修改的是 **同一个 Node**（``metadata[STAGE_KEY]``），
      绝不复制数据到第二份 IR；跨进程/持久化用 ``IRBuilder`` 产出
      ``DocumentIR`` 作为**序列化视图**，而不是另一套数据源。

Reserved metadata keys（唯一 schema —— 新增领域只加 semantic 子键，
不新增 IR）：

    STAGE_KEY      = "v3.stage"           # NodeStage
    SEMANTIC_KEY   = "semantic"           # 类型专属语义明细 dict
    POLICY_KEY     = "policy"             # translate / preserve / overlay / ...
    ORIGINAL_KEY   = "original_text"
    TRANSLATED_KEY = "translated_text"
    RENDER_KEY     = "render"             # 渲染阶段（font/size/layer/position）
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from enum import Enum
from types import SimpleNamespace
from typing import Dict, List, Optional, Tuple

from pdf2zh.v3.graph import DocumentGraph, DocumentNode, Edge, EdgeType, NodeType
from pdf2zh.v3.document_ir import IRBuilder, SemanticRole
from pdf2zh.v3.content_preservation import classify_node
from pdf2zh.v3.toc_semantics import TOCTranslationPolicy, parse_toc_entry

# ── 生命周期与保留键 ──────────────────────────────────────────────────────


class NodeStage(Enum):
    """节点生命周期阶段（同一 Node 的注解状态，非多份 IR）。"""

    RAW = "raw"
    SEMANTIC = "semantic"
    TRANSLATION = "translation"
    RENDER = "render"


STAGE_KEY = "v3.stage"
SEMANTIC_KEY = "semantic"
POLICY_KEY = "policy"
ORIGINAL_KEY = "original_text"
TRANSLATED_KEY = "translated_text"
RENDER_KEY = "render"


def get_semantic(node: DocumentNode) -> dict:
    """读（必要时初始化）节点的 semantic 明细桶 —— 唯一的类型细节入口。"""
    return node.metadata.setdefault(SEMANTIC_KEY, {})


def set_policy(node: DocumentNode, policy: str, reason: str = "") -> None:
    node.metadata[POLICY_KEY] = policy
    if reason:
        node.metadata.setdefault("policy_reasons", []).append(reason)


# ── Processor 抽象 ────────────────────────────────────────────────────────


class NodeProcessor(ABC):
    """AST-Pass / ECS-System：读取 Node，改写 Node metadata。

    - ``stages``：本 processor 参与的生命周期阶段。
    - ``target_types``：关注的节点类型（None = 全部）。
    - ``process``：逐节点执行，禁止抛错（抛错由 pipeline 捕获记录）。
    - ``finalize``：整图后处理（如跨节点连边），默认空操作。
    """

    name: str = "node_processor"
    stages: Tuple[NodeStage, ...] = (NodeStage.SEMANTIC,)
    target_types: Optional[Tuple[NodeType, ...]] = None

    def matches(self, node: DocumentNode) -> bool:
        return self.target_types is None or node.node_type in self.target_types

    @abstractmethod
    def process(self, node: DocumentNode, graph: DocumentGraph) -> None: ...

    def finalize(self, graph: DocumentGraph) -> None:  # 默认空操作
        return None


class ProcessorRegistry:
    """处理器注册表：按阶段调度、按类型过滤。"""

    def __init__(self, processors: Optional[List[NodeProcessor]] = None) -> None:
        self._processors: List[NodeProcessor] = list(processors or [])

    def register(self, processor: NodeProcessor) -> "ProcessorRegistry":
        self._processors.append(processor)
        return self

    def all(self) -> List[NodeProcessor]:
        return list(self._processors)

    def for_stage(self, stage: NodeStage) -> List[NodeProcessor]:
        return [p for p in self._processors if stage in p.stages]


def default_processor_registry() -> ProcessorRegistry:
    """默认注册表：RAW 先语义化类型，SEMANTIC 再产出翻译/渲染策略。

    顺序即依赖：TOC 先行（避免公式处理器误命中目录行），
    ContentPolicy 不覆盖已由更专门处理器写下的策略。
    """
    return ProcessorRegistry(
        [
            TOCSemanticProcessor(),
            FormulaNodeProcessor(),
            CodeNodeProcessor(),
            ImageTranslationProcessor(),
            TableNodeProcessor(),
            ReferenceNodeProcessor(),
            ContentPolicyProcessor(),
            CaptionNodeProcessor(),
        ]
    )


# ── 目录语义 Processor（V8.7 引擎的 Pass 化封装） ────────────────────────


class TOCSemanticProcessor(NodeProcessor):
    """把目录行段落语义化为 TOC_ENTRY 节点（复用 toc_semantics 纯逻辑）。"""

    name = "toc_semantic"
    stages = (NodeStage.RAW,)
    target_types = (NodeType.PARAGRAPH, NodeType.UNKNOWN)

    def __init__(self, lang_out: str = "zh-CN") -> None:
        self.lang_out = lang_out
        self._policy = TOCTranslationPolicy(lang_out)

    def process(self, node: DocumentNode, graph: DocumentGraph) -> None:
        entry = parse_toc_entry(node.text)
        if not entry.matched:
            return
        node.node_type = NodeType.TOC_ENTRY
        get_semantic(node)["toc"] = entry.to_dict()
        decision = self._policy.decide(entry)
        set_policy(
            node,
            "template_local" if decision["local_only"] else "translate_title_remainder",
            "toc_grammar",
        )


# ── 公式 Processor ────────────────────────────────────────────────────────


_FORMULA_MARKER_RE = re.compile(r"\{\s*v\d+\s*\}")


class FormulaNodeProcessor(NodeProcessor):
    """公式占位标记（``{v1}`` 等）→ FORMULA 节点；OCR/LaTeX 留待后端。"""

    name = "formula"
    stages = (NodeStage.RAW,)
    target_types = (NodeType.PARAGRAPH, NodeType.UNKNOWN)

    def process(self, node: DocumentNode, graph: DocumentGraph) -> None:
        m = _FORMULA_MARKER_RE.search(node.text or "")
        if m is None:
            return
        node.node_type = NodeType.FORMULA
        get_semantic(node)["formula"] = {"latex": "", "marker": m.group(0)}
        set_policy(node, "preserve", "formula_keep")


# ── 代码 Processor ────────────────────────────────────────────────────────


class CodeNodeProcessor(NodeProcessor):
    """CODE 节点注解（语言等）；不做代码语义识别，只挂明细。"""

    name = "code"
    stages = (NodeStage.RAW,)
    target_types = (NodeType.CODE,)

    def process(self, node: DocumentNode, graph: DocumentGraph) -> None:
        get_semantic(node)["code"] = {
            "language": node.metadata.get("language", "unknown"),
        }
        set_policy(node, "preserve", "code_keep")


# ── 图片翻译 Processor（V8.6 引擎的 Pass 化封装） ─────────────────────────


class ImageTranslationProcessor(NodeProcessor):
    """对 IMAGE 节点执行图片翻译决策链（复用 image_engine）。

    输入约定（metadata）：``pixels``（numpy 数组，走完整特征/分类/区域/决策链）
    或 ``features``（dict，仅挂明细不重算）。无像素数据时只留占位，绝不抛错。
    """

    name = "image_translation"
    stages = (NodeStage.SEMANTIC,)
    target_types = (NodeType.IMAGE,)

    def process(self, node: DocumentNode, graph: DocumentGraph) -> None:
        from pdf2zh.v3.image_engine import (
            analyze_image_bytes,
            TranslationDecisionEngine,
        )

        pixels = node.metadata.get("pixels")
        if pixels is not None:
            obj = analyze_image_bytes(
                pixels,
                object_id=node.id,
                page_num=node.page_num,
                has_alpha=bool(node.metadata.get("has_alpha", False)),
                engine=TranslationDecisionEngine(),
            )
            detail = {
                "class": obj.image_class.value,
                "confidence": obj.class_confidence,
                "features": obj.features,  # analyze_image_bytes 已序列化为 dict
                "regions": [r.to_dict() for r in obj.regions],
                "decision": obj.decision.to_dict(),
            }
            if obj.decision.translate:
                set_policy(node, "translate", "image_engine")
            elif obj.decision.render_mode.value == "overlay":
                set_policy(node, "overlay", "image_engine")
            else:
                set_policy(node, "preserve", "image_engine")
        else:
            detail = {"status": "no_pixels"}
        get_semantic(node)["image"] = detail


# ── 内容保护 Processor（V8.6 统一决策表的 Pass 化封装） ───────────────────


class ContentPolicyProcessor(NodeProcessor):
    """按语义角色产出一致处理策略（复用 content_preservation 默认表）。

    不覆盖已由更专门处理器（TOC/Formula/Code/Image）写下的 policy。
    """

    name = "content_policy"
    stages = (NodeStage.SEMANTIC,)
    target_types = None  # 全部节点

    def process(self, node: DocumentNode, graph: DocumentGraph) -> None:
        if POLICY_KEY in node.metadata:
            return  # 更专门的处理器已定夺
        role = IRBuilder.semantic_for(node.node_type)
        proxy = SimpleNamespace(semantic=role, id=node.id)
        decision = classify_node(proxy)
        set_policy(
            node,
            decision.action.value,
            decision.reasons[0] if decision.reasons else "role_default",
        )
        get_semantic(node)["preservation"] = decision.to_dict()


# ── 表格语义 Processor（V9.0 关系挂载点：栏数/表头） ──────────────────────


_CELL_SEP_RE = re.compile(r"(?:\t|\s{3,}|\|)")
_TABLE_LINE_MIN_CELLS = 2
_TABLE_MIN_LINES = 2


class TableNodeProcessor(NodeProcessor):
    """把类表格段落（多行 + 每行多格）语义化为 TABLE 节点。

    判定：≥2 行且 ≥2/3 行内存在 ≥2 个单元分隔符（tab / 3+ 空格 / 竖线）。
    只挂 ``semantic.table`` 明细与栏数/表头推断，不做单元格级解析。
    """

    name = "table_semantic"
    stages = (NodeStage.RAW,)
    target_types = (NodeType.PARAGRAPH, NodeType.UNKNOWN)

    def process(self, node: DocumentNode, graph: DocumentGraph) -> None:
        text = (node.text or "").strip()
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if len(lines) < _TABLE_MIN_LINES:
            return
        cell_counts = [
            len(_CELL_SEP_RE.split(ln.strip()))
            for ln in lines
            if _CELL_SEP_RE.search(ln)
        ]
        if not cell_counts:
            return
        multi_cell = sum(1 for c in cell_counts if c >= _TABLE_LINE_MIN_CELLS)
        if multi_cell * 3 < 2 * len(lines):
            return
        node.node_type = NodeType.TABLE
        cols = max(cell_counts)
        header = lines[0] if lines else ""
        get_semantic(node)["table"] = {
            "rows": len(lines),
            "cols": cols,
            "style": "row_major",
            "has_header": bool(header and _CELL_SEP_RE.search(header)),
        }


# ── 参考文献关系 Processor（引用/文献节） ──────────────────────────────────


_RE_CITATION_BRACKET = re.compile(r"^\[[\d\s,–\-\[\]]+\]")
_RE_BIBLIOGRAPHY_HEAD = re.compile(
    r"^\s*(?:references|bibliography|works cited|参考文献)\s*$",
    re.IGNORECASE,
)


class ReferenceNodeProcessor(NodeProcessor):
    """把引用/文献段语义化为 CITATION / REFERENCE / BIBLIOGRAPHY 节点。

    - 整行 ``[1]`` / ``[1,2]`` → CITATION（编号保留）
    - 文献节标题 → BIBLIOGRAPHY
    - finalize 把同页 CITATION 与 REFERENCE/BIBLIOGRAPHY 之间补
      ``CITATION_OF`` 关系边（渐进式挂载点）。
    """

    name = "reference_relations"
    stages = (NodeStage.RAW,)
    target_types = (NodeType.PARAGRAPH, NodeType.UNKNOWN)

    def process(self, node: DocumentNode, graph: DocumentGraph) -> None:
        text = (node.text or "").strip()
        if not text:
            return
        if _RE_BIBLIOGRAPHY_HEAD.match(text):
            node.node_type = NodeType.BIBLIOGRAPHY
            get_semantic(node)["reference"] = {"kind": "bibliography"}
            return
        if _RE_CITATION_BRACKET.match(text):
            node.node_type = NodeType.CITATION
            get_semantic(node)["reference"] = {
                "kind": "citation",
                "bracket": _RE_CITATION_BRACKET.match(text).group(0),
            }

    def finalize(self, graph: DocumentGraph) -> None:
        bibliography = [n for n in graph.nodes if n.node_type == NodeType.BIBLIOGRAPHY]
        for cite in [n for n in graph.nodes if n.node_type == NodeType.CITATION]:
            if any(
                e.edge_type == EdgeType.CITATION_OF and e.source_id == cite.id
                for e in graph.get_edges(source_id=cite.id)
            ):
                continue
            host = self._nearest_bibliography(cite, bibliography)
            if host is not None:
                graph.add_edge(Edge(cite.id, host.id, EdgeType.CITATION_OF))

    @staticmethod
    def _nearest_bibliography(cite, bibliography: List[DocumentNode]):
        best, best_gap = None, None
        for b in bibliography:
            if b.page_num != cite.page_num:
                continue
            gap = abs(b.y0 - cite.y0)
            if best_gap is None or gap < best_gap:
                best, best_gap = b, gap
        return best


# ── 题注 Processor ────────────────────────────────────────────────────────


# 题注编号模式："Fig. 1." / "Figure 3.2:" / "Table 2 –" / "图 1" / "表 2："
_RE_CAPTION_NUMBER = re.compile(
    r"^\s*(?:(?:fig(?:ure)?|tab(?:le)?|图|表|公式|equation)\.?\s*"
    r"\.?\s*)?([0-9]+(?:\.[0-9]+)*)\s*[.:、：）)\-–—]?\s*(.*)$",
    re.IGNORECASE,
)


class CaptionNodeProcessor(NodeProcessor):
    """把同页紧随图/表下方的题注链接为 CAPTION_OF（缺失时补边）。

    V1.6 补充：识别题注编号（``Fig. 1.`` / ``图 2``）写入
    ``semantic.caption.number`` —— 下游 TranslationRole.NEED_CONTEXT
    的 ``caption_number_keep`` 策略据此**保留编号、只翻描述**。
    """

    name = "caption_link"
    stages = (NodeStage.SEMANTIC,)
    target_types = None

    def process(self, node: DocumentNode, graph: DocumentGraph) -> None:
        if node.node_type != NodeType.CAPTION:
            return
        text = (node.text or "").strip()
        semantic = get_semantic(node)
        caption = dict(semantic.get("caption") or {})
        m = _RE_CAPTION_NUMBER.match(text)
        if m and m.group(1):
            caption["number"] = m.group(1)
            rest = (m.group(2) or "").strip()
            caption["title_remainder"] = rest
            caption["number_keep"] = True
        else:
            caption["number"] = ""
            caption["title_remainder"] = text
            caption["number_keep"] = False
        caption["raw"] = text
        semantic["caption"] = caption

    def _nearest_host(self, caption: DocumentNode, graph: DocumentGraph):
        best, best_gap = None, None
        for cand in graph.nodes:
            if cand.page_num != caption.page_num:
                continue
            if cand.node_type not in (NodeType.FIGURE, NodeType.TABLE):
                continue
            if cand.y0 > caption.y0 + 1e-6:  # 宿主须在题注上方或同行（图在题注之上）
                continue
            gap = caption.y0 - cand.y0
            if best_gap is None or gap < best_gap:
                best, best_gap = cand, gap
        return best

    def finalize(self, graph: DocumentGraph) -> None:
        for caption in [n for n in graph.nodes if n.node_type == NodeType.CAPTION]:
            if any(
                e.edge_type == EdgeType.CAPTION_OF and e.source_id == caption.id
                for e in graph.get_edges(source_id=caption.id)
            ):
                continue
            host = self._nearest_host(caption, graph)
            if host is not None:
                graph.add_edge(Edge(caption.id, host.id, EdgeType.CAPTION_OF))


__all__ = [
    "NodeStage",
    "STAGE_KEY",
    "SEMANTIC_KEY",
    "POLICY_KEY",
    "ORIGINAL_KEY",
    "TRANSLATED_KEY",
    "RENDER_KEY",
    "get_semantic",
    "set_policy",
    "NodeProcessor",
    "ProcessorRegistry",
    "default_processor_registry",
    "TOCSemanticProcessor",
    "FormulaNodeProcessor",
    "CodeNodeProcessor",
    "ImageTranslationProcessor",
    "ContentPolicyProcessor",
    "CaptionNodeProcessor",
]
