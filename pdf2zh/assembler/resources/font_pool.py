"""FontPool — 翻译字体去重池。

Phase 3.3 核心：翻译生成的字体对象共享。

与 resource_pool.py 的区别：
    - resource_pool.py: 优化原 PDF 已有字体（Subset 去重）
    - font_pool.py: 优化翻译生成的字体（全共享）

用法：
    pool = FontPool()

    for page_result in translated_pages:
        font_ref = pool.get_or_create_font(pdf, page_result.resources.fonts[0])
        page["/Font"]["/F1"] = font_ref

    # 统计
    pool.stats  # fonts_shared, fonts_created, etc.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Dict, Optional, Set

logger = logging.getLogger(__name__)

try:
    import pikepdf

    HAS_PIKEPDF = True
except ImportError:
    HAS_PIKEPDF = False


class FontPool:
    """翻译字体去重池：按 FontIdentity 共享字体对象。

    核心机制：
        1. 接收 FontRequirement（来自 PageResult）
        2. 计算 identity hash（family+weight+italic）
        3. 相同 identity 共享同一个 indirect reference
        4. 首次出现时创建字体对象，后续复用

    翻译字体特点：
        - 同一翻译任务使用同一字体（如 NotoSansSC）
        - 不同页面的 Subset 名可能不同，但实际字体相同
        - 目标：730 页 → 1 个字体对象

    Attributes:
        _pool: identity_hash → indirect_reference
        _created: 已创建的字体对象数
        _shared: 被共享的次数
    """

    def __init__(self) -> None:
        if not HAS_PIKEPDF:
            raise ImportError("pikepdf is required for FontPool")

        self._pool: Dict[str, pikepdf.Object] = {}
        self._created: int = 0
        self._shared: int = 0
        self._identity_map: Dict[str, str] = {}  # hash → display_name

    def _identity_hash(
        self,
        family: str,
        weight: int = 400,
        italic: bool = False,
    ) -> str:
        """计算字体 identity hash（忽略 subset 前缀）。"""
        key = f"{family}|{weight}|{italic}"
        return hashlib.sha256(key.encode()).hexdigest()[:16]

    def get_or_create_font(
        self,
        pdf: pikepdf.Pdf,
        family: str,
        weight: int = 400,
        italic: bool = False,
        glyph_ids: Optional[Set[int]] = None,
    ) -> pikepdf.Object:
        """获取或创建字体对象。

        如果 pool 中已有相同 identity 的字体，返回共享引用。
        否则创建新字体对象并加入 pool。

        Args:
            pdf: 目标 PDF
            family: 字体族名（如 "NotoSansSC"）
            weight: 字重（400=normal, 700=bold）
            italic: 是否斜体
            glyph_ids: 使用的字形 ID 集合（可选）

        Returns:
            pikepdf indirect reference to font object
        """
        identity_hash = self._identity_hash(family, weight, italic)

        if identity_hash in self._pool:
            # 已有 → 共享
            self._shared += 1
            logger.debug("FontPool: shared %s (hash=%s)", family, identity_hash[:8])
            return self._pool[identity_hash]

        # 新建 → 创建字体对象
        font_obj = pikepdf.Dictionary(
            {
                "/Type": "/Font",
                "/Subtype": "/Type0",
                "/BaseFont": pikepdf.Name(f"/{family}"),
                "/Encoding": "/Identity-H",
            }
        )

        font_ref = pdf.make_indirect(font_obj)
        self._pool[identity_hash] = font_ref
        self._created += 1
        self._identity_map[identity_hash] = family

        logger.debug("FontPool: created %s (hash=%s)", family, identity_hash[:8])
        return font_ref

    def has_font(self, family: str, weight: int = 400, italic: bool = False) -> bool:
        """检查 pool 中是否已有该字体。"""
        return self._identity_hash(family, weight, italic) in self._pool

    @property
    def unique_fonts(self) -> int:
        """pool 中唯一的字体数。"""
        return self._created

    @property
    def total_shares(self) -> int:
        """被共享的次数。"""
        return self._shared

    @property
    def sharing_ratio(self) -> float:
        """共享率 = shared / (created + shared)。"""
        total = self._created + self._shared
        if total == 0:
            return 0.0
        return self._shared / total

    def summary(self) -> str:
        return (
            f"FontPool | created={self._created} | shared={self._shared} | "
            f"ratio={self.sharing_ratio:.1%}"
        )

    def to_dict(self) -> dict:
        return {
            "created": self._created,
            "shared": self._shared,
            "sharing_ratio": self.sharing_ratio,
            "unique_fonts": self.unique_fonts,
        }
