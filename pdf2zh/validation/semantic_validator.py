"""SemanticValidator — 语义验证器。

检查翻译是否保持关键语义元素。

用法：
    validator = SemanticValidator()
    result = validator.validate(source_text, translated_text)
    # SemanticResult(score=0.95, numbers_preserved=True, ...)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set


@dataclass
class SemanticResult:
    """语义验证结果。"""

    score: float = 0.0
    numbers_preserved: bool = True
    entities_preserved: bool = True
    formulas_preserved: bool = True
    references_preserved: bool = True
    citations_preserved: bool = True
    issues: List[str] = field(default_factory=list)

    @property
    def is_perfect(self) -> bool:
        return self.score >= 0.99

    @property
    def is_good(self) -> bool:
        return self.score >= 0.9

    def summary(self) -> str:
        return (
            f"SemanticResult: score={self.score:.2f}, "
            f"numbers={'OK' if self.numbers_preserved else 'FAIL'}, "
            f"entities={'OK' if self.entities_preserved else 'FAIL'}, "
            f"formulas={'OK' if self.formulas_preserved else 'FAIL'}, "
            f"refs={'OK' if self.references_preserved else 'FAIL'}"
        )


class SemanticValidator:
    """语义验证器。"""

    # 数字模式
    NUMBER_PATTERNS = [
        r"\b\d+\.?\d*%?\b",  # 普通数字
        r"\b\d{4}\b",  # 年份
        r"\b\d+\.?\d*[eE][+-]?\d+\b",  # 科学计数法
        r"\b\d+/\d+\b",  # 分数
        r"\b\d+\.\d+\b",  # 小数
    ]

    # 引用模式
    REFERENCE_PATTERNS = [
        r"\[\d+\]",  # [12]
        r"\(\d+\)",  # (12)
        r"Fig\.?\s*\d+",  # Figure 3
        r"Table\s*\d+",  # Table 4
        r"Equation\s*\(\d+\)",  # Equation (3)
        r"Chapter\s*\d+",  # Chapter 5
        r"Section\s*\d+",  # Section 2.1
    ]

    # 公式模式
    FORMULA_PATTERNS = [
        r"\$[^$]+\$",  # LaTeX inline
        r"\\begin\{[^}]+\}",  # LaTeX environment
        r"[a-zA-Z]\s*[=<>]\s*\d+",  # x = 5
        r"[a-zA-Z]\s*\^\s*\{[^}]+\}",  # x^{2}
        r"[a-zA-Z]\s*_\s*\{[^}]+\}",  # x_{i}
    ]

    # 实体模式（技术术语）
    ENTITY_PATTERNS = [
        r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b",  # CamelCase
        r"\b(?:GPU|CPU|CUDA|PyTorch|TensorFlow|BERT|GPT)\b",  # 技术缩写
        r"\b(?:attention|transformer|neural|network)\b",  # 技术术语
    ]

    def __init__(self) -> None:
        self._compiled_patterns = {
            "numbers": [re.compile(p) for p in self.NUMBER_PATTERNS],
            "references": [
                re.compile(p, re.IGNORECASE) for p in self.REFERENCE_PATTERNS
            ],
            "formulas": [re.compile(p) for p in self.FORMULA_PATTERNS],
            "entities": [re.compile(p, re.IGNORECASE) for p in self.ENTITY_PATTERNS],
        }

    def validate(
        self,
        source: str,
        translated: str,
        check_numbers: bool = True,
        check_references: bool = True,
        check_formulas: bool = True,
        check_entities: bool = True,
    ) -> SemanticResult:
        """验证语义保持。"""
        result = SemanticResult()
        issues = []

        # 1. 数字
        if check_numbers:
            result.numbers_preserved = self._check_numbers(source, translated, issues)

        # 2. 引用
        if check_references:
            result.references_preserved = self._check_references(
                source, translated, issues
            )

        # 3. 公式
        if check_formulas:
            result.formulas_preserved = self._check_formulas(source, translated, issues)

        # 4. 实体
        if check_entities:
            result.entities_preserved = self._check_entities(source, translated, issues)

        result.issues = issues

        # 计算分数
        checks = [
            result.numbers_preserved,
            result.references_preserved,
            result.formulas_preserved,
            result.entities_preserved,
        ]
        result.score = sum(checks) / len(checks) if checks else 1.0

        return result

    def _check_numbers(
        self,
        source: str,
        translated: str,
        issues: List[str],
    ) -> bool:
        """检查数字保持。"""
        source_numbers = self._extract_numbers(source)
        translated_numbers = self._extract_numbers(translated)

        missing = source_numbers - translated_numbers
        if missing:
            issues.append(f"missing_numbers: {missing}")
            return False
        return True

    def _extract_numbers(self, text: str) -> Set[str]:
        """提取数字。"""
        numbers = set()
        for pattern in self._compiled_patterns["numbers"]:
            for match in pattern.finditer(text):
                numbers.add(match.group())
        return numbers

    def _check_references(
        self,
        source: str,
        translated: str,
        issues: List[str],
    ) -> bool:
        """检查引用保持。"""
        source_refs = self._extract_references(source)
        translated_refs = self._extract_references(translated)

        missing = source_refs - translated_refs
        if missing:
            issues.append(f"missing_references: {missing}")
            return False
        return True

    def _extract_references(self, text: str) -> Set[str]:
        """提取引用。"""
        refs = set()
        for pattern in self._compiled_patterns["references"]:
            for match in pattern.finditer(text):
                refs.add(match.group())
        return refs

    def _check_formulas(
        self,
        source: str,
        translated: str,
        issues: List[str],
    ) -> bool:
        """检查公式保持。"""
        source_formulas = self._extract_formulas(source)
        translated_formulas = self._extract_formulas(translated)

        # 公式应该完全相同
        missing = source_formulas - translated_formulas
        if missing:
            issues.append(f"modified_formulas: {missing}")
            return False
        return True

    def _extract_formulas(self, text: str) -> Set[str]:
        """提取公式。"""
        formulas = set()
        for pattern in self._compiled_patterns["formulas"]:
            for match in pattern.finditer(text):
                formulas.add(match.group())
        return formulas

    def _check_entities(
        self,
        source: str,
        translated: str,
        issues: List[str],
    ) -> bool:
        """检查实体保持。"""
        source_entities = self._extract_entities(source)
        translated_entities = self._extract_entities(translated)

        # 实体应该保留
        missing = source_entities - translated_entities
        if missing:
            # 允许部分实体被翻译
            if len(missing) > len(source_entities) * 0.3:
                issues.append(f"missing_entities: {missing}")
                return False
        return True

    def _extract_entities(self, text: str) -> Set[str]:
        """提取实体。"""
        entities = set()
        for pattern in self._compiled_patterns["entities"]:
            for match in pattern.finditer(text):
                entities.add(match.group())
        return entities

    def batch_validate(
        self,
        items: List[Dict[str, str]],
    ) -> Dict[str, Any]:
        """批量验证。"""
        results = []
        for item in items:
            result = self.validate(
                item.get("source", ""),
                item.get("translated", ""),
            )
            results.append(result)

        avg_score = sum(r.score for r in results) / len(results) if results else 0
        fail_count = sum(1 for r in results if not r.is_good)

        return {
            "count": len(results),
            "avg_score": avg_score,
            "fail_count": fail_count,
            "results": results,
        }
