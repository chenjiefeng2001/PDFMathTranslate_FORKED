"""Phase A 解耦层：RuntimeService 的标准 HTTP API（REST + SSE）。

为 SPA / 第三方客户端暴露与 Gradio 相同的能力，事件协议直接复用
TaskProgressEvent / RuntimeNoticeEvent（客户端无关，见 gui/events.py）。

启动方式：
    pdf2zh --api [--port 11009]
    python -m pdf2zh.services.api --port 11009

端点一览：
    GET  /api/health                        健康检查
    GET  /api/engines                       引擎列表（envs 只回显是否已配置，不回显值）
    POST /api/tasks                         提交任务（multipart 上传或 JSON source_path）
    GET  /api/tasks                         任务列表
    GET  /api/tasks/{task_id}               任务状态
    DELETE /api/tasks/{task_id}             取消任务
    POST /api/tasks/{task_id}/pause|resume|skip
    GET  /api/tasks/{task_id}/events        SSE 进度流（含初始 state 帧 + 终态 done 帧）
    GET  /api/tasks/{task_id}/artifacts     结果文件清单
    GET  /api/tasks/{task_id}/artifacts/{index}  下载结果文件
"""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse

from pdf2zh.services.runtime_service import RuntimeService, TranslationRequest
from pdf2zh.services.runtime_singleton import get_runtime_service

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = {"completed", "cancelled", "failed"}
_SSE_KEEPALIVE_SECONDS = 15.0


def _sse_frame(event: str, data: Any) -> str:
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


def _event_payload(evt: Any) -> Dict[str, Any]:
    payload = evt.to_dict()
    payload["type"] = type(evt).__name__
    return payload


def _mount_spa(app: FastAPI) -> None:
    """托管 SPA 构建产物（Phase B 双轨灰度）。

    设置 PDF2ZH_SPA_DIR=<frontend/dist> 后，根路径提供 SPA；/api 路由
    优先级更高。未设置或目录不存在时保持纯 API 形态。
    """
    import os

    spa_dir = os.environ.get("PDF2ZH_SPA_DIR", "")
    if not spa_dir or not Path(spa_dir).is_dir():
        return
    try:
        from fastapi.staticfiles import StaticFiles

        app.mount("/", StaticFiles(directory=spa_dir, html=True), name="spa")
        logger.info("SPA static hosting enabled: %s", spa_dir)
    except Exception as exc:  # noqa: BLE001 -- 托管失败不影响 API 可用性
        logger.warning("SPA mount failed (%s): %s", spa_dir, exc)


