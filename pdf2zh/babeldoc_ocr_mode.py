"""BabelDOC 扫描版（OCR）PDF 处理模式开关。

背景
----
BabelDOC 布局引擎内置三类扫描版 PDF 处理开关，互斥语义由 pdf2zh_next 内核
``SettingsModel.validate_settings`` 保证（auto_enable 与 ocr_workaround /
skip_scanned_detection 同时开启时会被内核强制覆盖，系统检测结果优先）：

- ``ocr_workaround``：强制 OCR。文档（含无文本层 PDF，如扫描件、纯矢量
  描边件）先经 OCR 识别出文本再翻译排版。最可靠但最慢，需要 OCR 模型。
- ``auto_enable_ocr_workaround``：自动检测。BabelDOC 先检测文档是否
  "高度扫描"，命中才自动启用 OCR 并跳过后续扫描检测（BabelDOC 默认关闭）。
- ``skip_scanned_detection``：跳过扫描检测，不触发任何 OCR。

pdf2zh 此前把 ``auto_enable_ocr_workaround`` 硬编码为 True，用户无法显式
切换。本模块把三者收敛为一个三态开关（``auto`` / ``on`` / ``off``），
并提供与 ``PDF2ZH_BABELDOC_BACKEND`` 一致的环境变量覆盖：

=============  ==============================================================
开关（优先级）  取值
=============  ==============================================================
环境变量        ``PDF2ZH_BABELDOC_OCR`` ∈ ``auto``/``on``/``off``
显式参数        调用方（GUI 开关 / CLI ``--babeldoc-ocr``）传入的 ``ocr_mode``
默认            ``auto`` = 自动检测扫描版 PDF 并启用 OCR workaround
=============  ==============================================================

三态到 BabelDOC 字段的映射（互斥，见 :func:`resolve_ocr_flags`）：

- ``auto`` -> ``ocr_workaround=False``, ``auto_enable_ocr_workaround=True``,
  ``skip_scanned_detection=False``（检测到扫描才 OCR，pdf2zh 默认行为）；
- ``on``   -> ``ocr_workaround=True``,  ``auto_enable_ocr_workaround=False``,
  ``skip_scanned_detection=False``（强制所有 PDF 走 OCR）；
- ``off``  -> ``ocr_workaround=False``, ``auto_enable_ocr_workaround=False``,
  ``skip_scanned_detection=True``（跳过扫描检测，不做 OCR）。
"""

from __future__ import annotations

import logging
import os
from typing import Tuple

logger = logging.getLogger(__name__)

#: 合法的 OCR 模式（含默认 auto）。
VALID_OCR_MODES = ("auto", "on", "off")

#: ``PDF2ZH_BABELDOC_OCR`` 环境变量名。
_ENV_OCR_MODE = "PDF2ZH_BABELDOC_OCR"

#: 信任预检开关：预检判定「健康文本层」时跳过 BabelDOC 内部 SSIM 二次扫描
#: 检测（省掉 ~20% 页面的双次栅格化 + SSIM，大文档提速；见
#: doc/babeldoc_large_doc_slow_progress_report.md §2.5）。设 ``0`` 恢复
#: 「预检 + BabelDOC 检测双重执行」的旧行为。
_ENV_TRUST_PREFLIGHT = "PDF2ZH_BABELDOC_TRUST_PREFLIGHT"

#: 判定「该页有文本层」的最小可抽取字符数（低于此值视为无文本/扫描页）。
_MIN_TEXT_CHARS_PER_PAGE = 32

#: OCR 模式 -> ``(ocr_workaround, auto_enable_ocr_workaround,
#: skip_scanned_detection)``。
#:
#: 三种模式刻意互斥：pdf2zh_next 内核的 ``validate_settings`` 在 auto_enable
#: 与 ocr_workaround / skip_scanned_detection 同时开启时会强制覆盖，因此这里
#: 保证任一模式最多只有其中一个字段为 True。
_OCR_FLAGS: dict = {
    "auto": (False, True, False),
    "on": (True, False, False),
    "off": (False, False, True),
}


def normalize_ocr_mode(ocr_mode: Optional[str] = None) -> str:
    """Normalise a user-supplied OCR mode to one of ``auto``/``on``/``off``.

    Invalid values fall back to ``auto`` with a warning so a bad GUI/CLI value
    never hard-fails the translation task.
    """
    raw = (ocr_mode or "auto").strip().lower() or "auto"
    if raw not in VALID_OCR_MODES:
        logger.warning(
            "Ignoring invalid BabelDOC OCR mode %r (expected one of %s); "
            "falling back to 'auto'",
            raw,
            list(VALID_OCR_MODES),
        )
        return "auto"
    return raw


