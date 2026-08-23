"""Module: Image Translation Engine — 「图片对象生命周期」的决策层。

本模块把"图片翻译"从 OCR 附属逻辑独立成一条与正文翻译平行的管线，
核心原则见 doc/link_rect_mismatch_report.md 旁支讨论：

    系统不是判断"图片里有没有文字"，而是判断"图片里的文字是否应该翻译、
    以及应该如何翻译"。

因此本模块**不内置 OCR**，也不调用任何翻译器。它只负责：

    ImageObject → ImageAnalyzer（分类） → TextRegion（文字区域，保留背景）
              → TranslationDecisionEngine（per-region 决策）
              → ImageTranslationPolicy（类型级默认策略）
              → Router（技术词典/品牌/UI 等 keep 规则）
              → RenderMode（Preserve / Overlay / RegionReplace / FullRepaint）

所有决策都是规则 + 统计特征（不做像素级 OCR、不依赖 LLM），与
``v3/structure.py`` 的纯规则分类风格一致；CNN/ViT 可作为未来可选的分
类后端（``ImageClassifierBackend`` 接口预留），但默认实现不掉任何重型依赖。

职责边界：
- 翻译器（Google/DeepL/OpenAI）在本模块之后按 region 逐段调用，且只认
  ``TranslationDecision.translate`` 为 True 的 region —— 见 Router。
- LLM 仅在 ``TranslationDecisionEngine`` 置信度不足时做类别辅助判定，
  不参与 OCR、不参与重绘。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

try:  # numpy 为本项目硬依赖（pyproject），此处惰性导入以保持纯逻辑可测
    import numpy as np
except ImportError:  # pragma: no cover - 环境告警而非崩溃
    np = None


# ── 图片类型 ──────────────────────────────────────────────────────────────


class ImageClass(Enum):
    """图片内容类型（Phase 2 Image Classification 的输出）。

    判定目标是「这张图属于什么」，而不是「图里有几行字」。
    """

    PHOTO = "photo"
    DIAGRAM = "diagram"
    CHART = "chart"
    SCREENSHOT = "screenshot"
    LOGO = "logo"
    EQUATION = "equation"
    QR_CODE = "qr_code"
    BARCODE = "barcode"
    MAP = "map"
    CAD = "cad"
    COMIC = "comic"
    UNKNOWN = "unknown"


class RenderMode(Enum):
    """Phase 5 的四种图片渲染模式。"""

    PRESERVE = "preserve"            # Mode 1：保持原图，仅正文翻译
    OVERLAY = "overlay"              # Mode 2：原图 + 透明译文图层
    REGION_REPLACE = "region_replace"  # Mode 3：仅替换 OCR region，背景保留
    FULL_REPAINT = "full_repaint"    # Mode 4：流程图/统计图，重排重绘


class ImageSource(Enum):
    """ImageObject.source 的取值。"""

    EMBEDDED = "embedded"
    VECTOR = "vector"
    FORM = "form"


# ── 数据结构 ──────────────────────────────────────────────────────────────


@dataclass
class ImageObject:
    """Phase 1 Image Object Detection 产出的图片对象。"""

    id: str
    page_num: int = 0
    bbox: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    width_px: int = 0
    height_px: int = 0
    dpi: float = 72.0
    has_alpha: bool = False
    source: str = ImageSource.EMBEDDED.value
    image_class: ImageClass = ImageClass.UNKNOWN
    class_confidence: float = 0.0
    features: Dict[str, float] = field(default_factory=dict)
    regions: List["TextRegion"] = field(default_factory=list)
    decision: Optional["TranslationDecision"] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "page_num": self.page_num,
            "bbox": list(self.bbox),
            "width_px": self.width_px,
            "height_px": self.height_px,
            "dpi": self.dpi,
            "has_alpha": self.has_alpha,
            "source": self.source,
            "image_class": self.image_class.value,
            "class_confidence": round(self.class_confidence, 4),
            "features": dict(self.features),
            "regions": [r.to_dict() for r in self.regions],
            "decision": self.decision.to_dict() if self.decision else None,
        }


@dataclass
class TextRegion:
    """Phase 4 Text Region Detection 产出的文字区域（只框不识别）。"""

    bbox: Tuple[float, float, float, float]  # 归一化 [0,1] 相对图片
    text: str = ""
    ocr_confidence: float = 0.0
    kind: str = "unknown"  # text/axis/label/ui/number/formula
    reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "bbox": list(self.bbox),
            "text": self.text,
            "ocr_confidence": round(self.ocr_confidence, 4),
            "kind": self.kind,
            "reasons": list(self.reasons),
        }


@dataclass
class TranslationDecision:
    """Phase 5 Translation Decision Engine 输出。

    与提案 §五 一致，输出结构化决策而非裸布尔：
      {"translate": true/false, "confidence": 0.xx, "render_mode": ...}
    """

    translate: bool
    confidence: float = 0.0
    render_mode: RenderMode = RenderMode.PRESERVE
    reasons: List[str] = field(default_factory=list)
    region_decisions: List["RegionDecision"] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "translate": self.translate,
            "confidence": round(self.confidence, 4),
            "render_mode": self.render_mode.value,
            "reasons": list(self.reasons),
            "regions": [d.to_dict() for d in self.region_decisions],
        }


@dataclass
class RegionDecision:
    """单个文字区域的翻译决策（decision engine 的最小粒度）。"""

    region: TextRegion
    translation_score: float = 0.0
    translate: bool = False
    confidence: float = 0.0
    reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "bbox": list(self.region.bbox),
            "text": self.region.text,
            "translation_score": round(self.translation_score, 4),
            "translate": self.translate,
            "confidence": round(self.confidence, 4),
            "reasons": list(self.reasons),
        }


# ── 类型级默认策略表（提案 §六） ──────────────────────────────────────────


@dataclass(frozen=True)
class ImagePolicy:
    """某种图片类型的默认翻译策略。"""

    render_mode: RenderMode
    translate_allowed: bool
    ocr_required: bool = False


IMAGE_POLICY: Dict[ImageClass, ImagePolicy] = {
    ImageClass.PHOTO: ImagePolicy(RenderMode.PRESERVE, False),
    ImageClass.LOGO: ImagePolicy(RenderMode.PRESERVE, False),
    ImageClass.QR_CODE: ImagePolicy(RenderMode.PRESERVE, False),
    ImageClass.BARCODE: ImagePolicy(RenderMode.PRESERVE, False),
    ImageClass.EQUATION: ImagePolicy(RenderMode.PRESERVE, False),
    ImageClass.SCREENSHOT: ImagePolicy(RenderMode.OVERLAY, True, ocr_required=True),
    ImageClass.DIAGRAM: ImagePolicy(RenderMode.REGION_REPLACE, True, ocr_required=True),
    ImageClass.CHART: ImagePolicy(RenderMode.REGION_REPLACE, True, ocr_required=True),
    ImageClass.MAP: ImagePolicy(RenderMode.OVERLAY, False),
    ImageClass.COMIC: ImagePolicy(RenderMode.OVERLAY, True, ocr_required=True),
    ImageClass.CAD: ImagePolicy(RenderMode.PRESERVE, False),
    ImageClass.UNKNOWN: ImagePolicy(RenderMode.PRESERVE, False),
}


# ── 路由器 keep 词典（提案 §七） ──────────────────────────────────────────

# 技术名词：一律 keep（不翻译）
TECHNICAL_KEEP_TERMS = frozenset(
    {
        "cpu", "gpu", "ram", "rom", "usb", "hdmi", "vga", "tcp", "ip",
        "http", "https", "url", "uri", "api", "sdk", "cli", "gui", "db",
        "sql", "html", "css", "json", "xml", "pdf", "png", "jpeg",
        "github", "gitlab", "docker", "kubernetes", "kubernetes",
        "windows", "linux", "macos", "android", "ios", "chrome",
    }
)

# 品牌/产品名：一律 keep
BRAND_KEEP_TERMS = frozenset(
    {
        "google", "amazon", "microsoft", "apple", "facebook", "meta",
        "alibaba", "tencent", "huawei", "xiaomi", "intel", "amd",
        "nvidia", "qualcomm", "samsung", "oracle", "salesforce",
    }
)

# 已知"UI 控件"，短文本一律 keep
UI_KEEP_TERMS = frozenset(
    {
        "ok", "cancel", "close", "open", "save", "exit", "run", "start",
        "stop", "next", "back", "yes", "no", "on", "off", "apply",
        "reset", "settings", "file", "edit", "view", "help", "about",
    }
)


def is_probably_brand_or_technical(text: str) -> bool:
    """Router 基础 keep 判定：技术名词/品牌/**UI 控件命中即 keep。"""
    tokens = [t.lower().strip(" ,.:;()[]{}") for t in str(text).split()]
    for tok in tokens:
        if not tok:
            continue
        if tok in TECHNICAL_KEEP_TERMS or tok in BRAND_KEEP_TERMS:
            return True
        # github.com → github
        base = tok.split(".")[0]
        if base in TECHNICAL_KEEP_TERMS or base in BRAND_KEEP_TERMS:
            return True
    return False


def router_should_translate(text: str) -> Tuple[bool, str]:
    """提案 §七 Translation Router：决定 region 文本是否进翻译器。

    Returns:
        (translate, reason)
    """
    t = (text or "").strip()
    if not t:
        return False, "empty"
    if is_probably_brand_or_technical(t):
        return False, "technical|brand"
    words = [w for w in t.split() if w]
    if len(words) == 1 and words[0].lower() in UI_KEEP_TERMS and len(t) <= 24:
        return False, "ui_control"
    if _looks_like_code(t):
        return False, "code"
    if _looks_like_number_only(t):
        return False, "number"
    return True, "translate"


def _looks_like_code(text: str) -> bool:
    if any(ch in text for ch in "{}_\\"):
        return True
    parts = text.split()
    return len(parts) == 1 and any(ch.isdigit() for ch in text) and \
        any(ch.isalpha() for ch in text)


def _looks_like_number_only(text: str) -> bool:
    stripped = text.replace("%", "").replace(".", "").replace("-", "").strip()
    return stripped.isdigit()


# ── 统计特征提取（Phase 1/2 输入） ────────────────────────────────────────


@dataclass
class ImageFeatures:
    """图片的统计特征向量（分类与决策共用，可 JSON 序列化）。"""

    width: int = 0
    height: int = 0
    aspect_ratio: float = 1.0
    color_count: int = 0
    unique_color_ratio: float = 0.0
    edge_density: float = 0.0
    luminance_std: float = 0.0
    dark_ratio: float = 0.0
    white_ratio: float = 0.0
    has_alpha: bool = False
    mostly_grayscale: bool = True

    def to_dict(self) -> Dict[str, float]:
        return {
            "width": self.width,
            "height": self.height,
            "aspect_ratio": round(self.aspect_ratio, 4),
            "color_count": self.color_count,
            "unique_color_ratio": round(self.unique_color_ratio, 4),
            "edge_density": round(self.edge_density, 4),
            "luminance_std": round(self.luminance_std, 4),
            "dark_ratio": round(self.dark_ratio, 4),
            "white_ratio": round(self.white_ratio, 4),
            "has_alpha": self.has_alpha,
            "mostly_grayscale": self.mostly_grayscale,
        }


def compute_image_features(pixels, has_alpha: bool = False) -> ImageFeatures:
    """从像素数组（RGB) 计算统计特征。

    纯 numpy；无法导入 numpy 或输入非数组时退化为空特征
    （调用方按 ``features.color_count == 0`` 归为 UNKNOWN）。
    """
    f = ImageFeatures(has_alpha=has_alpha)
    if np is None or pixels is None:
        return f
    try:
        arr = np.asarray(pixels)
        if arr.ndim == 2:  # grayscale -> fake RGB
            arr = np.stack([arr] * 3, axis=-1)
        if arr.ndim != 3 or arr.shape[2] < 3:
            return f
        rgb = arr[..., :3].astype(np.float32)
        h, w = rgb.shape[:2]
        f.width, f.height = int(w), int(h)
        f.aspect_ratio = w / max(h, 1)
        sample = rgb
        # downsample if large for stable quantized color counting
        if max(h, w) > 96:
            step = max(1, max(h, w) // 96)
            sample = rgb[::step, ::step]
        quant = (sample // 24).astype(np.int32)
        flat = quant.reshape(-1, 3)
        uniq = np.unique(flat, axis=0)
        f.color_count = int(len(uniq))
        f.unique_color_ratio = f.color_count / max(len(flat), 1)
        gray = rgb.mean(axis=2)
        f.luminance_std = float(np.std(gray))
        f.dark_ratio = float(np.mean(gray < 80))
        f.white_ratio = float(np.mean(gray > 220))
        # coarse Sobel-ish edge density via finite diff on downsampled gray
        gs = gray[::2, ::2]
        dx = np.abs(gs[1:, :] - gs[:-1, :])   # (h'-1, w')
        dy = np.abs(gs[:, 1:] - gs[:, :-1])   # (h', w'-1)
        dx = dx[:, :-1]                       # crop to common (h'-1, w'-1)
        dy = dy[:-1, :]
        edge = (dx + dy) / 2.0
        f.edge_density = float(np.mean(edge > 24))
        if arr.shape[2] >= 3:
            r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
            sat = float(np.mean(np.abs(r - g) + np.abs(g - b) > 12))
            f.mostly_grayscale = sat < 0.15
    except Exception:  # pragma: no cover - 特征失败不应击穿决策层
        return f
    return f


# ── Phase 2 分类器（规则优先，CNN 预留接口） ──────────────────────────────


class ImageClassifierBackend:
    """可替换分类后端接口。

    默认实现是纯规则统计（无模型依赖）；未来可挂 CNN / Vision
    Transformer，只需实现 classify(features) -> (ImageClass, confidence)。
    """

    def classify(self, features: ImageFeatures) -> Tuple[ImageClass, float]:
        raise NotImplementedError


@dataclass
class RuleClassifierConfig:
    """RuleImageClassifier 的全部判定阈值（V8.6 P2 标定入口）。

    默认值 = 启发式基线（与历史行为完全一致）；``calibrate``（见
    image_calibrate.py）可在真实语料上网格搜索并回写最优值。
    """

    # QR / 条形码
    qr_max_colors: int = 8
    qr_edge: float = 0.30
    qr_min_aspect: float = 0.8
    qr_max_aspect: float = 1.25
    barcode_max_aspect: float = 0.4
    barcode_edge: float = 0.25
    # CAD
    cad_max_colors: int = 4
    cad_edge: float = 0.35
    # Equation
    equation_min_aspect: float = 1.6
    equation_white: float = 0.55
    equation_max_colors: int = 64
    # Logo
    logo_max_area: int = 64 * 64
    logo_max_colors: int = 12
    # Photo
    photo_min_colors: int = 192
    photo_min_unique: float = 0.15
    photo_max_edge: float = 0.30
    # Chart
    chart_min_white: float = 0.4
    chart_max_colors: int = 96
    chart_min_edge: float = 0.18
    # Comic
    comic_min_colors: int = 120
    comic_min_edge: float = 0.28
    # Screenshot
    shot_min_colors: int = 24
    shot_max_colors: int = 192
    shot_max_edge: float = 0.30

    def tuned(self) -> Dict[str, float]:
        return {k: v for k, v in self.__dict__.items()
                if isinstance(v, (int, float))}


class RuleImageClassifier(ImageClassifierBackend):
    """纯统计规则分类器（与 structure.py 同风格，确定性、可单测）。

    判定顺序：QR/条形码 → CAD → Equation → Logo → 照片 → 图表/流程图 →
    Comic → 截图 → UNKNOWN。阈值全部收敛进 ``RuleClassifierConfig``
    （可调参，见 ``image_calibrate.calibrate``）。
    """

    def __init__(self, config: Optional[RuleClassifierConfig] = None) -> None:
        self.config = config or RuleClassifierConfig()

    def classify(self, features: ImageFeatures) -> Tuple[ImageClass, float]:
        f = features
        cfg = self.config
        area = max(f.width * f.height, 1)
        if f.width <= 0 or f.height <= 0 or f.color_count == 0:
            return ImageClass.UNKNOWN, 0.2

        # QR/条形码：方形/条状 + 黑白高对比 + 高边缘密度
        if f.mostly_grayscale and 0 < f.color_count <= cfg.qr_max_colors:
            if cfg.qr_min_aspect <= f.aspect_ratio <= cfg.qr_max_aspect \
                    and f.edge_density >= cfg.qr_edge:
                return ImageClass.QR_CODE, _bounded(f.edge_density * 1.6)
            if f.aspect_ratio < cfg.barcode_max_aspect and f.edge_density >= cfg.barcode_edge:
                return ImageClass.BARCODE, _bounded(0.6 + f.edge_density)

        # CAD：近单色工程图，超低色数 + 高边缘密度
        if f.color_count <= cfg.cad_max_colors and f.mostly_grayscale \
                and f.edge_density >= cfg.cad_edge and f.width >= 32 and f.height >= 32:
            return ImageClass.CAD, _bounded(0.55 + f.edge_density * 0.8)

        # Equation：窄高条、白底、密集细笔划、色数低
        if f.mostly_grayscale and f.aspect_ratio > cfg.equation_min_aspect \
                and f.white_ratio >= cfg.equation_white \
                and f.color_count <= cfg.equation_max_colors:
            return ImageClass.EQUATION, _bounded(0.55 + f.edge_density)

        # Logo：面积小、色数极少、对比强烈的简洁图形
        if area <= cfg.logo_max_area and f.color_count <= cfg.logo_max_colors \
                and (f.dark_ratio > 0.05 or f.edge_density > 0.02):
            return ImageClass.LOGO, _bounded(0.5 + max(0.0, 0.2 - f.unique_color_ratio))

        # 照片：高色数 + 低边缘密度（自然渐变）
        if f.color_count >= cfg.photo_min_colors \
                and f.unique_color_ratio >= cfg.photo_min_unique \
                and f.edge_density < cfg.photo_max_edge:
            return ImageClass.PHOTO, _bounded(0.55 + f.unique_color_ratio)

        # 图表/流程图：白底、中低色数、高边缘（线条/网格）
        if f.white_ratio >= cfg.chart_min_white \
                and f.color_count <= cfg.chart_max_colors \
                and f.edge_density >= cfg.chart_min_edge:
            return ImageClass.CHART, _bounded(0.5 + f.edge_density)

        # Comic：高色数 + 高边缘密度（粗描边）
        if f.color_count >= cfg.comic_min_colors and f.edge_density >= cfg.comic_min_edge:
            return ImageClass.COMIC, _bounded(0.55)

        # 截图：色数中等偏高 + 低-中边缘、常带深色/白色 UI 块
        if cfg.shot_min_colors <= f.color_count < cfg.shot_max_colors \
                and f.edge_density < cfg.shot_max_edge:
            return ImageClass.SCREENSHOT, _bounded(0.5)

        return ImageClass.UNKNOWN, 0.35


def _bounded(x: float) -> float:
    return float(max(0.0, min(1.0, x)))


def classify_image(features: ImageFeatures,
                   backend: Optional[ImageClassifierBackend] = None) -> Tuple[ImageClass, float]:
    backend = backend or RuleImageClassifier()
    return backend.classify(features)


# ── Phase 4 文字区域检测（保留背景，不整图 OCR） ──────────────────────────


def detect_text_regions(pixels, max_regions: int = 32) -> List[TextRegion]:
    """用对比度/密集度粗筛文字区域，返回归一化 bbox。

    用膨胀后的"暗像素 + 局部方差占比"分块扫描，命中块再聚合成连通 region。
    输出 bbox 归一化到 [0,1]，与图片坐标无耦合。
    """
    if np is None or pixels is None:
        return []
    try:
        arr = np.asarray(pixels)
        if arr.ndim == 3:
            gray = arr[..., :3].mean(axis=2)
        else:
            gray = arr
        h, w = gray.shape[:2]
        if h == 0 or w == 0:
            return []
        gs = gray.astype(np.float32)
        dark = gs < 120
        # smooth to reduce noise -> downscale
        ys, xs = np.where(dark)
        if len(ys) == 0:
            return []
        # candidate mask resampled on a 24x24 grid
        gy = (ys * 24 // h).clip(0, 23)
        gx = (xs * 24 // w).clip(0, 23)
        grid = np.zeros((24, 24), dtype=np.int32)
        for r, c in zip(gy, gx):
            grid[r, c] += 1
        occupied = grid > 0
        # simple connected-component over the coarse grid (8-connected)
        idx = np.argwhere(occupied)
        if len(idx) == 0:
            return []
        regions: List[Tuple[int, int, int, int]] = []
        assigned = np.zeros((24, 24), dtype=bool)
        for r0, c0 in idx:
            if assigned[r0, c0]:
                continue
            # BFS
            stack = [(int(r0), int(c0))]
            assigned[r0, c0] = True
            rs0, rs1, cs0, cs1 = int(r0), int(r0), int(c0), int(c0)
            while stack:
                r, c = stack.pop()
                rs0, cs0 = min(rs0, r), min(cs0, c)
                rs1, cs1 = max(rs1, r), max(cs1, c)
                for dr in (-1, 0, 1):
                    for dc in (-1, 0, 1):
                        nr, nc = r + dr, c + dc
                        if 0 <= nr < 24 and 0 <= nc < 24 and occupied[nr, nc] and \
                                not assigned[nr, nc]:
                            assigned[nr, nc] = True
                            stack.append((nr, nc))
            regions.append((rs0, cs0, rs1 + 1, cs1 + 1))
        out: List[TextRegion] = []
        for rs0, cs0, rs1, cs1 in regions[:max_regions]:
            bbox = (cs0 / 24.0, rs0 / 24.0, cs1 / 24.0, rs1 / 24.0)
            out.append(TextRegion(bbox=bbox))
        return out
    except Exception:  # pragma: no cover - 检测失败返回空列表
        logger.debug("text-region detection failed", exc_info=True)
        return []


# ── 决策引擎（Phase 3/5 核心） ────────────────────────────────────────────


class TranslationDecisionEngine:
    """为图片的每个文字区域计算 translation_score → 结构化决策。

    分数依据（提案 §五）：
      图片类型默认策略 / OCR 置信度 / 技术名词 / 品牌 Logo / UI 控件 /
      坐标轴数值 / 公式符号 / 文本长度。
    """

    def __init__(self, policy: Optional[Dict[ImageClass, ImagePolicy]] = None,
                 translate_threshold: float = 0.55) -> None:
        self.policy = dict(policy or IMAGE_POLICY)
        self.translate_threshold = translate_threshold

    def decide(self, image: ImageObject,
               regions: Optional[Sequence[TextRegion]] = None,
               ocr_confidence: Optional[float] = None) -> TranslationDecision:
        regions = list(regions if regions is not None else image.regions)

        pol = self.policy.get(image.image_class, IMAGE_POLICY[ImageClass.UNKNOWN])
        if not pol.translate_allowed:
            return TranslationDecision(
                translate=False,
                confidence=max(0.55, image.class_confidence),
                render_mode=pol.render_mode,
                reasons=[f"policy:{image.image_class.value}"],
            )

        region_decisions: List[RegionDecision] = []
        for reg in regions:
            conf = reg.ocr_confidence if reg.ocr_confidence > 0 else \
                (ocr_confidence or 0.5)
            score, reasons = self._score_region(reg, conf)
            region_decisions.append(RegionDecision(
                region=reg, translation_score=score, translate=score > self.translate_threshold,
                confidence=conf, reasons=reasons,
            ))

        translatable = [d for d in region_decisions if d.translate]
        if not translatable:
            return TranslationDecision(
                translate=False,
                confidence=0.6,
                render_mode=RenderMode.PRESERVE,
                reasons=["no_translatable_regions"],
                region_decisions=region_decisions,
            )
        avg = sum(d.translation_score for d in translatable) / len(translatable)
        return TranslationDecision(
            translate=True,
            confidence=_bounded(avg),
            render_mode=pol.render_mode,
            reasons=[f"policy:{image.image_class.value}", f"regions:{len(translatable)}"],
            region_decisions=region_decisions,
        )

    def _score_region(self, reg: TextRegion, ocr_conf: float) -> Tuple[float, List[str]]:
        """单区域分数累加（0~1）。"""
        if reg.kind == "axis_label":
            return 0.2, ["axis_number_keep"]
        if reg.kind == "ui_control":
            return 0.3, ["ui_keep"]
        text = (reg.text or "").strip()
        if not text:
            return 0.2, ["empty"]
        translate, reason = router_should_translate(text)
        if not translate:
            return 0.15, [f"router_keep:{reason}"]
        score = 0.5
        score += 0.15 * ocr_conf          # OCR 置信度高加分
        words = len(text.split())
        if words >= 2:
            score += 0.15
        if 2 <= len(text) <= 40:
            score += 0.05
        return _bounded(score), ["router_pass", "ocr_weight"]


# ── 端到端分析（可选 fitz 入口，guarded） ────────────────────────────────


def analyze_image_bytes(pixels, object_id: str = "img", page_num: int = 0,
                        has_alpha: bool = False,
                        engine: Optional[TranslationDecisionEngine] = None) -> ImageObject:
    """对内存中的像素数组执行完整分析：特征 → 分类 → 区域 → 决策。"""
    features = compute_image_features(pixels, has_alpha=has_alpha)
    image_class, conf = classify_image(features)
    regions = detect_text_regions(pixels)
    obj = ImageObject(
        id=object_id, page_num=page_num,
        width_px=features.width, height_px=features.height,
        has_alpha=has_alpha, image_class=image_class,
        class_confidence=conf, features=features.to_dict(),
        regions=regions,
    )
    obj.decision = (engine or TranslationDecisionEngine()).decide(obj)
    return obj


def analyze_pdf_images(doc, page_range: Optional[Sequence[int]] = None,
                       engine: Optional[TranslationDecisionEngine] = None) -> Dict[int, List[ImageObject]]:
    """从 PyMuPDF document 提取每页图片对象并做整条决策管线。

    guarded：PyMuPDF 不可用或单页失败时返回该页空列表，绝不抛错。
    输入 doc 需具有 ``page_count`` / ``__getitem__`` / 每页 ``get_images``
    与 ``doc.extract_image`` 接口（即 pymupdf.Document）。dpi 由像素/物理比估。
    """
    out: Dict[int, List[ImageObject]] = {}
    try:
        import numpy as _np  # noqa: F401  (确认 numpy 可用)
        n = doc.page_count
        rng = list(page_range) if page_range is not None else list(range(n))
        for pno in rng:
            if pno < 0 or pno >= n:
                continue
            page = doc[pno]
            pixmap = page.get_pixmap(matrix=None)  # 整页栅格
            pixels = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(
                pixmap.height, pixmap.width, pixmap.n
            ) if np is not None else None
            if pixels is None:
                continue
            # 每张嵌入图作为独立 ImageObject（bbox 取自页面）
            items = []
            try:
                img_infos = page.get_image_info()
            except Exception:
                img_infos = []
            if not img_infos and pixels is not None:
                items.append(analyze_image_bytes(
                    pixels[..., :3] if pixels.shape[2] >= 3 else pixels,
                    object_id=f"p{pno}_full", page_num=pno, engine=engine))
            for info in img_infos:
                obj = analyze_image_bytes(
                    pixels[..., :3] if pixels.shape[2] >= 3 else pixels,
                    object_id=f"p{pno}_x{info.get('xref', 0)}", page_num=pno,
                    engine=engine)
                bbox = info.get("bbox")
                if bbox:
                    obj.bbox = tuple(float(v) for v in bbox)
                    if "width" in info and "height" in info and bbox:
                        pw = max(float(bbox[2] - bbox[0]), 1e-6)
                        ph = max(float(bbox[3] - bbox[1]), 1e-6)
                        obj.dpi = float(info["width"]) / pw * 72.0
                items.append(obj)
            out[pno] = items
    except Exception as exc:  # pragma: no cover - guarded 边界
        logger.warning("image engine page analysis failed: %s", str(exc)[:160])
    return out


__all__ = [
    "ImageClass", "RenderMode", "ImageSource",
    "ImageObject", "TextRegion", "TranslationDecision", "RegionDecision",
    "ImagePolicy", "IMAGE_POLICY",
    "ImageFeatures", "compute_image_features",
    "ImageClassifierBackend", "RuleImageClassifier", "classify_image",
    "detect_text_regions", "TranslationDecisionEngine",
    "router_should_translate", "is_probably_brand_or_technical",
    "TECHNICAL_KEEP_TERMS", "BRAND_KEEP_TERMS", "UI_KEEP_TERMS",
    "analyze_image_bytes", "analyze_pdf_images",
]