def create_api_app(
    service: Optional[RuntimeService] = None,
    allow_origins: Optional[List[str]] = None,
) -> FastAPI:
    """Build the FastAPI application bound to a (shared) RuntimeService."""
    svc = service or get_runtime_service()
    app = FastAPI(title="pdf2zh API", version="1.0.0")
    if allow_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=allow_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # ── health ────────────────────────────────────────────────────────────
    @app.get("/api/health")
    def health() -> Dict[str, Any]:
        return {
            "status": "ok",
            "tasks": len(svc.list_task_ids()),
        }

    # ── engines ───────────────────────────────────────────────────────────
    @app.get("/api/engines")
    def engines() -> List[Dict[str, Any]]:
        from pdf2zh.config import ConfigManager
        from pdf2zh.translator import build_translator_registry

        out = []
        for cls in build_translator_registry():
            envs = []
            stored = ConfigManager.get_translator_by_name(cls.name) or {}
            for key in cls.envs:
                value = stored.get(key, cls.envs.get(key))
                envs.append({"key": key, "configured": bool(value)})
            out.append({"name": cls.name, "label": cls.__name__, "envs": envs})
        return out

    # ── submit ────────────────────────────────────────────────────────────
    def _submit(request: TranslationRequest) -> Dict[str, str]:
        task_id = svc.submit_task(request)
        return {"task_id": task_id}

    @app.post("/api/tasks")
    async def submit_task(
        file: Optional[UploadFile] = File(default=None),
        source_path: str = Form(default=""),
        target_lang: str = Form(default="zh-CN"),
        source_lang: str = Form(default="auto"),
        engine: str = Form(default="google"),
        threads: int = Form(default=4),
        page_range: str = Form(default=""),
        parse_engine: str = Form(default="auto"),
        mode_choice: str = Form(default="auto"),
        ocr_mode: str = Form(default="auto"),
        ignore_cache: bool = Form(default=False),
        extra_config: str = Form(default=""),
    ) -> Dict[str, str]:
        resolved_path = source_path.strip()
        if file is not None and file.filename:
            upload_dir = Path(tempfile.gettempdir()) / "pdf2zh_api_uploads"
            upload_dir.mkdir(parents=True, exist_ok=True)
            safe_name = f"{uuid.uuid4().hex[:8]}_{Path(file.filename).name}"
            dest = upload_dir / safe_name
            with dest.open("wb") as fh:
                while chunk := await file.read(1024 * 1024):
                    fh.write(chunk)
            resolved_path = str(dest)

        extra: Dict[str, Any] = {}
        if extra_config.strip():
            try:
                extra = json.loads(extra_config)
            except json.JSONDecodeError as exc:
                raise HTTPException(400, f"extra_config is not valid JSON: {exc}")
        if mode_choice and mode_choice != "auto":
            extra.setdefault("mode_choice", mode_choice)
        if ocr_mode and ocr_mode != "auto":
            extra.setdefault("ocr_mode", ocr_mode)

        request = TranslationRequest(
            source_path=resolved_path,
            target_lang=target_lang,
            source_lang=source_lang,
            engine=engine,
            threads=max(1, min(int(threads), 32)),
            page_range=page_range or None,
            parse_engine=parse_engine,
            ignore_cache=bool(ignore_cache),
            extra_config=extra,
        )
        return _submit(request)

    # ── query / control ───────────────────────────────────────────────────
    def _require_state(task_id: str):
        state = svc.get_task_state(task_id)
        if state is None:
            raise HTTPException(404, f"Unknown task: {task_id}")
        return state

    @app.get("/api/tasks")
    def list_tasks() -> List[Dict[str, Any]]:
        states = []
        for tid in svc.list_task_ids():
            state = svc.get_task_state(tid)
            if state is not None:
                states.append(state.to_dict())
        return states

    @app.get("/api/tasks/{task_id}")
    def get_task(task_id: str) -> Dict[str, Any]:
        return _require_state(task_id).to_dict()

    @app.delete("/api/tasks/{task_id}")
    def cancel_task(task_id: str) -> Dict[str, Any]:
        _require_state(task_id)
        return {"cancelled": svc.cancel_task(task_id)}

    def _control(task_id: str, action: str) -> Dict[str, Any]:
        _require_state(task_id)
        method = getattr(svc, action, None)
        if method is None:
            raise HTTPException(400, f"Unsupported action: {action}")
        result = method(task_id)
        return {action: bool(result) if not isinstance(result, dict) else result}

    @app.post("/api/tasks/{task_id}/pause")
    def pause_task(task_id: str) -> Dict[str, Any]:
        return _control(task_id, "pause_task")

    @app.post("/api/tasks/{task_id}/resume")
    def resume_task(task_id: str) -> Dict[str, Any]:
        return _control(task_id, "resume_task")

    @app.post("/api/tasks/{task_id}/skip")
    def skip_task(task_id: str) -> Dict[str, Any]:
        return _control(task_id, "skip_task")

    # ── SSE ───────────────────────────────────────────────────────────────
    @app.get("/api/tasks/{task_id}/events")
    async def stream_events(
        task_id: str,
        request: Request,
        since: int = 0,
    ) -> StreamingResponse:
        _require_state(task_id)
        # 断线续传游标：优先取浏览器 EventSource 自动回传的 Last-Event-ID，
        # 其次 ?since= 查询参数（非浏览器客户端）。
        last_event_id = 0
        header = request.headers.get("last-event-id") or ""
        try:
            last_event_id = int(header.strip())
        except (TypeError, ValueError):
            last_event_id = 0
        if last_event_id <= 0 and since > 0:
            last_event_id = since
        return StreamingResponse(
            _event_stream(svc, task_id, start_seq=max(0, last_event_id)),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ── artifacts ─────────────────────────────────────────────────────────
    @app.get("/api/tasks/{task_id}/artifacts")
    def list_artifacts(task_id: str) -> List[Dict[str, str]]:
        state = _require_state(task_id)
        return [
            {
                "index": str(i),
                "path": item.get("path", ""),
                "name": Path(item.get("path", "")).name,
                "type": item.get("type", ""),
            }
            for i, item in enumerate(state.result_files or [])
        ]

    @app.get("/api/tasks/{task_id}/artifacts/{index}")
    def download_artifact(task_id: str, index: int) -> FileResponse:
        state = _require_state(task_id)
        files = state.result_files or []
        if index < 0 or index >= len(files):
            raise HTTPException(404, f"Artifact index out of range: {index}")
        path = files[index].get("path", "")
        if not path or not Path(path).exists():
            raise HTTPException(404, f"Artifact file missing: {path}")
        return FileResponse(path, filename=Path(path).name)

    _mount_spa(app)

    return app


async def _event_stream(
    svc: RuntimeService, task_id: str, start_seq: int = 0
) -> AsyncIterator[str]:
    """SSE 桥（游标轮询泵）：帧携带绝对序号，天然支持 Last-Event-ID 续传。

    - 每个事件帧 ``id: <seq>``，seq = 该任务事件列表中的绝对位置（1-based）；
    - 浏览器 EventSource 自动重连时会回传 Last-Event-ID，服务端从
      ``get_task_events(since=seq)`` 重放缺失事件，零丢失；
    - 连接建立先发一帧完整 ``state`` 快照，再进入增量流。
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue(maxsize=512)
    stop = threading.Event()

    def _put(frame: str) -> None:
        if not queue.full():
            queue.put_nowait(frame)

    def _pump() -> None:
        """轮询游标泵：把新事件翻译成带 id 的 SSE 帧。"""
        cursor = start_seq
        try:
            while not stop.is_set():
                state = svc.get_task_state(task_id)
                if state is None:
                    loop.call_soon_threadsafe(
                        _put, _sse_frame("error", {"message": f"Unknown task: {task_id}"})
                    )
                    return
                events = svc.get_task_events(task_id, since=cursor)
                for offset, evt in enumerate(events):
                    seq = cursor + offset + 1
                    payload = _event_payload(evt)
                    payload["seq"] = seq
                    name = (
                        "progress"
                        if type(evt).__name__ == "TaskProgressEvent"
                        else "notice"
                    )
                    frame = _sse_frame(name, payload)
                    # 注入 SSE id 行（_sse_frame 只产出 event/data）
                    frame = frame.replace(
                        f"event: {name}\n", f"event: {name}\nid: {seq}\n", 1
                    )
                    loop.call_soon_threadsafe(_put, frame)
                cursor += len(events)
                if state.status in _TERMINAL_STATUSES:
                    return
                stop.wait(0.4)
        except Exception:  # noqa: BLE001 -- 泵异常不应拖垮整个生成器
            logger.exception("SSE event pump failed for task %s", task_id)

    pump_thread = threading.Thread(target=_pump, daemon=True)
    pump_thread.start()
    try:
        state = svc.get_task_state(task_id)
        if state is None:
            yield _sse_frame("error", {"message": f"Unknown task: {task_id}"})
            return
        yield "retry: 3000\n\n"
        yield _sse_frame("state", state.to_dict())
        # 统一收发循环：无论任务是否已终态（重连补帧场景），都先排空泵产出。
        while True:
            try:
                frame = await asyncio.wait_for(queue.get(), timeout=0.5)
                yield frame
                continue
            except asyncio.TimeoutError:
                pass
            if pump_thread.is_alive():
                yield ": keep-alive\n\n"
                continue
            # 泵已退出：再让事件循环跑一轮，把在途的 call_soon 回调落地。
            await asyncio.sleep(0.05)
            if not queue.empty():
                continue
            break
        current = svc.get_task_state(task_id)
        if current is None:
            return
        yield _sse_frame("done", {"status": current.status})
    finally:
        stop.set()


def main() -> int:
    """`python -m pdf2zh.services.api` 直接启动 API 服务。"""
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="Run the pdf2zh REST/SSE API server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=11009)
    args = parser.parse_args()

    app = create_api_app(allow_origins=["*"])
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
