"""PDF Resource Management — 资源生命周期管理。

Phase 3.3 核心模块：
    - FontUsageScope: 字体使用范围定义
    - ResourceOwnership: 资源生命周期作用域
    - FontPool: 翻译字体去重池
    - ResourceResolver: 页面资源解析器
    - ResourceUsageTracker: 资源使用跟踪

数据流：
    PageResult
        ↓
    ResourceRequirement
        ↓
    ResourceResolver
        ├── FontPool (共享字体对象)
        └── ResourceNameAllocator (分配 /F1, /F2, ...)
        ↓
    page /Resources dict
"""

from pdf2zh.assembler.resources.font_pool import FontPool
from pdf2zh.assembler.resources.models import (
    FontUsageScope,
    ResourceOwnership,
    ResourceUsageTracker,
)
from pdf2zh.assembler.resources.resolver import ResourceResolver

__all__ = [
    "FontPool",
    "FontUsageScope",
    "ResourceOwnership",
    "ResourceUsageTracker",
    "ResourceResolver",
]
