"""pdf2zh Services — Unified Multi-End Service Layer.

Provides:
  - RuntimeService: thread-safe facade over RuntimeFacade for Gradio UI / REST API / MCP
  - TranslationRequest / TaskState: strong-typed data models
  - TaskProgressEvent: real-time event streaming from RuntimeKernel EventBus
"""

from __future__ import annotations

from pdf2zh.services.runtime_service import (
    RuntimeService,
    TranslationRequest,
    TaskState,
    TaskProgressEvent,
    TaskStage,
    ServiceConfig,
)

__all__ = [
    "RuntimeService",
    "TranslationRequest",
    "TaskState",
    "TaskProgressEvent",
    "TaskStage",
    "ServiceConfig",
]
