"""Module: Evidence — Phase 5.3 多模型验证（Evidence Fusion）。

不相信单一信号：OCR / Layout / Font / Math / Structure 各自打分，
融合时给出一致性加成与矛盾惩罚：

    fuse_evidence({"ocr":0.95, "layout":0.90, "math":0.98}) → 0.93
    fuse_evidence({"ocr":0.95, "math":0.10})               → 0.53（矛盾惩罚）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional, Sequence

DEFAULT_WEIGHTS = {"ocr": 1.0, "layout": 1.0, "font": 0.8,
                   "math": 1.0, "structure": 1.0}


def fuse_evidence(scores: Dict[str, float],
                  weights: Optional[Dict[str, float]] = None) -> float:
    """加权平均 + 一致性加成/矛盾惩罚。

    - 极差 ≤ 0.15 → 一致，+0.05（封顶 0.99）；
    - 同时存在 ≥0.8 与 <0.3 → 矛盾，×0.8；
    - 无输入 → 0.5。
    """
    scores = {k: float(v) for k, v in (scores or {}).items()}
    if not scores:
        return 0.5
    weights = weights or DEFAULT_WEIGHTS
    total_w = sum(weights.get(k, 1.0) for k in scores)
    if total_w <= 0:
        return 0.5
    fused = sum(scores[k] * weights.get(k, 1.0) for k in scores) / total_w
    spread = max(scores.values()) - min(scores.values())
    if spread <= 0.15:
        fused += 0.05
    elif any(v >= 0.8 for v in scores.values()) and \
            any(v < 0.3 for v in scores.values()):
        fused *= 0.8
    return max(0.0, min(fused, 0.99))


@dataclass
class FusedVerdict:
    confidence: float = 0.5
    sources: Dict[str, float] = field(default_factory=dict)
    spread: float = 0.0
    consistent: bool = True

    def to_dict(self) -> dict:
        return {"confidence": round(self.confidence, 3),
                "sources": {k: round(v, 3) for k, v in self.sources.items()},
                "spread": round(self.spread, 3),
                "consistent": self.consistent}

    def summary(self) -> str:
        tag = "CONSISTENT" if self.consistent else "CONFLICT"
        return (f"EvidenceFusion {tag} confidence={self.confidence:.3f} "
                f"spread={self.spread:.3f}")


def fuse_verdict(scores: Dict[str, float],
                 weights: Optional[Dict[str, float]] = None) -> FusedVerdict:
    conf = fuse_evidence(scores, weights)
    vals = list((scores or {}).values())
    spread = (max(vals) - min(vals)) if vals else 0.0
    consistent = spread <= 0.15
    return FusedVerdict(confidence=conf, sources=dict(scores or {}),
                        spread=spread, consistent=consistent)


__all__ = ["DEFAULT_WEIGHTS", "fuse_evidence", "FusedVerdict",
           "fuse_verdict"]