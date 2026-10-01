"""Backend contract 定义：golden fixture 的 backend 行为契约。

字体层级：
    Font semantic identity  — 语义身份（family/weight/italic）
    Font glyph coverage    — 字形覆盖集
    Font object identity   — 对象编号（xref）
    Font subset name       — 子集名称

must_preserve: 语义层级（不同 backend 必须一致）
allowed_changes: 对象层级（不同 backend 可以不同）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


@dataclass(frozen=True, slots=True)
class BackendContract:
    """Backend 行为契约：定义 golden regression 的验证边界。

    字体三层模型：
        semantic  → must_preserve（family/weight/glyph_coverage）
        object    → allowed_changes（xref/subset_names）
        xref      → allowed_changes（对象编号）

    Attributes:
        must_preserve: 必须保留的语义元素
        allowed_changes: 允许变化的对象/结构元素
        forbidden_changes: 禁止变化的元素（即使跨 backend 也必须一致）
    """

    must_preserve: List[str] = field(
        default_factory=lambda: [
            # 页面结构
            "page_count",
            "page_size",
            "rotation",
            "media_box",
            # 字体语义（不是对象编号）
            "font_identity",  # family/weight/italic
            "font_glyph_coverage",  # 字形覆盖集
            # 内容
            "annotations",
            "links",
        ]
    )

    allowed_changes: List[str] = field(
        default_factory=lambda: [
            # 内容
            "content_stream_hash",
            # 对象结构（不同 backend 可以不同）
            "object_ids",
            "xref_structure",
            "internal_references",
            "compression",
            # 字体对象层（不是语义层）
            "font_object_ids",  # xref 编号
            "font_subset_names",  # 子集名称
        ]
    )

    forbidden_changes: List[str] = field(
        default_factory=lambda: [
            "page_index",
            "source_page_hash",
            "geometry.rotation",
            "geometry.media_box",
        ]
    )


# ── 预定义契约 ──────────────────────────────────────────────────────

CONTRACT_MUPDF = BackendContract()

CONTRACT_PIKEPDF = BackendContract(
    must_preserve=[
        "page_count",
        "page_size",
        "rotation",
        "media_box",
        "font_identity",
        "font_glyph_coverage",
    ],
    allowed_changes=[
        "content_stream_hash",
        "object_ids",
        "xref_structure",
        "internal_references",
        "compression",
        "font_object_ids",
        "font_subset_names",
        "annotations",  # Pikepdf 第一版不保留
        "links",  # Pikepdf 第一版不保留
    ],
)

CONTRACT_MINIMAL = BackendContract(
    must_preserve=[
        "page_count",
        "page_size",
        "rotation",
    ],
    allowed_changes=[
        "content_stream_hash",
        "object_ids",
        "xref_structure",
        "internal_references",
        "compression",
        "font_object_ids",
        "font_subset_names",
        "font_identity",
        "font_glyph_coverage",
        "annotations",
        "links",
        "media_box",
    ],
)
