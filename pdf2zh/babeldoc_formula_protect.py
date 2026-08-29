"""BabelDOC 公式检测过度识别缓解补丁（含公式的文本块漏翻修复）。

背景
----
BabelDOC 0.6.x 的公式字符判定（``formular_helper.is_formulas_start_char``）
非常激进：**字体映射缺失**（子集化/嵌入字体，色块叠加字体常触发）、**数字与
方括号**、数学符号/希腊字母、以及**视觉框与占位框不一致**（有色背景/图形
重叠常触发）都会被判为公式字符。结果：有色背景块、嵌入字体块这类「含公式的
文本块」整块被聚合成 ``PdfFormula`` 而被 ``ILTranslator`` 跳过 —— 严重漏翻；
且公式块会把段落 composition 拆散、在排版阶段按公式特殊处理，影响相邻/后续
段落的翻译与重排（用户报告的「漏翻并影响后续」）。

方案
----
运行时补丁（monkey-patch，与 :mod:`pdf2zh.babeldoc_toc_protect` 同一模式）
放宽 ``StylesAndFormulas.is_translatable_formula``：原始判定（纯数字/逗号等）
之后，若公式块文本含**普通文本信号**（CJK 字符，或 ≥2 个空格分隔的完整拉丁
单词），判定为「误判文本」→ 返回 True，由 ``process_translatable_formulas``
把该块转回普通文本行重新参与翻译。真正的数学公式（符号/希腊字母/运算符为主、
无完整单词、无 CJK）仍保持公式不翻译。

开关（``PDF2ZH_BABELDOC_FORMULA_PROTECT``，默认 ``1`` 开启）：

============  ==============================================================
取值          行为
============  ==============================================================
``1``/``on``  开启缓解：含普通文本信号的公式块转回翻译（推荐）
``0``/``off`` 保持 BabelDOC 原生公式检测（真公式块完全不翻译）
============  ==============================================================
"""

from __future__ import annotations

import logging
import os
import re
import threading
from typing import Optional

logger = logging.getLogger(__name__)

#: 环境变量名。
_ENV_FORMULA_PROTECT = "PDF2ZH_BABELDOC_FORMULA_PROTECT"

#: 补丁锁 + 原始 ``is_translatable_formula`` 引用（None = 未打补丁）。
_PATCH_LOCK = threading.Lock()
_ORIGINAL_IS_TRANSLATABLE: Optional[object] = None

#: 普通文本信号：CJK（中文/日文/韩文）。公式几乎不含 CJK，命中即视为文本块。
_CJK_RE = re.compile(r"[\u4E00-\u9FFF\u3040-\u30FF\uAC00-\uD7AF]")

#: 拉丁「完整单词」（≥3 个连续字母）。真公式以单字母/短串为主（如 ``x``/``mc``），
#: 而误判文本通常含空格分隔的完整英文单词（≥2 个）——据此区分。
_WORD_RE = re.compile(r"[A-Za-z]{3,}")


def get_babeldoc_formula_protect_enabled() -> bool:
    """读取公式误判缓解开关（环境变量，默认开启）。"""
    raw = os.environ.get(_ENV_FORMULA_PROTECT, "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _looks_like_misclassified_text(formula) -> bool:
    """公式块文本中是否存在「普通文本」信号（被误判为公式的证据）。

    判定规则（保守，避免把真公式转回翻译）：
    - 含 CJK 字符 → 必是文本（公式不含中文/日文/韩文）；
    - 含 ≥2 个空格分隔的完整拉丁单词 → 更像句子而非公式。
    """
    chars = list(getattr(formula, "pdf_character", None) or [])
    if not chars:
        return False
    text = "".join(
        c.char_unicode for c in chars if getattr(c, "char_unicode", None)
    )
    if not text.strip():
        return False
    if _CJK_RE.search(text):
        return True
    if len(_WORD_RE.findall(text)) >= 2:
        return True
    return False


def _is_translatable_formula(self, formula) -> bool:
    """补丁版判定：原始判定之后，把含普通文本信号的公式块转回翻译。"""
    if _ORIGINAL_IS_TRANSLATABLE is not None:
        try:
            if _ORIGINAL_IS_TRANSLATABLE(self, formula):
                return True
        except Exception:  # noqa: BLE001 -- 原始判定失败按误判处理
            pass
    try:
        # 上下标公式（y_offset > 0.1）不转回：避免把真正的上下标数学式当文本翻译。
        if getattr(formula, "y_offset", 0.0) > 0.1:
            return False
        return _looks_like_misclassified_text(formula)
    except Exception:  # noqa: BLE001 -- 兜底保守：不转回（保持公式）
        return False


def apply_babeldoc_formula_protect() -> None:
    """为 ``StylesAndFormulas.is_translatable_formula`` 打上缓解补丁（幂等）。

    babeldoc 缺失 / 导入失败 / 开关关闭时静默跳过，绝不干扰主流程。
    """
    global _ORIGINAL_IS_TRANSLATABLE
    if not get_babeldoc_formula_protect_enabled():
        return
    with _PATCH_LOCK:
        if _ORIGINAL_IS_TRANSLATABLE is not None:
            return
        try:
            from babeldoc.format.pdf.document_il.midend.styles_and_formulas import (
                StylesAndFormulas,
            )
        except Exception:  # noqa: BLE001 -- babeldoc 缺失/导入失败静默跳过
            logger.debug(
                "babeldoc formula protect: StylesAndFormulas unavailable"
            )
            return
        original = getattr(StylesAndFormulas, "is_translatable_formula", None)
        if original is None:
            return
        _ORIGINAL_IS_TRANSLATABLE = original
        setattr(
            StylesAndFormulas,
            "is_translatable_formula",
            _is_translatable_formula,
        )
        logger.info(
            "[babeldoc] formula protect patch installed "
            "(misclassified-text rescue for colored/embedded-font blocks)"
        )


__all__ = [
    "apply_babeldoc_formula_protect",
    "get_babeldoc_formula_protect_enabled",
    "_looks_like_misclassified_text",
]
