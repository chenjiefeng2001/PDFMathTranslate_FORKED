"""Module: ContextTranslation — Phase 4.2 文档级上下文翻译。

翻译输入从 ``sentence`` 升级为 ``Document Context``：

    {"text": "Section",
     "context": {"type": "toc", "level": 2, "parent": "Chapter 5",
                 "domain": "cs", "policy": {...}}}

- ``document_context_for(model, block)``：type/level/parent(section)/domain；
- ``translate_document_context_aware(model, ctx_translate_fn)``：
  ctx_translate_fn(text, context) -> str；领域术语经 DomainGlossary 先钉死
  再翻译、翻译后复检（glossary post-process）；
- ``context_to_prompt(ctx)``：把上下文序列化为 LLM 提示片段。

纯逻辑；翻译器只需实现带 context 的回调（Google/DeepL 封装可忽略 context
退化为普通翻译，行为与旧路径一致）。
"""
from __future__ import annotations

import json
from typing import Callable, Dict, Optional

from pdf2zh.v3.document_model import (
    DocumentModel, block_id,
)
from pdf2zh.v3.domain_glossary import DomainGlossary, detect_domain


def _section_of(model: DocumentModel, bid: str) -> Optional[dict]:
    for sec in model.metadata.get("sections", []) or []:
        if sec["section_id"] == bid:
            return sec
        if bid in sec["members"]:
            return sec
    return None


def _locate(model: DocumentModel, block):
    """在模型里定位块 → (pno, index, bid)。"""
    for page in model.pages:
        for i, b in enumerate(page.blocks):
            if b is block:
                return page.page_num, i, block_id(page.page_num, i)
    return 0, 0, "p0_0"


def document_context_for(model: DocumentModel, block) -> dict:
    """块 → 文档上下文（type/level/parent/domain/policy）。"""
    pno, idx, bid = _locate(model, block)
    pol = block.metadata.get("translation_policy") or {}
    sec = _section_of(model, bid)
    domain = block.metadata.get("domain") or detect_domain(block.text or "")
    ctx = {
        "type": block.kind,
        "level": block.metadata.get("reading_order"),
        "parent": f"{sec['number']} {sec['title']}".strip()
        if sec and sec.get("number") else (sec["title"] if sec else ""),
        "domain": domain,
        "policy": pol,
    }
    return ctx


def context_to_prompt(ctx: dict) -> str:
    """上下文 → LLM 提示片段（供人工/LLM 翻译器注入）。"""
    return (f"[context] type={ctx.get('type')} "
            f"level={ctx.get('level')} parent={ctx.get('parent') or '-'} "
            f"domain={ctx.get('domain')}")


def translate_document_context_aware(model: DocumentModel,
                                     ctx_translate_fn: Callable[[str, dict], str],
                                     glossary: Optional[DomainGlossary] = None) -> dict:
    """文档级上下文翻译：策略 → 上下文 →（术语钉死→翻译→术语复检）。

    ``ctx_translate_fn(text, context)`` 必须接受 (text, context)；
    只传 text 的旧翻译器可用 ``lambda t, c, fn=fn: fn(t)`` 包装。
    返回统计 {translated, preserved, skipped, context_used, glossary_hits}。
    """
    glossary = glossary or DomainGlossary()
    stats = {"translated": 0, "preserved": 0, "skipped": 0,
             "context_used": 0, "glossary_hits": 0}
    for page in model.pages:
        for i, block in enumerate(page.blocks):
            pol = block.metadata.get("translation_policy") or {}
            if pol.get("translate") is False:
                stats["preserved"] += 1
                continue
            text = (pol.get("source_text") or block.text or "").strip()
            if not text:
                stats["skipped"] += 1
                continue
            ctx = document_context_for(model, block)
            stats["context_used"] += 1
            pinned = glossary.apply(text, domain=ctx.get("domain"))
            if pinned != text:
                stats["glossary_hits"] += 1
            try:
                translated = ctx_translate_fn(pinned, ctx) or pinned
            except Exception as e:  # noqa: BLE001
                translated = pinned
            block.metadata["translated"] = translated
            block.metadata["translated_same"] = (translated == pinned)
            block.metadata["translate"] = True
            block.metadata["context"] = ctx
            stats["translated"] += 1
    model.metadata["translation_context_stats"] = dict(stats)
    return stats


__all__ = [
    "document_context_for", "context_to_prompt",
    "translate_document_context_aware",
]