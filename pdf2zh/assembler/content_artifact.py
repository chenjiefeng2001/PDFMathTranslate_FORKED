"""ContentArtifact — 翻译内容的中间表示。

Phase 4.2 核心：用 operations 描述页面内容，而不是直接操作 PDF bytes。

设计思路：
    PageResult 给出语义（哪些文字要翻译）
    ContentArtifact 给出操作（怎么画出来）
    pikepdf 只负责最终渲染

好处：
    - 可以在 operations 层做布局调整
    - 可以换渲染后端（pikepdf / PyMuPDF / 自定义）
    - 可以做 visual diff（比较 operations 而不是 PDF bytes）

用法：
    artifact = ContentArtifact(page_index=0)
    artifact.add_draw_text(
        text="你好世界",
        bbox=(100, 700, 200, 720),
        font="NotoSansSC",
        font_size=12,
    )
    artifact.add_draw_image(
        ref="img_001",
        bbox=(50, 500, 200, 650),
    )

    # 渲染到 pikepdf
    pdf_bytes = artifact.render_to_pikepdf()
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

import pikepdf


class DrawOp(str, Enum):
    """绘制操作类型。"""

    DRAW_TEXT = "draw_text"
    DRAW_IMAGE = "draw_image"
    DRAW_RECT = "draw_rect"
    MOVE_TO = "move_to"
    SET_FONT = "set_font"
    SET_COLOR = "set_color"
    RESTORE = "restore"


@dataclass
class Operation:
    """单个绘制操作。"""

    op: DrawOp
    params: Dict[str, Any] = field(default_factory=dict)

    def to_pdf_operators(self) -> List[str]:
        """转换为 PDF content stream operators。"""
        if self.op == DrawOp.DRAW_TEXT:
            text = self.params.get("text", "")
            font_size = self.params.get("font_size", 12)
            x = self.params.get("x", 0)
            y = self.params.get("y", 0)
            return [
                f"BT",
                f"/F1 {font_size} Tf",
                f"{x} {y} Td",
                f"({text}) Tj",
                f"ET",
            ]
        elif self.op == DrawOp.DRAW_IMAGE:
            ref = self.params.get("ref", "")
            x = self.params.get("x", 0)
            y = self.params.get("y", 0)
            w = self.params.get("width", 100)
            h = self.params.get("height", 100)
            return [
                f"q",
                f"{w} 0 0 {h} {x} {y} cm",
                f"/{ref} Do",
                f"Q",
            ]
        elif self.op == DrawOp.SET_FONT:
            font_name = self.params.get("font", "F1")
            size = self.params.get("size", 12)
            return [f"/{font_name} {size} Tf"]
        elif self.op == DrawOp.MOVE_TO:
            x = self.params.get("x", 0)
            y = self.params.get("y", 0)
            return [f"{x} {y} Td"]
        else:
            return []


@dataclass
class DrawText:
    """绘制文本操作。"""

    text: str = ""
    bbox: Tuple[float, float, float, float] = (0, 0, 0, 0)
    font: str = "F1"
    font_size: float = 12.0
    color: Tuple[float, float, float] = (0, 0, 0)

    @property
    def x(self) -> float:
        return self.bbox[0]

    @property
    def y(self) -> float:
        return self.bbox[1]


@dataclass
class DrawImage:
    """绘制图片操作。"""

    ref: str = ""
    bbox: Tuple[float, float, float, float] = (0, 0, 0, 0)

    @property
    def x(self) -> float:
        return self.bbox[0]

    @property
    def y(self) -> float:
        return self.bbox[1]

    @property
    def width(self) -> float:
        return self.bbox[2] - self.bbox[0]

    @property
    def height(self) -> float:
        return self.bbox[3] - self.bbox[1]


class ContentArtifact:
    """翻译内容的中间表示。

    存储：
        - 页面索引
        - 绘制操作列表
        - 资源引用（fonts, images）

    可以：
        - 渲染到 pikepdf content stream
        - 比较两个 artifact 的差异
        - 应用布局调整
    """

    def __init__(self, page_index: int = 0) -> None:
        self.page_index = page_index
        self._operations: List[Operation] = []
        self._fonts_used: set[str] = set()
        self._images_used: set[str] = set()

    def add_draw_text(
        self,
        text: str,
        bbox: Tuple[float, float, float, float],
        font: str = "F1",
        font_size: float = 12.0,
        color: Tuple[float, float, float] = (0, 0, 0),
    ) -> None:
        """添加绘制文本操作。"""
        op = Operation(
            op=DrawOp.DRAW_TEXT,
            params={
                "text": text,
                "x": bbox[0],
                "y": bbox[1],
                "bbox": bbox,
                "font": font,
                "font_size": font_size,
                "color": color,
            },
        )
        self._operations.append(op)
        self._fonts_used.add(font)

    def add_draw_image(
        self,
        ref: str,
        bbox: Tuple[float, float, float, float],
    ) -> None:
        """添加绘制图片操作。"""
        op = Operation(
            op=DrawOp.DRAW_IMAGE,
            params={
                "ref": ref,
                "x": bbox[0],
                "y": bbox[1],
                "width": bbox[2] - bbox[0],
                "height": bbox[3] - bbox[1],
            },
        )
        self._operations.append(op)
        self._images_used.add(ref)

    def add_set_font(self, font: str, size: float = 12.0) -> None:
        op = Operation(op=DrawOp.SET_FONT, params={"font": font, "size": size})
        self._operations.append(op)

    def add_move_to(self, x: float, y: float) -> None:
        op = Operation(op=DrawOp.MOVE_TO, params={"x": x, "y": y})
        self._operations.append(op)

    def render_to_content_bytes(self) -> bytes:
        """渲染为 PDF content stream bytes。"""
        lines = []
        for op in self._operations:
            lines.extend(op.to_pdf_operators())
        return "\n".join(lines).encode("latin-1")

    def render_to_pikepdf(self, pdf: pikepdf.Pdf) -> pikepdf.Stream:
        """渲染为 pikepdf Stream 对象。"""
        content_bytes = self.render_to_content_bytes()
        return pikepdf.Stream(pdf, content_bytes)

    @property
    def operation_count(self) -> int:
        return len(self._operations)

    @property
    def fonts_used(self) -> set[str]:
        return self._fonts_used.copy()

    @property
    def images_used(self) -> set[str]:
        return self._images_used.copy()

    def diff(self, other: ContentArtifact) -> Dict[str, Any]:
        """比较两个 artifact 的差异。"""
        return {
            "ops_same": self._operations == other._operations,
            "ops_count_diff": self.operation_count - other.operation_count,
            "fonts_diff": self._fonts_used - other._fonts_used,
            "images_diff": self._images_used - other._images_used,
        }

    def summary(self) -> str:
        return (
            f"ContentArtifact(page={self.page_index}) | "
            f"ops={self.operation_count} | "
            f"fonts={len(self._fonts_used)} | "
            f"images={len(self._images_used)}"
        )
