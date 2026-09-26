"""TranslationMemory — 术语一致性记忆。

保存已翻译术语，保证一致性。

收益：
    第一次: "attention mechanism" → "注意力机制"
    后续: "self-attention mechanism" → "自注意力机制" (一致)
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class TermEntry:
    """术语条目。"""

    source: str = ""
    translation: str = ""
    domain: str = ""
    confidence: float = 1.0
    hits: int = 0

    @property
    def key(self) -> str:
        return f"{self.domain}::{self.source.lower()}"


@dataclass
class TranslationMemory:
    """翻译记忆。"""

    def __init__(self, persistence_path: Optional[Path] = None) -> None:
        self._terms: Dict[str, TermEntry] = {}
        self._persistence_path = persistence_path
        self._load()

    def _load(self) -> None:
        """从文件加载。"""
        if self._persistence_path and self._persistence_path.exists():
            try:
                with open(self._persistence_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for item in data.get("terms", []):
                    entry = TermEntry(**item)
                    self._terms[entry.key] = entry
            except Exception:
                pass

    def _save(self) -> None:
        """保存到文件。"""
        if self._persistence_path:
            try:
                self._persistence_path.parent.mkdir(parents=True, exist_ok=True)
                data = {
                    "terms": [
                        {
                            "source": e.source,
                            "translation": e.translation,
                            "domain": e.domain,
                            "confidence": e.confidence,
                            "hits": e.hits,
                        }
                        for e in self._terms.values()
                    ]
                }
                with open(self._persistence_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            except Exception:
                pass

    def lookup(
        self,
        source: str,
        domain: str = "",
    ) -> Optional[str]:
        """查找术语翻译。"""
        key = f"{domain}::{source.lower()}"
        entry = self._terms.get(key)
        if entry:
            entry.hits += 1
            return entry.translation
        return None

    def store(
        self,
        source: str,
        translation: str,
        domain: str = "",
        confidence: float = 1.0,
    ) -> None:
        """存储术语翻译。"""
        key = f"{domain}::{source.lower()}"
        if key in self._terms:
            self._terms[key].hits += 1
        else:
            self._terms[key] = TermEntry(
                source=source,
                translation=translation,
                domain=domain,
                confidence=confidence,
            )
        self._save()

    @property
    def size(self) -> int:
        return len(self._terms)

    def get_high_frequency_terms(self, min_hits: int = 5) -> List[TermEntry]:
        """获取高频术语。"""
        return [e for e in self._terms.values() if e.hits >= min_hits]

    def apply_to_text(
        self,
        text: str,
        domain: str = "",
    ) -> str:
        """用记忆中的术语替换文本。"""
        result = text
        for entry in self._terms.values():
            if entry.source.lower() in result.lower():
                # 简单替换（实际应该更复杂）
                result = result.replace(
                    entry.source,
                    entry.translation,
                )
        return result
