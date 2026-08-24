"""Module: ImageRenderer — V8.6 图片渲染后端（RenderMode 的像素级实现）。

此前 ``IMAGE_POLICY`` 只有 PRESERVE 在决策层生效，RegionReplace / Overlay /
FullRepaint 只是决策契约。本模块给出四个模式的**像素级渲染核**（numpy，
无 fitz 依赖）：合成分辨率一致的译文画布，再把对应区域的像素写入原图。

    render_image_decision(pixels, decision, plates)
        ↔ 渲染模式派发：
            PRESERVE       → 原样返回
            REGION_REPLACE → 每个可翻译 region 用对应 plate 替换
            OVERLAY        → 保留原图 + 叠上译文 plate（半透明）
            FULL_REPAINT   → 白底 + 全部译文 plate 重排

输入：
    - ``plates``：{region_index: np.ndarray(RGB)} 译文字形画布（渲染器
      的职责：字形栅格化；本模块只负责几何拼接与透明度合成）。
    - bbox 为归一化 [0,1] 坐标（与 image_engine.detect_text_regions 一致）。
"""

from __future__ import annotations

import logging
from typing import Dict, Optional, Sequence

from pdf2zh.v3.image_engine import RenderMode, TranslationDecision, RegionDecision

logger = logging.getLogger(__name__)

try:
    import numpy as np
except ImportError:  # pragma: no cover - numpy 为硬依赖
    np = None


def _to_rgb(pixels) -> "np.ndarray":
    arr = np.asarray(pixels)
    if arr.ndim == 2:
        return np.stack([arr] * 3, axis=-1)
    if arr.ndim == 3 and arr.shape[2] == 4:
        return arr[..., :3].copy()
    return arr[..., :3].copy()


def _region_px(bbox, h: int, w: int):
    """归一化 bbox → 像素切片；越界自动裁剪。"""
    x0, y0, x1, y1 = (float(v) for v in bbox)
    px0 = max(0, min(int(x0 * w), w - 1))
    py0 = max(0, min(int(y0 * h), h - 1))
    px1 = max(px0 + 1, min(int(x1 * w), w))
    py1 = max(py0 + 1, min(int(y1 * h), h))
    return py0, py1, px0, px1


def _fit_plate(plate, rh: int, rw: int) -> "np.ndarray":
    """把 plate 缩放到 region 像素尺寸（最近邻，保持确定性）。"""
    p = np.asarray(plate)
    if p.ndim == 2:
        p = np.stack([p] * 3, axis=-1)
    if p.ndim != 3:
        return np.full((rh, rw, 3), 0, dtype=np.uint8)
    src_h, src_w = p.shape[:2]
    if src_h == rh and src_w == rw:
        return p[..., :3].astype(np.uint8)
    ys = (np.linspace(0, src_h - 1, rh)).astype(int)
    xs = (np.linspace(0, src_w - 1, rw)).astype(int)
    return p[np.ix_(ys, xs)][..., :3].astype(np.uint8)


def _scaled_plate(region, plate, canvas_h: int, canvas_w: int):
    py0, py1, px0, px1 = _region_px(region.bbox, canvas_h, canvas_w)
    return px0, px1, py0, py1, _fit_plate(plate, py1 - py0, px1 - px0)


# ── 渲染核 ───────────────────────────────────────────────────────────────


def render_region_replace(pixels, regions: Sequence, plates: dict) -> bytes:
    """Mode 3：仅替换可翻译 region（背景保留）。"""
    canvas = _to_rgb(pixels)
    h, w = canvas.shape[:2]
    for i, reg in enumerate(regions or []):
        plate = plates.get(i)
        if plate is None:
            continue
        px0, px1, py0, py1, fit = _scaled_plate(reg, plate, h, w)
        canvas[py0:py1, px0:px1] = fit
    return canvas[..., :3].tobytes()


def render_overlay(
    pixels, regions: Sequence, plates: dict, alpha: float = 0.55
) -> bytes:
    """Mode 2：原图 + 半透明译文 plate（图内叠加，保留背景纹理）。"""
    canvas = _to_rgb(pixels).astype(np.float32)
    h, w = canvas.shape[:2]
    a = float(alpha)
    for i, reg in enumerate(regions or []):
        plate = plates.get(i)
        if plate is None:
            continue
        px0, px1, py0, py1, fit = _scaled_plate(reg, plate, h, w)
        block = canvas[py0:py1, px0:px1]
        canvas[py0:py1, px0:px1] = (1.0 - a) * block + a * fit.astype(np.float32)
    return np.clip(canvas[..., :3], 0, 255).astype(np.uint8).tobytes()


def render_full_repaint(
    pixels, regions: Sequence, plates: dict, background=(255, 255, 255)
) -> bytes:
    """Mode 4：白底重排 —— 画布以背景填充，按 region 位置放回 plate。"""
    canvas = _to_rgb(pixels)
    h, w = canvas.shape[:2]
    canvas[...] = (
        np.array(background, dtype=np.uint8)
        if isinstance(background, (tuple, list))
        else background
    )
    for i, reg in enumerate(regions or []):
        plate = plates.get(i)
        if plate is None:
            continue
        px0, px1, py0, py1, fit = _scaled_plate(reg, plate, h, w)
        canvas[py0:py1, px0:px1] = fit
    return canvas[..., :3].tobytes()


def render_preserve(pixels) -> bytes:
    """Mode 1：原样返回（零改动 —— 保护原像素）。"""
    return _to_rgb(pixels)[..., :3].tobytes()


# ── 按决策派发 ───────────────────────────────────────────────────────────


def render_image_decision(
    pixels,
    decision: Optional[TranslationDecision] = None,
    plates: Optional[Dict[int, object]] = None,
    background=(255, 255, 255),
    alpha: float = 0.55,
) -> bytes:
    """按 ``TranslationDecision.render_mode`` 派发渲染后端。

    ``plates`` 是 {region_index: RGB array} 的译文字形画布；缺失的 region
    保持原像素（REGION_REPLACE/OVERLAY）或留白（FULL_REPAINT）。
    """
    plates = plates or {}
    mode = decision.render_mode if decision is not None else RenderMode.PRESERVE
    regions = []
    for rd in (decision.region_decisions if decision else []) or []:
        if isinstance(rd, RegionDecision) and rd.translate:
            regions.append(rd.region)
    if not regions:
        mode = RenderMode.PRESERVE
    if mode == RenderMode.PRESERVE:
        return render_preserve(pixels)
    if mode == RenderMode.REGION_REPLACE:
        return render_region_replace(pixels, regions, plates)
    if mode == RenderMode.OVERLAY:
        return render_overlay(pixels, regions, plates, alpha=alpha)
    if mode == RenderMode.FULL_REPAINT:
        return render_full_repaint(pixels, regions, plates, background=background)
    return render_preserve(pixels)


__all__ = [
    "render_preserve",
    "render_region_replace",
    "render_overlay",
    "render_full_repaint",
    "render_image_decision",
]
