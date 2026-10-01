"""Migration flags — Phase 1.1 迁移模式控制。

PDF2ZH_PAGE_RESULT_MODE 环境变量：
    legacy      worker -> obj_patch (默认)
    dual        worker -> obj_patch + PageResult (compare)
    page_result worker -> PageResult (future)

用法：
    from pdf2zh.migration.flags import get_page_result_mode, PageResultMode

    mode = get_page_result_mode()
    if mode == PageResultMode.DUAL:
        # 两条路径并存
        ...
"""

from __future__ import annotations

import os
from enum import Enum


class PageResultMode(Enum):
    """页面结果输出模式。"""

    LEGACY = "legacy"  # worker -> obj_patch -> merge
    DUAL = "dual"  # worker -> obj_patch + PageResult (compare)
    PAGE_RESULT = "page_result"  # worker -> PageResult (future)


def get_page_result_mode() -> PageResultMode:
    """获取当前迁移模式。

    从 PDF2ZH_PAGE_RESULT_MODE 环境变量读取，默认 legacy。
    """
    mode_str = os.environ.get("PDF2ZH_PAGE_RESULT_MODE", "legacy").lower().strip()
    try:
        return PageResultMode(mode_str)
    except ValueError:
        return PageResultMode.LEGACY
