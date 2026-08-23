"""Phase A 解耦层：RuntimeService 的标准 HTTP API（REST + SSE）。

为 SPA / 第三方客户端暴露与 Gradio 相同的能力，事件协议直接复用
TaskProgressEvent / RuntimeNoticeEvent（客户端无关，见 gui/events.py）。

启动方式：
    pdf2zh --api [--port 11009]
    python -m pdf2zh.services.api --port 11009

端点一览：
    GET  /api/health                        健康检查
    GET  /api/engines                       引擎列表（envs 只回显是否已配置，不回显值）
    GET  /api/engines/{name}/envs           引擎凭据明细（脱敏回显）
    PUT  /api/engines/{name}/envs           写入/清除用户级凭据（空串=清除）
    GET  /api/models/doclayout              版面模型状态（存在/校验/下载中）
    POST /api/models/doclayout/download     后台下载版面模型
    GET  /api/selftest/babeldoc             BabelDOC 导入链路自检
    GET  /api/selftest/magicpdf             magic-pdf/MinerU 可用性探测（含安装指引）
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
import os
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from pdf2zh.services.runtime_service import RuntimeService, TranslationRequest
from pdf2zh.services.runtime_singleton import get_runtime_service

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = {"completed", "cancelled", "failed"}
_SSE_KEEPALIVE_SECONDS = 15.0


class EngineEnvsPayload(BaseModel):
    """PUT /api/engines/{name}/envs 请求体：KEY → 新值；空串表示清除该凭据。"""

    envs: Dict[str, Optional[str]]


def _mask_secret(value: str) -> str:
    """凭据回显脱敏：只暴露首尾少量字符，供用户确认已配置内容。

    掩码用 ASCII ``*``（非 ``•``）：部分 HTTP 客户端对无 charset 的
    JSON 响应按 Latin-1 解码，多字节掩码符会乱码。
    """
    if not value:
        return ""
    if len(value) <= 8:
        return "****"
    return f"{value[:3]}****{value[-4:]}"


#: doclayout 模型后台下载状态（create_api_app 内的端点共享）
_model_download_state: Dict[str, Any] = {"running": False, "error": None}


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
    # 服务形态默认启用并行优化（CLI 单次任务不受影响，仍走原路径）：
    # - Warm Pool：常驻 worker 进程池，免去每任务 spawn + ONNX 模型加载
    #   （doc/performance_bottleneck_report.md §6.3 实测 8.2s，约占 29%）；
    # - worker ORT 单线程：多 worker 并发时避免 onnxruntime 全核过订阅
    #   （基准实测 t8 较 t4 劣化 16%）。
    os.environ.setdefault("PDF2ZH_WARM_POOL", "1")
    os.environ.setdefault("PDF2ZH_WORKER_ORT_THREADS", "1")
    # 关闭 ORT CPU arena：常驻多 worker 下每个 worker ~490MB RSS（模型仅
    # 72MB），关 arena 显著削峰，延迟代价个位数百分比。
    os.environ.setdefault("PDF2ZH_ORT_NO_ARENA", "1")

    # 后台预热并行 worker 池：首个用户任务不再承担 spawn + ONNX 模型加载
    # （实测 ~8s）。池大小取 2-4（按核数），后续请求更大并发时自动重建。
    def _prewarm_pool() -> None:
        try:
            from pdf2zh.doclayout import get_backend
            from pdf2zh.parallel.pool import get_shared_pool

            size = max(2, min(4, os.cpu_count() or 2))
            started = time.perf_counter()
            get_shared_pool(size, get_backend()).get()
            logger.info(
                "parallel worker pool prewarmed (%d workers) in %.1fs",
                size,
                time.perf_counter() - started,
            )
        except Exception as exc:  # noqa: BLE001 -- 预热失败不阻断服务
            logger.warning("parallel pool prewarm skipped: %s", str(exc)[:120])

    threading.Thread(target=_prewarm_pool, name="pool-prewarm", daemon=True).start()

    # 预热 translator 注册表：首次 GET /api/engines 实测 ~4.9s（懒导入全部
    # 引擎模块），SPA bootstrap 一启动就会调用它。后台提前建好注册表，
    # 前端引擎下拉即开即用。
    def _prewarm_registry() -> None:
        try:
            started = time.perf_counter()
            from pdf2zh.config import ConfigManager
            from pdf2zh.translator import build_translator_registry

            ConfigManager.get_instance()
            build_translator_registry()
            logger.info(
                "translator registry prewarmed in %.1fs",
                time.perf_counter() - started,
            )
        except Exception as exc:  # noqa: BLE001 -- 预热失败不阻断服务
            logger.warning("translator registry prewarm skipped: %s", str(exc)[:120])

    threading.Thread(target=_prewarm_registry, name="registry-prewarm", daemon=True).start()

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

    def _engine_cls(name: str):
        from pdf2zh.translator import build_translator_registry

        cls = next(
            (c for c in build_translator_registry() if c.name == name), None
        )
        if cls is None:
            raise HTTPException(404, f"engine not found: {name}")
        return cls

    @app.get("/api/engines/{name}/envs")
    def get_engine_envs(name: str) -> Dict[str, Any]:
        """引擎凭据明细（值脱敏回显，供设置界面确认已配置内容）。"""
        from pdf2zh.config import ConfigManager

        cls = _engine_cls(name)
        stored = ConfigManager.get_translator_by_name(name) or {}
        envs = []
        for key in cls.envs:
            value = str(stored.get(key) or "")
            envs.append(
                {
                    "key": key,
                    "configured": bool(value),
                    "masked": _mask_secret(value),
                }
            )
        return {"name": name, "envs": envs}

    @app.put("/api/engines/{name}/envs")
    def update_engine_envs(name: str, payload: EngineEnvsPayload) -> Dict[str, Any]:
        """写入/清除用户级凭据（ConfigManager 持久化，下次任务实例化即生效）。

        语义：值非空 → 设置；值为空串/null → 清除该键。仅接受引擎声明的键。
        """
        from pdf2zh.config import ConfigManager

        cls = _engine_cls(name)
        valid = set(cls.envs)
        unknown = sorted(k for k in payload.envs if k not in valid)
        if unknown:
            raise HTTPException(400, f"unknown env keys: {', '.join(unknown)}")

        stored = {
            k: v
            for k, v in (ConfigManager.get_translator_by_name(name) or {}).items()
            if k in valid
        }
        cleared = []
        for key, value in payload.envs.items():
            text = str(value or "").strip()
            if text:
                stored[key] = text
            elif stored.pop(key, None) is not None:
                cleared.append(key)
        ConfigManager.set_translator_by_name(name, stored)
        logger.info(
            "engine credentials updated: %s (set=%s cleared=%s)",
            name,
            sorted(k for k in payload.envs if not cleared or k not in cleared),
            cleared,
        )
        envs = [
            {
                "key": key,
                "configured": bool(str(stored.get(key) or "")),
                "masked": _mask_secret(str(stored.get(key) or "")),
            }
            for key in cls.envs
        ]
        return {"name": name, "envs": envs}

    # ── selftest（frozen 分发诊断） ─────────────────────────────────────
    @app.get("/api/selftest/babeldoc")
    def selftest_babeldoc() -> Dict[str, Any]:
        """尝试完整导入 BabelDOC 引擎链路，返回真实异常。

        frozen 打包环境中 babeldoc_adapter 会把任何 ImportError 包装成
        "engine not available"；该端点用于直接暴露缺失模块，便于定位打包缺件。
        """
        try:
            from babeldoc.format.pdf.high_level import (  # noqa: F401 PLC0415
                async_translate,
                init,
            )
            from babeldoc.format.pdf.translation_config import (  # noqa: F401 PLC0415
                TranslationConfig,
                WatermarkOutputMode,
            )
            # tiktoken 的编码插件经 entry_points 动态加载（frozen 环境常见
            # 缺件点），导入成功不等于运行时可用，这里按 babeldoc 实际用法
            # 直接实例化一次 o200k_base。
            import tiktoken  # noqa: PLC0415

            tiktoken.get_encoding("o200k_base")
            return {"ok": True, "error": None}
        except Exception as exc:  # noqa: BLE001 -- 诊断端点的职责就是回显
            return {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }

    # ── models（GPU 版面模型按需下载，不随安装包分发） ─────────────────
    _model_download_lock = threading.Lock()

    @app.get("/api/models/doclayout")
    def doclayout_model_status() -> Dict[str, Any]:
        """doclayout ONNX 模型状态：存在性 / 大小 / SHA3-256 校验。"""
        import hashlib

        from babeldoc.assets.assets import get_cache_file_path
        from babeldoc.assets.embedding_assets_metadata import (
            DOCLAYOUT_YOLO_DOCSTRUCTBENCH_IMGSZ1024ONNX_SHA3_256,
        )

        path = get_cache_file_path(
            "doclayout_yolo_docstructbench_imgsz1024.onnx", "models"
        )
        exists = path.is_file()
        size = path.stat().st_size if exists else 0
        sha_ok = False
        if exists:
            digest = hashlib.sha3_256()
            with path.open("rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    digest.update(chunk)
            sha_ok = digest.hexdigest() == DOCLAYOUT_YOLO_DOCSTRUCTBENCH_IMGSZ1024ONNX_SHA3_256
        return {
            "path": str(path),
            "exists": exists,
            "size_bytes": size,
            "sha_ok": sha_ok,
            "downloading": _model_download_state["running"],
            "last_error": _model_download_state["error"],
        }

    @app.post("/api/models/doclayout/download")
    def download_doclayout_model() -> Dict[str, Any]:
        """后台线程下载 doclayout ONNX 模型；进度经轮询 status 获取。"""
        if not _model_download_lock.acquire(blocking=False):
            return {"started": False, "reason": "already running"}
        if _model_download_state["running"]:
            _model_download_lock.release()
            return {"started": False, "reason": "already running"}

        def _run() -> None:
            _model_download_state.update(running=True, error=None)
            try:
                from babeldoc.assets.assets import get_doclayout_onnx_model_path

                # 下载完成后内部会做 SHA3-256 校验，失败抛异常。
                get_doclayout_onnx_model_path()
                logger.info("doclayout onnx model download finished")
            except Exception as exc:  # noqa: BLE001 -- 状态回显给前端
                logger.warning("doclayout model download failed: %s", exc)
                _model_download_state["error"] = f"{type(exc).__name__}: {exc}"
            finally:
                _model_download_state["running"] = False
                _model_download_lock.release()

        threading.Thread(target=_run, name="model-download", daemon=True).start()
        return {"started": True}

    @app.get("/api/selftest/magicpdf")
    def selftest_magicpdf() -> Dict[str, Any]:
        """magic-pdf/MinerU 解析链路可用性探测（frozen 包内默认不可用）。"""
        from pdf2zh.engine_env import available_backend, mineru_install_hint

        backend, ok = available_backend()
        return {
            "ok": bool(ok),
            "backend": backend,
            # 桌面包不内置 MinerU/torch（NSIS 2GB 上限）；给出与当前
            # Python 版本匹配的可执行安装命令，模型在首次解析时下载到
            # 用户缓存、与应用目录分离。
            "hint": mineru_install_hint() if not ok else "",
        }

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
        backend: str = Form(default="auto"),
        output_dir: str = Form(default=""),
        ignore_cache: bool = Form(default=False),
        extra_config: str = Form(default=""),
        glossaries: Optional[List[UploadFile]] = File(default=None),
        glossary_files: str = Form(default=""),
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

        # 专业词表：multipart 直传（glossaries）和/或服务端已有路径
        # （glossary_files，JSON 数组或逗号分隔；支持词表库内名称）。
        resolved_glossaries: List[str] = []
        for gf in glossaries or []:
            if not gf.filename:
                continue
            glossary_dir = (
                Path(tempfile.gettempdir()) / "pdf2zh_api_glossaries"
            )
            glossary_dir.mkdir(parents=True, exist_ok=True)
            gdest = glossary_dir / f"{uuid.uuid4().hex[:8]}_{Path(gf.filename).name}"
            with gdest.open("wb") as fh:
                while chunk := await gf.read(1024 * 1024):
                    fh.write(chunk)
            resolved_glossaries.append(str(gdest))
        if glossary_files.strip():
            from pdf2zh.glossary_store import resolve_store_names

            raw = [
                p.strip().strip('"')
                for p in (
                    json.loads(glossary_files)
                    if glossary_files.lstrip().startswith("[")
                    else glossary_files.split(",")
                )
                if p and p.strip()
            ]
            for p in raw:
                if Path(p).is_file():
                    resolved_glossaries.append(p)
                else:  # 词表库内名称
                    try:
                        resolved_glossaries.extend(resolve_store_names([p]))
                    except Exception as exc:
                        raise HTTPException(
                            400, f"glossary file not found: {p} ({exc})"
                        )

        request = TranslationRequest(
            source_path=resolved_path,
            target_lang=target_lang,
            source_lang=source_lang,
            engine=engine,
            threads=max(1, min(int(threads), 32)),
            page_range=page_range or None,
            parse_engine=parse_engine,
            backend=(backend or "auto").strip().lower() or "auto",
            output_dir=(output_dir or "").strip(),
            ignore_cache=bool(ignore_cache),
            glossary_files=resolved_glossaries,
            extra_config=extra,
        )
        return _submit(request)

    # ── glossary store ────────────────────────────────────────────────────
    @app.get("/api/glossaries")
    def list_glossaries() -> List[Dict[str, Any]]:
        from pdf2zh.glossary_store import list_store

        return list_store()

    @app.post("/api/glossaries")
    async def import_glossary(
        file: UploadFile = File(...),
        name: str = Form(default=""),
    ) -> Dict[str, Any]:
        from pdf2zh.glossary_store import GlossaryError, import_to_store, parse_csv

        if not file.filename:
            raise HTTPException(400, "empty upload")
        tmp_dir = Path(tempfile.gettempdir()) / "pdf2zh_api_glossaries"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp = tmp_dir / f"{uuid.uuid4().hex[:8]}_{Path(file.filename).name}"
        with tmp.open("wb") as fh:
            while chunk := await file.read(1024 * 1024):
                fh.write(chunk)
        try:
            entries = parse_csv(tmp)
            dest = import_to_store(tmp, name=name or None)
        except GlossaryError as exc:
            raise HTTPException(400, str(exc))
        return {"name": dest.stem, "path": str(dest), "entries": len(entries)}

    @app.get("/api/glossaries/{name}/download")
    def download_glossary(name: str) -> FileResponse:
        from pdf2zh.glossary_store import export_from_store

        export_tmp = Path(tempfile.gettempdir()) / (
            f"pdf2zh_glossary_export_{uuid.uuid4().hex[:8]}.csv"
        )
        try:
            export_from_store(name, export_tmp)
        except Exception as exc:
            raise HTTPException(404, str(exc))
        return FileResponse(
            export_tmp, filename=f"{name}.csv",
            media_type="text/csv",
        )

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
