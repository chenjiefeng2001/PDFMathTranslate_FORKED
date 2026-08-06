"""Module: DocumentModel — 文档统一模型（V11：多页树 + Relations + 图桥接）。

``canonical_page`` 给出单页树（Page → Block → Line → Span → Glyph）。
本模块把它升级为**整份文档的唯一模型**：

    DocumentModel
     ├── pages[]          （PageModel 树）
     ├── relations[]      （FOLLOWS 阅读序 / TOC_CHILD_OF 层级 / CAPTION_OF）
     └── metadata         （page_order / 统计 / 标注摘要）

所有后续 Pass（TOC / Formula / Role / Translation / Render）只写各节点的
``metadata``，不再各自重新解析页面。模型是唯一数据源；需要与既有 v3
生态对接时，``to_graph`` 把它投影为 ``DocumentGraph``（节点 = Block，
边 = Relations），再经 ``view_as_ir`` 得到序列化视图 —— **不新增第二套 IR**。

    LTChar 流
       │
       ▼
    build_document_model  （逐页：结构恢复 + 标注 Pass）
       │
       ├── to_dict()              JSON 可序列化（诊断/落盘）
       ├── annotate_*             TOC/公式/角色/翻译/渲染 只写 metadata
       └── to_graph()  →  DocumentGraph  →  view_as_ir（既有生态）
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

log = logging.getLogger(__name__)

# Relations（与 v3.graph.EdgeType 对齐的子集）
REL_FOLLOWS = "follows"
REL_TOC_CHILD_OF = "contains"
REL_CAPTION_OF = "caption_of"

# kind → v3 NodeType 映射（to_graph 用）
_KIND_TO_NODE_TYPE = {
    "toc": "toc_entry",
    "heading": "heading",
    "caption": "caption",
    "footnote": "footnote",
    "formula": "formula",
    "citation": "citation",
    "header": "header",
    "footer": "footer",
    "paragraph": "paragraph",
}


def block_id(page_num: int, index: int) -> str:
    return f"p{page_num}_{index}"


@dataclass
class Relation:
    type: str = REL_FOLLOWS
    source: str = ""
    target: str = ""

    def to_dict(self) -> dict:
        return {"type": self.type, "source": self.source,
                "target": self.target}


@dataclass
class DocumentModel:
    pages: List[object] = field(default_factory=list)
    relations: List[Relation] = field(default_factory=list)
    metadata: Dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "pages": [p.to_dict() for p in self.pages],
            "relations": [r.to_dict() for r in self.relations],
            "metadata": dict(self.metadata),
            "stats": self.stats(),
        }

    def stats(self) -> dict:
        glyphs = sum(p.stats().get("glyphs", 0) for p in self.pages)
        return {
            "pages": len(self.pages),
            "blocks": sum(len(p.blocks) for p in self.pages),
            "glyphs": glyphs,
            "relations": len(self.relations),
            "page_order": self.metadata.get("page_order", []),
        }

    # ── 追加页 + 页内 Relations ──────────────────────────────────────

    def add_page(self, page_model) -> None:
        """追加一页；重建页内/页间 Relations 与 page_order。"""
        self.pages.append(page_model)
        self._rebuild_page_relations(page_model)
        order = sorted(p.page_num for p in self.pages)
        self.metadata["page_order"] = order

    def _rebuild_page_relations(self, page_model) -> None:
        pno = page_model.page_num
        blocks = page_model.blocks
        # 1) 阅读序：FOLLOWS 链
        for i in range(1, len(blocks)):
            self.relations.append(Relation(
                REL_FOLLOWS, block_id(pno, i - 1), block_id(pno, i)))
        # 2) TOC 层级：按块 metadata.toc_number 前缀包含
        toc = [(i, b) for i, b in enumerate(blocks)
               if b.metadata.get("kind") == "toc" and b.metadata.get("toc_number")]
        for i, (idx, b) in enumerate(toc):
            parent = None
            for j in range(i - 1, -1, -1):
                pnum = toc[j][1].metadata["toc_number"]
                if _number_prefix(pnum, b.metadata["toc_number"]):
                    parent = toc[j][0]
                    break
            if parent is not None:
                self.relations.append(Relation(
                    REL_TOC_CHILD_OF, block_id(pno, idx), block_id(pno, parent)))
        # 3) 题注 → 宿主（同页最近的 figure/table 块，best-effort）
        for idx, b in enumerate(blocks):
            if b.metadata.get("role") != "caption" and b.kind != "caption":
                continue
            host = None
            for j in range(idx - 1, -1, -1):
                if blocks[j].kind in ("figure", "table") or \
                        blocks[j].metadata.get("role") in ("figure", "table"):
                    host = j
                    break
            if host is not None:
                self.relations.append(Relation(
                    REL_CAPTION_OF, block_id(pno, idx), block_id(pno, host)))

    # ── 投影到既有 v3 生态 ───────────────────────────────────────────

    def to_graph(self):
        """把模型投影为 DocumentGraph（Block → DocumentNode，Relation → Edge）。

        供既有 Processor / IR 视图（view_as_ir）消费 —— 唯一数据源，不复制。
        """
        from pdf2zh.v3.graph import DocumentGraph, DocumentNode, Edge, EdgeType
        g = DocumentGraph()
        index_of: Dict[tuple, int] = {}
        for page in self.pages:
            pno = page.page_num
            for i, b in enumerate(page.blocks):
                nid = block_id(pno, i)
                index_of[(pno, i)] = nid
                ntype = _KIND_TO_NODE_TYPE.get(
                    b.kind, "paragraph") if b.kind != "paragraph" \
                    else ("paragraph" if not b.metadata.get("role")
                          else _KIND_TO_NODE_TYPE.get(
                              b.metadata["role"], "paragraph"))
                g.add_node(DocumentNode(
                    id=nid,
                    node_type=ntype,
                    bbox=b.bbox,
                    text=b.text,
                    page_num=pno,
                    font_size=b.font_size or 0.0,
                    metadata=dict(b.metadata),
                ))
        edge_map = {
            REL_FOLLOWS: EdgeType.FOLLOWS,
            REL_TOC_CHILD_OF: EdgeType.CONTAINS,
            REL_CAPTION_OF: EdgeType.CAPTION_OF,
        }
        # Phase 4.1 语义边：belongs_to → CONTAINS，mentions → REFERENCE
        from pdf2zh.v3.semantic_graph import REL_BELONGS_TO, REL_MENTIONS
        edge_map.update({
            REL_BELONGS_TO: EdgeType.CONTAINS,
            REL_MENTIONS: EdgeType.REFERENCE,
        })
        for r in self.relations:
            etype = edge_map.get(r.type)
            if etype is None:
                continue
            g.add_edge(Edge(r.source, r.target, etype))
        return g


def _number_prefix(a: str, b: str) -> bool:
    """a 是否为 b 的编号前缀（5.2 是 5.2.1 的前缀）。"""
    sa, sb = a.split("."), b.split(".")
    return len(sa) < len(sb) and sb[:len(sa)] == sa


class _NodeProxy:
    """BlockModel → analyzer._RuleParagraphAdapter 所需的最小节点视图。"""

    def __init__(self, block):
        self.text = block.text or ""
        self.bbox = block.bbox
        self.font_size = block.font_size
        self.metadata = block.metadata


# ── 标注 Pass（文档级，只写 metadata） ───────────────────────────────────


def annotate_roles(page, classifier=None) -> int:
    """Role Pass：块级角色（heading/caption/formula/footnote/toc/citation…）。

    复用 structure.StructureClassifier 规则流（与 analyzer 融合同源），
    结果写 ``Block.kind``（映射角色）与 ``metadata.role/role_confidence``。
    返回标角色块数。
    """
    try:
        from pdf2zh.v3.analyzer import _RuleParagraphAdapter
        from pdf2zh.v3.structure import StructureClassifier
    except Exception:  # noqa: BLE001
        return 0
    classifier = classifier or StructureClassifier()
    body_size = _estimate_body_size(page)
    type_map = {
        "heading": "heading", "caption": "caption", "footnote": "footnote",
        "formula": "formula", "citation": "citation",
        "toc_entry": "toc", "header": "header", "footer": "footer",
    }
    hits = 0
    for block in page.blocks:
        if block.kind != "paragraph" or not (block.text or "").strip():
            continue
        try:
            classified = classifier.classify_paragraph(
                _RuleParagraphAdapter(_NodeProxy(block)), page=None,
                body_font_size=body_size)
        except Exception:  # noqa: BLE001
            continue
        role = classified.role
        conf = classified.confidence
        block.metadata["role"] = role.value
        block.metadata["role_confidence"] = round(conf, 4)
        mapped = type_map.get(role.value)
        if mapped is not None and conf >= 0.65:
            block.kind = mapped
            block.metadata["kind"] = mapped
            hits += 1
    return hits


def _estimate_body_size(page) -> float:
    sizes = [b.font_size for b in page.blocks
             if b.font_size and (b.text or "").strip()]
    if not sizes:
        return 12.0
    sizes.sort()
    return sizes[len(sizes) // 2]


def annotate_translation(page, translations: Dict[str, str]) -> int:
    """Translation Pass：把译后文本写进 Block.metadata.translated。

    ``translations`` 为 {block_id 或 "p{page}_{i}": translated}。
    返回标注块数。
    """
    hits = 0
    for i, block in enumerate(page.blocks):
        nid = block_id(page.page_num, i)
        t = (translations or {}).get(nid) or (translations or {}).get(str(i))
        if t is None:
            continue
        block.metadata["translated"] = t
        block.metadata["translated_same"] = (t == (block.text or ""))
        hits += 1
    return hits


def annotate_render(page) -> int:
    """Render Pass：按 kind/role 给出渲染路径（Renderer 只读 Document）。

    路径与 render_advisor 一致：toc/header/footer → overlay；
    figure/image/table/formula → preserve_float；其余 → translate_refit。
    返回标注块数。
    """
    overlay = {"toc", "header", "footer"}
    preserve = {"figure", "image", "table", "formula", "formula_inline"}
    hits = 0
    for block in page.blocks:
        kind = block.kind
        if kind in overlay:
            path = "overlay"
        elif kind in preserve:
            path = "preserve_float"
        else:
            path = "translate_refit"
        block.metadata["render_path"] = path
        hits += 1
    return hits


# ── 构建入口 ─────────────────────────────────────────────────────────────


def build_document_model(ltpages: Sequence,
                         annotate_toc_entries: Optional[Dict[int, Sequence[dict]]] = None,
                         classifier=None) -> DocumentModel:
    """从 LTChar 流构建文档统一模型（逐页：结构恢复 + 全部标注 Pass）。

    - 每页：build_page_model（树）→ annotate_roles / annotate_formulas /
      annotate_style / annotate_toc_scan / annotate_render；
    - 可选 ``annotate_toc_entries``（{page_num: toc_dump 条目}）做 gate
      记录匹配标注（legacy 检测成功路径）；
    - 页级 Relations（FOLLOWS / TOC_CHILD_OF / CAPTION_OF）自动重建。
    """
    from pdf2zh.v3.canonical_page import (
        annotate_formulas, annotate_style, annotate_toc, annotate_toc_scan,
        build_page_model,
    )
    model = DocumentModel()
    for ltpage in ltpages or []:
        pno = getattr(ltpage, "pageid", 0)
        try:
            page = build_page_model(ltpage, page_num=pno)
        except Exception as e:  # noqa: BLE001
            log.debug("document_model: page %s failed: %s", pno, e)
            continue
        annotate_roles(page, classifier=classifier)
        annotate_formulas(page)
        annotate_style(page)
        try:
            from pdf2zh.v3.toc_analyzer import split_toc_blocks
            split_toc_blocks(page)
        except Exception as e:  # noqa: BLE001
            log.debug("document_model: split_toc_blocks page %s failed: %s",
                      pno, e)
        annotate_toc_scan(page)
        for entry in (annotate_toc_entries or {}).get(pno, []) or []:
            annotate_toc(page, [entry])
        annotate_render(page)
        model.add_page(page)
    return model


# ── 模型消费：Translation / Render Plan / TOC 记录 ───────────────────────


_KEEP_KINDS = frozenset({"formula", "figure", "image", "table",
                         "header", "footer"})


def translate_document(model: DocumentModel, translate_fn,
                       lang_out: str = "zh-CN") -> dict:
    """Translation Pass：按翻译策略（TranslationPolicyPass 产出）翻译。

    - policy.translate=False（formula/figure/table/code/header/footer…）
      → 原样保留；
    - policy.partial（toc：仅描述标题；caption：保留编号）→ 只翻
      ``source_text``；
    - 无策略时按 kind 兜底。``translate_fn(text) -> str`` 缺省恒等。
    返回统计 {translated, preserved, skipped, toc_translated}。
    """
    stats = {"translated": 0, "preserved": 0, "skipped": 0,
             "toc_translated": 0}
    for page in model.pages:
        for i, block in enumerate(page.blocks):
            text = (block.text or "").strip()
            if not text:
                stats["skipped"] += 1
                continue
            pol = block.metadata.get("translation_policy") or {}
            if pol.get("translate") is False or block.kind in _KEEP_KINDS:
                block.metadata["translated"] = text
                block.metadata["translated_same"] = True
                block.metadata["translate"] = False
                stats["preserved"] += 1
                continue
            src = pol.get("source_text") or text
            if not src.strip():
                src = text
            translated = src
            if translate_fn is not None:
                try:
                    translated = translate_fn(src) or src
                except Exception as e:  # noqa: BLE001
                    log.debug("translate_document failed %s: %s",
                              block_id(page.page_num, i), e)
                    translated = src
            block.metadata["translated"] = translated
            block.metadata["translated_same"] = (translated == src)
            block.metadata["translate"] = True
            stats["translated"] += 1
            if block.kind == "toc":
                stats["toc_translated"] += 1
    model.metadata["translation_stats"] = dict(stats)
    return stats


def render_plan_from_model(model: DocumentModel) -> List[dict]:
    """Render Plan：为每个 Block 给出渲染决策（Renderer 只读 Document）。

    每块输出 {block_id, page, kind, text, translated, render_path,
    src_box, dst_box, font_size}。dst_box 初始等于源 bbox（后续由
    RenderTakeover 的 shift/block 决策修正）。纯数据，无 I/O。
    """
    plan: List[dict] = []
    for page in model.pages:
        pno = page.page_num
        for i, block in enumerate(page.blocks):
            plan.append({
                "block_id": block_id(pno, i),
                "page": pno,
                "kind": block.kind,
                "text": block.text,
                "translated": block.metadata.get("translated", block.text),
                "render_path": block.metadata.get(
                    "render_path", "translate_refit"),
                "src_box": [round(v, 2) for v in block.bbox],
                "dst_box": [round(v, 2) for v in block.bbox],
                "font_size": round(block.font_size, 2) or 12.0,
            })
    return plan


def toc_records_from_model(model: DocumentModel) -> List[dict]:
    """Document Tree → TOC IR 记录（与 toc_to_ir_records schema 一致）。

    从模型的 toc 块（toc_number/toc_title/toc_page/toc_scan/…）产出
    {raw, kind, level, number, title, page, leader, matched,
    title_remainder, translated_title, page_num, block_id}。
    """
    records: List[dict] = []
    for page in model.pages:
        pno = page.page_num
        for i, block in enumerate(page.blocks):
            md = block.metadata
            if md.get("kind") != "toc" or not md.get("toc_number"):
                continue
            records.append({
                "raw": block.text,
                "kind": "section" if "." in md["toc_number"] else "chapter",
                "level": md["toc_number"].count(".") + 1,
                "number": md["toc_number"],
                "title": md.get("toc_title", ""),
                "page": md.get("toc_page", ""),
                "leader": "",
                "matched": True,
                "title_remainder": md.get("toc_title", ""),
                "translated_title": md.get("translated", ""),
                "page_num": pno,
                "block_id": block_id(pno, i),
            })
    return records


def annotate_translation_from_records(page, records: Sequence[dict]) -> int:
    """按文本匹配把 gate 记录的译后文本写进块 metadata（best-effort）。

    ``records`` 为 ``_gate_records``（text/translated）。gate 文本可能是
    剥离后的 TOC 余量（converter 把号段拆走）—— 兼容：块为 toc 且余量
    等于块内 ``toc_title`` 时也标注。返回标注块数。
    """
    hits = 0
    for rec in records or []:
        src = str(rec.get("text", "")).strip()
        dst = str(rec.get("translated", "") or src)
        if not src:
            continue
        for i, block in enumerate(page.blocks):
            block_text = (block.text or "").strip()
            toc_title = str(block.metadata.get("toc_title", "")).strip()
            matched = (block_text == src or
                       (src and block_text.endswith(src)) or
                       (toc_title and (src == toc_title or src in toc_title)))
            if not matched:
                continue
            block.metadata["translated"] = dst
            block.metadata["translated_same"] = (dst == src)
            block.metadata["translate"] = True
            hits += 1
            break
    return hits


__all__ = [
    "REL_FOLLOWS", "REL_TOC_CHILD_OF", "REL_CAPTION_OF",
    "block_id", "Relation", "DocumentModel",
    "annotate_roles", "annotate_translation", "annotate_render",
    "build_document_model", "_number_prefix",
    "translate_document", "render_plan_from_model",
    "toc_records_from_model", "annotate_translation_from_records",
]