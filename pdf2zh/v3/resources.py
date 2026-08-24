"""Module: Resources — Phase 6.2 Resource Manager（字体/图片统一注册表）。

字体与图片不再散落（pdfminer/PyMuPDF/Renderer 各自持有），统一登记：

    ResourceManager
     ├── fonts   FontResource（name/family/subset/size）
     └── images  ImageResource（object_id/page/bbox/image_class/strategy）

``from_model`` 扫描模型：块字体（metadata.fonts）→ fonts，图片记录
（metadata.figures）→ images。纯数据结构 + 查询，无 I/O。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from pdf2zh.v3.document_model import DocumentModel


@dataclass
class FontResource:
    name: str = ""
    family: str = ""
    subset: bool = False
    size: float = 0.0
    meta: Dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "family": self.family,
            "subset": self.subset,
            "size": round(self.size, 2),
            "meta": dict(self.meta),
        }


@dataclass
class ImageResource:
    object_id: str = ""
    page: int = 0
    bbox: tuple = (0.0, 0.0, 0.0, 0.0)
    image_class: str = "unknown"
    strategy: str = "preserve"

    def to_dict(self) -> dict:
        return {
            "object_id": self.object_id,
            "page": self.page,
            "bbox": [round(v, 2) for v in self.bbox],
            "image_class": self.image_class,
            "strategy": self.strategy,
        }


class ResourceManager:
    """字体/图片统一注册表。"""

    def __init__(self) -> None:
        self.fonts: Dict[str, FontResource] = {}
        self.images: List[ImageResource] = []

    def register_font(self, name: str, **kw) -> FontResource:
        res = FontResource(name=name, **kw)
        self.fonts[name] = res
        return res

    def get_font(self, name: str) -> Optional[FontResource]:
        return self.fonts.get(name)

    def register_image(self, **kw) -> ImageResource:
        res = ImageResource(**kw)
        self.images.append(res)
        return res

    def images_on_page(self, page: int) -> List[ImageResource]:
        return [i for i in self.images if i.page == page]

    def from_model(self, model: DocumentModel) -> "ResourceManager":
        for page in model.pages:
            for block in page.blocks:
                for font, sizes in (block.metadata.get("fonts", {}) or {}).items():
                    if font and font not in self.fonts:
                        self.register_font(font, size=max(sizes) if sizes else 0.0)
        for fig in model.metadata.get("figures", []) or []:
            self.register_image(
                object_id=fig.get("object_id", ""),
                page=int(fig.get("page", 0) or 0),
                bbox=tuple(float(v) for v in fig.get("bbox", (0, 0, 0, 0))),
                image_class=fig.get("image_class", "unknown"),
                strategy=fig.get("strategy", "preserve"),
            )
        return self

    def summary(self) -> dict:
        return {"fonts": len(self.fonts), "images": len(self.images)}


__all__ = ["FontResource", "ImageResource", "ResourceManager"]
