"""Module: References — Phase 4.3 引用与交叉链接引擎。

正文 "see Figure 5" → Reference 节点 {target_type, id}；Figure 编号变化时
``renumber_references`` 重写正文与译文的引用文本，避免链接错位。

    resolve_references(model)  → 每块 mentions 列表（含解析结果）
    renumber_references(model, mapping)  → 按 (type, old) → new 重写
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from pdf2zh.v3.semantic_graph import detect_mentions, resolve_mentions

# 按类型重写引用文本（"Figure 5" → "Figure 3"，"图5" → "图3"）；
# group(1)=head（Fig./图…），group(2)=编号；\b 只用于英文分支（CJK 无边界）
_RE_REF_TEXT = {
    "figure": re.compile(r"(\bFig(?:ure)?\.?|图)\s*\.?\s*(\d+)", re.IGNORECASE),
    "table": re.compile(r"(\bTab(?:le)?\.?|表)\s*\.?\s*(\d+)", re.IGNORECASE),
    "equation": re.compile(
        r"(\bEq(?:uation)?\.?|公式)\s*\.?\s*\(?\s*" r"(\d+)\s*\)?", re.IGNORECASE
    ),
    "section": re.compile(
        r"(\bSection|Sec\.?|§|第)\s*\.?\s*" r"(\d+(?:\.\d+)*)", re.IGNORECASE
    ),
}


def resolve_references(model) -> Dict[str, List[dict]]:
    """逐块解析引用（mention 检测 + 同页目标解析），返回 {block_id: refs}。"""
    resolve_mentions(model)
    out: Dict[str, List[dict]] = {}
    for page in model.pages:
        pno = page.page_num
        for i, block in enumerate(page.blocks):
            bid = f"p{pno}_{i}"
            refs = block.metadata.get("mentions", []) or []
            if refs:
                out[bid] = [
                    {
                        "target_type": r["target_type"],
                        "target_id": r["target_id"],
                        "target": r.get("target"),
                        "raw": r.get("raw", ""),
                    }
                    for r in refs
                ]
    model.metadata["references"] = {bid: refs for bid, refs in out.items()}
    return out


def _rewrite(text: str, ref_type: str, old: str, new: str) -> str:
    def repl(m: re.Match):
        head = m.group(1)
        sep = "" if head and not head[0].isascii() else " "
        return f"{head}{sep}{new}"

    return _RE_REF_TEXT[ref_type].sub(repl, text or "")


def renumber_references(
    model, mapping: Dict[Tuple[str, str], str], rewrite_translated: bool = True
) -> int:
    """按 {(target_type, old_id): new_id} 重写引用文本。返回改写数。

    同时作用于 block.text 与（rewrite_translated=True 时）译后文本，
    保证「见图5」随编号变化而不错位。
    """
    count = 0
    for page in model.pages:
        for block in page.blocks:
            for (ref_type, old_id), new_id in (mapping or {}).items():
                if ref_type not in _RE_REF_TEXT:
                    continue
                if _RE_REF_TEXT[ref_type].search(block.text or "") and _ref_has_id(
                    block.text, ref_type, old_id
                ):
                    block.text = _rewrite(block.text, ref_type, old_id, new_id)
                    count += 1
                    if rewrite_translated and block.metadata.get("translated"):
                        block.metadata["translated"] = _rewrite(
                            block.metadata["translated"], ref_type, old_id, new_id
                        )
    return count


def _ref_has_id(text: str, ref_type: str, old_id: str) -> bool:
    for m in _RE_REF_TEXT[ref_type].finditer(text or ""):
        if m.group(2) == old_id:
            return True
    return False


__all__ = ["resolve_references", "renumber_references"]
