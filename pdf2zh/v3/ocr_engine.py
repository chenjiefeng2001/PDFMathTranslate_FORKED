"""Module: OCREngine — 把 OCR 结果接入图片决策链（V8.6 P1 后半程）。

``image_engine`` 的 Phase 4（``detect_text_regions``）**只框不识别**：
``TextRegion.text`` 默认空串，决策引擎 ``_score_region`` 在 ``empty``
分支停留（0.2 分），OCR 置信度权重（``0.15 * ocr_conf``）永远不生效。

本模块提供：
    - ``OCRBackend`` 接口 —— 只做"框内识别"，不参与是否翻译的决策；
    - ``DeterministicOCRBackend`` —— 确定性桩（无模型依赖，测试/离线用）；
    - ``ocr_regions_into_object`` / ``ocr_into_pixels`` —— 把识别结果
      回填 ``TextRegion.text / ocr_confidence``，然后决策链（Router /
      translation_score）才有真实输入。

决策纪律不变：OCR 只是把"框里的字符"交给后续的 Translation Policy 判断
（系统不是判断图里有没有文字，而是判断该不该翻译、怎么翻译）。
"""

from __future__ import annotations

import logging
from typing import List, Optional, Sequence

from pdf2zh.v3.image_engine import TextRegion

logger = logging.getLogger(__name__)


class OCRBackend:
    """可替换 OCR 后端：recognize(pixels, regions) -> 回填后的 TextRegion。"""

    def recognize(self, pixels, regions: Sequence[TextRegion]) -> List[TextRegion]:
        raise NotImplementedError


class DeterministicOCRBackend(OCRBackend):
    """确定性 OCR 桩：按区域形态产出稳定文本（无模型依赖）。

    - 高/宽 > 3 的宽扁区域（图表标题带）→ "Sample label text"（可翻译）；
    - 窄高区域（坐标轴/刻度带）→ "value 123"（数字 keep）；
    - 空像素区域 → 保持空文本。

    该桩让整条决策链在无 OCR 服务时可端到端执行（单测/离线探针）。
    """

    wide_text = "Sample label text"
    narrow_text = "value 123"

    def __init__(
        self, confidence: float = 0.9, wide_text: str = "", narrow_text: str = ""
    ) -> None:
        self.confidence = confidence
        if wide_text:
            self.wide_text = wide_text
        if narrow_text:
            self.narrow_text = narrow_text

    def recognize(self, pixels, regions: Sequence[TextRegion]) -> List[TextRegion]:
        out: List[TextRegion] = []
        for reg in regions or []:
            bbox = tuple(float(v) for v in reg.bbox)
            w = max(bbox[2] - bbox[0], 1e-6)
            h = max(bbox[3] - bbox[1], 1e-6)
            if self._region_is_empty(pixels, bbox):
                out.append(
                    TextRegion(
                        bbox=bbox,
                        text="",
                        ocr_confidence=0.0,
                        kind=reg.kind,
                        reasons=[*reg.reasons, "ocr:empty"],
                    )
                )
                continue
            if w / h > 3.0:
                out.append(
                    TextRegion(
                        bbox=bbox,
                        text=self.wide_text,
                        ocr_confidence=self.confidence,
                        kind=reg.kind or "text",
                        reasons=[*reg.reasons, "ocr:deterministic"],
                    )
                )
            else:
                out.append(
                    TextRegion(
                        bbox=bbox,
                        text=self.narrow_text,
                        ocr_confidence=self.confidence,
                        kind=reg.kind or "text",
                        reasons=[*reg.reasons, "ocr:deterministic"],
                    )
                )
        return out

    @staticmethod
    def _region_is_empty(pixels, bbox) -> bool:
        """区域 5×5 网格采样：暗像素占比 < 1% 视为空。"""
        import numpy as np

        try:
            arr = np.asarray(pixels)
            h, w = arr.shape[:2]
            x0, y0, x1, y1 = (int(v * min(w, h)) if v <= 1 else int(v) for v in bbox)
            x0, x1 = max(0, min(x0, w - 1)), max(0, min(x1, w))
            y0, y1 = max(0, min(y0, h - 1)), max(0, min(y1, h))
            patch = arr[y0:y1, x0:x1]
            if patch.size == 0:
                return True
            gray = patch[..., :3].mean(axis=2) if patch.ndim == 3 else patch
            dark = float((gray < 120).mean())
            return dark < 0.01
        except Exception:  # noqa: BLE001 — 采样失败按非空处理（保守）
            return False


def ocr_regions_into_object(obj, backend: Optional[OCRBackend] = None) -> None:
    """把 OCR 结果回填进 ImageObject.regions（原地修改）。

    backend 为 None 时保持现状（不引入 OCR 行为）—— side-channel 纪律：
    OCR 失败/缺失绝不改变决策链默认结果。
    """
    if backend is None or obj is None or not getattr(obj, "regions", None):
        return
    if any(r.text for r in obj.regions):
        return  # 已有识别结果不重复 OCR
    pixels = getattr(obj, "_pixels", None)
    try:
        obj.regions = backend.recognize(pixels, obj.regions)
    except Exception as e:  # noqa: BLE001
        logger.debug("OCR backend failed (regions kept as-is): %s", str(e)[:120])


def ocr_into_pixels(
    pixels, regions: Sequence[TextRegion], backend: Optional[OCRBackend] = None
) -> List[TextRegion]:
    """像素 + 框 → 识别后的 regions（独立入口，便于 analyze 链路组合）。"""
    if backend is None:
        return list(regions or [])
    return backend.recognize(pixels, regions or [])


__all__ = [
    "OCRBackend",
    "DeterministicOCRBackend",
    "ocr_regions_into_object",
    "ocr_into_pixels",
]
