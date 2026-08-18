"""多信号融合的统一扫描判定（scan_damaged_text 报告 §6.3 长期实现）。

背景
----
BabelDOC 的 ``DetectScannedFile`` 只用 SSIM 像素相似度判定「文本层是否在像素上
可见」，无法识别「渲染可见但语义损坏」的文本层（``(cid:N)`` / ``�`` / ToUnicode
缺失 / 错误码点）；legacy（pdfminer）内核则完全没有扫描检测。报告结论 3 指出：
损坏信号「能看到，但用不上」—— v3 侧已有的 ``has_replacement`` / ``glyph_dump``
/ ``diagnostics.unicode_error`` 能力没有接入翻译前决策。

本模块把 5 类信号收敛为**统一融合判定**（任一信号命中阈值即触发 OCR，
而不是「全部通过才算扫描」——降低漏检）：

| 信号 | 来源 | 判别力 |
|---|---|---|
| ``pixel_ssim`` | BabelDOC ``DetectScannedFile``（best-effort） | 判「文本层是否可见」 |
| ``text_cid_fffd`` | pdfminer 提取文本 ``(cid:N)`` / ``�`` 比例 | 判「ToUnicode 是否损坏」 |
| ``font_to_unicode`` | ``glyph_dump.has_to_unicode`` 缺失率 | 判「字体解码可信度」 |
| ``ocr_crosscheck`` | magic-pdf / UniMERNet OCR 文本（可选） | 交叉验证 |
| ``image_ratio`` | 版面色块 / 图像面积占比 | 判「是否扫描页面」 |

纯逻辑 + 可选 pdfminer/pymupdf（预检路径 guarded），不触碰主链路。预检
（``preflight_scan_check``）只读文件、绝不写盘。
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

_RE_CID_NOTDEF = re.compile(r"\(cid:\d+\)")

#: 低信息量乱码符号带（报告中 §6.1 建议统计的符号簇）。
_NOISE_SYMBOLS = frozenset("\xa5\xa6\xff\x00\x01\x02\x03\x7f\xad\u0378\u0379")

#: 文本损坏率阈值（报告 §6.1 建议初始 10% 字符）。
TEXT_BROKEN_RATIO_THRESHOLD = 0.10
#: 页面级损坏阈值：≥30% 的采样页含损坏信号即命中（报告 §6.1）。
PAGE_BROKEN_THRESHOLD = 0.30
#: 字体 ToUnicode CMap 缺失率阈值（缺失 > 60% 判定解码不可信）。
TO_UNICODE_MISSING_THRESHOLD = 0.60
#: 页面图像面积占比阈值（≥60% 判定为扫描页面）。
IMAGE_RATIO_THRESHOLD = 0.60
#: 预检默认采样页数。
DEFAULT_MAX_PAGES = 3


@dataclass
class TextQualityStats:
    """文本层质量统计（``analyze_text_quality`` 产出）。"""

    total_chars: int = 0
    cid_notdef_chars: int = 0
    fffd_chars: int = 0
    control_chars: int = 0
    noise_chars: int = 0
    pages: int = 0
    #: 页面级命中数（该页损坏字符占比 > ``TEXT_BROKEN_RATIO_THRESHOLD``）。
    broken_pages: int = 0

    @property
    def cid_notdef_ratio(self) -> float:
        return _safe_ratio(self.cid_notdef_chars, self.total_chars)

    @property
    def fffd_ratio(self) -> float:
        return _safe_ratio(self.fffd_chars, self.total_chars)

    @property
    def control_ratio(self) -> float:
        return _safe_ratio(self.control_chars, self.total_chars)

    @property
    def noise_ratio(self) -> float:
        return _safe_ratio(self.noise_chars, self.total_chars)

    @property
    def replacement_ratio(self) -> float:
        """替换字符 / 未定义 CID 的合并比例（报告的核心损坏信号）。"""
        return _safe_ratio(
            self.cid_notdef_chars + self.fffd_chars, self.total_chars)

    @property
    def broken_ratio(self) -> float:
        """归一化损坏率：取各类损坏信号的最大值（防单一信号稀释）。"""
        return max(
            self.cid_notdef_ratio,
            self.fffd_ratio,
            self.control_ratio,
            self.noise_ratio,
        )

    @property
    def broken_page_ratio(self) -> float:
        return _safe_ratio(self.broken_pages, self.pages) if self.pages else 0.0

    def to_dict(self) -> dict:
        return {
            "total_chars": self.total_chars,
            "cid_notdef_chars": self.cid_notdef_chars,
            "fffd_chars": self.fffd_chars,
            "control_chars": self.control_chars,
            "noise_chars": self.noise_chars,
            "pages": self.pages,
            "broken_pages": self.broken_pages,
            "cid_notdef_ratio": round(self.cid_notdef_ratio, 4),
            "fffd_ratio": round(self.fffd_ratio, 4),
            "control_ratio": round(self.control_ratio, 4),
            "noise_ratio": round(self.noise_ratio, 4),
            "replacement_ratio": round(self.replacement_ratio, 4),
            "broken_ratio": round(self.broken_ratio, 4),
            "broken_page_ratio": round(self.broken_page_ratio, 4),
        }


def _safe_ratio(part: int, total: int) -> float:
    return (part / total) if total > 0 else 0.0


def analyze_text_quality(texts: Sequence[str]) -> TextQualityStats:
    """从逐页提取文本统计文本层质量信号。

    统计 ``(cid:N)`` / ``�`` / 控制字符 / 低信息乱码符号带四类损坏信号，
    同时按页累计「该页损坏率超阈值」的页面数（报告 §6.1 的 10%/30% 双阈值
    决策面）。
    """
    stats = TextQualityStats()
    for page_text in texts or []:
        text = page_text or ""
        stats.pages += 1
        total = len(text)
        if total == 0:
            continue
        cid = len(_RE_CID_NOTDEF.findall(text))
        fffd = text.count("\ufffd")
        ctrl = sum(1 for ch in text if _is_control_char(ch))
        noise = sum(1 for ch in text if ch in _NOISE_SYMBOLS)
        stats.total_chars += total
        stats.cid_notdef_chars += cid
        stats.fffd_chars += fffd
        stats.control_chars += ctrl
        stats.noise_chars += noise
        page_broken = (cid + fffd + ctrl + noise) / total
        if page_broken >= TEXT_BROKEN_RATIO_THRESHOLD:
            stats.broken_pages += 1
    return stats


def _is_control_char(ch: str) -> bool:
    return ord(ch) < 0x20 or (0x7F <= ord(ch) < 0xA0)



@dataclass
class GlyphSignals:
    """字体解码信号（``analyze_glyph_signals`` 产出，来自 glyph_dump 记录）。"""

    total_glyphs: int = 0
    no_to_unicode: int = 0
    notdef_glyphs: int = 0
    fffd_glyphs: int = 0

    @property
    def to_unicode_missing_ratio(self) -> float:
        return _safe_ratio(self.no_to_unicode, self.total_glyphs)

    @property
    def decode_failure_ratio(self) -> float:
        return _safe_ratio(
            self.notdef_glyphs + self.fffd_glyphs, self.total_glyphs)

    def to_dict(self) -> dict:
        return {
            "total_glyphs": self.total_glyphs,
            "no_to_unicode": self.no_to_unicode,
            "notdef_glyphs": self.notdef_glyphs,
            "fffd_glyphs": self.fffd_glyphs,
            "to_unicode_missing_ratio": round(
                self.to_unicode_missing_ratio, 4),
            "decode_failure_ratio": round(self.decode_failure_ratio, 4),
        }


def analyze_glyph_signals(glyph_records: Sequence[dict]) -> GlyphSignals:
    """从 ``pipeline_dump.glyph_dump`` 的记录统计字体解码可信度。

    ``glyph_records`` 为 ``glyph_dump(ltpage)`` 产出的 dict 列表
    （含 ``has_to_unicode`` / ``decode`` / ``is_replacement`` 字段）。
    """
    signals = GlyphSignals()
    for rec in glyph_records or []:
        signals.total_glyphs += 1
        hu = rec.get("has_to_unicode")
        if hu is False:
            signals.no_to_unicode += 1
        elif hu is None:
            # 无字体对象 / 无法判定：计入「不可信」（保守，降低漏检）。
            signals.no_to_unicode += 1
        decode = rec.get("decode", "")
        if decode == "notdef":
            signals.notdef_glyphs += 1
        elif decode == "fffd":
            signals.fffd_glyphs += 1
    return signals


def layout_image_ratio(blocks: Sequence[dict]) -> float:
    """按页块的图像面积占比判「是否扫描页面」（0..1）。

    ``blocks`` 为页面块列表（magic-pdf middle.json 或 v3 ``BlockModel``
    dict）；含 ``has_image`` / ``type``∈{figure,image} 的块视为图像块。
    """
    total_area = 0.0
    image_area = 0.0
    for blk in blocks or []:
        box = _block_bbox(blk)
        if box is None:
            continue
        area = (box[2] - box[0]) * (box[3] - box[1])
        total_area += area
        kind = str(blk.get("kind") or blk.get("cls") or blk.get("type") or "")
        is_image = (
            blk.get("has_image")
            or kind.lower() in {"figure", "image", "table"}
        )
        if is_image:
            image_area += area
    return _safe_ratio(image_area, total_area)


def _block_bbox(blk: dict) -> Optional[List[float]]:
    bbox = blk.get("bbox")
    if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
        return [float(v) for v in bbox]
    for key in ("x0", "y0", "x1", "y1"):
        if key not in blk:
            return None
    return [float(blk["x0"]), float(blk["y0"]),
            float(blk["x1"]), float(blk["y1"])]


def ocr_crosscheck(texts: Sequence[str], ocr_texts: Sequence[str]) -> float:
    """提取文本与 OCR 文本的相似度（0..1，1=完全一致）。

    交叉验证信号：文本层完好 → 提取文本与 OCR 文本高度一致；损坏 → 差异大。
    采用字符级 Jaccard 相似度，对长短文本稳定。
    """
    if not texts or not ocr_texts or len(texts) != len(ocr_texts):
        return 0.0
    scores = []
    for a, b in zip(texts, ocr_texts):
        sa, sb = set(a or ""), set(b or "")
        if not sa and not sb:
            scores.append(1.0)
            continue
        if not sa or not sb:
            scores.append(0.0)
            continue
        inter = len(sa & sb)
        scores.append(inter / (len(sa) + len(sb) - inter))
    return sum(scores) / len(scores)



def _ssim_approx(a, b) -> float:
    """简化全局 SSIM（0..1）—— numpy-only，供像素差异信号使用。

    完整滑窗 SSIM（BabelDOC 用 skimage）需额外依赖；这里用全局统计近似，
    趋势一致：两图越相似越接近 1。
    """
    import numpy as np

    a = a.astype(np.float64)
    b = b.astype(np.float64)
    if a.shape != b.shape:
        raise ValueError("SSIM inputs must share shape")
    if a.size == 0:
        return 1.0
    mu_a, mu_b = a.mean(), b.mean()
    var_a, var_b = a.var(), b.var()
    cov = ((a - mu_a) * (b - mu_b)).mean()
    c1 = (0.01 * 255.0) ** 2
    c2 = (0.03 * 255.0) ** 2
    denom = ((mu_a ** 2 + mu_b ** 2 + c1) * (var_a + var_b + c2))
    if denom <= 1e-12:
        return 0.0
    return float(
        ((2 * mu_a * mu_b + c1) * (2 * cov + c2)) / denom
    )


def render_page_image(pdf_path: str, page_index: int, dpi: int = 72):
    """渲染 PDF 指定页为 RGB numpy 数组（best-effort，失败返回 None）。"""
    try:
        import pymupdf

        doc = pymupdf.open(pdf_path)
        try:
            pix = doc[page_index].get_pixmap(dpi=dpi)
            import numpy as np

            arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
                pix.height, pix.width, pix.n)
            if pix.n >= 3:
                return arr[:, :, :3]
            return arr
        finally:
            doc.close()
    except Exception as exc:  # noqa: BLE001 -- best-effort 信号
        logger.debug("render_page_image failed page %s: %s", page_index, exc)
        return None


def ssim_scanned_signal(pdf_path: str, max_pages: int = DEFAULT_MAX_PAGES):
    """SSIM 像素相似度信号（best-effort）。

    优先复用 BabelDOC 的 ``DetectScannedFile``（最接近其真实判定）；BabelDOC
    不可用或接口变化时回退到「渲染 → 与灰度噪声底图对比」的近似，仍失败则
    返回 ``None``（信号不可用，不参与融合触发）。
    """
    detector = _try_load_babeldoc_detector()
    if detector is not None:
        try:
            score = detector(pdf_path)
            if score is not None:
                return score
        except Exception as exc:  # noqa: BLE001
            logger.debug("babeldoc DetectScannedFile failed: %s", exc)
    return _approx_text_visibility_score(pdf_path, max_pages=max_pages)


def _try_load_babeldoc_detector():
    """探测 BabelDOC ``DetectScannedFile``（接口按 0.6.x 防御式探测）。

    当前版本 ``process()`` 接收 BabelDOC 内部 IL 文档对象而非文件路径，无法
    在无 IL 时直接调用，因此返回 None 交给近似路径。保留该探针以便未来
    BabelDOC 提供文件级入口时直接复用。
    """
    try:
        from babeldoc.format.pdf.document_il.midend.detect_scanned_file import (
            DetectScannedFile,
        )

        return DetectScannedFile() if DetectScannedFile else None
    except Exception:  # noqa: BLE001
        return None


def _approx_text_visibility_score(pdf_path: str,
                                  max_pages: int = DEFAULT_MAX_PAGES):
    """近似「文本层可见性」：全文本层渲染 vs 空白底图的差异度。

    扫描件（文本层不可见/缺失）→ 与空白底图高度相似（SSIM→1）；
    文本层可见 → 相似度明显下降。与 BabelDOC SSIM 判定同向。
    """
    try:
        import numpy as np

        doc = None
        try:
            import pymupdf

            doc = pymupdf.open(pdf_path)
            n_pages = min(int(max_pages or 1), doc.page_count)
            if n_pages <= 0:
                return None
            scores = []
            for idx in range(n_pages):
                page = doc[idx]
                pix = page.get_pixmap(dpi=72)
                arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
                    pix.height, pix.width, pix.n)
                rgb = arr[:, :, :3] if pix.n >= 3 else arr
                gray = rgb.mean(axis=2)
                # 空白底图：与该页平均灰度的纯色图（文本层可见时差异大）。
                blank = np.full_like(gray, gray.mean(), dtype=np.uint8)
                scores.append(_ssim_approx(gray, blank))
            return sum(scores) / len(scores) if scores else None
        finally:
            if doc is not None:
                doc.close()
    except Exception as exc:  # noqa: BLE001
        logger.debug("approx text-visibility signal failed: %s", exc)
        return None



@dataclass
class ScannedSignal:
    """单个信号的判定结果。"""

    name: str
    value: Optional[float]  # 信号强度（None=不可用，不参与触发）
    threshold: float
    detail: str = ""

    @property
    def triggered(self) -> bool:
        return self.value is not None and self.value >= self.threshold

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "value": None if self.value is None else round(float(self.value), 4),
            "threshold": round(float(self.threshold), 4),
            "triggered": self.triggered,
            "detail": self.detail,
        }


@dataclass
class ScanDecision:
    """多信号融合的扫描判定结果。"""

    is_scanned: bool
    signals: List[ScannedSignal] = field(default_factory=list)
    text_quality: Optional[TextQualityStats] = None
    glyph_signals: Optional[GlyphSignals] = None
    note: str = ""

    @property
    def reasons(self) -> List[str]:
        return [f"{s.name}: {s.value:.3f} >= {s.threshold:.2f}"
                for s in self.signals if s.triggered]

    def to_dict(self) -> dict:
        return {
            "is_scanned": self.is_scanned,
            "signals": [s.to_dict() for s in self.signals],
            "text_quality": self.text_quality.to_dict()
            if self.text_quality else None,
            "glyph_signals": self.glyph_signals.to_dict()
            if self.glyph_signals else None,
            "reasons": self.reasons,
            "note": self.note,
        }


def fused_scan_decision(
    signals: Sequence[ScannedSignal],
    text_quality: Optional[TextQualityStats] = None,
    glyph_signals: Optional[GlyphSignals] = None,
    note: str = "",
) -> ScanDecision:
    """融合判定：任一信号命中阈值即判定为扫描（降低漏检）。

    「任一命中即触发」是报告 §6.3 的核心要求——而不是「全部通过才算扫描」。
    """
    sig_list = list(signals)
    is_scanned = any(s.triggered for s in sig_list)
    return ScanDecision(
        is_scanned=is_scanned,
        signals=sig_list,
        text_quality=text_quality,
        glyph_signals=glyph_signals,
        note=note,
    )



# ── 预检：PDF 文件 → 融合判定 ────────────────────────────────────────────────


def _extract_pdf_samples(pdf_path: str, max_pages: int = DEFAULT_MAX_PAGES):
    """轻量提取 PDF 前 ``max_pages`` 页的文本与 glyph 记录。

    复用 ``PDFConverterEx`` 的 ``render_char`` 语义（``(cid:N)`` / ``�`` 由
    pdfminer 的 ``to_unichr`` 失败自然产生），但只收集字符，不跑翻译。
    返回 ``(page_texts, glyph_records)``。
    """
    from io import BytesIO

    from pdfminer.pdfdocument import PDFDocument
    from pdfminer.pdfinterp import PDFResourceManager
    from pdfminer.pdfpage import PDFPage
    from pdfminer.pdfparser import PDFParser

    from pdf2zh.converter import PDFConverterEx
    from pdf2zh.pdfinterp import PDFPageInterpreterEx

    page_texts: List[str] = []
    glyph_records: List[dict] = []
    with open(pdf_path, "rb") as fh:
        parser = PDFParser(BytesIO(fh.read()))
        doc = PDFDocument(parser)
        rsrcmgr = PDFResourceManager()
        device = PDFConverterEx(rsrcmgr)
        interp = PDFPageInterpreterEx(rsrcmgr, device, {})
        for pageno, page in enumerate(PDFPage.create_pages(doc)):
            if max_pages is not None and pageno >= max_pages:
                break
            page.pageno = pageno
            page.page_xref = pageno
            try:
                interp.process_page(page)
            except Exception as exc:  # noqa: BLE001
                logger.debug("preflight page %s extract failed: %s", pageno, exc)
                continue
            cur = device.cur_item
            chars = [o for o in getattr(cur, "_objs", []) if
                     o.__class__.__name__ == "LTChar"]
            page_texts.append("".join(c.get_text() or "" for c in chars))
            for ch in chars:
                font_obj = getattr(ch, "font", None)
                has_to_unicode = None
                if font_obj is not None:
                    try:
                        has_to_unicode = font_obj.get_toUnicode() is not None
                    except Exception:  # noqa: BLE001
                        has_to_unicode = None
                char = ch.get_text() or ""
                glyph_records.append({
                    "char": char,
                    "has_to_unicode": has_to_unicode,
                    "decode": "notdef" if _RE_CID_NOTDEF.search(char) else
                              ("fffd" if "\ufffd" in char else "ok"),
                    "is_replacement": bool(_RE_CID_NOTDEF.search(char)
                                           or "\ufffd" in char),
                })
    return page_texts, glyph_records


def preflight_scan_check(
    pdf_path: str,
    max_pages: int = DEFAULT_MAX_PAGES,
    blocks_by_page: Optional[Sequence[Sequence[dict]]] = None,
    ocr_texts: Optional[Sequence[str]] = None,
) -> ScanDecision:
    """PDF 翻译前综合预检：多信号融合判定是否为扫描/损坏文档。

    Args:
        pdf_path: 输入 PDF 路径。
        max_pages: 采样页数（默认 3，前 N 页代表性足够，快）。
        blocks_by_page: 可选页块列表（magic-pdf / BabelDOC 布局产物），
            用于图像面积占比信号。
        ocr_texts: 可选 OCR 文本（magic-pdf 已跑 OCR 时），用于交叉验证。

    Returns:
        :class:`ScanDecision`（``is_scanned=True`` 时任一信号命中阈值）。
    """
    signals: List[ScannedSignal] = []

    # 信号 2/3：文本质量 + 字体解码（pdfminer 轻量提取）。
    text_quality = TextQualityStats()
    glyph_signals = GlyphSignals()
    page_texts: List[str] = []
    if pdf_path and os.path.exists(pdf_path) and pdf_path.lower().endswith(".pdf"):
        try:
            page_texts, glyph_records = _extract_pdf_samples(
                pdf_path, max_pages=max_pages)
            text_quality = analyze_text_quality(page_texts)
            glyph_signals = analyze_glyph_signals(glyph_records)
        except Exception as exc:  # noqa: BLE001 -- 预检失败不阻断翻译
            logger.debug("preflight text extraction failed: %s", exc)

    signals.append(ScannedSignal(
        name="text_cid_fffd",
        value=max(text_quality.cid_notdef_ratio, text_quality.fffd_ratio),
        threshold=TEXT_BROKEN_RATIO_THRESHOLD,
        detail=f"(cid:N)/� 占比 {text_quality.replacement_ratio:.3f}",
    ))
    signals.append(ScannedSignal(
        name="text_broken_pages",
        value=text_quality.broken_page_ratio,
        threshold=PAGE_BROKEN_THRESHOLD,
        detail=(f"{text_quality.broken_pages}/{text_quality.pages} 页含损坏"
                f"信号（>={TEXT_BROKEN_RATIO_THRESHOLD:.0%}）"),
    ))
    signals.append(ScannedSignal(
        name="font_to_unicode",
        value=glyph_signals.to_unicode_missing_ratio,
        threshold=TO_UNICODE_MISSING_THRESHOLD,
        detail=f"ToUnicode 缺失率 {glyph_signals.to_unicode_missing_ratio:.3f}",
    ))

    # 信号 4：提取文本 vs OCR 文本一致性（可选）。
    if ocr_texts:
        cross = ocr_crosscheck(page_texts, ocr_texts)
        signals.append(ScannedSignal(
            name="ocr_crosscheck",
            value=1.0 - cross,
            threshold=0.5,
            detail=f"提取/OCR 字符 Jaccard 差异 {1.0 - cross:.3f}",
        ))

    # 信号 5：图像面积占比（可选）。
    if blocks_by_page:
        ratios = [layout_image_ratio(b or []) for b in blocks_by_page]
        avg = sum(ratios) / len(ratios) if ratios else 0.0
        signals.append(ScannedSignal(
            name="image_ratio",
            value=avg,
            threshold=IMAGE_RATIO_THRESHOLD,
            detail=f"图像块面积占比 {avg:.3f}",
        ))

    # 信号 1：SSIM 像素相似度（best-effort，BabelDOC 或近似）。
    if pdf_path and os.path.exists(pdf_path):
        ssim = ssim_scanned_signal(pdf_path, max_pages=max_pages)
        if ssim is not None:
            signals.append(ScannedSignal(
                name="pixel_ssim",
                value=ssim,
                threshold=0.95,
                detail="文本层可见性 SSIM（≈BabelDOC DetectScannedFile）",
            ))

    return fused_scan_decision(
        signals, text_quality=text_quality, glyph_signals=glyph_signals,
        note="任一信号命中阈值即触发 OCR（报告 §6.3 多信号融合）",
    )


def recommend_ocr_flags(decision: ScanDecision):
    """把融合判定映射为 BabelDOC 三个互斥扫描版开关。

    Returns:
        ``(ocr_workaround, auto_enable_ocr_workaround, skip_scanned_detection)``。
        融合判定为扫描 → 强制 ``ocr_workaround=True``（相当于临时
        ``--babeldoc-ocr on``）；否则保持 auto 语义。
    """
    if decision.is_scanned:
        return True, False, False
    return False, True, False


__all__ = [
    "TextQualityStats", "GlyphSignals", "ScannedSignal", "ScanDecision",
    "analyze_text_quality", "analyze_glyph_signals", "layout_image_ratio",
    "ocr_crosscheck", "fused_scan_decision", "preflight_scan_check",
    "ssim_scanned_signal", "render_page_image", "recommend_ocr_flags",
    "TEXT_BROKEN_RATIO_THRESHOLD", "PAGE_BROKEN_THRESHOLD",
    "TO_UNICODE_MISSING_THRESHOLD", "IMAGE_RATIO_THRESHOLD",
    "DEFAULT_MAX_PAGES",
]
