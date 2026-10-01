"""FailureReplay — 失败重放系统。

保存失败上下文，支持重现和调试。

用法：
    replayer = FailureReplayer()

    # 保存失败
    replayer.save_failure(
        pdf_path="input.pdf",
        page_index=328,
        failure_type="overflow",
        context={...},
    )

    # 重放
    snapshot = replayer.load_failure("failure_328.json")
    replayer.replay(snapshot)
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class FailureSnapshot:
    """失败快照。"""

    # 元数据
    failure_id: str = ""
    timestamp: float = 0.0
    pdf_path: str = ""
    pdf_hash: str = ""

    # 位置
    page_index: int = -1
    block_index: int = -1

    # 失败信息
    failure_type: str = ""
    error_message: str = ""
    severity: str = "medium"

    # 上下文
    source_text: str = ""
    translated_text: str = ""
    layout_result: Dict[str, Any] = field(default_factory=dict)
    translation_config: Dict[str, Any] = field(default_factory=dict)

    # 用于重现
    snapshot_hash: str = ""

    def __post_init__(self):
        if not self.failure_id:
            self.failure_id = f"failure_{self.page_index}_{int(time.time())}"
        if not self.snapshot_hash:
            self.snapshot_hash = self._compute_hash()

    def _compute_hash(self) -> str:
        """计算快照哈希。"""
        data = f"{self.pdf_path}:{self.page_index}:{self.source_text}"
        return hashlib.md5(data.encode()).hexdigest()[:12]


class FailureReplayer:
    """失败重放系统。"""

    def __init__(self, storage_dir: str = "failures") -> None:
        self._storage_dir = Path(storage_dir)
        self._storage_dir.mkdir(parents=True, exist_ok=True)
        self._index: Dict[str, str] = {}
        self._load_index()

    def _load_index(self) -> None:
        """加载索引。"""
        index_file = self._storage_dir / "_index.json"
        if index_file.exists():
            try:
                with open(index_file, "r") as f:
                    self._index = json.load(f)
            except Exception:
                pass

    def _save_index(self) -> None:
        """保存索引。"""
        index_file = self._storage_dir / "_index.json"
        with open(index_file, "w") as f:
            json.dump(self._index, f, indent=2)

    def save_failure(
        self,
        pdf_path: str,
        page_index: int,
        failure_type: str,
        error_message: str = "",
        source_text: str = "",
        translated_text: str = "",
        layout_result: Optional[Dict] = None,
        translation_config: Optional[Dict] = None,
        block_index: int = -1,
    ) -> str:
        """保存失败快照。"""
        # 计算 PDF 哈希
        try:
            with open(pdf_path, "rb") as f:
                pdf_hash = hashlib.md5(f.read()).hexdigest()[:12]
        except Exception:
            pdf_hash = "unknown"

        snapshot = FailureSnapshot(
            pdf_path=pdf_path,
            pdf_hash=pdf_hash,
            page_index=page_index,
            block_index=block_index,
            failure_type=failure_type,
            error_message=error_message,
            source_text=source_text,
            translated_text=translated_text,
            layout_result=layout_result or {},
            translation_config=translation_config or {},
        )

        # 保存
        file_path = self._storage_dir / f"{snapshot.failure_id}.json"
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "failure_id": snapshot.failure_id,
                    "timestamp": snapshot.timestamp or time.time(),
                    "pdf_path": snapshot.pdf_path,
                    "pdf_hash": snapshot.pdf_hash,
                    "page_index": snapshot.page_index,
                    "block_index": snapshot.block_index,
                    "failure_type": snapshot.failure_type,
                    "error_message": snapshot.error_message,
                    "severity": snapshot.severity,
                    "source_text": snapshot.source_text,
                    "translated_text": snapshot.translated_text,
                    "layout_result": snapshot.layout_result,
                    "translation_config": snapshot.translation_config,
                    "snapshot_hash": snapshot.snapshot_hash,
                },
                f,
                indent=2,
                ensure_ascii=False,
            )

        # 更新索引
        self._index[snapshot.failure_id] = str(file_path)
        self._save_index()

        return snapshot.failure_id

    def load_failure(self, failure_id: str) -> Optional[FailureSnapshot]:
        """加载失败快照。"""
        file_path = self._index.get(failure_id)
        if not file_path:
            return None

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return FailureSnapshot(**data)
        except Exception:
            return None

    def list_failures(
        self,
        failure_type: Optional[str] = None,
        pdf_path: Optional[str] = None,
    ) -> List[str]:
        """列出失败。"""
        failures = []
        for failure_id in self._index:
            snapshot = self.load_failure(failure_id)
            if snapshot:
                if failure_type and snapshot.failure_type != failure_type:
                    continue
                if pdf_path and snapshot.pdf_path != pdf_path:
                    continue
                failures.append(failure_id)
        return failures

    def get_statistics(self) -> Dict[str, Any]:
        """获取统计。"""
        type_counts: Dict[str, int] = {}
        for failure_id in self._index:
            snapshot = self.load_failure(failure_id)
            if snapshot:
                ft = snapshot.failure_type
                type_counts[ft] = type_counts.get(ft, 0) + 1

        return {
            "total": len(self._index),
            "by_type": type_counts,
        }

    def delete_failure(self, failure_id: str) -> bool:
        """删除失败快照。"""
        file_path = self._index.get(failure_id)
        if file_path and Path(file_path).exists():
            Path(file_path).unlink()
            del self._index[failure_id]
            self._save_index()
            return True
        return False

    def clear(self) -> int:
        """清空所有失败。"""
        count = len(self._index)
        for failure_id in list(self._index.keys()):
            self.delete_failure(failure_id)
        return count
