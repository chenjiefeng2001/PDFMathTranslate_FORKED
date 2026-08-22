"""进程级共享的 RuntimeService 单例。

Phase A（前端解耦）：GUI、REST API 与 Flask backend 必须共享同一个
RuntimeService 实例——任务存储是纯内存 dict，多实例会导致任务状态跨端不可见
（backend.py v2 的历史缺陷即源于每请求新建实例）。
"""

from __future__ import annotations

from typing import Optional

from pdf2zh.services.runtime_service import RuntimeService

_runtime_service: Optional[RuntimeService] = None


def get_runtime_service() -> RuntimeService:
    """Get or create the process-wide RuntimeService singleton."""
    global _runtime_service
    if _runtime_service is None:
        _runtime_service = RuntimeService()
    return _runtime_service


def reset_runtime_service() -> None:
    """Drop the singleton (tests / hot reconfiguration)."""
    global _runtime_service
    _runtime_service = None
