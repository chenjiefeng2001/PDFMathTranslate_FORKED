"""BabelDOC 扫描版（OCR）PDF 处理模式开关。

⚠️ 先读这一段：``ocr_workaround`` **不是 OCR**
------------------------------------------------
pdf2zh 早期把 BabelDOC 的 ``ocr_workaround`` 当成「强制 OCR」开关，文档与
UI 文案都据此写成「先经 OCR 识别出文本再翻译排版」。这是**错的**。对照
babeldoc 0.6.4 源码：

- ``format/pdf/translation_config.py:196`` 的字段说明是 *Force translated text
  to be black and add white background*；
- 唯一实际作用在 ``format/pdf/document_il/frontend/il_creater.py:1084-1086``：
  ``pdf_style.graphic_state = BLACK`` + ``render_order = None``；
- 原先的表格文字检测器 ``docvision/table_detection/rapidocr.py:9-10`` 已退化为
  no-op（类文档串自述 *"Compatibility no-op for the retired RapidOCR table text
  detector."*）；
- 整个 babeldoc 包内 ``tesseract`` / ``ocrmypdf`` / ``paddle`` 零命中。

即 ``ocr_workaround`` 的真实语义是**强制译文字为黑色并加白底**（应对原文是
白色文字 / 渲染顺序错乱导致译文不可见）。它只对**已经带有文本层**的 PDF 有
帮助 —— 尤其是「不可见 OCR 文本层」那类（``3 Tr`` 绘制、页面看着是扫描图但
能被抽取到文字）。

**纯图片 PDF（连一个字符都抽不出来）走 BabelDOC 链路产出为零** —— 无论三态
设成什么。本模块不再假装能 OCR，而是在这种场景下打警告并给出可操作的出路
（见 :func:`warn_if_babeldoc_ocr_is_a_noop`）。pdf2zh 里唯一真正做 OCR 的链路
是 ``--parse-engine magicpdf``（MinerU ``PytorchPaddleOCR``）。

三个开关
--------
BabelDOC 内置的三个扫描版开关，语义互斥由 pdf2zh_next 内核
``SettingsModel.validate_settings`` 保证（auto_enable 与 ocr_workaround /
skip_scanned_detection 同时开启时会被内核强制覆盖，系统检测结果优先）：

- ``ocr_workaround``：见上，强制黑字白底（**不 OCR**）；
- ``auto_enable_ocr_workaround``：自动检测。BabelDOC 先检测文档是否
  "高度扫描"，命中才自动启用 ``ocr_workaround`` 并跳过后续扫描检测
  （BabelDOC 默认关闭）；
- ``skip_scanned_detection``：跳过扫描检测，不触发 ``ocr_workaround``。

=============  ==============================================================
开关（优先级）  取值
=============  ==============================================================
环境变量        ``PDF2ZH_BABELDOC_OCR`` ∈ ``auto``/``on``/``off``
显式参数        调用方（GUI 开关 / CLI ``--babeldoc-ocr``）传入的 ``ocr_mode``
默认            ``auto`` = 自动检测扫描版 PDF 并启用 workaround
=============  ==============================================================

三态到 BabelDOC 字段的映射（互斥，见 :func:`resolve_ocr_flags`）:

- ``auto`` -> ``ocr_workaround=False``, ``auto_enable_ocr_workaround=True``,
  ``skip_scanned_detection=False``（检测到扫描才启用 workaround，pdf2zh 默认行为）；
- ``on``   -> ``ocr_workaround=True``,  ``auto_enable_ocr_workaround=False``,
  ``skip_scanned_detection=False``（强制所有 PDF 走黑字白底）；
- ``off``  -> ``ocr_workaround=False``, ``auto_enable_ocr_workaround=False``,
  ``skip_scanned_detection=True``（跳过扫描检测）。
"""

from __future__ import annotations

import logging
import os
from typing import Optional, Tuple

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
    raw = str(ocr_mode or "auto").strip().lower() or "auto"
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

    注意：本函数产出的三个开关**都不执行 OCR**（见模块 docstring），
    ``ocr_workaround`` 的真实语义是「强制译文字黑 + 白底」。

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

    请求 OCR 语义但 PDF 无文本层时，经
    :func:`warn_if_babeldoc_ocr_is_a_noop` 告警并指向 ``--parse-engine
    magicpdf``，避免「任务成功但一个字符没翻」。

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
    warn_if_babeldoc_ocr_is_a_noop(source_path, mode)
    return _OCR_FLAGS[mode]


def document_has_text_layer(pdf_path: str) -> bool:
    """文档是否存在**任何**可抽取文本层（任一页达标即算有）。

    这是 :func:`warn_if_babeldoc_ocr_is_a_noop` 的判据：``ocr_workaround``
    只能作用于已有文本层的字符，纯图片 PDF 走 BabelDOC 链路产出为零。
    与 :func:`_all_pages_have_text_layer`（要求**每一页**都有文本）不同，
    这里只关心「整个文档是不是一个字符都抽不出来」。
    """
    try:
        import pymupdf  # noqa: PLC0415

        with pymupdf.open(pdf_path) as doc:
            for page in doc:
                if len(page.get_text().strip()) >= _MIN_TEXT_CHARS_PER_PAGE:
                    return True
        return False
    except Exception:  # noqa: BLE001 -- 读不了就当作「有文本层」，不打扰用户
        return True


def warn_if_babeldoc_ocr_is_a_noop(
    source_path: Optional[str],
    mode: Optional[str],
) -> bool:
    """当请求 OCR 语义、但 PDF 连文本层都没有时，打印可操作的警告。

    ``ocr_workaround`` 不做 OCR（见模块 docstring），因此对纯图片 PDF 而言
    ``--babeldoc-ocr on`` 是彻底的空操作：没有文本层就没有可翻译的字符，
    产物会是一份「排版正常但一个字没翻」的 PDF，而任务状态却是成功。

    这里不抛异常 —— 抛异常会把「pymupdf 抽不到但 BabelDOC 可能抽得到」的
    边缘情况变成硬失败。改为 ``logger.error`` + 明确指出唯一能做 OCR 的
    链路（``--parse-engine magicpdf`` / MinerU），把静默失败变成可见失败。

    Returns:
        ``True`` 表示确认是空操作（已告警），``False`` 表示无需告警。
    """
    if not source_path or not str(source_path).lower().endswith(".pdf"):
        return False
    resolved = get_babeldoc_ocr_mode(mode)
    if resolved == "off":
        return False
    if document_has_text_layer(source_path):
        return False
    logger.error(
        "[babeldoc] %s 没有可抽取的文本层，--babeldoc-ocr=%s 对它无效："
        "BabelDOC 的 ocr_workaround 只做「强制黑字白底」，不执行 OCR，"
        "因此不会产出任何译文。请改用真正带 OCR 的链路："
        "--parse-engine magicpdf（--magicpdf-ocr on），"
        "或 --ingest-backend marker / jina。",
        source_path,
        resolved,
    )
    return True


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
        ``(True, False, False)``：预检命中扫描/损坏信号 → 强制 ``ocr_workaround``
        （黑字白底；对无文本层的纯扫描件仍是空操作，故额外告警）；
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
                "文本层质量预检命中扫描/损坏信号，已自动启用 ocr_workaround"
                "（注意：该 workaround 只做黑字白底，不执行 OCR；"
                "multi-signal fusion: %s）",
                "; ".join(decision.reasons) or "unknown",
            )
            # 纯扫描件没有文本层可作用 → 明确告知出路，而不是静默产出空译文。
            warn_if_babeldoc_ocr_is_a_noop(source_path, "on")
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
