"""Module: SemanticGraph — Phase 4.1 文档语义图（不再是树）。

文档不是树：Figure 被 Caption 引用、正文 mention 公式/图/表、段落属于
章节。本模块在 DocumentModel 之上重建语义层：

    sections[]         （Heading → Section → members 归属）
    belongs_to 边      （member → section）
    mentions 边        （"see Figure 3" → figure/caption 块）

纯逻辑、只写 metadata/relations；投影到 v3 DocumentGraph 时 belongs_to→
CONTAINS、mentions→REFERENCE（既有 EdgeType，不新增 IR）。
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from pdf2zh.v3.document_model import (
    DocumentModel, Relation, block_id,
)

REL_BELONGS_TO = "belongs_to"
REL_MENTIONS = "mentions"

_RE_SECTION_NUM = re.compile(r"^\s*(\d+(?:\.\d+)*)\s*([\s\S]*)$")
_RE_MENTION = re.compile(
    r"\b(?:Fig(?:ure)?|FIG\.?|图)\s*\.?\s*(\d+)|"
    r"\b(?:Table|Tab\.?|表)\s*\.?\s*(\d+)|"
    r"\b(?:Eq(?:uation)?\.?|公式)\s*\.?\s*\(?\s*(\d+)\s*\)?|"
    r"\b(?:Section|Sec\.?|§)\s*\.?\s*(\d+(?:\.\d+)*)",
    re.IGNORECASE)


def section_number(text: str) -> Optional[str]:
    m = _RE_SECTION_NUM.match(text or "")
    return m.group(1) if m else None


def build_sections(model: DocumentModel) -> List[dict]:
    """从 heading 块重建 Section（编号 + 标题 + 成员块，按阅读序切分）。"""
    sections: List[dict] = []
    current: Optional[dict] = None
    for page in model.pages:
        pno = page.page_num
        for i, block in enumerate(page.blocks):
            bid = block_id(pno, i)
            if block.kind == "heading" or \
                    block.metadata.get("role") == "heading":
                num = section_number(block.text or "")
                current = {
                    "section_id": bid,
                    "page": pno,
                    "number": num or "",
                    "title": (block.text or "").strip(),
                    "level": int(block.metadata.get("heading_level", 1) or 1),
                    "members": [],
                }
                sections.append(current)
                block.metadata["section_id"] = bid
                continue
            if current is not None and block.kind not in (
                    "header", "footer", "toc"):
                current["members"].append(bid)
                block.metadata["section_id"] = current["section_id"]
    model.metadata["sections"] = sections
    return sections


def detect_mentions(text: str) -> List[dict]:
    """正文中的交叉引用："see Figure 3" / "Table 2" / "Eq.(4)" / "§5.2"。"""
    out: List[dict] = []
    for m in _RE_MENTION.finditer(text or ""):
        if m.group(1):
            out.append({"target_type": "figure", "id": m.group(1),
                        "raw": m.group(0)})
        elif m.group(2):
            out.append({"target_type": "table", "id": m.group(2),
                        "raw": m.group(0)})
        elif m.group(3):
            out.append({"target_type": "equation", "id": m.group(3),
                        "raw": m.group(0)})
        elif m.group(4):
            out.append({"target_type": "section", "id": m.group(4),
                        "raw": m.group(0)})
    return out


_RE_CAPTION_NUM = re.compile(
    r"^\s*(?:Fig(?:ure)?\.?|Table|Tab\.?|图|表|Eq(?:uation)?\.?)\s*\.?\s*"
    r"(\d+(?:\.\d+)*)", re.IGNORECASE)


def _caption_number(text: str) -> Optional[str]:
    m = _RE_CAPTION_NUM.match(text or "")
    return m.group(1) if m else None


def _target_index(model: DocumentModel, page_num: int,
                  target_type: str, target_id: str) -> Optional[str]:
    """同页内按编号解析 mention 的目标块（caption/figure/table/equation）。"""
    kind_map = {"figure": ("caption", "figure"),
                "table": ("table", "caption"),
                "equation": ("formula", "equation")}
    kinds = kind_map.get(target_type, (target_type,))
    for page in model.pages:
        if page.page_num != page_num:
            continue
        for i, block in enumerate(page.blocks):
            if block.kind not in kinds:
                continue
            num = _caption_number(block.text or "")
            if num == target_id:
                return block_id(page_num, i)
    return None


def resolve_mentions(model: DocumentModel) -> int:
    """解析全部 mention → 目标块，写 relations（mentions 边）。返回边数。"""
    added = 0
    for page in model.pages:
        pno = page.page_num
        for i, block in enumerate(page.blocks):
            for mention in detect_mentions(block.text or ""):
                target = _target_index(model, pno, mention["target_type"],
                                       mention["id"])
                if target is None:
                    continue
                model.relations.append(Relation(
                    REL_MENTIONS, block_id(pno, i), target))
                block.metadata.setdefault("mentions", []).append({
                    "target_type": mention["target_type"],
                    "target_id": mention["id"],
                    "target": target,
                })
                added += 1
    return added


def build_semantic_relations(model: DocumentModel) -> dict:
    """Phase 4.1 入口：sections + belongs_to + mentions。"""
    sections = build_sections(model)
    added = 0
    for sec in sections:
        for member in sec["members"]:
            model.relations.append(Relation(REL_BELONGS_TO, member,
                                            sec["section_id"]))
            added += 1
    mentions = resolve_mentions(model)
    model.metadata["semantic_graph"] = {
        "sections": len(sections),
        "belongs_to": added,
        "mentions": mentions,
    }
    return model.metadata["semantic_graph"]


__all__ = [
    "REL_BELONGS_TO", "REL_MENTIONS",
    "section_number", "build_sections", "detect_mentions",
    "resolve_mentions", "build_semantic_relations",
]