def get_babeldoc_ocr_mode(ocr_mode: Optional[str] = None) -> str:
    """Resolve the effective BabelDOC OCR mode.

    Precedence: ``PDF2ZH_BABELDOC_OCR`` env var > explicit argument > ``auto``.
    The env var lets CLI/CI and non-GUI callers control BabelDOC's OCR handling
    without touching the GUI switch.
    """
    override = os.environ.get(_ENV_OCR_MODE, "").strip().lower()
    if override:
        if override not in VALID_OCR_MODES:
            logger.warning(
                "Ignoring invalid %s=%r (expected one of %s); "
                "falling back to the explicit/default OCR mode",
                _ENV_OCR_MODE,
                override,
                list(VALID_OCR_MODES),
            )
        else:
            return override
    return normalize_ocr_mode(ocr_mode)


def resolve_ocr_flags(
    ocr_mode: Optional[str] = None,
    source_path: Optional[str] = None,
) -> Tuple[bool, bool, bool]:
    """Map an OCR mode onto BabelDOC's three scanned-PDF switches.

    在 ``auto`` 模式下，若 ``source_path`` 指向 PDF，会先运行
    :func:`pdf2zh.scanned_detection.preflight_scan_check` 多信号融合预检
    （scan_damaged_text 报告 §6.3 长期实现）：

    - 任一损坏信号命中阈值 → 强制 ``ocr_workaround=True``（相当于临时
      ``--babeldoc-ocr on``），并把 ``auto_enable_ocr_workaround`` 关闭——
      从根上解决「渲染可见但语义损坏」的文本层骗过 BabelDOC SSIM 判定、
      乱码被直接翻译的问题；
    - 预检判定健康文本层且**每一页**都有可抽取文本（:func:
      `_all_pages_have_text_layer`，防混合扫描文档漏检）→ 直接跳过 BabelDOC
      内部 SSIM 扫描检测（``skip_scanned_detection=True``），省掉大文档在
      检测上的双次栅格化开销；设 ``PDF2ZH_BABELDOC_TRUST_PREFLIGHT=0``
      可恢复双重检测的旧行为；
    - 预检失败/文件不可读时保持原 auto 语义（不阻断主链路）。

    Returns:
        ``(ocr_workaround, auto_enable_ocr_workaround, skip_scanned_detection)``
        as a mutually-exclusive triple the two BabelDOC adapters (legacy
        ``TranslationConfig`` and pdf2zh_next ``PDFSettings``) can apply
        directly.
    """
    mode = get_babeldoc_ocr_mode(ocr_mode)
    if mode == "auto" and source_path:
        flags = _preflight_forced_flags(source_path)
        if flags is not None:
            return flags
    return _OCR_FLAGS[mode]


def _all_pages_have_text_layer(pdf_path: str) -> bool:
    """逐页快速检查是否存在可抽取文本层（pymupdf 纯文本提取，毫秒级/页）。

    是「信任预检、跳过 BabelDOC 二次扫描检测」的安全前提：任何一页几乎无
    文本即视为混合扫描文档，不跳过（交回 BabelDOC 检测/OCR 兜底）。
    任何异常都返回 False（保守：不跳过）。
    """
    try:
        import pymupdf  # noqa: PLC0415

        with pymupdf.open(pdf_path) as doc:
            for page in doc:
                if len(page.get_text().strip()) < _MIN_TEXT_CHARS_PER_PAGE:
                    return False
        return True
    except Exception:  # noqa: BLE001 -- 检查失败时不跳过（保守）
        return False


def _preflight_forced_flags(source_path: str):
    """运行多信号融合预检并映射为互斥三元组。

    Returns:
        ``(True, False, False)``：预检命中扫描/损坏信号 → 强制 OCR；
        ``(False, False, True)``：预检判定健康文本层且每页均有文本层 →
            跳过 BabelDOC 内部 SSIM 二次检测（提速优化，可经
            ``PDF2ZH_BABELDOC_TRUST_PREFLIGHT=0`` 关闭）；
        ``None``：预检不可用/失败/未通过健康前提 → 保持调用方原 auto 语义。
    """
    if not source_path or not source_path.lower().endswith(".pdf"):
        return None
    try:
        from pdf2zh.scanned_detection import preflight_scan_check

        decision = preflight_scan_check(source_path)
        if decision.is_scanned:
            logger.warning(
                "文本层质量预检命中扫描/损坏信号，已自动启用 OCR workaround"
                "（multi-signal fusion: %s）",
                "; ".join(decision.reasons) or "unknown",
            )
            return True, False, False
        if os.environ.get(
            _ENV_TRUST_PREFLIGHT, ""
        ).strip().lower() != "0" and _all_pages_have_text_layer(source_path):
            logger.info(
                "文本层质量预检通过（healthy text layer）；跳过 BabelDOC 内部"
                "扫描二次检测以加速大文档（%s=0 可关闭此优化）",
                _ENV_TRUST_PREFLIGHT,
            )
            return False, False, True
    except Exception as exc:  # noqa: BLE001 -- 预检失败绝不阻断翻译
        logger.debug("preflight scan check skipped: %s", exc)
    return None
