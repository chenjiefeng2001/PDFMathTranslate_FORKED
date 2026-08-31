"""Unified block translation-unit + render-payload dispatch — Commit 7A.

把散落在 ``document_model.translate_document`` 里的 4 条特判（preserve /
list / toc / flow）收敛为**单一分派入口**，同时保持行为完全等价：

    block
      │
      ▼
    block_translation_unit(block, translate_fn, model)
      │   （policy 优先，kind 兜底）
      ▼
    TranslationUnit = {kind, text, translated, translate, translated_same, payload}
      │
      ▼
    build_render_payload(unit, block)  →  render plan 的统一 render_payload

目标（Commit 7A 架构收口）：

- **Translation 输入统一**：一个块只有一个 TranslationUnit；list/toc 的
  结构化载荷（marker/title_only/commands/entries）作为 unit.payload 附加，
  不再各自发明一套 ``translate_list() / translate_toc()`` 契约。
- **Geometry 只消费、不重推断**：所有几何（title_x/page_x/indent/bbox/
  marker_x/content_x）来自解析阶段并原样透传；本模块只搬运，绝不重算。
- **RenderPlan 统一 schema**：``render_payload`` 带显式 ``kind``，渲染端
  按 kind 分派（list/toc/flow），不再靠 ``if list_items: if toc_commands:``
  字段探测。
- **converter 不膨胀**：本模块是 v3 层入口，converter 只保留 orchestration。

兼容性：``translate_document`` 继续把 list_items/toc_entries/toc_commands
写回 block.metadata（既有消费端/测试依赖这些字段）；render plan 同时携带
统一 ``render_payload`` 与旧字段。
"""

from __future__ import annotations

import logging
from typing import Callable, Dict, Mapping, Optional, Sequence

log = logging.getLogger(__name__)

__all__ = [
    "block_translation_unit",
    "build_render_payload",
    "payload_commands",
    "KEEP_KINDS",
]

#: 保留块（不翻译、几何原样保留）—— 与 document_model._KEEP_KINDS 对齐。
#: 7H-2C：纳入 COMMAND / FILENAME / IDENTIFIER（由语义角色仲裁定型后保留）。
KEEP_KINDS = frozenset(
    {
        "formula",
        "figure",
        "image",
        "table",
        "code",
        "command",
        "filename",
        "identifier",
        "header",
        "footer",
    }
)


def _policy_of(block) -> dict:
    """读取块的翻译策略（TranslationPolicyPass 产出，缺省为空策略）。"""
    md = getattr(block, "metadata", None) or {}
    return md.get("translation_policy") or {}


