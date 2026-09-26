"""AnnotationSchema — 人工标注 schema。

定义标准化的人工标注格式。

用法：
    schema = AnnotationSchema()
    annotation = schema.create_annotation(
        source="...",
        translation="...",
        scores={"semantic": 4, "terminology": 5},
    )
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class QualityScores:
    """质量评分（1-5）。"""

    semantic: int = 3  # 语义准确性
    terminology: int = 3  # 术语准确性
    numerical: int = 3  # 数字/公式一致性
    structural: int = 3  # 结构保持
    readability: int = 3  # 可读性

    @property
    def average(self) -> float:
        scores = [
            self.semantic,
            self.terminology,
            self.numerical,
            self.structural,
            self.readability,
        ]
        return sum(scores) / len(scores)

    @property
    def is_acceptable(self) -> bool:
        return self.average >= 3.0

    @property
    def has_critical_issue(self) -> bool:
        return self.semantic <= 2 or self.numerical <= 2


@dataclass
class ErrorAnnotation:
    """错误标注。"""

    error_type: str = ""
    severity: str = "medium"  # low, medium, high, critical
    description: str = ""
    source_span: str = ""
    translated_span: str = ""
    expected_span: str = ""


@dataclass
class HumanAnnotation:
    """人工标注。"""

    # 元数据
    annotation_id: str = ""
    annotator_id: str = ""
    timestamp: float = 0.0

    # 内容
    source: str = ""
    translation: str = ""
    reference: Optional[str] = None

    # 评分
    scores: QualityScores = field(default_factory=QualityScores)

    # 错误
    errors: List[ErrorAnnotation] = field(default_factory=list)

    # 总体评价
    acceptable: bool = True
    notes: str = ""

    # 上下文
    domain: str = "general"
    block_type: str = "paragraph"
    page_index: int = -1


class AnnotationSchema:
    """标注 schema。"""

    # 评分说明
    SCORE_DESCRIPTIONS = {
        1: "Very Poor - Completely wrong or unintelligible",
        2: "Poor - Major errors, meaning lost",
        3: "Acceptable - Some errors but meaning preserved",
        4: "Good - Minor errors, acceptable quality",
        5: "Excellent - Perfect or near-perfect",
    }

    # 错误类型
    ERROR_TYPES = [
        "meaning_loss",
        "meaning_change",
        "hallucination",
        "omission",
        "wrong_term",
        "inconsistent_term",
        "number_changed",
        "formula_changed",
        "reference_break",
        "table_alignment",
        "style_issue",
        "other",
    ]

    def __init__(self) -> None:
        self._annotations: List[HumanAnnotation] = []

    def create_annotation(
        self,
        source: str,
        translation: str,
        annotator_id: str = "unknown",
        reference: Optional[str] = None,
        domain: str = "general",
        block_type: str = "paragraph",
        page_index: int = -1,
    ) -> HumanAnnotation:
        """创建标注。"""
        annotation = HumanAnnotation(
            annotation_id=f"ann_{int(time.time())}_{len(self._annotations)}",
            annotator_id=annotator_id,
            timestamp=time.time(),
            source=source,
            translation=translation,
            reference=reference,
            domain=domain,
            block_type=block_type,
            page_index=page_index,
        )
        self._annotations.append(annotation)
        return annotation

    def set_scores(
        self,
        annotation_id: str,
        semantic: int = 3,
        terminology: int = 3,
        numerical: int = 3,
        structural: int = 3,
        readability: int = 3,
    ) -> Optional[HumanAnnotation]:
        """设置评分。"""
        for ann in self._annotations:
            if ann.annotation_id == annotation_id:
                ann.scores = QualityScores(
                    semantic=semantic,
                    terminology=terminology,
                    numerical=numerical,
                    structural=structural,
                    readability=readability,
                )
                ann.acceptable = ann.scores.is_acceptable
                return ann
        return None

    def add_error(
        self,
        annotation_id: str,
        error_type: str,
        severity: str = "medium",
        description: str = "",
        source_span: str = "",
        translated_span: str = "",
        expected_span: str = "",
    ) -> Optional[ErrorAnnotation]:
        """添加错误。"""
        for ann in self._annotations:
            if ann.annotation_id == annotation_id:
                error = ErrorAnnotation(
                    error_type=error_type,
                    severity=severity,
                    description=description,
                    source_span=source_span,
                    translated_span=translated_span,
                    expected_span=expected_span,
                )
                ann.errors.append(error)
                return error
        return None

    def get_annotation(self, annotation_id: str) -> Optional[HumanAnnotation]:
        """获取标注。"""
        for ann in self._annotations:
            if ann.annotation_id == annotation_id:
                return ann
        return None

    def get_statistics(self) -> Dict[str, Any]:
        """获取统计。"""
        if not self._annotations:
            return {"total": 0}

        scores = [ann.scores for ann in self._annotations]
        avg_scores = {
            "semantic": sum(s.semantic for s in scores) / len(scores),
            "terminology": sum(s.terminology for s in scores) / len(scores),
            "numerical": sum(s.numerical for s in scores) / len(scores),
            "structural": sum(s.structural for s in scores) / len(scores),
            "readability": sum(s.readability for s in scores) / len(scores),
        }

        acceptable_count = sum(1 for ann in self._annotations if ann.acceptable)

        return {
            "total": len(self._annotations),
            "avg_scores": avg_scores,
            "acceptable_rate": acceptable_count / len(self._annotations),
        }

    def export(self) -> List[Dict[str, Any]]:
        """导出为字典列表。"""
        return [
            {
                "annotation_id": ann.annotation_id,
                "annotator_id": ann.annotator_id,
                "timestamp": ann.timestamp,
                "source": ann.source,
                "translation": ann.translation,
                "reference": ann.reference,
                "scores": {
                    "semantic": ann.scores.semantic,
                    "terminology": ann.scores.terminology,
                    "numerical": ann.scores.numerical,
                    "structural": ann.scores.structural,
                    "readability": ann.scores.readability,
                    "average": ann.scores.average,
                },
                "errors": [
                    {
                        "error_type": e.error_type,
                        "severity": e.severity,
                        "description": e.description,
                    }
                    for e in ann.errors
                ],
                "acceptable": ann.acceptable,
                "notes": ann.notes,
                "domain": ann.domain,
                "block_type": ann.block_type,
            }
            for ann in self._annotations
        ]
