"""P6.3 — 锚点保护机制（规范书 §6.1 + §9.2）。

公式在提取 TranslationUnit 时被转换为语义无关的字符串占位符
``<formula_x>``，LLM 只翻译文本部分；译文返回后锚点被完整还原。

    "Let f(x) = x^2 + 1 be a continuous function."
        → 提取语义 "Let <formula_0> be a continuous function."
        → LLM 输出 "设 <formula_0> 为连续函数。"

QA（§9.2 Anchor Integrity Score）：译文返回后 ``<formula_x>`` 占位符
的提取匹配率必须达到 100%。
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

ANCHOR_RE = re.compile(r"<formula_(\d+)>")
_FORMULA_PLACEHOLDER_RE = re.compile(r"<formula_\d+>")

# 宽松锚点变体（真实 LLM 污染容忍，失效点 2）：
#   <formula_0> / < formula_0 > / <formula 0> / <FORMULA_0> / <formula0>
LOOSE_ANCHOR_RE = re.compile(r"<\s*[Ff][Oo][Rr][Mm][Uu][Ll][Aa]\s*[ _]?\s*(\d+)\s*>")


def normalize_anchor_token(token: str) -> Optional[str]:
    """把任意锚点变体规范化为 ``<formula_N>``；非锚点返回 None。"""
    if not isinstance(token, str):
        return None
    m = LOOSE_ANCHOR_RE.fullmatch(token.strip())
    return f"<formula_{m.group(1)}>" if m else None


def extract_anchors_loose(text: str) -> List[str]:
    """宽松提取文本中的锚点（有序、已规范化），容忍空格/大小写/缺下划线。"""
    return [f"<formula_{m.group(1)}>" for m in LOOSE_ANCHOR_RE.finditer(text or "")]


def repair_anchors(
    translated: str, formula_map: Dict[str, object], source_text: Optional[str] = None
) -> str:
    """容错还原锚点（真实 LLM 锚点污染的兜底，§9.2）。

    1. 译文中的锚点变体（``< formula_0 >``/``<FORMULA_0>``/``<formula 0>``/
       ``<formula0>``/空格大小写混排）一律规范化为 ``<formula_N>``；
    2. 期望锚点（``formula_map`` 键，inject 顺序）缺失时按顺序回退补回
       —— 补到最后一个已识别锚点之后（保持公式相对顺序）；一个锚点都
       没有时全部补在译文末尾。**绝不丢弃公式几何**（Layout Solver 依赖
       锚点落位），完整性由 QA ``anchor_ok`` 标记，不由 repair 静默吞掉。
    """
    if not formula_map:
        return LOOSE_ANCHOR_RE.sub(
            lambda m: f"<formula_{m.group(1)}>", translated or ""
        )
    normalized = LOOSE_ANCHOR_RE.sub(
        lambda m: f"<formula_{m.group(1)}>", translated or ""
    )
    found = extract_anchors_loose(normalized)
    expected = list(formula_map.keys())
    missing = [k for k in expected if k not in found]
    if not missing:
        return normalized
    if found:
        last = found[-1]
        pos = normalized.rfind(last)
        if pos >= 0:
            pos_end = pos + len(last)
            suffix = " " + " ".join(missing)
            return normalized[:pos_end] + suffix + normalized[pos_end:]
    return normalized.strip() + (" " if normalized.strip() else "") + " ".join(missing)


class AnchorProtector:
    """``<formula_x>`` 占位符注入与还原。"""

    def __init__(
        self, placeholder_prefix: str = "formula", reserved_token: str = "<formula_{}>"
    ) -> None:
        self.prefix = placeholder_prefix
        self.token = reserved_token

    # ── 注入 ──────────────────────────────────────────────────────

    def inject(
        self, text: str, formula_bboxes: Optional[List] = None
    ) -> Tuple[str, Dict[str, object]]:
        """把文本中的公式占位符替换为锚点（若文本已含 ``<formula_x>`` 则保留）。

        返回 (anchored_text, formula_map)。``formula_bboxes`` 为与锚点
        顺序对齐的几何列表（供提取器映射 bbox → 公式对象）。
        """
        formula_map: Dict[str, object] = {}

        def _repl(m: "re.Match[str]") -> str:
            token = self.token.format(m.group(1))
            formula_map[token] = m.group(1)
            return token

        new_text = _FORMULA_PLACEHOLDER_RE.sub(_repl, text)
        return new_text, formula_map

    def protect(self, text: str) -> Tuple[str, Dict[str, str]]:
        """把已带 ``<formula_x>`` 的文本登记到 formula_map 并原样返回。"""
        formula_map: Dict[str, str] = {}
        for m in ANCHOR_RE.finditer(text):
            formula_map[f"<formula_{m.group(1)}>"] = m.group(1)
        return text, formula_map

    # ── 还原 ──────────────────────────────────────────────────────

    def restore(self, translated: str, formula_map: Dict[str, object]) -> str:
        """把译文中的锚点替换为渲染占位符（保持锚点便于 Layout Solver 落位）。"""
        if not formula_map:
            return translated
        return ANCHOR_RE.sub(lambda m: f"<formula_{m.group(1)}>", translated)

    def replace_with_text(self, translated: str, formula_texts: Dict[str, str]) -> str:
        """把锚点替换为公式原文本（用于纯文本摘要/日志，不用于渲染）。"""

        def _repl(m: "re.Match[str]") -> str:
            key = f"<formula_{m.group(1)}>"
            return formula_texts.get(key, key)

        return ANCHOR_RE.sub(_repl, translated)

    # ── 完整性校验（§9.2 Anchor Integrity Score）──────────────────

    def integrity_score(
        self, translated: str, formula_map: Dict[str, object], loose: bool = True
    ) -> float:
        """锚点匹配率：期望锚点集合与译文实际锚点集合的交叠率。

        ``loose=True`` 时用宽松匹配（容忍真实 LLM 的 ``< formula_0 >``/
        ``<FORMULA_0>``/``<formula 0>`` 等污染变体）；严格模式保持
        只认 ``<formula_N>``。**只统计不修复**——修复走 ``repair``。
        """
        expected = set(formula_map.keys())
        if loose:
            found = set(extract_anchors_loose(translated))
        else:
            found = {f"<formula_{m.group(1)}>" for m in ANCHOR_RE.finditer(translated)}
        if not expected:
            return 1.0 if not found else 0.0
        # 1. 所有期望锚点必须出现（丢失扣分）
        missing = expected - found
        # 2. 译文不得引入未知锚点（幻觉扣分）
        unknown = found - expected
        matched = len(expected & found)
        return (
            matched / (len(expected) + len(unknown)) if (expected or unknown) else 1.0
        )

    def repair(
        self,
        translated: str,
        formula_map: Dict[str, object],
        source_text: Optional[str] = None,
    ) -> str:
        """容错还原 + 缺失回退（真实 LLM 锚点污染的兜底）。"""
        return repair_anchors(translated, formula_map, source_text)


def anchors_in_text(text: str) -> List[str]:
    """提取文本中的全部锚点 token（有序，严格匹配）。"""
    return [f"<formula_{m.group(1)}>" for m in ANCHOR_RE.finditer(text)]


def anchors_in_text_loose(text: str) -> List[str]:
    """提取文本中的全部锚点 token（有序，宽松匹配已规范化）。"""
    return extract_anchors_loose(text)


__all__ = [
    "AnchorProtector",
    "ANCHOR_RE",
    "LOOSE_ANCHOR_RE",
    "normalize_anchor_token",
    "anchors_in_text",
    "anchors_in_text_loose",
    "extract_anchors_loose",
    "repair_anchors",
]
