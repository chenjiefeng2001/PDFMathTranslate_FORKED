"""ResourcePool — PDF 资源去重池。

Phase 3.2 核心：解决 clone 时 Resources 线性膨胀问题。

设计原则：
    1. 按语义 identity 去重（不是 xref 编号）
    2. 保持 PDF namespace 正确（不合并整个 Resources dict）
    3. 渐进式：先做 Font，再做 XObject/Image/ExtGState

用法：
    pool = ResourcePool()

    for page in pdf.pages:
        resources = page["/Resources"]
        font_dict = resources.get("/Font")
        if font_dict:
            shared_font_dict = pool.dedup_fonts(pdf, font_dict)
            resources["/Font"] = shared_font_dict
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Dict, Optional, Set, Tuple

logger = logging.getLogger(__name__)

try:
    import pikepdf

    HAS_PIKEPDF = True
except ImportError:
    HAS_PIKEPDF = False


class ResourcePool:
    """PDF 资源去重池：按语义 identity 共享对象。

    核心机制：
        1. 遍历所有 page 的 /Resources/Font
        2. 计算每个 font 的 content hash（Subset name 除外）
        3. 相同 content hash 的 font 共享同一个 indirect reference
        4. 返回去重后的 Font dict

    Attributes:
        _font_pool: content_hash → indirect_reference
        _font_seen: xref_set（已处理的 xref，避免重复）
        stats: 统计信息
    """

    def __init__(self) -> None:
        if not HAS_PIKEPDF:
            raise ImportError("pikepdf is required for ResourcePool")

        self._font_pool: Dict[str, pikepdf.Object] = {}
        self._font_seen: Set[int] = set()
        self._font_name_map: Dict[str, str] = {}  # old_name → shared_name

        self.stats = {
            "fonts_total": 0,
            "fonts_unique": 0,
            "fonts_shared": 0,
            "pages_processed": 0,
        }

    def _font_content_hash(self, font_obj: pikepdf.Dictionary) -> str:
        """计算 font 对象的内容 hash（忽略 /Name 和 Subset 前缀）。

        PDF 字体 Subset 命名规则：XXXX+FontName
        例如：ABCDEF+NotoSans-Regular

        去重时需要忽略 Subset 前缀，只比较实际字体内容。
        """
        # 提取关键属性（不含 /Name，因为 Subset 名字每次不同）
        significant_keys = [
            "/BaseFont",
            "/Subtype",
            "/Encoding",
            "/FirstChar",
            "/LastChar",
            "/Widths",
        ]

        hash_parts = []
        for key in significant_keys:
            val = font_obj.get(key)
            if val is not None:
                hash_parts.append(f"{key}={repr(val)}")

        # 对于 TrueType/OpenType 字体，还需要比较 /FontDescriptor
        font_desc = font_obj.get("/FontDescriptor")
        if font_desc is not None:
            if isinstance(font_desc, pikepdf.Dictionary):
                for key in ["/FontName", "/Flags", "/FontBBox"]:
                    val = font_desc.get(key)
                    if val is not None:
                        hash_parts.append(f"fd:{key}={repr(val)}")

        content = "|".join(hash_parts)
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    def _get_font_name(self, font_obj: pikepdf.Dictionary) -> str:
        """获取 font 的逻辑名称（去掉 Subset 前缀）。"""
        name = font_obj.get("/Name", "")
        if isinstance(name, pikepdf.String):
            name = str(name)
        # 去掉 Subset 前缀：ABCDEF+FontName → FontName
        if "+" in name:
            name = name.split("+", 1)[1]
        return name

    def dedup_fonts(
        self,
        pdf: pikepdf.Pdf,
        font_dict: pikepdf.Dictionary,
    ) -> pikepdf.Dictionary:
        """对 Font dict 去重：相同内容的 font 共享同一个 indirect reference。

        Args:
            pdf: 目标 PDF（用于创建 indirect reference）
            font_dict: 原始 Font dict

        Returns:
            去重后的 Font dict（新 dict 或原 dict）
        """
        if font_dict is None or len(font_dict) == 0:
            return font_dict

        new_font_dict = pikepdf.Dictionary()
        changed = False

        for key, font_ref in font_dict.items():
            self.stats["fonts_total"] += 1

            # 解引用 indirect reference
            if isinstance(font_ref, pikepdf.Object) and hasattr(font_ref, "_objgen"):
                font_obj = font_ref
            else:
                font_obj = font_ref

            # 获取 xref 用于去重检查
            try:
                xref = font_ref.objgen[0] if hasattr(font_ref, "objgen") else 0
            except (AttributeError, IndexError):
                xref = 0

            # 计算 content hash
            content_hash = self._font_content_hash(font_obj)

            if content_hash in self._font_pool:
                # 已有相同内容的 font → 共享
                shared_ref = self._font_pool[content_hash]
                new_font_dict[key] = shared_ref
                self.stats["fonts_shared"] += 1
                changed = True
                logger.debug(
                    "Font dedup: %s → shared with existing (hash=%s)",
                    key,
                    content_hash[:8],
                )
            else:
                # 新 font → 保持原引用，加入 pool
                new_font_dict[key] = font_ref
                self._font_pool[content_hash] = font_ref
                self.stats["fonts_unique"] += 1

        self.stats["pages_processed"] += 1

        if changed:
            return new_font_dict
        else:
            return font_dict

    def dedup_resources(
        self,
        pdf: pikepdf.Pdf,
        resources: pikepdf.Dictionary,
    ) -> pikepdf.Dictionary:
        """对整个 Resources dict 去重（渐进式：目前只处理 Font）。

        Args:
            pdf: 目标 PDF
            resources: 原始 Resources dict

        Returns:
            去重后的 Resources dict
        """
        if resources is None:
            return resources

        font_dict = resources.get("/Font")
        if font_dict is not None:
            new_font_dict = self.dedup_fonts(pdf, font_dict)
            if new_font_dict is not font_dict:
                resources["/Font"] = new_font_dict

        # Phase 3.2.2+: XObject dedup 在这里扩展
        # xobj_dict = resources.get("/XObject")
        # if xobj_dict is not None:
        #     resources["/XObject"] = self.dedup_xobjects(pdf, xobj_dict)

        return resources

    def summary(self) -> str:
        """人类可读摘要。"""
        total = self.stats["fonts_total"]
        shared = self.stats["fonts_shared"]
        sharing_rate = shared / total if total > 0 else 0.0

        return (
            f"ResourcePool | pages={self.stats['pages_processed']} | "
            f"fonts: total={total}, unique={self.stats['fonts_unique']}, "
            f"shared={shared} ({sharing_rate:.1%})"
        )

    def to_dict(self) -> dict:
        """转为 dict（用于 JSON 序列化）。"""
        total = self.stats["fonts_total"]
        shared = self.stats["fonts_shared"]
        return {
            "pages_processed": self.stats["pages_processed"],
            "fonts_total": total,
            "fonts_unique": self.stats["fonts_unique"],
            "fonts_shared": shared,
            "font_sharing_ratio": shared / total if total > 0 else 0.0,
        }
