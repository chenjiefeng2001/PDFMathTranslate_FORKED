"""BabelDOC 伪代码保护布局模型（pseudo-code protection layout model）。

背景
----
BabelDOC 对算法/伪代码块的保护完全依赖布局模型的 ``algorithm`` 类别：被识别
为 ``algorithm`` 的字符不会进入翻译（``paragraph_finder`` 把非文本布局字符丢进
``skip_chars``，导出阶段原样绘制保留）。

但 BabelDOC 默认布局模型 ``doclayout_yolo_docstructbench_imgsz1024.onnx``
只有 10 类、没有 ``algorithm``，伪代码块会被识别为 ``plain text``/``paragraph``
而进入翻译管线（详见 ``doc/babeldoc_pseudocode_mistranslation_report.md``）。

方案
----
本模块引入 PaddleOCR **PP-DocLayoutV2**（25 类，含 ``algorithm``）作为*辅助*
布局检测器，与 BabelDOC 默认模型融合（:class:`PseudoCodeProtectedLayoutModel`）：

* 默认模型负责全部常规布局（翻译质量与现有行为完全不变）；
* PP-DocLayoutV2 只负责找出 ``algorithm`` 框；
* 默认模型输出的**文本框**若被 ``algorithm`` 框覆盖足够比例，就被重新标记为
  ``algorithm`` —— BabelDOC 的 ``layout_priority`` 里 ``algorithm`` 优先级极高
  （第 4 位），这些字符会被跳过翻译并在输出 PDF 中原样保留。

融合模型实现 BabelDOC 0.6.x 的 ``DocLayoutModel`` 接口（``handle_document``），
可注入 ``TranslationConfig(doc_layout_model=...)``。任一环节不可用（模型文件缺失、
加载失败）都会自动降级：只返回默认模型结果，BabelDOC 模式本身不受影响。

模型分发
--------
PP-DocLayoutV2.onnx（约 204 MB）默认放在 BabelDOC 的模型缓存目录
（``babeldoc.const.get_cache_file_path("PP-DocLayoutV2.onnx", "models")``），
可用环境变量 ``PDF2ZH_PP_DOCLAYOUT_MODEL`` 覆盖路径。缺失时通过
``python -m pdf2zh.doclayout_pseudocode --download`` 从 hf-mirror 下载；
未下载则伪代码保护静默禁用（保持现状）。
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

#: PP-DocLayoutV2 的 ``algorithm`` 类别 id（25 类 id2label）。
_ALGORITHM_CLS_ID = 1

#: 与 BabelDOC ``layout_helper.is_text_layout`` 白名单保持一致的文本类别。
#: 只有这些类别的框才可能被提升为 ``algorithm``（伪代码块必然先被默认模型
#: 识别为其中一种文本类别）。
TEXT_LAYOUT_CLASSES = frozenset(
    {
        "plain text",
        "tiny text",
        "title",
        "abandon",
        "figure_caption",
        "table_caption",
        "table_text",
        "table_footnote",
        "paragraph_title",
        "abstract",
        "content",
        "figure_title",
        "table_title",
        "doc_title",
        "footnote",
        "header",
        "footer",
        "seal",
        "text",
        "chart_title",
        "paragraph",
        "table_cell",
        "figure_text",
        "list_item",
        "caption",
        "page_header",
        "page_footer",
        "wired_table_cell",
        "wireless_table_cell",
    }
)

#: PP-DocLayoutV2 ONNX 模型下载地址（hf-mirror，与
#: ``tools/diag_pp_doclayout.py`` 实测所用模型一致）。
PP_DOCLAYOUT_V2_MODEL_URL = (
    "https://hf-mirror.com/alex-dinh/PP-DocLayoutV2-ONNX/"
    "resolve/main/PP-DocLayoutV2.onnx"
)

_MODEL_FILE_NAME = "PP-DocLayoutV2.onnx"


def get_pp_doclayout_model_path() -> Path:
    """返回 PP-DocLayoutV2 模型文件路径（不保证存在）。

    - 优先 ``PDF2ZH_PP_DOCLAYOUT_MODEL`` 环境变量；
    - 否则用 BabelDOC 模型缓存目录（``babeldoc.const.get_cache_file_path``）；
    - 兜底 ``~/.cache/babeldoc/models/PP-DocLayoutV2.onnx``。
    """
    override = os.environ.get("PDF2ZH_PP_DOCLAYOUT_MODEL")
    if override:
        return Path(override)
    try:
        from babeldoc.const import get_cache_file_path

        return get_cache_file_path(_MODEL_FILE_NAME, "models")
    except Exception:  # noqa: BLE001 -- babeldoc 缓存 API 变化时兜底
        return Path.home() / ".cache" / "babeldoc" / "models" / _MODEL_FILE_NAME


def download_paddle_doclayout_algorithm_model(force: bool = False) -> Path:
    """把 PP-DocLayoutV2.onnx 下载到 BabelDOC 模型缓存目录（幂等）。

    Args:
        force: True 时即使文件已存在也重新下载。

    Returns:
        模型文件路径。

    Raises:
        OSError: 下载失败。
    """
    target = get_pp_doclayout_model_path()
    if target.exists() and not force:
        logger.info("PP-DocLayoutV2 model already present: %s", target)
        return target

    import urllib.request

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".downloading")
    logger.info("Downloading PP-DocLayoutV2 model from %s", PP_DOCLAYOUT_V2_MODEL_URL)
    logger.info("This is a ~204 MB download and may take a while ...")
    urllib.request.urlretrieve(PP_DOCLAYOUT_V2_MODEL_URL, tmp)
    tmp.replace(target)
    logger.info("PP-DocLayoutV2 model saved to %s", target)
    return target


def _is_text_layout_name(name: str) -> bool:
    """判断布局类别名是否为文本类别（与 BabelDOC 的 ``is_text_layout`` 同步）。"""
    if name in TEXT_LAYOUT_CLASSES:
        return True
    try:
        from babeldoc.format.pdf.document_il.utils.layout_helper import (
            is_text_layout,
        )
        import types as _types

        return bool(is_text_layout(_types.SimpleNamespace(name=name)))
    except Exception:  # noqa: BLE001 -- 白名单兜底即可
        return False



class PaddleDocLayoutV2Detector:
    """PP-DocLayoutV2 ONNX 推理，只用于提取 ``algorithm`` 类别的检测框。

    推理协议（实测有效，见 ``tools/diag_pp_doclayout.py``）：
    * 输入 RGB 图，非保比 resize 到 800x800 + ImageNet 归一化；
    * 三个输入：``im_shape=[1,2]``、``image=[1,3,800,800]``、``scale_factor=[1,2]``；
    * 输出 ``[1, N, 8]``，每行 ``[label_idx, score, xmin, ymin, xmax, ymax, ...]``，
      坐标为经 ``scale_factor`` 反算后的原图像素坐标。
    """

    def __init__(
        self,
        model_path: os.PathLike | str,
        conf_threshold: float = 0.45,
    ) -> None:
        import onnxruntime as ort

        self.model_path = str(model_path)
        self.conf_threshold = conf_threshold
        # 遵循 BabelDOC 后端开关（--backend / PDF2ZH_BABELDOC_BACKEND）：
        # auto=CPU（原生行为）、cuda/dml 显式启用 GPU，GPU 不可用时自动回退 CPU。
        from pdf2zh.babeldoc_onnx_backend import (
            get_babeldoc_backend,
            resolve_babeldoc_providers,
        )

        providers = resolve_babeldoc_providers(get_babeldoc_backend())
        self._session = ort.InferenceSession(self.model_path, providers=providers)
        self._lock = threading.Lock()

    def detect_algorithm_boxes(
        self, image_rgb: np.ndarray
    ) -> list[tuple[float, float, float, float]]:
        """检测图中的 algorithm 框。

        Args:
            image_rgb: RGB 图像（如 ``RasterGeometry.image``）。

        Returns:
            ``[(x1, y1, x2, y2), ...]``，坐标为图像像素坐标（x/y 分别与宽/高对齐）。
        """
        with self._lock:
            blob, scale = self._preprocess(image_rgb)
            shape = np.array([[800, 800]], dtype=np.float32)
            out = self._session.run(
                None,
                {
                    "im_shape": shape,
                    "image": blob,
                    "scale_factor": scale,
                },
            )[0]

        boxes: list[tuple[float, float, float, float]] = []
        for row in out:
            score = float(row[1])
            if score < self.conf_threshold:
                continue
            if int(round(float(row[0]))) != _ALGORITHM_CLS_ID:
                continue
            # 模型输出坐标已通过 scale_factor 反算回原图像素坐标（实测：
            # 输入 842x595 时输出 xmin/ymin/xmax/ymax 即 0..842 / 0..595 范围）。
            x1 = float(row[2])
            y1 = float(row[3])
            x2 = float(row[4])
            y2 = float(row[5])
            boxes.append((x1, y1, x2, y2))
        return boxes

    @staticmethod
    def _preprocess(image_rgb: np.ndarray):
        import cv2

        orig_h, orig_w = image_rgb.shape[:2]
        scale_h = 800.0 / orig_h
        scale_w = 800.0 / orig_w
        resized = cv2.resize(
            image_rgb,
            (int(orig_w * scale_w), int(orig_h * scale_h)),
            interpolation=cv2.INTER_LINEAR,
        )
        img = resized.astype(np.float32) / 255.0
        mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
        std = np.array([0.229, 0.224, 0.225], dtype=np.float32)
        img = (img - mean) / std
        blob = np.transpose(img, (2, 0, 1))[None]
        return blob, np.array([[scale_h, scale_w]], dtype=np.float32)



class PseudoCodeProtectedLayoutModel:
    """BabelDOC 默认布局模型 + PP-DocLayoutV2 ``algorithm`` 保护的融合模型。

    实现 BabelDOC 0.6.x 的 ``DocLayoutModel`` 接口（``stride`` /
    ``handle_document``），可直接注入 ``TranslationConfig(doc_layout_model=...)``。

    流程（每个页面）：
    1. 用与 BabelDOC 默认模型完全相同的渲染（``with_target_long_edge``）生成
       页面图，调用默认模型 ``predict`` 得到常规布局（10 类，pt 坐标）；
    2. 调用 PP-DocLayoutV2 检测 ``algorithm`` 框（像素坐标），经
       ``RasterGeometry.px_len_to_pt`` 转为 pt 坐标；
    3. 默认模型的**文本框**若被任一 ``algorithm`` 框覆盖比例
       ``>= cover_threshold``，其类别被改写为 ``algorithm``（并把置信度抬高，
       避免被后续排序/过滤丢弃）。

    被标记为 ``algorithm`` 的框在 BabelDOC 布局分配中优先级极高
    （``layout_helper.layout_priority`` 第 4 位），其内的字符全部跳过翻译、
    在输出 PDF 中原样保留。
    """

    def __init__(
        self,
        base_model,
        detector: Optional[PaddleDocLayoutV2Detector] = None,
        cover_threshold: float = 0.35,
    ) -> None:
        self.base_model = base_model
        self.detector = detector
        self.cover_threshold = cover_threshold
        self._algo_cls_cache: dict[int, int] = {}
        self._algo_cls_lock = threading.Lock()

    @property
    def stride(self) -> int:
        return self.base_model.stride

    def handle_document(self, pages, mupdf_doc, translate_config, save_debug_image):
        """与 BabelDOC ``DocLayoutModel.handle_document`` 签名一致的生成器。"""
        from babeldoc.format.pdf.document_il.utils.raster_geometry import (
            with_target_long_edge,
        )

        for page in pages:
            translate_config.raise_if_cancelled()
            with self.base_model.lock:
                geometry = with_target_long_edge(
                    mupdf_doc[page.page_number],
                    72,
                    1024,
                    normalize_rotation=True,
                )
                image_bgr = geometry.image[:, :, ::-1]  # RGB -> BGR（与默认模型一致）
                predict_result = self.base_model.predict(
                    image_bgr, geometry=geometry
                )[0]
            try:
                self._protect_page(
                    geometry, predict_result, page.page_number
                )
            except Exception:  # noqa: BLE001 -- 保护失败不应阻断翻译主流程
                logger.warning(
                    "pseudo-code protection failed on page %s",
                    page.page_number,
                    exc_info=True,
                )
            save_debug_image(image_bgr, predict_result, page.page_number + 1)
            yield page, predict_result

    def _protect_page(self, geometry, yolo_result,
                      page_number: Optional[int] = None) -> None:
        """把默认模型文本框中被 algorithm 框覆盖的部分提升为 algorithm。"""
        if self.detector is None:
            return
        # 检测器接口兼容：MinerUAlgorithmDetector 支持按页提取（page_index），
        # PaddleDocLayoutV2Detector 无页概念（只接收 image）。统一先按主接口
        # 调用，遇到 TypeError 再退化为纯图像接口，避免 PP-DocLayoutV2 路径
        # 每页静默失败。
        try:
            algo_boxes = self.detector.detect_algorithm_boxes(
                geometry.image, page_index=page_number
            )
        except TypeError:
            algo_boxes = self.detector.detect_algorithm_boxes(geometry.image)
        if not algo_boxes:
            return
        algo_pt = []
        for x1, y1, x2, y2 in algo_boxes:
            algo_pt.append(
                (
                    geometry.px_len_to_pt(x1, "x"),
                    geometry.px_len_to_pt(y1, "y"),
                    geometry.px_len_to_pt(x2, "x"),
                    geometry.px_len_to_pt(y2, "y"),
                )
            )
        for box in yolo_result.boxes:
            cls_name = yolo_result.names.get(int(box.cls), "")
            if not _is_text_layout_name(cls_name):
                continue
            bx1, by1, bx2, by2 = (float(v) for v in box.xyxy)
            box_area = (bx2 - bx1) * (by2 - by1)
            if box_area <= 0:
                continue
            best_cover = 0.0
            for ax1, ay1, ax2, ay2 in algo_pt:
                ix = min(bx2, ax2) - max(bx1, ax1)
                iy = min(by2, ay2) - max(by1, ay1)
                if ix > 0 and iy > 0:
                    cover = (ix * iy) / box_area
                    if cover > best_cover:
                        best_cover = cover
            if best_cover >= self.cover_threshold:
                algo_cls = self._allocate_algorithm_cls(yolo_result)
                box.cls = algo_cls
                # 保持 numpy 标量类型：BabelDOC layout_parser 对 conf 调用
                # ``.item()``，赋成 Python float 会直接抛 AttributeError。
                box.conf = max(box.conf, np.float32(0.99))
                logger.debug(
                    "page layout: promoted %r box to algorithm (cover=%.2f)",
                    cls_name,
                    best_cover,
                )

    def _allocate_algorithm_cls(self, yolo_result) -> int:
        """在 names 中登记 ``algorithm`` 类别并返回其 id（幂等）。"""
        key = id(yolo_result.names)
        with self._algo_cls_lock:
            cached = self._algo_cls_cache.get(key)
            if cached is not None:
                return cached
            for existing, name in yolo_result.names.items():
                if name == "algorithm":
                    self._algo_cls_cache[key] = existing
                    return existing
            keys = [int(k) for k in yolo_result.names]
            new_cls = max(keys) + 1 if keys else 0
            yolo_result.names[new_cls] = "algorithm"
            self._algo_cls_cache[key] = new_cls
            return new_cls




class MinerUAlgorithmDetector:
    """MinerU VLM 布局模型分支：从 magic-pdf/MinerU 解析结果提取 algorithm/code 框。

    Step 1.2 目标——用 MinerU 的 VLM 布局能力替代/补充 PP-DocLayoutV2 检测
    ``algorithm``（伪代码）块。接口与 :class:`PaddleDocLayoutV2Detector` 兼容
    （``detect_algorithm_boxes(image_rgb, page_index=None)``），因此可直接注入
    :class:`PseudoCodeProtectedLayoutModel`。

    坐标处理：magic-pdf 的 bbox 是「PDF 点、左上角原点、y 向下」；本类把它
    换算成与 ``RasterGeometry.image`` 对齐的像素坐标（按渲染图宽高等比缩放），
    ``_protect_page`` 无需区分来源即可统一按像素坐标处理。

    注意：本检测器依赖具体 PDF（per-document），**不参与** ``_fused_model``
    全局缓存，避免跨文档串台。
    """

    #: magic-pdf 布局类别中被视为 algorithm/伪代码的类别。
    _ALGO_CLASSES = frozenset({"code", "algorithm"})

    def __init__(
        self,
        pdf_path: str,
        backend: Optional[str] = None,
    ) -> None:
        from pdf2zh.magicpdf_adapter import MagicPdfAdapter

        adapter = MagicPdfAdapter(device=backend or "auto")
        if not adapter.is_available():
            raise RuntimeError("magic-pdf/MinerU not available")
        self.results = adapter.parse(pdf_path, ocr=False)
        self._page_boxes: list[list[tuple[float, float, float, float]]] = []
        self._page_pt_sizes: list[tuple[float, float]] = []
        self._lock = threading.Lock()
        for result in self.results or []:
            boxes: list[tuple[float, float, float, float]] = []
            pw = float(getattr(result, "width", 0.0) or 0.0)
            ph = float(getattr(result, "height", 0.0) or 0.0)
            self._page_pt_sizes.append((pw, ph))
            for blk in (result.blocks or []):
                if str(blk.get("cls", "") or "").lower() not in self._ALGO_CLASSES:
                    continue
                bbox = blk.get("bbox") or [0, 0, 0, 0]
                if len(bbox) != 4 or pw <= 0 or ph <= 0:
                    continue
                x0, y0, x1, y1 = (float(v) for v in bbox)
                boxes.append((x0, y0, x1, y1))
            self._page_boxes.append(boxes)

    def detect_algorithm_boxes(
        self, image_rgb: np.ndarray, page_index: Optional[int] = None
    ) -> list[tuple[float, float, float, float]]:
        """返回指定页的 algorithm/code 框（与 ``image_rgb`` 对齐的像素坐标）。

        magic-pdf 的 bbox 是 PDF 点坐标（左上角原点、y 向下）；按渲染图宽高
        与页面 PDF 尺寸的比例换算为像素坐标，使 ``_protect_page`` 中统一的
        ``px_len_to_pt`` 转换得到正确的 pt 结果。
        """
        with self._lock:
            if page_index is None or not (0 <= page_index < len(self._page_boxes)):
                return []
            h, w = image_rgb.shape[:2]
            pw, ph = self._page_pt_sizes[page_index]
            if pw <= 0 or ph <= 0 or w <= 0 or h <= 0:
                return []
            sx, sy = w / pw, h / ph
            out: list[tuple[float, float, float, float]] = []
            for x0, y0, x1, y1 in self._page_boxes[page_index]:
                out.append((x0 * sx, y0 * sy, x1 * sx, y1 * sy))
            return out


#: 进程级缓存，避免每个翻译任务重复加载 71MB + 204MB 两个模型。
_fused_model = None
_fused_model_lock = threading.Lock()


def _load_base_layout_model():
    """加载 BabelDOC 默认布局模型（10 类 docstructbench）。

    构造前先应用后端补丁（幂等）：显式 ``cuda``/``dml`` 时让 BabelDOC 内部
    ONNX 会话也走 GPU，而不是硬编码的 CPU-only。
    """
    from pdf2zh.babeldoc_onnx_backend import apply_babeldoc_backend

    apply_babeldoc_backend()
    from babeldoc.docvision.doclayout import OnnxModel as BabelOnnxModel

    return BabelOnnxModel.from_pretrained()


def _try_build_algorithm_detector() -> Optional[PaddleDocLayoutV2Detector]:
    """构建 PP-DocLayoutV2 检测器；不可用时返回 None（保护降级）。"""
    model_path = get_pp_doclayout_model_path()
    if not model_path.exists():
        logger.warning(
            "PP-DocLayoutV2 model not found at %s; pseudo-code protection "
            "disabled. Run `python -m pdf2zh.doclayout_pseudocode --download` "
            "to enable it.",
            model_path,
        )
        return None
    try:
        return PaddleDocLayoutV2Detector(model_path)
    except Exception:  # noqa: BLE001 -- 降级：保留默认模型行为
        logger.warning(
            "failed to load PP-DocLayoutV2 detector from %s; "
            "pseudo-code protection disabled",
            model_path,
            exc_info=True,
        )
        return None


def build_pseudo_code_protected_layout_model(pdf_path: Optional[str] = None):
    """构建融合布局模型（默认模型 + algorithm 伪代码保护），进程内幂等。

    Step 1.2：当 ``pdf_path`` 提供且 magic-pdf/MinerU 可用时，优先用
    MinerU VLM 布局解析提取 algorithm/code 块（:class:`MinerUAlgorithmDetector`，
    per-document、不缓存）；否则回退 PP-DocLayoutV2 ONNX 检测器（全局缓存）。
    任一不可用时返回基础默认布局模型；BabelDOC 不可用时返回 ``None``。

    Args:
        pdf_path: 可选输入 PDF 路径；提供时尝试 MinerU VLM 分支。

    Returns:
        ``PseudoCodeProtectedLayoutModel``；模型/检测器任一不可用时返回基础
        默认布局模型；BabelDOC 不可用时返回 ``None``（调用方回退到 BabelDOC
        自身默认模型）。
    """
    global _fused_model
    if pdf_path:
        return _build_with_mineru_or_paddle(pdf_path)
    if _fused_model is not None:
        return _fused_model
    with _fused_model_lock:
        if _fused_model is not None:
            return _fused_model
        try:
            base = _load_base_layout_model()
        except Exception:  # noqa: BLE001 -- 让 BabelDOC 用默认模型
            logger.warning(
                "failed to load BabelDOC default layout model; "
                "using engine default",
                exc_info=True,
            )
            return None
        detector = _try_build_algorithm_detector()
        if detector is None:
            _fused_model = base
            return base
        _fused_model = PseudoCodeProtectedLayoutModel(base, detector)
        logger.info(
            "BabelDOC pseudo-code protection enabled "
            "(PP-DocLayoutV2 algorithm detector)"
        )
        return _fused_model


def _build_with_mineru_or_paddle(pdf_path: str):
    """per-document 构建：优先 MinerU VLM 分支，失败回退 PP-DocLayoutV2。

    MinerU detector 依赖具体 PDF，不能进全局缓存；任何一步失败都退化为
    PP-DocLayoutV2（或纯 base 模型），绝不让保护功能阻断 BabelDOC 主链路。
    """
    try:
        base = _load_base_layout_model()
    except Exception:  # noqa: BLE001
        logger.warning(
            "failed to load BabelDOC default layout model; "
            "using engine default",
            exc_info=True,
        )
        return None
    detector = None
    try:
        detector = MinerUAlgorithmDetector(pdf_path)
        logger.info(
            "BabelDOC pseudo-code protection enabled "
            "(MinerU VLM algorithm detector)"
        )
    except Exception as exc:  # noqa: BLE001 -- 回退 PP-DocLayoutV2
        logger.debug(
            "MinerU algorithm detector unavailable (%s); "
            "falling back to PP-DocLayoutV2",
            exc,
        )
        detector = _try_build_algorithm_detector()
    if detector is None:
        return base
    return PseudoCodeProtectedLayoutModel(base, detector)


def _main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="BabelDOC 伪代码保护：PP-DocLayoutV2 模型管理"
    )
    parser.add_argument(
        "--download",
        action="store_true",
        help="下载 PP-DocLayoutV2.onnx 到 BabelDOC 模型缓存目录（幂等）",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="检查模型是否就绪",
    )
    args = parser.parse_args(argv)
    if args.download:
        path = download_paddle_doclayout_algorithm_model()
        print(f"PP-DocLayoutV2 model ready at: {path}")
        return 0
    if args.check:
        path = get_pp_doclayout_model_path()
        if path.exists():
            print(f"OK: {path} ({path.stat().st_size // (1024 * 1024)} MB)")
            return 0
        print(f"MISSING: {path}")
        return 1
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())

