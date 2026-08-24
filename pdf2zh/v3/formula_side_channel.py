"""公式 LaTeX / OCR 侧通道（mineru_integration Step 1.3）。

magic-pdf/MinerU 解析出的公式（interline_equation / inline_equation / formula）
块自带 UniMERNet 的 LaTeX 结果（middle.json 的 ``latex`` 字段）。这些信息
在桥接（magicpdf_bridge）阶段已随块写入 ``metadata["latex"]``，但主翻译/重排
管线（BabelDOC / legacy）读不到 v3 metadata——侧通道把它独立导出、再按匹配
回填，让下游公式重建（MathRecoveryRepair / RenderTakeover）可以用真实 LaTeX
替换 OCR 噪声文本。

三个纯逻辑入口（不触碰主链路，异常仅记日志）：

- :func:`collect_formula_latex`：扫描 DocumentModel，收集公式块 LaTeX；
- :class:`FormulaLatexChannel`：侧通道容器（JSON 可序列化）；
- :func:`apply_formula_latex`：把侧通道中匹配的 LaTeX 回填到模型公式块
  ``metadata["latex"]``，供渲染计划/评测消费。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: 视为公式块的 kind 集合（与 canonical_page / document_model 一致）。
FORMULA_KINDS = frozenset({"formula", "formula_inline", "interline_equation"})


def _normalize_text(text: Optional[str]) -> str:
    return (text or "").replace(" ", "").replace("\n", "").strip()


@dataclass
class FormulaLatexChannel:
    """公式 LaTeX 侧通道容器。

    ``entries``：``{block_id: {"latex", "confidence", "kind", "page"}}``。
    """

    entries: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def register(
        self,
        block_id: str,
        latex: str,
        confidence: Optional[float] = None,
        kind: str = "formula",
        page: int = 0,
    ) -> None:
        latex = (latex or "").strip()
        if not latex or not block_id:
            return
        prev = self.entries.get(block_id)
        if prev and (prev.get("confidence") or 0.0) >= (confidence or 0.0):
            return
        self.entries[block_id] = {
            "latex": latex,
            "confidence": round(float(confidence), 4) if confidence else None,
            "kind": kind,
            "page": page,
        }

    def lookup(self, block_id: str) -> Optional[Dict[str, Any]]:
        return self.entries.get(block_id)

    def to_dict(self) -> Dict[str, Any]:
        return {"formula_count": len(self.entries), "entries": self.entries}

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "FormulaLatexChannel":
        ch = cls()
        for block_id, entry in ((data or {}).get("entries") or {}).items():
            if isinstance(entry, dict) and entry.get("latex"):
                ch.register(
                    block_id=str(block_id),
                    latex=str(entry["latex"]),
                    confidence=entry.get("confidence"),
                    kind=str(entry.get("kind", "formula")),
                    page=int(entry.get("page", 0)),
                )
        return ch


def collect_formula_latex(model) -> FormulaLatexChannel:
    """扫描 DocumentModel 的公式块，收集 LaTeX 侧通道。"""
    from pdf2zh.v3.document_model import block_id

    channel = FormulaLatexChannel()
    for page in getattr(model, "pages", []) or []:
        for i, block in enumerate(page.blocks):
            kind = block.kind
            latex = block.metadata.get("latex") or block.metadata.get("formula_latex")
            if kind not in FORMULA_KINDS or not latex:
                continue
            channel.register(
                block_id=block_id(page.page_num, i),
                latex=str(latex),
                confidence=block.metadata.get("confidence"),
                kind=kind,
                page=page.page_num,
            )
    return channel


def apply_formula_latex(model, channel: FormulaLatexChannel) -> int:
    """把侧通道 LaTeX 回填到模型公式块的 ``metadata["latex"]``。

    对「块 kind 为公式且没有 LaTeX」或「有 LaTeX 但置信度更高」的块回填。
    返回回填块数。
    """
    from pdf2zh.v3.document_model import block_id

    applied = 0
    for page in getattr(model, "pages", []) or []:
        for i, block in enumerate(page.blocks):
            if block.kind not in FORMULA_KINDS:
                continue
            entry = channel.lookup(block_id(page.page_num, i))
            if not entry:
                continue
            cur_latex = block.metadata.get("latex")
            cur_conf = block.metadata.get("confidence")
            entry_conf = entry.get("confidence") or 0.0
            if cur_latex and (cur_conf or 0.0) > entry_conf:
                continue
            block.metadata["latex"] = entry["latex"]
            block.metadata["latex_source"] = "magicpdf_side_channel"
            applied += 1
    return applied


def latex_channel_from_magicpdf_json(path: str) -> FormulaLatexChannel:
    """从 magicpdf 侧通道 dump JSON 恢复通道（评测/离线消费）。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return FormulaLatexChannel.from_dict(json.load(fh))
    except Exception as exc:  # noqa: BLE001 -- 侧通道缺失不影响主链路
        logger.debug("formula channel load failed: %s", exc)
        return FormulaLatexChannel()


__all__ = [
    "FormulaLatexChannel",
    "FORMULA_KINDS",
    "collect_formula_latex",
    "apply_formula_latex",
    "latex_channel_from_magicpdf_json",
]
