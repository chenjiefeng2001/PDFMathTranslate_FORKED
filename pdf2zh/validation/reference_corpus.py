"""ReferenceCorpus — 参考语料库。

存储人工翻译基准，用于评估翻译质量。

用法：
    corpus = ReferenceCorpus("tests/golden_translation")
    ref = corpus.get_reference("paper1", page=1)
    # ReferenceEntry(source="...", reference="...", domain="academic")
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class ReferenceEntry:
    """参考条目。"""

    doc_id: str = ""
    page_index: int = 0
    source: str = ""
    reference: str = ""
    domain: str = "general"
    block_type: str = "paragraph"
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ReferencePair:
    """参考对（source → reference）。"""

    source: str = ""
    reference: str = ""
    domain: str = "general"
    quality_score: float = 1.0  # 人工翻译质量


class ReferenceCorpus:
    """参考语料库。"""

    def __init__(self, corpus_dir: str = "tests/golden_translation") -> None:
        self._corpus_dir = Path(corpus_dir)
        self._entries: List[ReferenceEntry] = []
        self._pairs: List[ReferencePair] = []
        self._load_corpus()

    def _load_corpus(self) -> None:
        """加载语料库。"""
        if not self._corpus_dir.exists():
            self._corpus_dir.mkdir(parents=True, exist_ok=True)
            self._create_sample_corpus()
            return

        # 加载 JSON 格式
        for json_file in self._corpus_dir.glob("*.json"):
            try:
                with open(json_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for item in data.get("entries", []):
                    self._entries.append(ReferenceEntry(**item))
                for item in data.get("pairs", []):
                    self._pairs.append(ReferencePair(**item))
            except Exception:
                pass

    def _create_sample_corpus(self) -> None:
        """创建示例语料库。"""
        sample_pairs = [
            # 学术论文
            ReferencePair(
                source="The attention mechanism allows the model to focus on relevant parts of the input.",
                reference="注意力机制使模型能够关注输入的相关部分。",
                domain="academic",
            ),
            ReferencePair(
                source="We propose a novel architecture based on transformer networks.",
                reference="我们提出了一种基于Transformer网络的新架构。",
                domain="academic",
            ),
            ReferencePair(
                source="The experimental results demonstrate significant improvements over baseline methods.",
                reference="实验结果表明，与基线方法相比有显著改进。",
                domain="academic",
            ),
            # 技术书籍
            ReferencePair(
                source="Memory management is crucial for performance optimization.",
                reference="内存管理对于性能优化至关重要。",
                domain="technical",
            ),
            ReferencePair(
                source="The garbage collector automatically reclaims unused memory.",
                reference="垃圾回收器自动回收未使用的内存。",
                domain="technical",
            ),
            # 数学
            ReferencePair(
                source="The probability density function is defined as f(x) = λe^(-λx).",
                reference="概率密度函数定义为 f(x) = λe^(-λx)。",
                domain="math",
            ),
            ReferencePair(
                source="Let X be a random variable with mean μ and variance σ².",
                reference="设 X 为均值 μ、方差 σ² 的随机变量。",
                domain="math",
            ),
            # TOC
            ReferencePair(
                source="Chapter 1: Introduction .................. 15",
                reference="第 1 章：引言 .................. 15",
                domain="toc",
            ),
            ReferencePair(
                source="2.1 Background ......................... 23",
                reference="2.1 背景 ......................... 23",
                domain="toc",
            ),
            # 通用
            ReferencePair(
                source="This paper makes the following contributions:",
                reference="本文做出以下贡献：",
                domain="general",
            ),
            ReferencePair(
                source="In conclusion, our method achieves state-of-the-art performance.",
                reference="总之，我们的方法达到了最先进的性能。",
                domain="general",
            ),
        ]

        self._pairs = sample_pairs

        # 保存
        self._save_corpus()

    def _save_corpus(self) -> None:
        """保存语料库。"""
        self._corpus_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "pairs": [
                {
                    "source": p.source,
                    "reference": p.reference,
                    "domain": p.domain,
                    "quality_score": p.quality_score,
                }
                for p in self._pairs
            ]
        }
        with open(self._corpus_dir / "corpus.json", "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def get_pairs(
        self,
        domain: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[ReferencePair]:
        """获取参考对。"""
        pairs = self._pairs
        if domain:
            pairs = [p for p in pairs if p.domain == domain]
        if limit:
            pairs = pairs[:limit]
        return pairs

    def get_reference(
        self,
        doc_id: str,
        page: int,
    ) -> Optional[ReferenceEntry]:
        """获取参考条目。"""
        for entry in self._entries:
            if entry.doc_id == doc_id and entry.page_index == page:
                return entry
        return None

    @property
    def size(self) -> int:
        return len(self._pairs)

    def summary(self) -> str:
        domains = {}
        for p in self._pairs:
            domains[p.domain] = domains.get(p.domain, 0) + 1

        lines = [
            f"Reference Corpus: {self.size} pairs",
            "Domains:",
        ]
        for domain, count in sorted(domains.items()):
            lines.append(f"  {domain}: {count}")

        return "\n".join(lines)
