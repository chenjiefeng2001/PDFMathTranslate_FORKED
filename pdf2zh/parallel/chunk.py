"""Chunk 级任务/结果/内存态 manifest（V3 iteration，不落盘）。

设计约束（纯数据契约）：
- ``ChunkTask`` 全部字段为标量 / ``bytes`` / ``dict`` / ``tuple``，
  ``cancel_event`` 仅允许 ``CancelToken`` 或 ``None``（协议强制）。
- ``CancelToken`` 是 pickle-safe 的跨进程取消令牌：mp.Event /
  mp.Value 等同步原语从 Python 3.12 起在 spawn 下不可 pickle
  （``Condition objects should only be shared between processes
  through inheritance``），取消状态改由临时目录标记文件承载。
- ``ChunkResult`` 是 worker → 主进程的唯一回传载体；异常经 ``error_message``
  承载，避免单 chunk 失败打穿进程池。
- ``ChunkManifest`` 是"增量恢复"的内存态：记录每个 chunk 的 pending /
  running / ok / failed 状态与待串行补跑的失败索引，不落盘。
"""

from __future__ import annotations

import os
import tempfile
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from pdf2zh.parallel.errors import ProtocolViolationError

__all__ = ["CancelToken", "ChunkTask", "ChunkResult", "ChunkManifest"]


class CancelToken:
    """跨进程取消令牌（pickle-safe：仅含字符串，状态经临时文件轮询）。

    Python 3.12+ 的 ``multiprocessing.Event`` / ``Value`` / ``Condition``
    在 spawn 下不可 pickle（``Condition objects should only be shared
    between processes through inheritance``），不能作为 ``ChunkTask`` 字段
    跨进程传输。取消信号改为临时目录标记文件：

    - 主进程 ``set()`` → 创建 ``<tempdir>/pdf2zh_cancel_<token>``；
    - worker ``is_set()`` → 轮询文件存在性（每页一次 ``stat``，开销可忽略）；
    - ``clear()`` → 翻译结束/取消后删除标记（尽力而为）。
    """

    __slots__ = ("token", "_path")

    def __init__(self, token: str = "") -> None:
        self.token = token or uuid.uuid4().hex
        self._path = os.path.join(
            tempfile.gettempdir(), f"pdf2zh_cancel_{self.token}"
        )

    @property
    def path(self) -> str:
        return self._path

    def set(self) -> None:
        """标记取消（幂等；失败仅降级日志，绝不阻断主流程）。"""
        try:
            with open(self._path, "w", encoding="utf-8") as fh:
                fh.write("1")
        except OSError:
            pass

    def is_set(self) -> bool:
        return os.path.exists(self._path)

    def clear(self) -> None:
        """清除取消标记（幂等；失败静默）。"""
        try:
            os.unlink(self._path)
        except OSError:
            pass

    def __repr__(self) -> str:  # pragma: no cover -- 调试友好
        return f"<CancelToken token={self.token} set={self.is_set()}>"


@dataclass(frozen=True, slots=True)
class ChunkTask:
    """纯标量任务契约（等价现有 ``_translate_parallel_chunk`` 参数集）。

    ``cancel_event`` 仅允许 ``CancelToken`` 或 ``None``；混入
    ``threading.Event`` / ``multiprocessing.Event`` 等同步原语将立即抛出
    ``ProtocolViolationError``（协议强制，拒绝在 spawn 时崩溃）。
    """

    chunk_pages: Tuple[int, ...] = ()
    fp_bytes: bytes = b""
    page_xref_map: Optional[dict] = None
    cancel_event: Optional[object] = None  # 仅 CancelToken 或 None
    # --- 与现有 _translate_parallel_chunk 一致的标量参数集 ---
    lang_in: str = ""
    lang_out: str = ""
    service: str = ""
    thread: int = 4
    vfont: str = ""
    vchar: str = ""
    noto_name: str = ""
    font_path: str = ""
    skip_subset_fonts: bool = False
    ignore_cache: bool = False
    use_text_metrics: bool = True
    use_translation_cache: bool = True
    envs_str: str = "{}"
    prompt_template: str = ""
    processor_channels: bool = True
    render_takeover: bool = False
    translation_qa: bool = False
    geometry_cluster: bool = False
    toc_split: bool = True
    pipeline_dump: bool = False
    document_model: bool = False
    observability: bool = False
    reconstruction_channel: bool = True
    reconstruction_adopt: bool = True

    def __post_init__(self) -> None:
        if self.cancel_event is not None and not isinstance(
            self.cancel_event, CancelToken
        ):
            raise ProtocolViolationError(
                "cancel_event must be a CancelToken or None "
                f"(got {type(self.cancel_event).__name__}); "
                "mp/threading sync primitives cannot be pickled across "
                "spawn process boundaries"
            )


@dataclass(frozen=True, slots=True)
class ChunkResult:
    """Worker → 主进程的纯数据回传。

    ``error_message`` 非空表示 chunk 级计算失败（``is_fatal`` 预留标记
    需整体降级的异常）；无异常时 ``obj_patch`` / ``obs_bundle`` 承载结果。
    """

    obj_patch: Optional[dict] = None
    obs_bundle: Optional[dict] = None
    elapsed: float = 0.0
    error_message: str = ""
    is_fatal: bool = False

    @property
    def ok(self) -> bool:
        return not self.error_message


class ChunkManifest:
    """增量恢复的内存态（替代落盘 manifest）。

    状态机：pending → running → ok / failed。``failed_indices`` 即
    待串行补跑的 chunk 列表（coordinator 在其内部消费）。
    """

    def __init__(self, total: int) -> None:
        self.total = max(0, total)
        self.chunk_status: Dict[int, str] = {
            i: "pending" for i in range(self.total)
        }
        self.failed_indices: List[int] = []

    def mark_running(self, idx: int) -> None:
        if 0 <= idx < self.total:
            self.chunk_status[idx] = "running"

    def mark_ok(self, idx: int) -> None:
        if 0 <= idx < self.total:
            self.chunk_status[idx] = "ok"

    def mark_failed(self, idx: int) -> None:
        if 0 <= idx < self.total and self.chunk_status[idx] != "failed":
            self.chunk_status[idx] = "failed"
            if idx not in self.failed_indices:
                self.failed_indices.append(idx)

    @property
    def pending_chunks(self) -> List[int]:
        return [i for i, s in self.chunk_status.items() if s == "pending"]

    @property
    def is_complete(self) -> bool:
        return all(s in ("ok", "failed") for s in self.chunk_status.values())

    @property
    def ok_count(self) -> int:
        return sum(1 for s in self.chunk_status.values() if s == "ok")
