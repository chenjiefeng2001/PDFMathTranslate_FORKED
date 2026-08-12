"""P10 — Dual PDF Patch Verification Layer（规范书 §7 / §9.2）。

    dual_patcher.py   # PDF 增量更新与渲染（双层补丁合成 + QA 校验）
"""
from __future__ import annotations

from pdf2zh.patch.dual_patcher import (
    DualPatcher,
    DualPatch,
    QAReport,
)

__all__ = ["DualPatcher", "DualPatch", "QAReport"]
