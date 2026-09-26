"""TOCTranslator — 目录专用翻译器。

TOC 是几何问题，不是字符串问题。

禁止：
    Introduction ........ 12
    ↓
    介绍................................12  ✗

应该：
    Introduction ........ 12
    ↓
    介绍 ................. 12  ✓
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class TOCEntry:
    """TOC 条目。"""

    title: str = ""
    leader: str = "."
    page_number: str = ""
    indent_level: int = 0
    style: str = ""

    @classmethod
    def from_text(cls, text: str) -> Optional[TOCEntry]:
        """从文本解析 TOC 条目。"""
        import re

        # 匹配: Title ....... 12
        pattern = r"^(.+?)([\.\.]+)\s*(\d+)\s*$"
        match = re.match(pattern, text.strip())
        if match:
            return cls(
                title=match.group(1).strip(),
                leader=match.group(2),
                page_number=match.group(3),
            )
        return None

    def format(self, translated_title: str) -> str:
        """格式化 TOC 条目。"""
        # 计算 leader 长度
        title_len = len(translated_title)
        page_len = len(self.page_number)
        leader_len = max(3, 40 - title_len - page_len)
        leader = self.leader[0] * leader_len
        indent = "  " * self.indent_level
        return f"{indent}{translated_title} {leader} {self.page_number}"


class TOCTranslator:
    """TOC 专用翻译器。"""

    def __init__(self, glossary: Optional[Any] = None) -> None:
        self._glossary = glossary

    def translate_entry(
        self,
        entry: TOCEntry,
        translate_func,
    ) -> str:
        """翻译 TOC 条目。"""
        # 翻译标题部分
        translated_title = translate_func(entry.title)

        # 格式化
        return entry.format(translated_title)

    def translate_toc(
        self,
        toc_text: str,
        translate_func,
    ) -> str:
        """翻译整个 TOC 文本。"""
        lines = toc_text.split("\n")
        translated_lines = []

        for line in lines:
            entry = TOCEntry.from_text(line)
            if entry:
                translated = self.translate_entry(entry, translate_func)
                translated_lines.append(translated)
            else:
                # 非 TOC 行，直接翻译
                translated_lines.append(translate_func(line))

        return "\n".join(translated_lines)

    def validate(self, original: str, translated: str) -> bool:
        """验证翻译是否保持结构。"""
        orig_entry = TOCEntry.from_text(original)
        trans_entry = TOCEntry.from_text(translated)

        if orig_entry is None or trans_entry is None:
            return True  # 无法验证

        # 检查 leader
        if orig_entry.leader != trans_entry.leader:
            return False

        # 检查页码
        if orig_entry.page_number != trans_entry.page_number:
            return False

        # 检查缩进
        if orig_entry.indent_level != trans_entry.indent_level:
            return False

        return True
