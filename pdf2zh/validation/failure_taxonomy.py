"""FailureTaxonomy — 失败分类系统。

分类翻译失败原因，支持根因分析。

用法：
    taxonomy = FailureTaxonomy()
    category = taxonomy.classify(error)
    # FailureCategory(type="translation_overflow", severity="medium")
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class FailureType(Enum):
    """失败类型。"""

    # 翻译相关
    TRANSLATION_OVERFLOW = "translation_overflow"
    TRANSLATION_TRUNCATION = "translation_truncation"
    TRANSLATION_API_ERROR = "translation_api_error"
    TRANSLATION_QUALITY_LOW = "translation_quality_low"

    # 布局相关
    LAYOUT_OVERFLOW = "layout_overflow"
    LAYOUT_COLLISION = "layout_collision"
    LAYOUT_FONT_MISSING = "layout_font_missing"

    # PDF 相关
    PDF_CORRUPT = "pdf_corrupt"
    PDF_PASSWORD = "pdf_password"
    PDF_ENCRYPTED = "pdf_encrypted"

    # 解析相关
    PARSER_ERROR = "parser_error"
    PARSER_TIMEOUT = "parser_timeout"

    # 资源相关
    RESOURCE_MEMORY = "resource_memory"
    RESOURCE_DISK = "resource_disk"

    # 其他
    UNKNOWN = "unknown"


class Severity(Enum):
    """严重程度。"""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class FailureCategory:
    """失败分类。"""

    type: FailureType = FailureType.UNKNOWN
    severity: Severity = Severity.MEDIUM
    description: str = ""
    suggested_fix: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class FailureInstance:
    """失败实例。"""

    category: FailureCategory = field(default_factory=FailureCategory)
    page_index: int = -1
    block_index: int = -1
    error_message: str = ""
    timestamp: float = 0.0
    context: Dict[str, Any] = field(default_factory=dict)


class FailureTaxonomy:
    """失败分类系统。"""

    def __init__(self) -> None:
        self._patterns = self._build_patterns()

    def _build_patterns(self) -> List[Dict]:
        """构建分类规则。"""
        return [
            # 翻译溢出
            {
                "pattern": r"overflow|too long|exceeds.*limit",
                "type": FailureType.TRANSLATION_OVERFLOW,
                "severity": Severity.MEDIUM,
                "fix": "Use concise translation mode or reduce max_length_ratio",
            },
            # 翻译截断
            {
                "pattern": r"truncat|incomplete|cut off",
                "type": FailureType.TRANSLATION_TRUNCATION,
                "severity": Severity.MEDIUM,
                "fix": "Increase max_tokens or split text into smaller chunks",
            },
            # API 错误
            {
                "pattern": r"api.*error|rate.*limit|timeout|5\d{2}",
                "type": FailureType.TRANSLATION_API_ERROR,
                "severity": Severity.HIGH,
                "fix": "Add retry with exponential backoff",
            },
            # 布局溢出
            {
                "pattern": r"layout.*overflow|bbox.*exceed|text.*overflow",
                "type": FailureType.LAYOUT_OVERFLOW,
                "severity": Severity.MEDIUM,
                "fix": "Reduce font size or use shorter translation",
            },
            # 字体缺失
            {
                "pattern": r"font.*missing|glyph.*not found|character.*unsupported",
                "type": FailureType.LAYOUT_FONT_MISSING,
                "severity": Severity.HIGH,
                "fix": "Add font fallback or embed missing fonts",
            },
            # PDF 损坏
            {
                "pattern": r"corrupt|invalid.*pdf|cannot.*open|malformed",
                "type": FailureType.PDF_CORRUPT,
                "severity": Severity.CRITICAL,
                "fix": "Use PDF repair tool or skip corrupted pages",
            },
            # 密码保护
            {
                "pattern": r"password|encrypted|protected",
                "type": FailureType.PDF_PASSWORD,
                "severity": Severity.CRITICAL,
                "fix": "Provide password or skip encrypted PDFs",
            },
            # 解析错误
            {
                "pattern": r"parse.*error|syntax.*error|token.*error",
                "type": FailureType.PARSER_ERROR,
                "severity": Severity.HIGH,
                "fix": "Check PDF structure or use different parser",
            },
            # 内存不足
            {
                "pattern": r"memory|out of memory|oom|alloc.*fail",
                "type": FailureType.RESOURCE_MEMORY,
                "severity": Severity.HIGH,
                "fix": "Reduce batch size or enable streaming mode",
            },
        ]

    def classify(self, error: str) -> FailureCategory:
        """分类错误。"""
        error_lower = error.lower()

        for rule in self._patterns:
            if re.search(rule["pattern"], error_lower):
                return FailureCategory(
                    type=rule["type"],
                    severity=rule["severity"],
                    description=error,
                    suggested_fix=rule["fix"],
                )

        return FailureCategory(
            type=FailureType.UNKNOWN,
            severity=Severity.MEDIUM,
            description=error,
            suggested_fix="Check logs for details",
        )

    def classify_exception(self, exc: Exception) -> FailureCategory:
        """分类异常。"""
        return self.classify(str(exc))

    def get_fix_suggestions(self, category: FailureCategory) -> List[str]:
        """获取修复建议。"""
        suggestions = [category.suggested_fix]

        # 根据类型添加额外建议
        if category.type == FailureType.TRANSLATION_OVERFLOW:
            suggestions.extend(
                [
                    "Enable shorten_if_overflow in TranslationStrategy",
                    "Use TOCTranslator for TOC content",
                    "Set max_length_ratio < 1.5",
                ]
            )
        elif category.type == FailureType.LAYOUT_FONT_MISSING:
            suggestions.extend(
                [
                    "Add CJK fonts to font pool",
                    "Enable font fallback",
                    "Use Unicode-compatible fonts",
                ]
            )
        elif category.type == FailureType.RESOURCE_MEMORY:
            suggestions.extend(
                [
                    "Enable streaming pipeline mode",
                    "Reduce worker count",
                    "Enable memory pressure controller",
                ]
            )

        return suggestions

    def summary(self, failures: List[FailureInstance]) -> str:
        """汇总失败统计。"""
        type_counts: Dict[str, int] = {}
        severity_counts: Dict[str, int] = {}

        for f in failures:
            type_name = f.category.type.value
            sev_name = f.category.severity.value
            type_counts[type_name] = type_counts.get(type_name, 0) + 1
            severity_counts[sev_name] = severity_counts.get(sev_name, 0) + 1

        lines = [
            "Failure Summary:",
            f"  Total failures: {len(failures)}",
            "",
            "By Type:",
        ]
        for t, count in sorted(type_counts.items(), key=lambda x: -x[1]):
            lines.append(f"    {t}: {count}")

        lines.append("")
        lines.append("By Severity:")
        for s, count in sorted(severity_counts.items(), key=lambda x: -x[1]):
            lines.append(f"    {s}: {count}")

        return "\n".join(lines)
