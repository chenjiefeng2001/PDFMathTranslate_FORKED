"""Module: DomainGlossary — Phase 4.2 领域知识层（Domain Adapter）。

术语固定翻译（kernel→内核 等）避免领域歧义（field→域/字段/场）；
``apply`` 做整词替换（后处理），``detect_domain`` 复用 planner_chain 的
DomainDetector。纯逻辑、无网络。
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

DEFAULT_GLOSSARIES: Dict[str, Dict[str, str]] = {
    "cs": {
        "kernel": "内核",
        "scheduler": "调度器",
        "thread": "线程",
        "process": "进程",
        "cache": "缓存",
        "buffer": "缓冲区",
        "compiler": "编译器",
        "runtime": "运行时",
        "stack": "栈",
        "heap": "堆",
        "deadlock": "死锁",
        "deadlock detection": "死锁检测",
        "garbage collector": "垃圾回收器",
        "instruction": "指令",
        "register": "寄存器",
        "scheduler": "调度器",
    },
    "math": {
        "field": "域",
        "ring": "环",
        "space": "空间",
        "group": "群",
        "manifold": "流形",
        "topology": "拓扑",
        "eigenvalue": "特征值",
        "eigenvector": "特征向量",
        "gradient": "梯度",
        "lemma": "引理",
        "corollary": "推论",
        "theorem": "定理",
    },
    "medicine": {
        "lesion": "病灶",
        "symptom": "症状",
        "biopsy": "活检",
        "morbidity": "发病率",
        "mortality": "死亡率",
        "prognosis": "预后",
        "pathogen": "病原体",
        "antibody": "抗体",
    },
    "law": {
        "plaintiff": "原告",
        "defendant": "被告",
        "jurisdiction": "管辖权",
        "statute": "法规",
        "precedent": "判例",
        "tort": "侵权",
    },
    "engineering": {
        "stress": "应力",
        "strain": "应变",
        "fatigue": "疲劳",
        "tolerance": "公差",
        "torque": "扭矩",
        "bearing": "轴承",
        "vibration": "振动",
        "damping": "阻尼",
    },
}

_DOMAIN_KEYWORDS = {
    "cs": (
        "kernel",
        "scheduler",
        "thread",
        "compiler",
        "runtime",
        "deadlock",
        "register",
        "instruction",
    ),
    "math": (
        "lemma",
        "theorem",
        "eigenvalue",
        "manifold",
        "topology",
        "gradient",
        "vector space",
    ),
    "medicine": ("lesion", "patient", "clinical", "biopsy", "antibody", "symptom"),
    "law": (
        "plaintiff",
        "defendant",
        "statute",
        "jurisdiction",
        "contract",
        "liability",
    ),
    "engineering": (
        "stress",
        "strain",
        "fatigue",
        "torque",
        "bearing",
        "vibration",
        "tolerance",
    ),
}


def detect_domain(text: str, detector=None) -> str:
    """文本 → 领域键（cs/math/medicine/law/engineering/generic）。

    优先使用调用方给的 detector（planner_chain.DomainDetector），
    否则按关键词启发。
    """
    t = (text or "").lower()
    if detector is not None:
        try:
            primary = detector.primary_domain(text)
            if primary:
                for key in _DOMAIN_KEYWORDS:
                    if key in primary.lower() or primary.lower() in key:
                        return key
                return str(primary).lower()
        except Exception:  # noqa: BLE001
            pass
    best, best_hits = "generic", 0
    for key, kws in _DOMAIN_KEYWORDS.items():
        hits = sum(1 for kw in kws if kw in t)
        if hits > best_hits:
            best, best_hits = key, hits
    return best


def _word_boundary_re(term: str) -> re.Pattern:
    # 兼容复数/三单（thread→threads）；前后非字母数字（kernel32 不误替换）
    return re.compile(
        r"(?<![A-Za-z0-9])" + re.escape(term) + r"s?(?![A-Za-z0-9])", re.IGNORECASE
    )


class DomainGlossary:
    """领域词典：apply(text) 把固定术语替换为领域译文（后处理）。"""

    def __init__(
        self,
        domains: Optional[List[str]] = None,
        glossaries: Optional[Dict[str, Dict[str, str]]] = None,
    ) -> None:
        self.glossaries = dict(glossaries or DEFAULT_GLOSSARIES)
        self.domains = list(domains or []) or list(self.glossaries)

    def terms(self, domain: Optional[str] = None) -> Dict[str, str]:
        if domain:
            return dict(self.glossaries.get(domain, {}))
        out: Dict[str, str] = {}
        for d in self.domains:
            out.update(self.glossaries.get(d, {}))
        return out

    def apply(self, text: str, domain: Optional[str] = None) -> str:
        out = text or ""
        for term, translation in self.terms(domain).items():
            out = _word_boundary_re(term).sub(translation, out)
        return out

    def hint(self, text: str) -> str:
        """术语提示串（供 LLM/上下文注入，无副作用）。"""
        hits = [t for t in self.terms() if _word_boundary_re(t).search(text or "")]
        if not hits:
            return ""
        return "；".join(f"{t}→{self.terms()[t]}" for t in hits[:10])


__all__ = [
    "DEFAULT_GLOSSARIES",
    "detect_domain",
    "DomainGlossary",
]
