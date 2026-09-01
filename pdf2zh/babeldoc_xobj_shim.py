"""BabelDOC ``xobj_id=None`` → ``-1`` 归一化补丁（7I-7C，upstream workaround）。

背景
----
BabelDOC 0.6.4 的 ``TypesettingUnit.__init__`` 对 **unicode 排版单元**强制断言
``xobj_id is not None``（"Xobj id must be provided when unicode is provided"）。
``xobj_id`` 用于把文本锚定到原 PDF 的 Form XObject 容器；但 **page-level 文本**
（不在任何 Form XObject 内）经 new-parser 解析后字符的 ``xobj_id`` 可能为
``None``，段落 ``xobj_id = chars[0].xobj_id`` 随之成为 ``None`` —— 真实翻译
（任何改变文本的翻译都会生成 unicode 单元）在 typesetting 阶段触发断言，
整个任务失败（Matrix Algebra / Groups and Symmetries 两本历史失败书即此形态：
几乎全部页面 ``xobjects=0``，纯 page-level 文本流）。

BabelDOC 自身已定义 ``-1`` 为「无 XObject」哨兵（``typesetting.py`` 中
passthrough 段落构造即用 ``xobj_id=-1``），因此 ``None`` 归一化为 ``-1``
符合 BabelDOC 自身语义，且不会改变任何真实 ``xobj_id`` 的路径。

方案
----
运行时补丁（monkey-patch，与 :mod:`pdf2zh.babeldoc_formula_protect` 同一模式）：
包装 ``TypesettingUnit.__init__`` —— 仅当 ``unicode`` 且 ``xobj_id is None``
时改写为 ``-1``，其余参数原样透传，随后调用原始 ``__init__``。真实
``xobj_id``（0/正数）完全不动。

**这是 upstream workaround**：一旦上游 BabelDOC 修复（把 None 按 -1 语义
处理）进入依赖版本，应删除本模块并移除 adapter 中的调用。

开关（``PDF2ZH_BABELDOC_XOBJ_SHIM``，默认 ``1`` 开启）：

============  ==============================================================
取值          行为
============  ==============================================================
``1``/``on``  开启归一化：None → -1（推荐）
``0``/``off``  保持 BabelDOC 原生行为（None 触发断言 → 任务失败）
============  ==============================================================
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Optional

logger = logging.getLogger(__name__)

#: 环境变量名。
_ENV_XOBJ_SHIM = "PDF2ZH_BABELDOC_XOBJ_SHIM"

#: 补丁锁 + 原始 ``__init__`` 引用（None = 未打补丁）。
_PATCH_LOCK = threading.Lock()
_ORIGINAL_INIT: Optional[object] = None

#: BabelDOC 自身定义的「无 XObject」哨兵（typesetting.py passthrough 用 -1）。
_NO_XOBJ_SENTINEL = -1


def get_babeldoc_xobj_shim_enabled() -> bool:
    """读取 xobj_id 归一化开关（环境变量，默认开启）。"""
    raw = os.environ.get(_ENV_XOBJ_SHIM, "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _normalize_xobj_id_init(self, *args, **kwargs) -> None:
    """补丁版 ``TypesettingUnit.__init__``：unicode + xobj_id=None → -1。

    只归一化「无 XObject」状态；真实 xobj_id（0/正数）原样保留。随后调用
    原始 ``__init__``，其余行为完全一致。
    """
    if kwargs.get("unicode") is not None and kwargs.get("xobj_id") is None:
        kwargs["xobj_id"] = _NO_XOBJ_SENTINEL
    assert _ORIGINAL_INIT is not None
    _ORIGINAL_INIT(self, *args, **kwargs)


def apply_babeldoc_xobj_shim() -> None:
    """为 ``TypesettingUnit.__init__`` 打上 xobj_id 归一化补丁（幂等）。

    babeldoc 缺失 / 导入失败 / 开关关闭时静默跳过，绝不干扰主流程。
    """
    global _ORIGINAL_INIT
    if not get_babeldoc_xobj_shim_enabled():
        return
    with _PATCH_LOCK:
        if _ORIGINAL_INIT is not None:
            return
        try:
            from babeldoc.format.pdf.document_il.midend.typesetting import (
                TypesettingUnit,
            )
        except Exception:  # noqa: BLE001 -- babeldoc 缺失/导入失败静默跳过
            logger.debug("babeldoc xobj shim: TypesettingUnit unavailable")
            return
        original = getattr(TypesettingUnit, "__init__", None)
        if original is None:
            return
        _ORIGINAL_INIT = original
        setattr(TypesettingUnit, "__init__", _normalize_xobj_id_init)
        logger.info(
            "[babeldoc] xobj shim installed "
            "(TypesettingUnit xobj_id=None → -1 for unicode units)"
        )


__all__ = [
    "apply_babeldoc_xobj_shim",
    "get_babeldoc_xobj_shim_enabled",
    "_normalize_xobj_id_init",
]