def block_translation_unit(
    block,
    translate_fn: Optional[Callable[[str], str]],
    model=None,
) -> dict:
    """单一分派：一个 BlockModel → 统一 TranslationUnit。

    Returns:
        ``{"kind", "text", "translated", "translate", "translated_same",
        "payload"}``，其中 ``kind ∈ {skip, preserve, list, toc, flow}``，
        ``payload`` 为结构化载荷（list/toc 时非空，其余为 None）。
        任何异常都不会向上抛出（side-channel 纪律：失败回退 flow）。
    """
    text = (getattr(block, "text", None) or "").strip()
    unit: dict = {
        "kind": "skip",
        "text": text,
        "translated": text,
        "translate": False,
        "translated_same": True,
        "payload": None,
    }
    if not text:
        return unit

    pol = _policy_of(block)
    kind = getattr(block, "kind", "") or ""

    # ── preserve：策略显式禁译 / 保留 kind ─────────────────────────────
    if pol.get("translate") is False or kind in KEEP_KINDS:
        unit["kind"] = "preserve"
        unit["translated"] = text
        unit["translated_same"] = True
        unit["translate"] = False
        return unit

    # ── list：逐 item 翻译（marker 永不进 translator），几何整体保留 ──
    if kind == "list" and getattr(block, "lines", None):
        try:
            from pdf2zh.v3.list_sidechannel import build_block_list_payload

            payload = build_block_list_payload(block, translate_fn)
            unit.update(
                {
                    "kind": "list",
                    "translated": text,
                    "translated_same": False,
                    "translate": True,
                    "payload": payload,
                }
            )
            return unit
        except Exception as e:  # noqa: BLE001 -- 失败回退普通段翻译
            log.debug("list translation unit failed: %s", e)

    # ── toc：结构化条目 —— 只翻 title_only，页码/编号/leader/几何 PRESERVE ──
    if kind == "toc" and (getattr(block, "metadata", None) or {}).get("toc_entries"):
        try:
            from pdf2zh.v3.toc_render_sidechannel import build_block_toc_payload
            from pdf2zh.v3.toc_sidechannel import (
                resolve_toc_headings,
                translate_toc_entries,
            )

            entries = translate_toc_entries(
                (getattr(block, "metadata", None) or {}).get("toc_entries"),
                translate_fn,
            )
            links = resolve_toc_headings(entries, _heading_candidates(model))
            for _fi, _e in enumerate(entries):
                if _fi in links:
                    _e["heading_ref"] = links[_fi]
            # 把翻译后的条目写回 block.metadata，渲染载荷构建器（读取
            # metadata["toc_entries"]）才能拿到 translated_title，避免二次翻译。
            (getattr(block, "metadata", None) or {})["toc_entries"] = entries
            payload = build_block_toc_payload(block, translate_fn)
            unit.update(
                {
                    "kind": "toc",
                    "translated": "\n".join(e["translated"] for e in entries),
                    "translated_same": "\n".join(e["translated"] for e in entries)
                    == text,
                    "translate": True,
                    "payload": {
                        "entries": entries,
                        "commands": payload.get("commands") or [],
                    },
                }
            )
            return unit
        except Exception as e:  # noqa: BLE001 -- 回退普通段翻译
            log.debug("toc translation unit failed: %s", e)

    # ── flow：普通段落 —— policy.source_text 优先（partial：caption 等） ──
    src = pol.get("source_text") or text
    if not src.strip():
        src = text
    translated = src
    if translate_fn is not None:
        try:
            translated = translate_fn(src) or src
        except Exception as e:  # noqa: BLE001
            log.debug("flow translation failed: %s", e)
            translated = src
    unit.update(
        {
            "kind": "flow",
            "translated": translated,
            "translated_same": translated == src,
            "translate": True,
        }
    )
    return unit


def _heading_candidates(model) -> list[dict]:
    """模型内所有 heading 块（供 TOC → heading 关联；无 model 时为空池）。"""
    heads: list[dict] = []
    if model is None:
        return heads
    try:
        from pdf2zh.v3.document_model import block_id

        for page in model.pages:
            pno = page.page_num
            for i, b in enumerate(page.blocks):
                if b.kind == "heading" or (b.metadata or {}).get("role") == "heading":
                    heads.append(
                        {
                            "id": block_id(pno, i),
                            "title": b.text,
                            "page_num": pno,
                            "level": int((b.metadata or {}).get("level", 0) or 0),
                        }
                    )
    except Exception:  # noqa: BLE001 -- 关联失败回退空池
        heads = []
    return heads


def payload_commands(payload: Optional[Mapping]) -> list:
    """从 payload 取渲染命令列表（list/toc 通用）；缺失返回 []。"""
    if not payload:
        return []
    return list(payload.get("commands") or [])


def build_render_payload(unit: Mapping, block=None) -> dict:
    """把 TranslationUnit 编译成 render plan 的统一 ``render_payload``。

    Returns ``{"kind", "commands", "entries"}``；block 提供几何/字号透传
    （仅透传，不重算）。flow/preserve/skip → commands 为空。

    7F-7: flow 的已定版布局诊断（``overflow`` / ``policy`` / ``font_size`` /
    ``recovery`` / ``trace`` / ``lines`` / ``primitive_kind``）原样透传
    （仅透传，不重算），使 render plan 成为诊断链的“已定版结果”载体 ——
    诊断层从 plan 读取，绝不重新 layout。
    """
    kind = unit.get("kind", "flow")
    payload = unit.get("payload") or {}
    out = {
        "kind": kind,
        "commands": payload_commands(payload),
        "entries": list(payload.get("entries") or []),
    }
    if kind == "flow" and isinstance(payload, dict):
        for k in (
            "overflow", "policy", "font_size", "recovery", "trace",
            "lines", "line_widths", "primitive_kind", "layout_ok", "bbox",
        ):
            if k in payload:
                out[k] = payload[k]
    return out


__all__ = [
    "block_translation_unit",
    "build_render_payload",
    "payload_commands",
    "KEEP_KINDS",
]