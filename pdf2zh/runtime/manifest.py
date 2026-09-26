"""TranslationManifest — 翻译清单。

记录翻译状态，支持增量更新和恢复。

用法：
    manifest = TranslationManifest.from_pdf("input.pdf")
    manifest.mark_done(page_index=0, snapshot_hash="abc")
    manifest.save("manifest.json")

    # Resume
    manifest = TranslationManifest.load("manifest.json")
    pending = manifest.get_pending_pages()
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class PageState:
    """单页状态。"""

    page_index: int = 0
    snapshot_hash: str = ""
    layout_hash: str = ""
    translation_hash: str = ""
    status: str = "pending"  # pending, done, failed, changed
    error: Optional[str] = None
    timestamp: float = 0.0

    @property
    def is_done(self) -> bool:
        return self.status == "done"

    @property
    def needs_translation(self) -> bool:
        return self.status in ("pending", "changed", "failed")


@dataclass
class TranslationManifest:
    """翻译清单。"""

    pdf_path: str = ""
    pdf_hash: str = ""
    total_pages: int = 0
    pages: List[PageState] = field(default_factory=list)
    glossary_version: int = 0
    policy_version: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0

    @classmethod
    def from_pdf(cls, pdf_path: str) -> TranslationManifest:
        """从 PDF 创建清单。"""
        import os
        import time

        path = Path(pdf_path)
        stat = path.stat()
        pdf_hash = hashlib.md5(
            f"{path.name}:{stat.st_size}:{stat.st_mtime}".encode()
        ).hexdigest()

        # 估算页数（简单方法）
        try:
            import pikepdf

            pdf = pikepdf.open(pdf_path)
            total_pages = len(pdf.pages)
            pdf.close()
        except Exception:
            total_pages = 0

        return cls(
            pdf_path=str(path.absolute()),
            pdf_hash=pdf_hash,
            total_pages=total_pages,
            pages=[PageState(page_index=i) for i in range(total_pages)],
            created_at=time.time(),
            updated_at=time.time(),
        )

    @classmethod
    def load(cls, path: str) -> TranslationManifest:
        """从文件加载。"""
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return cls._from_dict(data)

    def save(self, path: str) -> None:
        """保存到文件。"""
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self._to_dict(), f, ensure_ascii=False, indent=2)

    def get_page(self, page_index: int) -> Optional[PageState]:
        """获取页面状态。"""
        if 0 <= page_index < len(self.pages):
            return self.pages[page_index]
        return None

    def mark_done(
        self,
        page_index: int,
        snapshot_hash: str = "",
        layout_hash: str = "",
        translation_hash: str = "",
    ) -> None:
        """标记页面完成。"""
        import time

        page = self.get_page(page_index)
        if page:
            page.status = "done"
            page.snapshot_hash = snapshot_hash
            page.layout_hash = layout_hash
            page.translation_hash = translation_hash
            page.error = None
            page.timestamp = time.time()
            self.updated_at = time.time()

    def mark_failed(self, page_index: int, error: str) -> None:
        """标记页面失败。"""
        import time

        page = self.get_page(page_index)
        if page:
            page.status = "failed"
            page.error = error
            page.timestamp = time.time()
            self.updated_at = time.time()

    def mark_changed(self, page_index: int) -> None:
        """标记页面需要重新翻译。"""
        page = self.get_page(page_index)
        if page:
            page.status = "changed"
            page.translation_hash = ""
            self.updated_at = time.time()

    def get_pending_pages(self) -> List[int]:
        """获取待处理页面。"""
        return [p.page_index for p in self.pages if p.needs_translation]

    def get_done_pages(self) -> List[int]:
        """获取已完成页面。"""
        return [p.page_index for p in self.pages if p.is_done]

    def get_failed_pages(self) -> List[int]:
        """获取失败页面。"""
        return [p.page_index for p in self.pages if p.status == "failed"]

    @property
    def progress(self) -> float:
        """完成进度。"""
        if not self.pages:
            return 0.0
        done = sum(1 for p in self.pages if p.is_done)
        return done / len(self.pages)

    @property
    def is_complete(self) -> bool:
        """是否全部完成。"""
        return all(p.is_done for p in self.pages)

    def invalidate_glossary(self) -> None:
        """使术语表失效。"""
        import time

        self.glossary_version += 1
        # 所有已翻译页面标记为 changed
        for page in self.pages:
            if page.is_done:
                page.status = "changed"
                page.translation_hash = ""
        self.updated_at = time.time()

    def summary(self) -> str:
        done = sum(1 for p in self.pages if p.is_done)
        failed = sum(1 for p in self.pages if p.status == "failed")
        pending = sum(1 for p in self.pages if p.status == "pending")
        changed = sum(1 for p in self.pages if p.status == "changed")
        return (
            f"Manifest({self.total_pages} pages) | "
            f"done={done} failed={failed} pending={pending} changed={changed} | "
            f"glossary_v{self.glossary_version} policy_v{self.policy_version}"
        )

    def _to_dict(self) -> Dict[str, Any]:
        return {
            "pdf_path": self.pdf_path,
            "pdf_hash": self.pdf_hash,
            "total_pages": self.total_pages,
            "pages": [
                {
                    "page_index": p.page_index,
                    "snapshot_hash": p.snapshot_hash,
                    "layout_hash": p.layout_hash,
                    "translation_hash": p.translation_hash,
                    "status": p.status,
                    "error": p.error,
                    "timestamp": p.timestamp,
                }
                for p in self.pages
            ],
            "glossary_version": self.glossary_version,
            "policy_version": self.policy_version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def _from_dict(cls, data: Dict[str, Any]) -> TranslationManifest:
        pages = [PageState(**p) for p in data.get("pages", [])]
        return cls(
            pdf_path=data.get("pdf_path", ""),
            pdf_hash=data.get("pdf_hash", ""),
            total_pages=data.get("total_pages", 0),
            pages=pages,
            glossary_version=data.get("glossary_version", 0),
            policy_version=data.get("policy_version", 0),
            created_at=data.get("created_at", 0),
            updated_at=data.get("updated_at", 0),
        )
