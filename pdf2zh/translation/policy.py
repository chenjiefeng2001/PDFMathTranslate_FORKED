"""SemanticPolicy — 语义策略引擎。

不同 block 类型有不同的翻译策略。

用法：
    policy = SemanticPolicy.resolve(block)
    # SemanticPolicy(block_type="TOC", preserve=["leader", "page_number"])
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class TranslationMode(Enum):
    """翻译模式。"""

    NORMAL = "normal"  # 正文
    STRUCTURE_PRESERVE = "structure_preserve"  # TOC, 结构化内容
    STYLE_PRESERVE = "style_preserve"  # 标题, 保持样式
    SKIP = "skip"  # 公式, 代码
    CELL_PRESERVE = "cell_preserve"  # 表格
    COMMENT_ONLY = "comment_only"  # 代码注释
    SHORT = "short"  # Caption, 简短翻译
    COMPACT = "compact"  # Footnote, 紧凑


@dataclass
class SemanticPolicy:
    """语义策略。"""

    block_type: str = "unknown"
    translation_mode: TranslationMode = TranslationMode.NORMAL
    preserve: List[str] = field(default_factory=list)
    allow: List[str] = field(default_factory=list)
    forbidden: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def resolve(cls, block: Any) -> SemanticPolicy:
        """根据 block 类型解析策略。"""
        block_type = getattr(block, "type", "unknown")
        return cls._resolve_by_type(block_type)

    @classmethod
    def _resolve_by_type(cls, block_type: str) -> SemanticPolicy:
        """根据 block type 解析策略。"""
        policies = {
            "paragraph": cls(
                block_type="paragraph",
                translation_mode=TranslationMode.NORMAL,
                allow=["rephrase"],
            ),
            "title": cls(
                block_type="title",
                translation_mode=TranslationMode.STYLE_PRESERVE,
                preserve=["font_size", "font_weight", "position"],
                forbidden=["wrap"],
            ),
            "heading": cls(
                block_type="heading",
                translation_mode=TranslationMode.STYLE_PRESERVE,
                preserve=["font_size", "font_weight", "position"],
                forbidden=["wrap"],
            ),
            "toc": cls(
                block_type="toc",
                translation_mode=TranslationMode.STRUCTURE_PRESERVE,
                preserve=["leader", "page_number", "indent", "style"],
                forbidden=["bbox_change", "reformat"],
            ),
            "formula": cls(
                block_type="formula",
                translation_mode=TranslationMode.SKIP,
                metadata={"skip_reason": "mathematical_expression"},
            ),
            "code": cls(
                block_type="code",
                translation_mode=TranslationMode.COMMENT_ONLY,
                preserve=["syntax", "indentation"],
                forbidden=["modify_content"],
            ),
            "table": cls(
                block_type="table",
                translation_mode=TranslationMode.CELL_PRESERVE,
                preserve=["rows", "columns", "span", "border"],
                forbidden=["merge_cells", "reformat"],
            ),
            "caption": cls(
                block_type="caption",
                translation_mode=TranslationMode.SHORT,
                allow=["rephrase"],
                metadata={"max_words": 20},
            ),
            "footnote": cls(
                block_type="footnote",
                translation_mode=TranslationMode.COMPACT,
                preserve=["superscript", "position"],
            ),
            "header": cls(
                block_type="header",
                translation_mode=TranslationMode.STRUCTURE_PRESERVE,
                preserve=["position", "style"],
            ),
            "footer": cls(
                block_type="footer",
                translation_mode=TranslationMode.STRUCTURE_PRESERVE,
                preserve=["position", "style"],
            ),
            "image": cls(
                block_type="image",
                translation_mode=TranslationMode.SKIP,
                metadata={"skip_reason": "image_content"},
            ),
        }
        return policies.get(
            block_type,
            cls(
                block_type=block_type,
                translation_mode=TranslationMode.NORMAL,
            ),
        )

    @property
    def should_translate(self) -> bool:
        """是否应该翻译。"""
        return self.translation_mode not in [
            TranslationMode.SKIP,
        ]

    @property
    def should_preserve_structure(self) -> bool:
        """是否应该保持结构。"""
        return self.translation_mode in [
            TranslationMode.STRUCTURE_PRESERVE,
            TranslationMode.CELL_PRESERVE,
        ]


# 预定义策略映射
BLOCK_TYPE_POLICIES: Dict[str, SemanticPolicy] = {
    "paragraph": SemanticPolicy._resolve_by_type("paragraph"),
    "title": SemanticPolicy._resolve_by_type("title"),
    "heading": SemanticPolicy._resolve_by_type("heading"),
    "toc": SemanticPolicy._resolve_by_type("toc"),
    "formula": SemanticPolicy._resolve_by_type("formula"),
    "code": SemanticPolicy._resolve_by_type("code"),
    "table": SemanticPolicy._resolve_by_type("table"),
    "caption": SemanticPolicy._resolve_by_type("caption"),
    "footnote": SemanticPolicy._resolve_by_type("footnote"),
    "header": SemanticPolicy._resolve_by_type("header"),
    "footer": SemanticPolicy._resolve_by_type("footer"),
    "image": SemanticPolicy._resolve_by_type("image"),
}
