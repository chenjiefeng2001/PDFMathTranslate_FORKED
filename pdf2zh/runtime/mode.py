"""RuntimeMode — 双运行模式。

batch: 旧 pipeline（全量串行）
stream: 新 streaming compiler (DAG)

用法：
    mode = RuntimeMode.from_env()
    # 或
    mode = RuntimeMode.BATCH
"""

from __future__ import annotations

import os
from enum import Enum


class RuntimeMode(str, Enum):
    """运行模式。"""

    BATCH = "batch"
    STREAM = "stream"

    @classmethod
    def from_env(cls) -> RuntimeMode:
        """从环境变量读取。"""
        value = os.environ.get("PDF2ZH_RUNTIME_MODE", "batch").lower()
        try:
            return cls(value)
        except ValueError:
            return cls.BATCH

    @property
    def label(self) -> str:
        return "Batch (legacy)" if self == self.BATCH else "Streaming Compiler"
