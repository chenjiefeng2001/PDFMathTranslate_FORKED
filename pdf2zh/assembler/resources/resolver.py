"""ResourceResolver — 页面资源解析器。

Phase 3.3 核心：将 FontRequirement 映射到 PDF indirect reference。

职责：
    1. 接收 PageResult 的 ResourceRequirement
    2. 通过 FontPool 获取/创建字体对象
    3. 构建 page /Resources dictionary
    4. 分配资源名称（/F1, /F2, ...）

用法：
    resolver = ResourceResolver(pdf)
    resources = resolver.resolve(page_result.resources)
    page["/Resources"] = resources
"""

from __future__ import annotations

import logging
from typing import Dict, Optional

logger = logging.getLogger(__name__)

try:
    import pikepdf

    HAS_PIKEPDF = True
except ImportError:
    HAS_PIKEPDF = False


class ResourceResolver:
    """页面资源解析器：将 ResourceRequirement 映射到 PDF 对象。

    资源名称分配规则：
        /F1, /F2, /F3, ... — 字体
        /Im1, /Im2, ... — 图片
        /X1, /X2, ... — 其他 XObject

    Attributes:
        pdf: 目标 PDF
        font_pool: 字体去重池
        _font_counter: 字体名称计数器
        _name_map: 内部名称 → PDF 名称映射
    """

    def __init__(self, pdf: pikepdf.Pdf) -> None:
        if not HAS_PIKEPDF:
            raise ImportError("pikepdf is required for ResourceResolver")

        self.pdf = pdf
        self._font_counter: int = 0
        self._name_map: Dict[str, str] = {}

    def resolve_fonts(
        self,
        resources: pikepdf.Dictionary,
        fonts: list,  # List[FontRequirement]
        font_pool=None,
    ) -> None:
        """解析字体资源：创建/共享字体对象，分配名称。

        Args:
            resources: 目标 Resources dict（会被修改）
            fonts: FontRequirement 列表
            font_pool: FontPool 实例（可选）
        """
        if not fonts:
            return

        font_dict = resources.get("/Font")
        if font_dict is None:
            font_dict = pikepdf.Dictionary()
            resources["/Font"] = font_dict

        for fr in fonts:
            # 分配名称
            self._font_counter += 1
            pdf_name = f"/F{self._font_counter}"

            if font_pool and hasattr(font_pool, "get_or_create_font"):
                # 使用 FontPool 共享
                font_ref = font_pool.get_or_create_font(
                    self.pdf,
                    family=fr.identity.family,
                    weight=fr.identity.weight,
                    italic=fr.identity.italic,
                )
            else:
                # 直接创建（不共享）
                font_obj = pikepdf.Dictionary(
                    {
                        "/Type": "/Font",
                        "/Subtype": "/Type0",
                        "/BaseFont": pikepdf.Name(f"/{fr.identity.family}"),
                        "/Encoding": "/Identity-H",
                    }
                )
                font_ref = self.pdf.make_indirect(font_obj)

            font_dict[pdf_name] = font_ref
            self._name_map[fr.identity.family] = pdf_name

    def resolve(
        self,
        resource_requirement,  # ResourceRequirement
        font_pool=None,
    ) -> pikepdf.Dictionary:
        """解析完整资源需求，返回 Resources dict。

        Args:
            resource_requirement: ResourceRequirement 实例
            font_pool: FontPool 实例（可选）

        Returns:
            pikepdf.Dictionary — page /Resources
        """
        resources = pikepdf.Dictionary()

        # 字体
        if resource_requirement.fonts:
            self.resolve_fonts(resources, resource_requirement.fonts, font_pool)

        # Phase 3.4: XObject/Image 在这里扩展

        return resources

    def get_font_name(self, family: str) -> Optional[str]:
        """获取字体的 PDF 名称。"""
        return self._name_map.get(family)

    def summary(self) -> str:
        return (
            f"ResourceResolver | fonts={self._font_counter} | "
            f"mapped={len(self._name_map)}"
        )

    def to_dict(self) -> dict:
        return {
            "fonts_created": self._font_counter,
            "names_mapped": len(self._name_map),
        }
