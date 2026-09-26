"""Glossary — 领域术语表。

防止专业术语被错误翻译。

用法：
    glossary = Glossary.from_domain("computer_science")
    protected = glossary.protect("Transformer model with attention mechanism")
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set


@dataclass
class GlossaryTerm:
    """术语条目。"""

    term: str = ""
    translation: str = ""
    case_sensitive: bool = False
    preserve_english: bool = False


# 预定义领域术语
DOMAIN_GLOSSARIES: Dict[str, List[GlossaryTerm]] = {
    "computer_science": [
        GlossaryTerm(
            term="Transformer", translation="Transformer", preserve_english=True
        ),
        GlossaryTerm(term="GPU", preserve_english=True),
        GlossaryTerm(term="CUDA", preserve_english=True),
        GlossaryTerm(term="PyTorch", preserve_english=True),
        GlossaryTerm(term="TensorFlow", preserve_english=True),
        GlossaryTerm(term="BERT", preserve_english=True),
        GlossaryTerm(term="GPT", preserve_english=True),
        GlossaryTerm(term="attention mechanism", translation="注意力机制"),
        GlossaryTerm(term="self-attention", translation="自注意力"),
        GlossaryTerm(term="multi-head attention", translation="多头注意力"),
        GlossaryTerm(term="embedding", translation="嵌入"),
        GlossaryTerm(term="tokenization", translation="分词"),
        GlossaryTerm(term="batch size", translation="批大小"),
        GlossaryTerm(term="learning rate", translation="学习率"),
        GlossaryTerm(term="loss function", translation="损失函数"),
        GlossaryTerm(term="gradient descent", translation="梯度下降"),
        GlossaryTerm(term="backpropagation", translation="反向传播"),
        GlossaryTerm(term="neural network", translation="神经网络"),
        GlossaryTerm(term="deep learning", translation="深度学习"),
        GlossaryTerm(term="machine learning", translation="机器学习"),
        GlossaryTerm(term="API", preserve_english=True),
        GlossaryTerm(term="REST", preserve_english=True),
        GlossaryTerm(term="JSON", preserve_english=True),
        GlossaryTerm(term="HTTP", preserve_english=True),
        GlossaryTerm(term="URL", preserve_english=True),
    ],
    "mathematics": [
        GlossaryTerm(term="matrix", translation="矩阵"),
        GlossaryTerm(term="vector", translation="向量"),
        GlossaryTerm(term="eigenvalue", translation="特征值"),
        GlossaryTerm(term="convolution", translation="卷积"),
        GlossaryTerm(term="derivative", translation="导数"),
        GlossaryTerm(term="integral", translation="积分"),
        GlossaryTerm(term="gradient", translation="梯度"),
        GlossaryTerm(term="probability", translation="概率"),
        GlossaryTerm(term="distribution", translation="分布"),
    ],
    "physics": [
        GlossaryTerm(term="quantum", translation="量子"),
        GlossaryTerm(term="relativity", translation="相对论"),
        GlossaryTerm(term="photon", translation="光子"),
        GlossaryTerm(term="electron", translation="电子"),
    ],
}


class Glossary:
    """术语表。"""

    def __init__(self, terms: Optional[List[GlossaryTerm]] = None) -> None:
        self._terms: Dict[str, GlossaryTerm] = {}
        if terms:
            for term in terms:
                self._terms[term.term.lower()] = term

    @classmethod
    def from_domain(cls, domain: str) -> Glossary:
        """从预定义领域创建。"""
        terms = DOMAIN_GLOSSARIES.get(domain, [])
        return cls(terms)

    @classmethod
    def from_domains(cls, domains: List[str]) -> Glossary:
        """从多个领域创建。"""
        all_terms = []
        for domain in domains:
            all_terms.extend(DOMAIN_GLOSSARIES.get(domain, []))
        return cls(all_terms)

    def add(self, term: GlossaryTerm) -> None:
        """添加术语。"""
        self._terms[term.term.lower()] = term

    def lookup(self, term: str) -> Optional[GlossaryTerm]:
        """查找术语。"""
        return self._terms.get(term.lower())

    def protect(self, text: str) -> str:
        """保护术语不被翻译。"""
        # 用占位符替换术语
        placeholders = {}
        result = text

        for i, (key, term) in enumerate(self._terms.items()):
            pattern = re.compile(re.escape(term.term), re.IGNORECASE)
            if pattern.search(result):
                placeholder = f"__GLOSSARY_{i}__"
                placeholders[placeholder] = term
                result = pattern.sub(placeholder, result)

        return result, placeholders

    def restore(self, text: str, placeholders: Dict[str, GlossaryTerm]) -> str:
        """恢复术语。"""
        result = text
        for placeholder, term in placeholders.items():
            replacement = term.translation if term.translation else term.term
            result = result.replace(placeholder, replacement)
        return result

    @property
    def size(self) -> int:
        return len(self._terms)

    def summary(self) -> str:
        return f"Glossary({self.size} terms)"
