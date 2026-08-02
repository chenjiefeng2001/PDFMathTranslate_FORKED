"""Module: V6.0 Planner Decision Chain — Language / Domain / Confidence.

The report (chapter 5) upgrades the Planner from "prompt template selection"
to an explicit, auditable decision chain:

    Node
      ├─ Language Detection   -> source/target language, skip flags
      ├─ Domain Detection     -> subject domain -> Reasoning Memory
      ├─ Confidence           -> layout confidence -> human-in-the-loop / fallback
      ├─ Translator Route     -> model by node-type x domain x cost
      ├─ Prompt Template      -> by SemanticRole
      ├─ Glossary             -> Document + Entity memory
      ├─ Memory               -> Style + Reasoning layers
      └─ Chunk                -> paragraph/sentence strategy

This module provides the three missing detectors plus a chain orchestrator
that produces a full plan payload consumed by the TranslationPlanner.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# ── Language Detection ──────────────────────────────────────────────────

_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")
_LATIN_RE = re.compile(r"[A-Za-z]")


class LanguageDetector:
    """Heuristic script-based language detection (no external model).

    Detects the dominant script of a text: zh / ja / ko / en / fr / de / ...
    Enough to decide whether a node should be translated or kept.
    """

    @staticmethod
    def detect(text: str) -> str:
        if not text:
            return "unknown"
        cjk = len(_CJK_RE.findall(text))
        latin = len(_LATIN_RE.findall(text))
        if cjk == 0 and latin == 0:
            return "unknown"
        if cjk > latin:
            # disambiguate CJK scripts via kana/hangul
            if re.search(r"[\u3040-\u30ff]", text):
                return "ja"
            if re.search(r"[\uac00-\ud7af]", text):
                return "ko"
            return "zh"
        return "en"  # default Latin -> English (roadmap targets en->zh)

    @staticmethod
    def is_translatable(text: str, source_lang: str = "en") -> bool:
        """True when the node text is in the source language and needs
        translation into the target language."""
        detected = LanguageDetector.detect(text)
        if detected == "unknown":
            return False
        # Already in target language (e.g. zh) -> keep as-is.
        return detected == source_lang


# ── Domain Detection ────────────────────────────────────────────────────

# keyword -> domain; ordered by specificity
DOMAIN_KEYWORDS: Dict[str, str] = {
    "diffusion model": "diffusion",
    "latent diffusion": "diffusion",
    "transformer": "nlp",
    "attention mechanism": "nlp",
    "language model": "nlp",
    "natural language": "nlp",
    "convolutional": "computer_vision",
    "object detection": "computer_vision",
    "segmentation": "computer_vision",
    "reinforcement": "reinforcement_learning",
    "gradient descent": "optimization",
    "convex": "optimization",
    "linear programming": "optimization",
    "bayesian": "statistics",
    "regression": "statistics",
    "hypothesis": "statistics",
    "quantum": "physics",
    "hamiltonian": "physics",
    "protein": "biology",
    "genome": "biology",
    "differential equation": "mathematics",
    "stochastic": "mathematics",
    "theorem": "mathematics",
}


class DomainDetector:
    """Keyword-based subject-domain detection feeding Reasoning Memory."""

    def __init__(self, keywords: Optional[Dict[str, str]] = None) -> None:
        self.keywords = keywords or dict(DOMAIN_KEYWORDS)

    def detect(self, text: str) -> List[str]:
        lower = text.lower()
        hits: Set[str] = set()
        for keyword, domain in self.keywords.items():
            if keyword in lower:
                hits.add(domain)
        return sorted(hits)

    def detect_with_scores(self, text: str) -> List[Tuple[str, int]]:
        lower = text.lower()
        scores: Dict[str, int] = {}
        for keyword, domain in self.keywords.items():
            count = lower.count(keyword)
            if count > 0:
                scores[domain] = scores.get(domain, 0) + count
        return sorted(scores.items(), key=lambda x: -x[1])

    def primary_domain(self, text: str) -> Optional[str]:
        scored = self.detect_with_scores(text)
        return scored[0][0] if scored else None


# ── Confidence Estimation ───────────────────────────────────────────────


@dataclass
class ConfidenceReport:
    score: float
    reasons: List[str] = field(default_factory=list)
    flags_human_review: bool = False

    def to_dict(self) -> dict:
        return {
            "score": round(self.score, 3),
            "reasons": list(self.reasons),
            "flags_human_review": self.flags_human_review,
        }


class ConfidenceEstimator:
    """Estimate how confidently a node can be machine-translated.

    Factors: layout confidence, text length, formula presence, glossary
    coverage. Below the threshold the reviewer agent / human is flagged.
    """

    def __init__(self, low_confidence_threshold: float = 0.45) -> None:
        self.threshold = low_confidence_threshold

    @staticmethod
    def _formula_ratio(text: str) -> float:
        if not text:
            return 0.0
        math_chars = sum(
            1 for ch in text if ch in "=<>≤≥∑∫√πθλμ+-×÷^_{}"
        )
        return math_chars / len(text)

    def estimate(self, text: str, layout_confidence: float = 1.0,
                 glossary_coverage: float = 1.0) -> ConfidenceReport:
        score = 1.0
        reasons: List[str] = []
        if not text:
            return ConfidenceReport(0.0, ["empty text"])
        score *= layout_confidence
        if layout_confidence < 1.0:
            reasons.append(f"layout_confidence={layout_confidence:.2f}")
        if len(text) < 4:
            score *= 0.9
            reasons.append("very short text")
        formula_ratio = self._formula_ratio(text)
        if formula_ratio > 0.3:
            score *= 0.85
            reasons.append(f"high formula ratio={formula_ratio:.2f}")
        score *= max(0.3, glossary_coverage)
        if glossary_coverage < 0.6:
            reasons.append(f"low glossary coverage={glossary_coverage:.2f}")
        score = max(0.0, min(1.0, score))
        return ConfidenceReport(
            score=score, reasons=reasons,
            flags_human_review=score < self.threshold,
        )


# ── Chain Orchestrator ──────────────────────────────────────────────────


@dataclass
class PlanDecision:
    """Auditable output of the decision chain for one node."""

    node_id: str
    language: str = "en"
    translatable: bool = True
    domains: List[str] = field(default_factory=list)
    primary_domain: Optional[str] = None
    confidence: float = 1.0
    confidence_reasons: List[str] = field(default_factory=list)
    flags_human_review: bool = False
    translator: str = "gpt-4o"
    prompt_template: str = "paragraph"
    chunk: str = "paragraph"
    keep_formula: bool = False
    keep_numbers: bool = False

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "language": self.language,
            "translatable": self.translatable,
            "domains": list(self.domains),
            "primary_domain": self.primary_domain,
            "confidence": round(self.confidence, 3),
            "confidence_reasons": list(self.confidence_reasons),
            "flags_human_review": self.flags_human_review,
            "translator": self.translator,
            "prompt_template": self.prompt_template,
            "chunk": self.chunk,
            "keep_formula": self.keep_formula,
            "keep_numbers": self.keep_numbers,
        }


class PlannerChain:
    """Orchestrate the full decision chain for a batch of nodes.

    Steps per node:
      1. language detection (translatable?)
      2. domain detection -> ReasoningMemory seeding
      3. confidence estimation
      4. route translator, prompt template, chunk strategy
    """

    def __init__(self, language_detector: Optional[LanguageDetector] = None,
                 domain_detector: Optional[DomainDetector] = None,
                 confidence_estimator: Optional[ConfidenceEstimator] = None,
                 reasoning_memory=None) -> None:
        self.language_detector = language_detector or LanguageDetector()
        self.domain_detector = domain_detector or DomainDetector()
        self.confidence_estimator = confidence_estimator or ConfidenceEstimator()
        self.reasoning_memory = reasoning_memory

    def _route(self, node, domains: List[str], confidence: float,
               is_formula: bool) -> PlanDecision:
        primary = domains[0] if domains else None
        keep_formula = bool(is_formula)
        keep_numbers = primary in ("mathematics", "physics", "statistics")
        # Cost-aware model routing.
        if is_formula or primary in ("mathematics", "physics"):
            translator = "gpt-4o"
        elif confidence < 0.5:
            translator = "gpt-4o"  # hard cases get a strong model
        else:
            translator = "gpt-4o-mini"
        return PlanDecision(
            node_id=getattr(node, "id", ""),
            domains=domains,
            primary_domain=primary,
            confidence=confidence,
            flags_human_review=confidence < self.confidence_estimator.threshold,
            translator=translator,
            keep_formula=keep_formula,
            keep_numbers=keep_numbers,
        )

    def plan_node(self, node, source_lang: str = "en") -> PlanDecision:
        text = getattr(node, "text", "") or ""
        language = self.language_detector.detect(text)
        translatable = self.language_detector.is_translatable(text, source_lang)
        domains = self.domain_detector.detect(text)
        layout_conf = float(getattr(node, "confidence", 1.0) or 1.0)
        report = self.confidence_estimator.estimate(text, layout_conf)
        is_formula = bool(getattr(node, "is_math", False)) or "math" in str(
            getattr(node, "node_type", "")
        ).lower()
        decision = self._route(node, domains, report.score, is_formula)
        decision.language = language
        decision.translatable = translatable
        decision.confidence_reasons = report.reasons

        if self.reasoning_memory is not None and domains:
            for d in domains:
                self.reasoning_memory.record_domain(
                    d, detail="detected via planner chain", confidence=0.7,
                    source="planner",
                )
        return decision

    def plan_nodes(self, nodes, source_lang: str = "en") -> List[PlanDecision]:
        return [self.plan_node(n, source_lang) for n in nodes]


__all__ = [
    "LanguageDetector", "DomainDetector", "ConfidenceReport",
    "ConfidenceEstimator", "PlanDecision", "PlannerChain", "DOMAIN_KEYWORDS",
]


