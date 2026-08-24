"""Module: V6.0 Review Agent & Quality Pipeline.

Implements the report's "协同双代理" (collaborative dual agents) reviewer side
plus the quality gate: after translation, a reviewer agent checks each chunk
for formula integrity, glossary adherence, number preservation and fluency,
and re-translates flagged chunks (optionally with the stronger model).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ReviewIssue:
    code: str  # e.g. FORMULA_CHANGED, GLOSSARY_VIOLATION
    node_id: str
    message: str
    severity: str = "warning"  # info | warning | error

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "node_id": self.node_id,
            "message": self.message,
            "severity": self.severity,
        }


@dataclass
class ReviewResult:
    node_id: str
    passed: bool
    issues: List[ReviewIssue] = field(default_factory=list)
    action: str = "keep"  # keep | retranslate

    @property
    def error_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "error")

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "passed": self.passed,
            "issues": [i.to_dict() for i in self.issues],
            "action": self.action,
        }


# Simple token extraction for formula comparison.
_MATH_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+(?:\.\d+)?")
_MATH_OPS_RE = re.compile(r"[=<>+*/\^_{}|\\]")

# Common words that are NOT formula symbols (skip in formula check).
_STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "that",
    "this",
    "are",
    "was",
    "were",
    "from",
    "into",
    "over",
    "under",
    "using",
    "used",
    "uses",
    "use",
    "where",
    "which",
    "what",
    "then",
    "than",
    "when",
    "its",
    "it",
    "on",
    "in",
    "is",
    "of",
    "to",
    "as",
    "an",
    "by",
    "at",
    "be",
    "not",
}


class ReviewAgent:
    """Rule-based reviewer for translated chunks.

    Checks:
      1. Formula integrity   — every math token present in the source must
                               appear in the translation.
      2. Number preservation — digits survive translation.
      3. Glossary adherence  — pinned terms appear unchanged.
      4. Language sanity     — translated text should not equal source verbatim
                               when translation was expected.
    """

    def __init__(
        self,
        glossary: Optional[Dict[str, str]] = None,
        require_translation: bool = True,
    ) -> None:
        self.glossary = dict(glossary or {})
        self.require_translation = require_translation

    def check_formula_integrity(
        self, source: str, translated: str, node_id: str
    ) -> List[ReviewIssue]:
        issues = []
        # Only meaningful when the source actually contains math operators.
        if _MATH_OPS_RE.search(source) is None:
            return issues
        src_tokens = {
            t for t in _MATH_TOKEN_RE.findall(source) if t.lower() not in _STOPWORDS
        }
        tr_tokens = {
            t for t in _MATH_TOKEN_RE.findall(translated) if t.lower() not in _STOPWORDS
        }
        missing = src_tokens - tr_tokens
        # Numbers handled by the number check; only letter tokens matter here.
        missing_alpha = {t for t in missing if not t.replace(".", "").isdigit()}
        if missing_alpha:
            issues.append(
                ReviewIssue(
                    code="FORMULA_CHANGED",
                    node_id=node_id,
                    message=f"Formula tokens missing: {sorted(missing_alpha)[:8]}",
                    severity="error",
                )
            )
        return issues

    def check_numbers(
        self, source: str, translated: str, node_id: str
    ) -> List[ReviewIssue]:
        src_nums = set(re.findall(r"\d+", source))
        tr_nums = set(re.findall(r"\d+", translated))
        missing = src_nums - tr_nums
        if missing:
            return [
                ReviewIssue(
                    code="NUMBER_CHANGED",
                    node_id=node_id,
                    message=f"Numbers missing: {sorted(missing)[:8]}",
                    severity="warning",
                )
            ]
        return []

    def check_glossary(
        self, source: str, translated: str, node_id: str
    ) -> List[ReviewIssue]:
        issues = []
        for term, pinned in self.glossary.items():
            if term.lower() in source.lower() and pinned not in translated:
                issues.append(
                    ReviewIssue(
                        code="GLOSSARY_VIOLATION",
                        node_id=node_id,
                        message=f"'{term}' should appear as '{pinned}'",
                        severity="warning",
                    )
                )
        return issues

    def check_identity(
        self, source: str, translated: str, node_id: str
    ) -> List[ReviewIssue]:
        if (
            self.require_translation
            and source.strip()
            and source.strip() == translated.strip()
        ):
            return [
                ReviewIssue(
                    code="UNTRANSLATED",
                    node_id=node_id,
                    message="Translation equals source verbatim",
                    severity="error",
                )
            ]
        return []

    def review(
        self,
        node_id: str,
        source: str,
        translated: str,
        is_formula: bool = False,
        skip_formula_check: bool = False,
    ) -> ReviewResult:
        """Run all checks on one chunk and produce a ReviewResult."""
        issues: List[ReviewIssue] = []
        if not skip_formula_check and not is_formula:
            issues.extend(self.check_formula_integrity(source, translated, node_id))
            issues.extend(self.check_glossary(source, translated, node_id))
        issues.extend(self.check_numbers(source, translated, node_id))
        issues.extend(self.check_identity(source, translated, node_id))
        has_errors = any(i.severity == "error" for i in issues)
        action = "retranslate" if has_errors else "keep"
        return ReviewResult(
            node_id=node_id,
            passed=not has_errors,
            issues=issues,
            action=action,
        )


class QualityPipeline:
    """Batch review + optional re-translation with a stronger model."""

    def __init__(
        self,
        reviewer: Optional[ReviewAgent] = None,
        retranslator: Optional[Callable[[str], str]] = None,
    ) -> None:
        self.reviewer = reviewer or ReviewAgent()
        self.retranslator = retranslator

    def run(
        self,
        translated_map: Dict[str, Dict[str, str]],
        is_formula_map: Optional[Dict[str, bool]] = None,
    ) -> dict:
        """Review a {node_id: {source, translated}} map.

        Returns dict with per-node ReviewResult plus a final quality score.
        """
        is_formula_map = is_formula_map or {}
        results: Dict[str, ReviewResult] = {}
        final_translations: Dict[str, str] = {}
        total = 0
        errors = 0

        for node_id, pair in translated_map.items():
            source = pair.get("source", "")
            translated = pair.get("translated", "")
            result = self.reviewer.review(
                node_id,
                source,
                translated,
                is_formula=is_formula_map.get(node_id, False),
            )
            if result.action == "retranslate" and self.retranslator is not None:
                retried = self.retranslator(source)
                result2 = self.reviewer.review(
                    node_id,
                    source,
                    retried,
                    is_formula=is_formula_map.get(node_id, False),
                )
                if result2.passed:
                    result = result2
                    final_translations[node_id] = retried
                else:
                    result.issues.append(
                        ReviewIssue(
                            code="RETRANSLATE_FAILED",
                            node_id=node_id,
                            message="Second pass still fails review",
                            severity="error",
                        )
                    )
                    final_translations[node_id] = retried
            else:
                final_translations[node_id] = translated

            results[node_id] = result
            total += 1
            if not result.passed:
                errors += 1

        quality_score = round(1.0 - (errors / total if total else 0.0), 3)
        return {
            "results": {k: v.to_dict() for k, v in results.items()},
            "final_translations": final_translations,
            "quality_score": quality_score,
            "total": total,
            "errors": errors,
        }


__all__ = [
    "ReviewIssue",
    "ReviewResult",
    "ReviewAgent",
    "QualityPipeline",
]
