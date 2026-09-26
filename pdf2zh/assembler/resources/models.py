"""Resource 数据结构：FontUsageScope, ResourceOwnership。

Phase 3.3 核心：定义资源生命周期语义。

字体生命周期：
    DOCUMENT_SHARED — 全文档共享（翻译字体应走这条路）
    PAGE_LOCAL      — 页面级（annotation、临时 xobject）
    INLINE          — 内联（小内容流）

用法：
    scope = FontUsageScope(
        font_id="NotoSansSC",
        first_page=0,
        last_page=729,
        page_count=730,
    )
    assert scope.is_global  # True — 跨全文档

    ownership = ResourceOwnership.DOCUMENT_SHARED
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Set

# ── 资源所有权 ──────────────────────────────────────────────────────


class ResourceOwnership(str, Enum):
    """资源生命周期作用域。

    规则：
        DOCUMENT_SHARED: Font、common logo — 跨页共享
        PAGE_LOCAL: annotation、temporary xobject — 页面级
        INLINE: small content stream — 内联
    """

    DOCUMENT_SHARED = "document_shared"
    PAGE_LOCAL = "page_local"
    INLINE = "inline"


# ── 字体使用范围 ────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class FontUsageScope:
    """字体在文档中的使用范围——决定资源池策略。

    翻译字体应该尽量 DOCUMENT_SHARED，
    原 PDF 字体通常是 PAGE_LOCAL（不同 subset）。

    Attributes:
        font_id: 字体逻辑标识（family+weight+italic）
        first_page: 首次出现页（0-based）
        last_page: 最后出现页（0-based）
        page_count: 出现的页面总数
        ownership: 生命周期作用域
        glyph_count: 使用的字形数量
        isTranslated: 是否为翻译生成的字体
    """

    font_id: str = ""
    first_page: int = 0
    last_page: int = 0
    page_count: int = 0
    ownership: ResourceOwnership = ResourceOwnership.DOCUMENT_SHARED
    glyph_count: int = 0
    is_translated: bool = False

    @property
    def is_global(self) -> bool:
        """是否跨全文档使用。"""
        return self.ownership == ResourceOwnership.DOCUMENT_SHARED

    @property
    def page_span(self) -> int:
        """使用跨度（last_page - first_page + 1）。"""
        return (
            self.last_page - self.first_page + 1
            if self.last_page >= self.first_page
            else 0
        )

    @property
    def coverage_ratio(self) -> float:
        """页面覆盖率（page_count / page_span）。1.0 = 每页都用。"""
        span = self.page_span
        if span == 0:
            return 0.0
        return self.page_count / span

    def summary(self) -> str:
        return (
            f"{self.font_id} | pages={self.first_page}-{self.last_page} "
            f"({self.page_count}) | {self.ownership.value} | "
            f"translated={self.is_translated}"
        )


# ── 资源使用跟踪 ────────────────────────────────────────────────────


@dataclass
class ResourceUsageTracker:
    """资源使用跟踪器：记录每个资源在哪些页面被使用。

    用途：
        - 判断字体是否应该 DOCUMENT_SHARED
        - 检测资源泄漏
        - 优化资源分配

    用法：
        tracker = ResourceUsageTracker()
        tracker.record_font("NotoSansSC", page=0)
        tracker.record_font("NotoSansSC", page=1)
        ...
        scope = tracker.get_font_scope("NotoSansSC", total_pages=730)
    """

    _font_pages: dict[str, Set[int]] = field(default_factory=dict)
    _font_glyphs: dict[str, Set[int]] = field(default_factory=dict)

    def record_font(self, font_id: str, page: int, glyph_id: int = 0) -> None:
        """记录字体使用。"""
        if font_id not in self._font_pages:
            self._font_pages[font_id] = set()
        self._font_pages[font_id].add(page)

        if glyph_id and font_id not in self._font_glyphs:
            self._font_glyphs[font_id] = set()
        if glyph_id:
            self._font_glyphs[font_id].add(glyph_id)

    def get_font_scope(
        self,
        font_id: str,
        total_pages: int,
        is_translated: bool = False,
    ) -> FontUsageScope:
        """获取字体使用范围。"""
        pages = self._font_pages.get(font_id, set())
        glyphs = self._font_glyphs.get(font_id, set())

        if not pages:
            return FontUsageScope(font_id=font_id)

        sorted_pages = sorted(pages)
        first = sorted_pages[0]
        last = sorted_pages[-1]

        # 判断 ownership
        page_count = len(pages)
        if page_count >= total_pages * 0.5:
            ownership = ResourceOwnership.DOCUMENT_SHARED
        elif page_count >= 3:
            ownership = ResourceOwnership.DOCUMENT_SHARED
        else:
            ownership = ResourceOwnership.PAGE_LOCAL

        return FontUsageScope(
            font_id=font_id,
            first_page=first,
            last_page=last,
            page_count=page_count,
            ownership=ownership,
            glyph_count=len(glyphs),
            is_translated=is_translated,
        )

    def all_font_scopes(
        self,
        total_pages: int,
        translated_fonts: Optional[Set[str]] = None,
    ) -> list[FontUsageScope]:
        """获取所有字体的使用范围。"""
        if translated_fonts is None:
            translated_fonts = set()

        scopes = []
        for font_id in self._font_pages:
            is_translated = font_id in translated_fonts
            scope = self.get_font_scope(font_id, total_pages, is_translated)
            scopes.append(scope)

        return sorted(scopes, key=lambda s: s.page_count, reverse=True)

    def summary(self) -> str:
        lines = [f"ResourceUsageTracker | {len(self._font_pages)} fonts"]
        for font_id, pages in sorted(
            self._font_pages.items(), key=lambda x: len(x[1]), reverse=True
        )[:5]:
            lines.append(f"  {font_id}: {len(pages)} pages")
        return "\n".join(lines)
