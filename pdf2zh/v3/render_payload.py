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
import unicodedata
from typing import Callable, Dict, Mapping, Optional, Sequence

log = logging.getLogger(__name__)


def _same_content(a: str, b: str) -> bool:
    """忽略空白/连字符差异的文本相等 —— 判断「翻译是否真的产出了新内容」。

    精确串比较在生产上漏掉一整类退化：OpenCode 挂掉时"译文"常常只是源英文
    去掉换行、去掉连字符空格（实测 Structural holes 全书 325 页：
    ``"National Science Foundation" -> "National ScienceFoundation"``，
    ``"STRUCTURAL HOLES\\nThe Social..." -> "STRUCTURAL HOLESThe Social..."``）。
    按精确比较这些块算"翻译过了"，于是英文被当 CJK 文本重排，325 页里
    1097 个块被重新排版，4 页正文被排到页外（22 个 span 越界）。

    归一化只动**排版伪影**（空白、连字符），不动内容字符：内容真变了
    （含中英互译）两者归一化后仍不相等。
    """
    if a == b:
        return True

    def _norm(s: str) -> str:
        s = unicodedata.normalize("NFKC", s or "")
        s = s.replace("-", "")  # 源提取会去掉软连字符的换行衔接
        return "".join(ch for ch in s if not ch.isspace())

    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return na == nb
    return na == nb


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
            # 逐 item 比较 content vs translated：内容没变（翻译服务退化为
            # 原文）时整块按保留处理 —— list 的命令是逐 item 生成的，
            # 块级 translated 恒等于原文，必须在 payload 里比。
            items = payload.get("items") or []
            same = bool(items) and all(
                _same_content(it.get("content"), it.get("translated")) for it in items
            )
            unit.update(
                {
                    "kind": "list",
                    "translated": text,
                    "translated_same": same,
                    "translate": not same,
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
            joined = "\n".join(e["translated"] for e in entries)
            unit.update(
                {
                    "kind": "toc",
                    "translated": joined,
                    "translated_same": _same_content(joined, text),
                    "translate": not _same_content(joined, text),
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
    # ``_same_content`` 而非精确比较：翻译服务退化时译文常只差空白/连字符
    # （实测 325 页全书 1097 块），按精确比较会当成有效译文送进 CJK 排版。
    same = _same_content(src, translated)
    unit.update(
        {
            "kind": "flow",
            "translated": translated,
            "translated_same": same,
            "translate": not same,
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
            "overflow",
            "policy",
            "font_size",
            "recovery",
            "trace",
            "lines",
            "line_widths",
            "primitive_kind",
            "layout_ok",
            "bbox",
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
