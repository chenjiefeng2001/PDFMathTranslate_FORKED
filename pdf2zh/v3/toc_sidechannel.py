"""TOC Document-Model side-channel — Compter 6B.

把 semantic 的视觉 TOC（``pdf2zh.semantic.toc_parser.TOCNode`` / ``TOCEntryNode``）
正式接进 v3 统一模型（``document_model``），同时保持 strangulation gate：
所有 TOC 编排逻辑定义在这里（与 ``list_sidechannel`` 同构），converter 不膨胀。

职责：
- :func:`entry_to_dict`   — TOCEntryNode → JSON-safe 条目 dict（几何/metadata 原样，
  不重新计算）。
- :func:`translate_toc_entries` — 逐条：**只有 title 进 translator**。printing
  page number / numbering prefix / dot leader / destination / 几何 全部 PRESERVE。
  译后 title 写 ``translated_title``，toc_number/title/leader/page 保留。
- :func:`resolve_toc_headings` — 轻量 TOC→Heading 关联：normalized title +
  text similarity + page relationship + level。允许失败（heading_ref=None），
  失败绝不阻塞翻译/渲染。
- :func:`attach_toc_entries` — 把条目 dict 写到某 Block.metadata["toc_entries"]，
  并把该块标为 kind="toc"（正式 semantic block，而非 debug-only）。
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Callable, Sequence

from pdf2zh.v3.toc_semantics import parse_toc_entry

__all__ = [
    "entry_to_dict",
    "translate_toc_entries",
    "resolve_toc_headings",
    "attach_toc_entries",
    "entry_clean_title",
]


def entry_to_dict(e) -> dict:
    """TOCEntryNode → JSON-safe dict（几何不重新计算，直接抄节点）。"""
    return {
        "title": e.title,  # 原始标题（含开头编号/结构词）
        "number": "",
        "title_only": entry_clean_title(e.title),
        "level": int(e.level or 0),
        "page_number": e.page_number,
        "destination_page": e.destination_page,
        "indent": round(float(e.indent or 0.0), 1),
        "title_x": round(float(e.title_x or 0.0), 1),
        "page_x": round(float(e.page_x or 0.0), 1),
        "dot_leader": e.dot_leader,
        "leader_present": bool(e.leader_present),
        "continuation": list(e.continuation or []),
        "bbox": [
            round(float(v), 1) for v in (getattr(e, "bbox", None) or (0, 0, 0, 0))
        ],
        "heading_ref": None,
        "translated_title": "",
    }


#: 开头编号/结构词剥离正则（把 "2.1 Dataset" → "Dataset"）。
_NUMBER_LEAD_RE = re.compile(
    r"^\s*(?:(?:\d+(?:\.\d+)*|[ivxlcdmIVXLCDM]{1,4})"
    r"[\s.、:：)）.．]*|第\s*[\d零一二三四五六七八九十百千万]+\s*[章节篇部卷]\s*)?"
    r"(.*)$"
)


def entry_clean_title(title: str) -> str:
    """去掉条目标题的开头编号/结构词，仅返回可翻译的描述性标题。

    无编号时原样返回；剥离失败时保守返回原文（translator 收到的不可能是
    页码 —— 页码从不进 title）。
    """
    if not title:
        return ""
    return (_NUMBER_LEAD_RE.match(title).group(1) or "").strip()


def _entry_translation_split(title: str) -> tuple[str, str]:
    """把条目标题拆成 (preserve 前缀, 可翻译 title)。

    用 ``toc_semantics.parse_toc_entry`` 结构化：对已命中的条目，其
    ``entry.title``（剩余标题）在原文中出现的位置之前都是 PRESERVE 前缀
    （编号/分隔符）。PLAIN（未命中）→ 前缀为空、整条 title 可翻译。
    """
    text = (title or "").strip()
    if not text:
        return "", ""
    se = parse_toc_entry(text)
    remainder = (se.title or "").strip()
    if remainder:
        idx = text.find(remainder)
        if idx >= 0:
            return text[:idx], remainder
    return "", text


def translate_toc_entries(
    entries: Sequence[dict],
    translate_fn: Callable[[str], str] | None,
) -> list[dict]:
    """逐条译 TOC：**只有 title_only 进 translator**，其余字段原样保留。

    返回与输入同序的 dict 列表，每条多出 ``number`` / ``title_only`` /
    ``translated_title`` / ``translated``（构成后的可渲染标题 = 前缀 + 译文）。
    ``page_number`` / ``dot_leader`` / ``destination_page`` / ``leader_present``
    / 几何一律不送 translator，也不被改写。
    """
    out: list[dict] = []
    for raw in entries or []:
        d = dict(raw)
        title = d.get("title") or ""
        number, title_only = _entry_translation_split(title)
        d["number"] = number.strip()
        d["title_only"] = title_only
        d["translated_title"] = ""
        if title_only and translate_fn is not None:
            try:
                d["translated_title"] = translate_fn(title_only) or title_only
            except Exception:  # noqa: BLE001 -- 单条 TOC 翻译失败不影响其它
                d["translated_title"] = title_only
        d["translated"] = _compose(number, d["translated_title"])
        out.append(d)
    return out


def _compose(number: str, translated_title: str) -> str:
    prefix = (number or "").rstrip()
    title = (translated_title or "").strip()
    if prefix and title:
        return f"{prefix} {title}"
    if not title:
        return prefix
    return title


def _normalize(s: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", (s or "").lower())


def resolve_toc_headings(
    entries: Sequence[dict],
    headings: Sequence[dict],
    *,
    threshold: float = 0.62,
) -> dict[int, str]:
    """轻量 TOCEntry → Heading 关联（best-effort，允许失败）。

    ``headings`` 为 ``[{id, title, page_num, level}]``（来自模型中的 heading 块）。
    匹配依据：normalized title 精确 / 互为子串 / difflib 相似度 ≥ 阈值，
    page relationship 与 level 一致性做加分项。返回 ``{entry_idx: heading_id}``；
    未匹配的条目不出现（调用方保持 heading_ref=None）。
    """
    result: dict[int, str] = {}
    for idx, e in enumerate(entries or []):
        et = _normalize(e.get("title_only") or e.get("title") or "")
        if not et:
            continue
        best_id, best = None, threshold
        for h in headings or []:
            ht = _normalize(h.get("title") or "")
            if not ht:
                continue
            score = 0.0
            if et == ht:
                score = 100.0
            elif et in ht or ht in et:
                score = 70.0
            else:
                ratio = SequenceMatcher(None, et, ht).ratio()
                if ratio >= threshold:
                    score = ratio
            if score <= 0:
                continue
            # page relationship / level 一致性加分（仅作 tie-break）
            try:
                if str(h.get("page_num")) == str(e.get("page_number")):
                    score += 15.0
            except (TypeError, ValueError):
                pass
            if int(h.get("level") or 0) == int(e.get("level") or 0):
                score += 5.0
            if score > best:
                best, best_id = score, h.get("id")
        if best_id is not None:
            result[idx] = best_id
    return result


def attach_toc_entries(page, toc_entries: Sequence[dict]) -> bool:
    """把编译好的 TOC 条目 dict 挂到页上最合适的 Block。

    选择优先级：已 kind=="toc" 的块 → 与条目几何最重叠的块 → 首个块。
    挂载后该块 kind="toc" 且 metadata["toc_entries"]=list。返回是否挂载成功。
    """
    entries = list(toc_entries or [])
    if not entries or not getattr(page, "blocks", None):
        return False
    # 用条目 bbox 的中位中心点找最重叠的目标块；否则回退已 kind=="toc" 的块。
    xs = [(e.get("bbox") or (0, 0, 0, 0))[0] for e in entries]
    ys = [(e.get("bbox") or (0, 0, 0, 0))[1] for e in entries]
    tx = sum(xs) / len(xs) if xs else 0.0
    ty = sum(ys) / len(ys) if ys else 0.0
    toc_candidates = [b for b in page.blocks if b.kind == "toc"]
    target = toc_candidates[0] if toc_candidates else None
    if target is None or len(toc_candidates) > 1:
        best_overlap, best = -1.0, None
        for b in page.blocks:
            overlap = _geo_overlap(b, tx, ty)
            if overlap > best_overlap:
                best_overlap, best = overlap, b
        target = best or (toc_candidates[0] if toc_candidates else page.blocks[0])
    if target is None:
        return False
    target.kind = "toc"
    target.metadata["kind"] = "toc"
    target.metadata["toc_entries"] = entries
    return True


def _geo_overlap(block, x: float, y: float) -> float:
    try:
        x0, y0, x1, y1 = block.bbox
        if x0 <= x <= x1 and y0 <= y <= y1:
            return 1.0
        # 垂直带重叠也算（行高容差）
        if y0 - 20.0 <= y <= y1 + 20.0:
            return 0.5
    except Exception:  # noqa: BLE001
        return 0.0
    return 0.0
