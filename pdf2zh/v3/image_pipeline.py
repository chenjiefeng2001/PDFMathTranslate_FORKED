"""Module: ImagePipeline — V8.6 OCR 结果喂入决策链 + 渲染后端接入（P1 闭环）。

此前 OCR（``ocr_engine``）与渲染后端（``image_renderer``）都已落地但无人
调用：决策链只看到空文本区域，RegionReplace/Overlay/FullRepaint 仍是决策
契约。本模块把整条流水线串起来（纯逻辑、无 fitz）：

    pixels
      ├─ detect_text_regions（只框不识别）
      ├─ OCR 回填（``OCRBackend.recognize`` → region.text / ocr_confidence）
      ├─ TranslationDecisionEngine.decide（OCR 结果真正参与决策）
      ├─ 逐区域 translate_fn 翻译（缺省恒等契约）
      ├─ PlateRenderer.render（字形画布，缺省确定性灰阶）
      └─ render_image_decision（按 RenderMode 合成最终 RGB bytes）

输出 ``ImageRenderSummary``（渲染模式 / 翻译区域数 / 保留区域数），
side-channel 纪律：像素替换只在显式调用情况下发生，翻译器失败只降级为
原样保留。纯逻辑、无 I/O。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from pdf2zh.v3.image_engine import (
    ImageClass,
    ImageObject,
    RenderMode,
    TextRegion,
    TranslationDecision,
    TranslationDecisionEngine,
    analyze_image_bytes,
)
from pdf2zh.v3.image_renderer import render_image_decision

log = logging.getLogger(__name__)


@dataclass
class ImageRenderSummary:
    """一次图片翻译管线的结果摘要。"""

    object_id: str = ""
    page_num: int = 0
    image_class: str = "unknown"
    render_mode: str = "preserve"
    regions_total: int = 0
    regions_translated: int = 0
    regions_kept: int = 0
    ok: bool = True

    def to_dict(self) -> dict:
        return {
            "object_id": self.object_id,
            "page_num": self.page_num,
            "image_class": self.image_class,
            "render_mode": self.render_mode,
            "regions_total": self.regions_total,
            "regions_translated": self.regions_translated,
            "regions_kept": self.regions_kept,
            "ok": self.ok,
        }

    def summary(self) -> str:
        return (f"ImageRender {self.object_id} [{self.image_class}] "
                f"mode={self.render_mode} "
                f"translated={self.regions_translated}/{self.regions_total}")


class PlateRenderer:
    """译文字形画布渲染器协议。

    ``render(text, height_px, width_px)`` → RGB np.ndarray(uint8)。
    默认实现为确定性灰阶纹路（无字形依赖）；真实字形栅格化接 Pillow 后端。
    """

    def render(self, text: str, height_px: int, width_px: int) -> "object":
        raise NotImplementedError


class SolidPlateRenderer(PlateRenderer):
    """确定性实心 plate：灰度取自文本哈希，保证测试可复现、渲染稳定。"""

    def render(self, text: str, height_px: int, width_px: int) -> "object":
        import numpy as np
        h = max(1, int(height_px))
        w = max(1, int(width_px))
        value = 70 + (hash((text or "").strip() + "|plate") % 110)
        plate = np.full((h, w, 3), value, dtype=np.uint8)
        return plate


@dataclass
class PillowPlateRenderer(PlateRenderer):
    """Pillow 字形栅格化后端（guarded：无 PIL 时退化为灰阶占位）。

    仅作渲染质量增强，不影响管线正确性；字体文件缺省时用默认字体。
    """

    def render(self, text: str, height_px: int, width_px: int) -> "object":
        try:
            from PIL import Image, ImageDraw, ImageFont
            import numpy as np
            h, w = max(1, int(height_px)), max(1, int(width_px))
            font_size = max(6, int(h * 0.8))
            canvas = Image.new("RGB", (w, h), (255, 255, 255))
            draw = ImageDraw.Draw(canvas)
            draw.text((2, 1), (text or " ")[: max(1, int(w / max(font_size / 2, 1)))],
                      fill=(20, 20, 20))
            return np.asarray(canvas, dtype=np.uint8)
        except Exception:  # noqa: BLE001 — PIL 后端失败即退化
            return SolidPlateRenderer().render(text, height_px, width_px)


def _region_px_wh(bbox: Sequence[float], canvas_h: int, canvas_w: int) -> Tuple[int, int]:
    x0, y0, x1, y1 = (float(v) for v in bbox)
    return max(1, int((y1 - y0) * canvas_h)), max(1, int((x1 - x0) * canvas_w))


def decide_with_ocr(obj: ImageObject, ocr_backend=None,
                    engine: Optional[TranslationDecisionEngine] = None) -> TranslationDecision:
    """OCR 回填区域文本后重新决策（OCR 结果进入决策链）。

    ``ocr_backend`` 缺省时保持空文本重新决策（与既有行为一致）。
    """
    if obj is None:
        return TranslationDecision(translate=False)
    if ocr_backend is not None:
        try:
            from pdf2zh.v3.ocr_engine import ocr_regions_into_object
            ocr_regions_into_object(obj, backend=ocr_backend)
        except Exception as e:  # noqa: BLE001 — OCR 失败只降级，不抛出
            log.debug("ImagePipeline: OCR backfill failed: %s", e)
    return (engine or TranslationDecisionEngine()).decide(obj)


def translate_image_pixels(pixels,
                           object_id: str = "img",
                           page_num: int = 0,
                           decision: Optional[TranslationDecision] = None,
                           translate_fn: Optional[Callable[[str], str]] = None,
                           ocr_backend=None,
                           plate_renderer: Optional[PlateRenderer] = None,
                           engine: Optional[TranslationDecisionEngine] = None,
                           ) -> Tuple[bytes, ImageRenderSummary]:
    """端到端图片翻译管线 →（渲染后 RGB bytes, 摘要）。

    步骤：区域检测 →（如有 OCR 后端）文本回填 → 决策 → 翻译 → 渲染。
     ``translate_fn=None`` 时区域文本原样保留（恒等契约）；不带 OCR 后端
    时决策只依赖区域几何/类型（与旧行为一致）。失败一律降级不抛出。
    """
    import numpy as np
    arr = np.asarray(pixels)
    canvas_h, canvas_w = arr.shape[:2]
    image_class = "unknown"

    if decision is None:
        try:
            obj = analyze_image_bytes(pixels, object_id=object_id,
                                      page_num=page_num, engine=engine)
            decision = decide_with_ocr(obj, ocr_backend=ocr_backend, engine=engine)
            image_class = obj.image_class.value
        except Exception as e:  # noqa: BLE001 — 管线失败降级为保留原图
            log.debug("ImagePipeline.analyze failed (%s): %s", object_id, e)
            decision = TranslationDecision(translate=False)
    decision = decision or TranslationDecision(translate=False)

    renderer = plate_renderer or SolidPlateRenderer()
    plates: Dict[int, object] = {}
    translated = 0
    total = len(decision.region_decisions)
    if engine is None:
        engine = TranslationDecisionEngine()
    for i, rd in enumerate(decision.region_decisions):
        if not getattr(rd, "translate", False):
            continue
        text = (rd.region.text or "").strip()
        if translate_fn is not None and text:
            try:
                text = translate_fn(text) or text
            except Exception as e:  # noqa: BLE001
                log.debug("ImagePipeline.translate failed (%s): %s", object_id, e)
        if not text:
            continue
        translated += 1
        ph, pw = _region_px_wh(rd.region.bbox, canvas_h, canvas_w)
        plates[i] = renderer.render(text, ph, pw)

    out = None
    try:
        out = render_image_decision(pixels, decision, plates)
    except Exception as e:  # noqa: BLE001 — 渲染失败降级为保留原图
        log.debug("ImagePipeline.render failed (%s): %s", object_id, e)
    if out is None:
        from pdf2zh.v3.image_renderer import render_preserve
        out = render_preserve(pixels)
    summary = ImageRenderSummary(
        object_id=object_id,
        page_num=page_num,
        image_class=image_class,
        render_mode=decision.render_mode.value,
        regions_total=total,
        regions_translated=translated,
        regions_kept=total - translated,
        ok=True,
    )
    return out, summary


__all__ = [
    "ImageRenderSummary", "PlateRenderer", "SolidPlateRenderer",
    "PillowPlateRenderer", "decide_with_ocr", "translate_image_pixels",
]