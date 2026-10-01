"""BackendSelector — backend 选择器。

根据环境变量和 artifact 特性选择 backend。

用法：
    selector = BackendSelector()
    backend = selector.select(artifact)
"""

from __future__ import annotations

import os
from typing import Optional

from pdf2zh.assembler.backend import PDFBackend
from pdf2zh.assembler.mupdf_backend import MuPDFBackend
from pdf2zh.assembler.pikepdf_backend import PikepdfBackend


class BackendSelector:
    """Backend 选择器。

    优先级：
        1. 环境变量 PDF2ZH_BACKEND
        2. artifact 特性
        3. 默认 pikepdf
    """

    def __init__(self, default: str = "pikepdf") -> None:
        self._default = default
        self._backends = {
            "pikepdf": PikepdfBackend,
            "mupdf": MuPDFBackend,
        }

    def select(self, artifact=None) -> PDFBackend:
        """选择 backend。"""
        # 1. 环境变量
        env_backend = os.environ.get("PDF2ZH_BACKEND", "").lower()
        if env_backend in self._backends:
            return self._backends[env_backend]()

        # 2. artifact 特性
        if artifact:
            requires = getattr(artifact, "requires_mupdf", False)
            if requires:
                return MuPDFBackend()

        # 3. 默认
        return self._backends.get(self._default, PikepdfBackend)()

    @property
    def available_backends(self) -> list[str]:
        return list(self._backends.keys())
