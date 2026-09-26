"""PDFBackend — backend 抽象接口。

Runtime 不直接操作 pikepdf/MuPDF，而是通过 BackendInterface。
这样 runtime 不知道 xref、object id 等细节。

用法：
    backend = PikepdfBackend()
    backend.begin(source_pdf)
    for artifact in artifacts:
        backend.append(artifact)
    backend.finalize(output_pdf)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, Optional

import pikepdf


@dataclass
class PDFBackendCapabilities:
    """PDFBackend 能力声明。"""

    supports_incremental: bool = False
    supports_font_reuse: bool = False
    supports_resource_pool: bool = False
    supports_annotation: bool = False
    supports_form_xobject: bool = False
    supports_transparency: bool = False


BackendCapabilities = PDFBackendCapabilities


@dataclass
class BackendMetrics:
    """Backend 指标。"""

    backend_name: str = ""
    pages_assembled: int = 0
    objects_before: int = 0
    objects_after: int = 0
    reused_objects: int = 0
    resource_growth: float = 1.0
    assembly_ms: float = 0.0
    write_ms: float = 0.0
    valid: bool = False

    def summary(self) -> str:
        return (
            f"BackendMetrics({self.backend_name}) | "
            f"pages={self.pages_assembled} | "
            f"objects={self.objects_before}->{self.objects_after} | "
            f"reused={self.reused_objects} | "
            f"growth={self.resource_growth:.2f}x | "
            f"assembly={self.assembly_ms:.0f}ms | "
            f"write={self.write_ms:.0f}ms"
        )


class PDFBackend(ABC):
    """PDF backend 抽象接口。"""

    @property
    @abstractmethod
    def name(self) -> str:
        """Backend 名称。"""

    @property
    @abstractmethod
    def capabilities(self) -> PDFBackendCapabilities:
        """Backend 能力。"""

    @abstractmethod
    def begin(self, source: str | pikepdf.Pdf) -> None:
        """开始组装。"""

    @abstractmethod
    def append(
        self,
        page_index: int,
        content_stream: Optional[bytes] = None,
        resources: Optional[Dict] = None,
    ) -> None:
        """追加/更新页面。"""

    @abstractmethod
    def finalize(self, output: str) -> BackendMetrics:
        """完成写入。"""

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}>"
