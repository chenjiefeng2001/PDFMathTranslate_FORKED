"""Module: FigureUnderstanding — Phase 4.4 图片理解（不是「禁止翻译」）。

图片分类（image_engine）已产出 ImageClass/决策；本模块把它接进文档模型：
- 按类型给操作策略（照片保留 / UI 截图 OCR+Overlay / 图表保坐标翻标签 /
  流程图 OCR+重绘 / 扫描页 OCR Pipeline）；
- ``annotate_figures`` 把图片记录变成模型里的 figure 块（kind=figure +
  metadata.image_class/strategy）+ 与最近题注的 caption_of 关系；
- ``figure_strategy`` 提供类型→操作映射（可直接消费 image_pipeline）。
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from pdf2zh.v3.canonical_page import BlockModel
from pdf2zh.v3.document_model import DocumentModel, Relation, block_id

# ImageClass → 操作（对应 V8.6 IMAGE_POLICY 的翻译决策扩展）
STRATEGY_MAP: Dict[str, str] = {
    "photo": "preserve",          # 图片照片 → 保留
    "logo": "preserve",
    "qr_code": "preserve",
    "barcode": "preserve",
    "equation": "preserve",
    "screenshot": "ocr_overlay",  # UI 截图 → OCR + Overlay
    "chart": "keep_labels",       # 图表 → 保留坐标 + 翻译标签
    "diagram": "ocr_redraw",      # 流程图 → OCR + 重绘
    "map": "keep_labels",
    "comic": "ocr_overlay",
    "cad": "preserve",
    "scanned": "ocr_pipeline",    # 扫描页面 → OCR Pipeline
    "unknown": "preserve",
}


def figure_strategy(image_class: str) -> str:
    return STRATEGY_MAP.get((image_class or "unknown").lower(), "preserve")


def annotate_figures(model: DocumentModel,
                     image_records: Sequence[dict]) -> int:
    """图片记录 → 模型 figure 块 + caption_of 关系。

    ``image_records`` 为 ``[{page, object_id, bbox, image_class, decision}]``
    （``analyze_pdf_images`` 输出可适配）。返回新增 figure 块数。
    """
    added = 0
    for rec in image_records or []:
        pno = int(rec.get("page", 0) or 0)
        for page in model.pages:
            if page.page_num != pno:
                continue
            bbox = tuple(float(v) for v in rec.get("bbox", (0, 0, 0, 0)) or ())
            cls = str(rec.get("image_class", "unknown")).lower()
            fig = BlockModel(
                text="", kind="figure",
                x0=bbox[0], y0=bbox[1], x1=bbox[2], y1=bbox[3],
            )
            fig.metadata.update({
                "image_class": cls,
                "strategy": figure_strategy(cls),
                "object_id": rec.get("object_id", ""),
                "translate": False,
                "render_path": "preserve_float",
            })
            decision = rec.get("decision") or {}
            if isinstance(decision, dict):
                fig.metadata["render_mode"] = decision.get("render_mode")
            page.blocks.append(fig)
            added += 1
            # 与最近的题注建立 caption_of（宿主 → 题注，取 |y 差| 最小）
            caption = None
            best_gap = None
            for b in page.blocks[:-1]:
                if b.kind != "caption":
                    continue
                gap = abs(b.y0 - fig.y0)
                if best_gap is None or gap < best_gap:
                    best_gap = gap
                    caption = b
            if caption is not None:
                model.relations.append(Relation(
                    "caption_of", block_id(pno, page.blocks.index(caption)),
                    block_id(pno, page.blocks.index(fig))))
    model.metadata["figures"] = [
        {"page": int(r.get("page", 0) or 0),
         "object_id": r.get("object_id", ""),
         "image_class": str(r.get("image_class", "unknown")).lower(),
         "strategy": figure_strategy(str(r.get("image_class", "unknown"))),
         "bbox": list(r.get("bbox", (0, 0, 0, 0)) or ())}
        for r in image_records or []
    ]
    return added


__all__ = ["STRATEGY_MAP", "figure_strategy", "annotate_figures"]