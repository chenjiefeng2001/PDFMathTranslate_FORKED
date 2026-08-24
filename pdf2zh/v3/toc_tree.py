"""Module: TOCTree — 目录条目 → 章节层级树（第四层：结构层级恢复）。

目录不是线性文本，而是树：

    Chapter 5
    ├── 5.1
    ├── 5.2
    │   ├── 5.2.1
    │   └── 5.2.2
    └── 5.3

本模块把已解析的 TOC 记录（``toc_dump`` / ``toc_to_ir_records`` 输出，
含 ``number``/``title``/``page``/``level``/``line``）重建成层级树：

- 点号编号（5 / 5.2 / 5.2.1）→ 前缀包含关系定父子；
- 非点号编号（附录 A / 第X章 / 罗马数字）→ 按 kind 层级 + 顺序兜底；
- 输出 ``depth``/``parent``/``indent`` —— Renderer 据此决定缩进，
  不再「把树压成线性」。

纯逻辑、无 I/O；不做任何决策，只回答「谁是谁的父/子」。
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence

_RE_DOTTED = re.compile(r"^\d+(\.\d+)*$")


def _segments(number: str) -> Optional[List[int]]:
    n = (number or "").strip()
    if not _RE_DOTTED.match(n):
        return None
    return [int(part) for part in n.split(".")]


def _is_prefix(a: List[int], b: List[int]) -> bool:
    return len(a) < len(b) and b[: len(a)] == a


def build_toc_tree(entries: Sequence[dict]) -> dict:
    """把 TOC 记录重建为章节树。

    ``entries`` 为 ``[{line, number, title, page, level, kind, ...}]``
    （``pipeline_dump.toc_dump`` 输出可直接喂入）。返回：

        {
          "roots": [line, ...],
          "nodes": [{line, number, title, page, depth, parent, indent}, ...],
          "max_depth": int,
        }

    ``parent`` 为父条目 line（根为 None）；``indent`` = depth（渲染缩进用）。
    """
    ordered = sorted(
        (dict(e) for e in entries or []),
        key=lambda e: (int(e.get("line", 0))),
    )
    nodes: List[dict] = []
    stack: List[dict] = []  # (segments|None, kind_level, line, depth)

    def _kind_level(e: dict) -> int:
        return int(e.get("level", 0) or 0)

    for e in ordered:
        number = str(e.get("number", "")).strip()
        segs = _segments(number)
        line = int(e.get("line", 0))
        depth = 0
        parent = None
        if segs is not None:
            # 前缀包含：找最近的、段数更少的祖先
            while stack:
                top_segs, top_kind, top_line, top_depth = stack[-1]
                if top_segs is not None and _is_prefix(top_segs, segs):
                    depth = top_depth + 1
                    parent = top_line
                    break
                if top_segs is None and top_kind < len(segs):
                    depth = top_depth + 1
                    parent = top_line
                    break
                stack.pop()
        else:
            # 非点号编号（第X章/附录A/罗马数字）：按 kind 层级 + 顺序兜底
            kind_level = _kind_level(e)
            while stack and stack[-1][1] >= kind_level:
                stack.pop()
            if stack:
                depth = stack[-1][3] + 1
                parent = stack[-1][2]
            else:
                depth = 0
        nodes.append(
            {
                "line": line,
                "number": number,
                "title": str(e.get("title", "")),
                "page": str(e.get("page", "")),
                "depth": depth,
                "parent": parent,
                "indent": depth,
            }
        )
        if segs is not None:
            stack.append((segs, len(segs), line, depth))
        else:
            stack.append((None, _kind_level(e), line, depth))

    roots = [n["line"] for n in nodes if n["parent"] is None]
    return {
        "roots": roots,
        "nodes": nodes,
        "max_depth": max((n["depth"] for n in nodes), default=0),
    }


__all__ = ["build_toc_tree", "_segments", "_is_prefix"]
