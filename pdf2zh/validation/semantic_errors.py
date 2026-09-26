"""SemanticErrorTaxonomy — 语义错误分类。

分类翻译语义错误，支持根因分析。

用法：
    taxonomy = SemanticErrorTaxonomy()
    error = taxonomy.classify(source, translated, reference)
    # SemanticError(type="wrong_term", severity="high", details={...})
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class SemanticErrorType(Enum):
    """语义错误类型。"""

    # 意义错误
    MEANING_LOSS = "meaning_loss"
    MEANING_CHANGE = "meaning_change"
    HALLUCINATION = "hallucination"
    OMISSION = "omission"

    # 术语错误
    WRONG_TERM = "wrong_term"
    INCONSISTENT_TERM = "inconsistent_term"
    UNTRANSLATED_TERM = "untranslated_term"

    # 数值错误
    NUMBER_CHANGED = "number_changed"
    UNIT_CHANGED = "unit_changed"
    FORMULA_CHANGED = "formula_changed"

    # 结构错误
    REFERENCE_BREAK = "reference_break"
    TABLE_ALIGNMENT = "table_alignment"
    TOC_FORMAT = "toc_format"

    # 风格错误
    TONE_MISMATCH = "tone_mismatch"
    REGISTER_MISMATCH = "register_mismatch"


class Severity(Enum):
    """严重程度。"""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class SemanticError:
    """语义错误。"""

    type: SemanticErrorType = SemanticErrorType.MEANING_LOSS
    severity: Severity = Severity.MEDIUM
    description: str = ""
    source_span: str = ""
    translated_span: str = ""
    expected_span: str = ""
    details: Dict[str, Any] = field(default_factory=dict)


class SemanticErrorTaxonomy:
    """语义错误分类系统。"""

    def __init__(self) -> None:
        self._errors: List[SemanticError] = []

    def classify(
        self,
        source: str,
        translated: str,
        reference: Optional[str] = None,
    ) -> List[SemanticError]:
        """分类语义错误。"""
        errors = []

        # 1. 意义错误
        meaning_errors = self._check_meaning(source, translated, reference)
        errors.extend(meaning_errors)

        # 2. 术语错误
        term_errors = self._check_terminology(source, translated)
        errors.extend(term_errors)

        # 3. 数值错误
        num_errors = self._check_numerical(source, translated)
        errors.extend(num_errors)

        # 4. 结构错误
        struct_errors = self._check_structural(source, translated)
        errors.extend(struct_errors)

        self._errors.extend(errors)
        return errors

    def _check_meaning(
        self,
        source: str,
        translated: str,
        reference: Optional[str],
    ) -> List[SemanticError]:
        """检查意义错误。"""
        errors = []

        # 长度异常
        ratio = len(translated) / len(source) if source else 0
        if ratio > 3.0:
            errors.append(
                SemanticError(
                    type=SemanticErrorType.HALLUCINATION,
                    severity=Severity.HIGH,
                    description=f"Translation too long: {ratio:.1f}x",
                    source_span=source[:100],
                    translated_span=translated[:100],
                )
            )
        elif ratio < 0.3:
            errors.append(
                SemanticError(
                    type=SemanticErrorType.OMISSION,
                    severity=Severity.HIGH,
                    description=f"Translation too short: {ratio:.1f}x",
                    source_span=source[:100],
                    translated_span=translated[:100],
                )
            )

        # 有参考时比较
        if reference:
            sim = self._similarity(translated, reference)
            if sim < 0.5:
                errors.append(
                    SemanticError(
                        type=SemanticErrorType.MEANING_CHANGE,
                        severity=Severity.CRITICAL,
                        description=f"Low similarity to reference: {sim:.2f}",
                        source_span=source[:100],
                        translated_span=translated[:100],
                        expected_span=reference[:100],
                    )
                )

        return errors

    def _check_terminology(
        self,
        source: str,
        translated: str,
    ) -> List[SemanticError]:
        """检查术语错误。"""
        errors = []

        # 技术术语
        tech_terms = {
            "GPU": "GPU",
            "CPU": "CPU",
            "CUDA": "CUDA",
            "PyTorch": "PyTorch",
            "TensorFlow": "TensorFlow",
            "BERT": "BERT",
            "GPT": "GPT",
            "Transformer": "Transformer",
            "attention mechanism": "注意力机制",
            "neural network": "神经网络",
            "deep learning": "深度学习",
        }

        for term, expected in tech_terms.items():
            if term.lower() in source.lower():
                # 检查是否保留或正确翻译
                if (
                    term.lower() not in translated.lower()
                    and expected not in translated
                ):
                    errors.append(
                        SemanticError(
                            type=SemanticErrorType.WRONG_TERM,
                            severity=Severity.MEDIUM,
                            description=f"Term '{term}' not preserved",
                            source_span=term,
                            translated_span="",
                            expected_span=expected,
                        )
                    )

        return errors

    def _check_numerical(
        self,
        source: str,
        translated: str,
    ) -> List[SemanticError]:
        """检查数值错误。"""
        errors = []

        # 提取数字
        source_nums = re.findall(r"\b\d+\.?\d*%?\b", source)
        translated_nums = re.findall(r"\b\d+\.?\d*%?\b", translated)

        # 检查丢失
        for num in source_nums:
            if num not in translated_nums:
                errors.append(
                    SemanticError(
                        type=SemanticErrorType.NUMBER_CHANGED,
                        severity=Severity.HIGH,
                        description=f"Number '{num}' missing in translation",
                        source_span=num,
                        translated_span="",
                    )
                )

        return errors

    def _check_structural(
        self,
        source: str,
        translated: str,
    ) -> List[SemanticError]:
        """检查结构错误。"""
        errors = []

        # 检查引用
        source_refs = re.findall(r"\[\d+\]", source)
        translated_refs = re.findall(r"\[\d+\]", translated)

        for ref in source_refs:
            if ref not in translated_refs:
                errors.append(
                    SemanticError(
                        type=SemanticErrorType.REFERENCE_BREAK,
                        severity=Severity.MEDIUM,
                        description=f"Reference '{ref}' missing",
                        source_span=ref,
                        translated_span="",
                    )
                )

        return errors

    def _similarity(self, a: str, b: str) -> float:
        """计算相似度。"""
        if not a or not b:
            return 0.0
        a_chars = set(c for c in a if "\u4e00" <= c <= "\u9fff")
        b_chars = set(c for c in b if "\u4e00" <= c <= "\u9fff")
        if not a_chars:
            return 0.5
        overlap = len(a_chars & b_chars) / len(a_chars)
        return min(1.0, overlap * 1.1)

    def summary(self) -> str:
        type_counts: Dict[str, int] = {}
        for error in self._errors:
            t = error.type.value
            type_counts[t] = type_counts.get(t, 0) + 1

        lines = ["Semantic Error Summary:"]
        for t, count in sorted(type_counts.items(), key=lambda x: -x[1]):
            lines.append(f"  {t}: {count}")

        return "\n".join(lines)
