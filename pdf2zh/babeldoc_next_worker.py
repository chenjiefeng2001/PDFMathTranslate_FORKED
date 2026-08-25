"""babeldoc-next 内核子进程隔离 worker（性能基准报告 P0 #3 / Bug #4）。

背景：babeldoc 内核在进程内运行时，连续任务的 RSS 稳定增长
（实测 6 次运行 +2GB：ONNX 会话 / il_creater 结构 / BabelDOC 内部缓存
无法确定性释放）。把每次任务放进一个全新子进程执行，进程退出即把
全部原生内存归还 OS，长会话不再泄积。

协议（NDJSON）
--------------
stdin ：单个 JSON 对象 = ``run_babeldoc_next_translation`` 的 kwargs
        （仅 JSON 兼容字段；callables 不可跨进程，取消由父进程杀进程实现）。
stdout：逐行 JSON：
        - 进度帧 ``{"progress": true, "stage": ..., "pct": ..., "msg": ...,
          "detail": {...}}``（每帧立即 flush）；
        - 终帧 ``{"ok": true, "files": [...]}`` 或
          ``{"ok": false, "error": "...", "error_type": "..."}``。
退出码：0 成功 / 1 翻译失败 / 2 内核不可用（上层据此走 legacy BabelDOC 降级）。

父进程入口见
:func:`pdf2zh.babeldoc_next_adapter.run_babeldoc_next_translation_subprocess`。
由 ``PDF2ZH_BABELDOC_SUBPROCESS=1`` 启用。
"""

from __future__ import annotations

import json
import sys

__all__ = ["main"]


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception as exc:  # noqa: BLE001 -- 协议错误直接退出
        _emit({"ok": False, "error": f"bad payload: {exc}", "error_type": "ValueError"})
        return 1
    if not isinstance(payload, dict):
        _emit(
            {
                "ok": False,
                "error": "payload must be an object",
                "error_type": "ValueError",
            }
        )
        return 1

    # 懒导入：内核缺失时走 unavailable 退出码（父进程降级 legacy 适配器）。
    try:
        from pdf2zh.babeldoc_next_adapter import run_babeldoc_next_translation
    except Exception as exc:  # noqa: BLE001
        _emit(
            {
                "ok": False,
                "error": str(exc),
                "error_type": "BabeldocNextUnavailableError",
            }
        )
        return 2

    def _progress_cb(stage: str, pct: float, msg: str, detail=None) -> None:
        frame = {"progress": True, "stage": stage, "pct": pct, "msg": msg}
        if detail is not None:
            frame["detail"] = detail
        try:
            _emit(frame)
        except Exception:  # noqa: BLE001 -- 进度上报永不致命
            pass

    kwargs = dict(payload)
    kwargs["progress_cb"] = _progress_cb
    kwargs["cancelled_check"] = None  # 取消由父进程 terminate 实现

    try:
        files = run_babeldoc_next_translation(**kwargs)
        _emit({"ok": True, "files": list(files or [])})
        return 0
    except Exception as exc:  # noqa: BLE001 -- 错误经 JSON 回传父进程
        from pdf2zh.babeldoc_next_adapter import BabeldocNextUnavailableError

        unavailable = isinstance(exc, BabeldocNextUnavailableError)
        _emit(
            {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "error_type": type(exc).__name__,
            }
        )
        return 2 if unavailable else 1


if __name__ == "__main__":
    raise SystemExit(main())
