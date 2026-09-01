"""List render side-channel — Commit 4 接线（detect → AST → translate → render）。

把 semantic 列表层（``pdf2zh.semantic``）串进 v3 PDF 渲染链，同时保持
``converter.py`` 的 strangulation gate 不变：所有列表编排逻辑都定义在这里与
``pdf2zh.semantic.renderer.list.build_page_list_plan``。

- :func:`build_block_list_payload` — 一个 ``kind == "list"`` 的块 → 逐 item 的
  渲染载荷；**content 才进 translator，marker 从不进**，marker/content_x/层级
  几何全部来自检测/解析阶段（renderer 不重新推断）。
- :func:`list_payload_from_lines` — 通用入口：传入行文本 + 几何 + 翻译回调。

输出是 JSON 安全 dict（与 ``build_page_list_plan`` 同构），供
``document_model.render_plan_from_model`` 透传到
``magicpdf_renderer.render_plan_to_pdf`` 直接消费。
"""

from __future__ import annotations

import logging
from typing import Callable

from pdf2zh.semantic.renderer.list import build_page_list_plan

log = logging.getLogger(__name__)

__all__ = ["build_block_list_payload", "list_payload_from_lines"]


def list_payload_from_lines(
    texts: list[str],
    geom: list[dict] | None = None,
    translate: Callable[[str], str] | None = None,
    line_height: float = 12.0,
) -> dict:
    """对一页/一块的行序列运行完整 ``detect → parse → translate → render``。

    Args:
        texts: 行文本（阅读顺序，含 marker 行与延续行）。
        geom: 每行 ``{x0, x1, y0, y1, size}`` 几何（可选）。
        translate: 仅 content 的翻译回调；marker 不会被调用。
        line_height: 延续行垂直步进。

    Returns:
        ``build_page_list_plan`` 的 JSON 载荷；无列表时 ``commands`` 为空。
    """
    try:
        return build_page_list_plan(
            texts,
            geom=geom,
            translate=translate,
            line_height=line_height,
        )
    except Exception as exc:  # noqa: BLE001 — 列表侧信道失败不影响主链
        log.debug("list_sidechannel: payload build failed: %s", exc)
        return {"tree": None, "items": [], "commands": [], "translated_calls": []}


def build_block_list_payload(
    block,
    translate: Callable[[str], str] | None = None,
    line_height: float = 12.0,
) -> dict:
    """一个 v3 ``BlockModel``（kind == "list"）→ 逐 item 渲染载荷。

    每个 ``LineModel`` 一行；几何使用块内行 bbox（v3 左下原点坐标系，与
    renderer 的 flip 约定一致）。块无行或检测不到列表时返回空载荷 —— 上层
    回退到普通块渲染（不改变既有行为）。
    """
    lines = list(getattr(block, "lines", None) or [])
    if not lines:
        return {"tree": None, "items": [], "commands": [], "translated_calls": []}
    texts = [(ln.text or "") for ln in lines]
    geom = [
        {
            "x0": getattr(ln, "x0", 0.0),
            "x1": getattr(ln, "x1", 0.0),
            "y0": getattr(ln, "y0", 0.0),
            "y1": getattr(ln, "y1", 0.0),
        }
        for ln in lines
    ]
    return list_payload_from_lines(
        texts,
        geom=geom,
        translate=translate,
        line_height=line_height,
    )
