"""Phase A 解耦层：RuntimeService 的标准 HTTP API（REST + SSE）。

为 SPA / 第三方客户端暴露与 Gradio 相同的能力，事件协议直接复用
TaskProgressEvent / RuntimeNoticeEvent（客户端无关，见 gui/events.py）。

启动方式：
    pdf2zh --api [--port 11009]
    python -m pdf2zh.services.api --port 11009

端点一览（与本文件的路由注册保持同步）：
    GET    /api/health                       健康检查（唯一免鉴权端点）
    GET    /api/engines                      引擎列表（envs 只回显是否已配置）
    GET    /api/engines/{name}/envs          引擎凭据明细（脱敏回显）
    PUT    /api/engines/{name}/envs          写入/清除用户级凭据（JSON body）

    GET    /api/selftest/babeldoc            BabelDOC 导入链路自检
    GET    /api/selftest/magicpdf            MinerU/magic-pdf 可用性探测
    GET    /api/selftest/jina                Jina OCR 隔离运行时探测
    GET    /api/models/doclayout             版面模型状态（存在/校验/下载中）
    POST   /api/models/doclayout/download    后台下载版面模型
    GET    /api/gpu/provider                 CUDA 执行器状态（本体不携带）
    POST   /api/gpu/provider/download        后台下载并安装 CUDA 执行器
    POST   /api/gpu/provider/remove          移除已下载的 CUDA 执行器
    POST   /api/setup/mineru                 后台构建 MinerU 隔离环境
    GET    /api/setup/mineru                 MinerU 构建状态
    POST   /api/setup/mineru/cuda            后台把 MinerU torch 换成 CUDA 版
    GET    /api/setup/mineru/cuda            CUDA torch 升级状态
    POST   /api/setup/jina                   后台构建 Jina OCR 隔离环境
    GET    /api/setup/jina                   Jina 构建状态

    POST   /api/tasks                        提交任务（仅 multipart/form-data）
    GET    /api/tasks                        任务列表（摘要模式）
    GET    /api/tasks/{task_id}              任务详情（全量）
    DELETE /api/tasks/{task_id}              取消任务 -> {"cancelled": bool}
    POST   /api/tasks/{task_id}/pause        -> {"pause_task": bool}
    POST   /api/tasks/{task_id}/resume       -> {"resume_task": bool}
    POST   /api/tasks/{task_id}/skip         -> {"skip_task": bool}
    GET    /api/tasks/{task_id}/events       SSE 进度流（state/progress/log/done 帧）
    GET    /api/tasks/{task_id}/artifacts    结果文件清单
    GET    /api/tasks/{task_id}/artifacts/{index}   下载结果文件（index 为整数）
    GET    /api/tasks/{task_id}/result-zip   下载打包 ZIP

    GET    /api/glossaries                  术语库清单
    POST   /api/glossaries                  上传导入术语库 CSV
    GET    /api/glossaries/{name}/download  导出术语库 CSV

通用约定（调用方须知）：
- 成功一律 200（无 201/202/204）；"已在进行中"一类冲突也用 200 +
  ``{"started": false, "reason": "already running"}`` 表达，不用 409。
- 错误体统一为 ``{"detail": "<可读说明>"}``（含鉴权/Host 守卫）。
  FastAPI 自身的请求校验错误是 422 + ``{"detail": [ ... ]}``（数组形态）。
- 自检类端点（``/api/selftest/*``）永远 200，用 ``ok`` 字段表达成败。
- ``GET`` 列表端点（engines / glossaries / tasks / artifacts）返回**裸数组**，
  其余端点返回对象。
- 鉴权：设置 ``PDF2ZH_API_TOKEN`` 后，除 ``/api/health`` 外均需
  ``X-API-Key`` 头或 ``?api_key=`` 查询参数（``Authorization: Bearer`` 亦可）。
- Host/Origin 守卫：默认只接受回环 Host（``127.0.0.1`` / ``localhost`` /
  ``::1``）与 ``tauri.localhost``；需要对外暴露时设 ``PDF2ZH_API_TRUST_ANY_HOST=1``。
- SSE：``?since=<seq>`` 或 ``Last-Event-ID`` 头续传；并发流上限 16，
  超出返回 503。
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from urllib.parse import urlsplit
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel

from pdf2zh.services.runtime_service import RuntimeService, TranslationRequest
from pdf2zh.v3.ingestion.config import (
    JINA_DEFAULT_DPI,
    JINA_DEFAULT_MAX_NEW_TOKENS,
    JINA_DEFAULT_MAX_PIXELS,
    JINA_DEFAULT_TIMEOUT,
    JINA_MIN_COVERAGE,
    JINA_MODEL_ID,
    JINA_PROMPT,
    JINA_REVISION,
    INGEST_REQUEST_CHOICES,
    JinaOcrOptions,
    normalize_ingest_backend,
    normalize_parse_engine,
    normalize_user_page_indices,
)
from pdf2zh.services.runtime_singleton import get_runtime_service

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = {"completed", "cancelled", "failed"}
#: 空闲时 keep-alive 注释帧的发送间隔（秒）—— 与声明的心跳意图一致。
_SSE_KEEPALIVE_SECONDS = 15.0
#: 接收侧轮询间隔（秒）：决定「多久才判定空闲」的最坏延迟，必须明显小于
#: keep-alive 间隔，否则心跳永远发不出去。
_SSE_POLL_SECONDS = 0.5

# ════════════════════════════════════════════════════════════════════
# 入站防护（安全加固）
#
# 服务默认只绑定 loopback，但浏览器内任意网页仍可向
# http://127.0.0.1:<port> 发起跨站请求（简单请求免预检；DNS rebinding
# 可绕过同源策略）。以下三层防护按序生效：
#
#   1. Host 守卫：Host 头必须是回环地址 —— 直接封死 DNS rebinding；
#      部署到非回环地址时需显式设 ``PDF2ZH_API_TRUST_ANY_HOST=1``。
#   2. Origin 守卫：携带 Origin 的请求（浏览器发起）其源主机必须是
#      回环地址或显式 allow_origins —— 封死网页 drive-by 读/写。
#   3. 可选 Token 鉴权：设置 ``PDF2ZH_API_TOKEN`` 后，除健康检查外的
#      全部 /api 端点要求凭据（供远程可信客户端使用）。
# ════════════════════════════════════════════════════════════════════

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
#: Tauri v2 Windows WebView 的自定义协议源（跨源调用 sidecar 时出现）。
_EXTRA_TRUSTED_ORIGIN_HOSTS = frozenset({"tauri.localhost"})


def _hostname_of(netloc: str) -> str:
    """从 Host/Origin 头提取 hostname（兼容 IPv6 字面量与端口）。"""
    netloc = netloc.rsplit("@", 1)[-1].strip()
    if netloc.startswith("["):
        end = netloc.find("]")
        return (netloc[1:end] if end > 0 else netloc).lower()
    return netloc.split(":", 1)[0].lower()


def _trust_any_host() -> bool:
    return (os.environ.get("PDF2ZH_API_TRUST_ANY_HOST") or "").strip() == "1"


def _api_token() -> str:
    return (os.environ.get("PDF2ZH_API_TOKEN") or "").strip()


def _max_upload_bytes() -> int:
    """上传体积上限（默认 200MB，``PDF2ZH_MAX_UPLOAD_MB`` 可调）。"""
    raw = (os.environ.get("PDF2ZH_MAX_UPLOAD_MB") or "").strip()
    try:
        mb = int(raw) if raw else 200
    except ValueError:
        mb = 200
    return max(1, mb) << 20


def _allowed_source_dirs() -> List[Path]:
    """允许作为 source_path 的额外目录（os.pathsep 分隔）。"""
    raw = os.environ.get("PDF2ZH_ALLOWED_SOURCE_DIRS") or ""
    out = []
    for part in raw.split(os.pathsep):
        part = part.strip()
        if part:
            out.append(Path(part).expanduser().resolve())
    return out


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


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


async def _save_upload(file: UploadFile, dest: Path) -> None:
    """分块落盘上传内容，超过 ``PDF2ZH_MAX_UPLOAD_MB`` 即拒绝（防磁盘耗尽）。"""
    limit = _max_upload_bytes()
    total = 0
    with dest.open("wb") as fh:
        while chunk := await file.read(1024 * 1024):
            total += len(chunk)
            if total > limit:
                fh.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(
                    413,
                    f"upload exceeds size limit ({limit >> 20} MB)",
                )
            fh.write(chunk)


#: Windows 保留设备名（不含扩展名的 stem 部分）。
_WIN_RESERVED_STEMS = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def _safe_upload_name(filename: str, used=None) -> str:
    """把上传文件名规整为可直接落盘的安全名，并保留可读原名。

    此前上传文件被无条件改写为 ``{uuid8}_{原名}``（防 temp 目录同名覆盖），
    导致翻译产物 ``{stem}-mono/-dual.pdf`` 与源文件名对不上，且对未超长、
    不重名的文件也一律生效。现在：文件保留原始名字，只做最小清洗
    （路径成分 / Windows 非法字符与控制符 / 保留设备名 / 尾点空格）；
    同一提交内的重名按 ``stem (n).ext`` 顺延防覆盖，跨批次覆盖由
    ``submit_task`` 的批次子目录隔离。
    """
    name = os.path.basename(str(filename or "").replace("\\", "/")).strip()
    name = re.sub(r'[<>:"/|?*\x00-\x1f]', "_", name)
    stem, ext = os.path.splitext(name)
    stem = stem.strip().rstrip(". ") or "document"
    if stem.upper() in _WIN_RESERVED_STEMS:
        stem = f"_{stem}"
    name = f"{stem}{ext.strip()}"
    if used is not None:
        candidate = name
        n = 1
        while candidate.lower() in used:
            candidate = f"{stem} ({n}){ext.strip()}"
            n += 1
        used.add(candidate.lower())
        name = candidate
    return name


def _validate_source_path(resolved_path: str) -> None:
    """限制 ``source_path`` 指向可信目录，封死本地任意文件外泄链。

    攻击面：无鉴权的网页可提交 ``source_path=<任意本机文件>`` 的任务，
    完成后经 artifacts 端点把文件下载走。规则：
      - 未配置 token（纯 loopback 本机模式）→ 仅允许上传临时目录、
        词表目录及 ``PDF2ZH_ALLOWED_SOURCE_DIRS`` 白名单内的路径；
      - 已配置 token（操作方声明存在远程可信客户端）→ 仅要求文件
        存在（客户端已通过凭据校验，等价于本机 CLI 信任级别）。
    """
    path = Path(resolved_path)
    if _api_token():
        if not path.is_file():
            raise HTTPException(404, f"source file not found: {resolved_path}")
        return
    resolved = path.resolve()
    trusted_roots = [*_allowed_source_dirs()]
    for name in ("pdf2zh_api_uploads", "pdf2zh_api_glossaries"):
        trusted_roots.append(Path(tempfile.gettempdir()) / name)
    if any(_is_under(resolved, root) for root in trusted_roots):
        return
    if not resolved.exists():
        return  # 不存在的路径交由任务执行期自然报错（保持既有语义）
    logger.warning(
        "Rejected source_path outside allowed directories: %s "
        "(set PDF2ZH_ALLOWED_SOURCE_DIRS to trust a directory, or "
        "PDF2ZH_API_TOKEN for authenticated remote access)",
        resolved_path,
    )
    raise HTTPException(
        403,
        "source_path is outside allowed directories; upload the file via "
        "multipart instead, or configure PDF2ZH_ALLOWED_SOURCE_DIRS",
    )


#: doclayout 模型后台下载状态（create_api_app 内的端点共享）
_model_download_state: Dict[str, Any] = {"running": False, "error": None}

#: MinerU 隔离 venv 后台构建状态（桌面/冻结分发无 submodule，需就地安装）
_mineru_setup_state: Dict[str, Any] = {
    "running": False,
    "done": False,
    "error": None,
    "interpreter": None,
}

#: MinerU 隔离 venv 的 CUDA torch 升级状态（启用 GPU 按钮触发）。
_mineru_cuda_setup_state: Dict[str, Any] = {
    "running": False,
    "done": False,
    "error": None,
    "interpreter": None,
}

_jina_setup_state: Dict[str, Any] = {
    "running": False,
    "done": False,
    "error": None,
    "interpreter": None,
}
_jina_setup_lock = threading.Lock()


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

    # 预热线程统一延迟点火：冷启动 trace（doc/perf/coldstart-trace/report.md）
    # 表明 registry/layout/pool 三路预热在服务 bind 前后立刻开跑，其 SSL
    # 上下文（~4s CPU）、ONNX 会话、字体下载会与主线程的模块导入/模型构建
    # 抢核，拖慢端口就绪。这里统一推迟到服务可用之后再逐级错峰执行；
    # PDF2ZH_PREWARM_DELAY 可调（秒，默认 1）。
    try:
        prewarm_delay = max(
            0.0, float(os.environ.get("PDF2ZH_PREWARM_DELAY", "1") or "1")
        )
    except ValueError:
        prewarm_delay = 1.0

    def _spawn_prewarm(name: str, extra_delay: float, target) -> None:
        def _runner() -> None:
            if prewarm_delay + extra_delay > 0:
                time.sleep(prewarm_delay + extra_delay)
            target()

        threading.Thread(target=_runner, name=name, daemon=True).start()

    _spawn_prewarm("pool-prewarm", 2.0, _prewarm_pool)

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

    _spawn_prewarm("registry-prewarm", 0.0, _prewarm_registry)

    # P0（基准报告）：主进程 doclayout 模型 + 远程字体预热。legacy 首任务
    # 实测 ~15s 冷加载（L_block parsing=14.9s：OnnxModel.load_available +
    # download_remote_fonts 磁盘/网络成本）全部发生在第一个用户任务内；
    # 这里后台提前完成，把成本移出用户感知路径。并行池预热只覆盖 worker
    # 进程，主进程 ModelInstance 单例仍需单独预热（runtime_service 直传）。
    def _prewarm_layout_model() -> None:
        if os.environ.get("PDF2ZH_NO_WARMUP", "") in ("1", "true", "True"):
            return
        try:
            started = time.perf_counter()
            from pdf2zh.doclayout import ModelInstance, OnnxModel

            if ModelInstance.value is None:
                ModelInstance.value = OnnxModel.load_available()
            try:
                from pdf2zh.high_level import download_remote_fonts

                download_remote_fonts("zh")
            except Exception as font_exc:  # noqa: BLE001 -- 字体预热失败不致命
                logger.debug("remote font prewarm skipped: %s", str(font_exc)[:120])
            logger.info(
                "layout model prewarmed in %.1fs",
                time.perf_counter() - started,
            )
        except Exception as exc:  # noqa: BLE001 -- 预热失败不阻断服务
            logger.warning("layout model prewarm skipped: %s", str(exc)[:120])

    _spawn_prewarm("layout-model-prewarm", 1.0, _prewarm_layout_model)

    svc = service or get_runtime_service()
    app = FastAPI(title="pdf2zh API", version="1.0.0")

    # ── 入站防护中间件（Host / Origin / Token） ───────────────────────
    @app.middleware("http")
    async def _inbound_guard(request: Request, call_next):
        if _trust_any_host():
            host_ok = True
        else:
            hostname = _hostname_of(request.headers.get("host") or "")
            host_ok = hostname in _LOOPBACK_HOSTS
        origin = (request.headers.get("origin") or "").strip()
        if host_ok and origin:
            try:
                ohost = (urlsplit(origin).hostname or "").lower()
            except ValueError:
                ohost = ""
            host_ok = (
                ohost in _LOOPBACK_HOSTS
                or ohost in _EXTRA_TRUSTED_ORIGIN_HOSTS
                or bool(allow_origins and origin in allow_origins)
            )
        if not host_ok:
            # 统一用 ``detail``：SPA 的错误提示只读 body.detail，用 ``error``
            # 会让 401/403 在界面上显示成空消息。
            return JSONResponse(
                {"detail": "forbidden: untrusted Host/Origin"},
                status_code=403,
            )
        token = _api_token()
        path = request.url.path
        if token and path.startswith("/api/") and path != "/api/health":
            supplied = (
                request.headers.get("x-api-key")
                or request.query_params.get("api_key")
                or ""
            )
            auth = request.headers.get("authorization") or ""
            if auth.lower().startswith("bearer "):
                supplied = supplied or auth[7:].strip()

            if not supplied or not hmac.compare_digest(supplied, token):
                return JSONResponse(
                    {"detail": "unauthorized: missing/invalid API key"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
        return await call_next(request)

    # CORS：仅对显式声明的非通配源开启。历史调用方传 ["*"] 等价于
    # 关闭（防护由上方 Origin 守卫承担），避免向任意网页开放跨源读。
    cors_origins = [o for o in (allow_origins or []) if o and o != "*"]
    if "*" in (allow_origins or []):
        logger.warning(
            "allow_origins=['*'] ignored; loopback Host/Origin guard is "
            "enforced instead. Pass explicit origins to enable CORS."
        )
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # ── health ────────────────────────────────────────────────────────────
    _health_first_call = True

    @app.get("/api/health")
    def health() -> Dict[str, Any]:
        nonlocal _health_first_call
        if _health_first_call:
            _health_first_call = False
            logger.info("sidecar health check passed (first call)")
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

        cls = next((c for c in build_translator_registry() if c.name == name), None)
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
            sha_ok = (
                digest.hexdigest()
                == DOCLAYOUT_YOLO_DOCSTRUCTBENCH_IMGSZ1024ONNX_SHA3_256
            )
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

    # ── GPU provider（CUDA 执行器按需下载，本体不携带） ──────────────
    @app.get("/api/gpu/provider")
    def gpu_provider_status() -> Dict[str, Any]:
        """CUDA provider 状态：内置 onnxruntime 版本 / DLL 存在性 / 可用 provider。"""
        from pdf2zh.services.gpu_provider import get_provider_status

        return get_provider_status()

    @app.post("/api/gpu/provider/download")
    def download_gpu_provider() -> Dict[str, Any]:
        """后台下载并安装与内置 onnxruntime 同版本的 CUDA provider。"""
        from pdf2zh.services.gpu_provider import start_download

        started, reason = start_download()
        return {"started": started, "reason": reason}

    @app.post("/api/gpu/provider/remove")
    def remove_gpu_provider() -> Dict[str, Any]:
        """移除已下载的 CUDA provider DLL，还原 CPU-only。"""
        from pdf2zh.services.gpu_provider import remove_cuda_provider

        return {"removed": remove_cuda_provider()}

    @app.get("/api/selftest/magicpdf")
    def selftest_magicpdf() -> Dict[str, Any]:
        """magic-pdf/MinerU 解析链路可用性探测（frozen 包内默认不可用）。"""
        from pdf2zh.engine_env import available_backend, mineru_install_hint

        backend, ok = available_backend()
        # MinerU 隔离 venv 的 torch 是否 CUDA（子进程解析实际用它，决定
        # 「启用 GPU」按钮的显示状态）。
        mineru_cuda = False
        mineru_venv = ""
        try:
            from pdf2zh.engine_env import mineru_python_override

            mineru_venv = mineru_python_override() or ""
            if mineru_venv:
                from pdf2zh.kernel.mineru_env import _venv_torch_cuda

                mineru_cuda = bool(_venv_torch_cuda(mineru_venv))
        except Exception:  # noqa: BLE001 -- 探测失败不影响主链路
            mineru_cuda = False
        return {
            "ok": bool(ok),
            "backend": backend,
            # 桌面包不内置 MinerU/torch（NSIS 2GB 上限）；给出与当前
            # Python 版本匹配的可执行安装命令，模型在首次解析时下载到
            # 用户缓存、与应用目录分离。
            "hint": mineru_install_hint() if not ok else "",
            # MinerU 隔离 venv 的 CUDA 可用性（启用 GPU 按钮据此显示）。
            "mineru_cuda": mineru_cuda,
            "mineru_venv": mineru_venv,
        }

    @app.post("/api/setup/mineru")
    def setup_mineru() -> Dict[str, Any]:
        """后台构建 MinerU 隔离 venv（无 submodule 时回退 PyPI 安装）。

        桌面/冻结分发不携带 ``vendor/MinerU`` 源码，常规
        ``pdf2zh-setup-mineru`` 控制台入口也未进包，故提供 HTTP 触发；
        torch 等重依赖下载量大，放到守护线程，前端轮询
        :meth:`mineru_setup_status` 获取进度。
        """
        if _mineru_setup_state["running"]:
            return {"started": False, "reason": "already running"}
        _mineru_setup_state.update(
            running=True, done=False, error=None, interpreter=None
        )

        def _run() -> None:
            try:
                from pdf2zh.kernel.mineru_env import ensure_venv

                interpreter = ensure_venv()
                _mineru_setup_state["interpreter"] = interpreter
                _mineru_setup_state["done"] = True
            except Exception as exc:  # noqa: BLE001 -- 状态回显给前端
                logger.warning("mineru setup failed: %s", exc)
                _mineru_setup_state["error"] = f"{type(exc).__name__}: {exc}"
            finally:
                _mineru_setup_state["running"] = False

        threading.Thread(target=_run, name="mineru-setup", daemon=True).start()
        return {"started": True}

    @app.get("/api/setup/mineru")
    def mineru_setup_status() -> Dict[str, Any]:
        """MinerU 隔离 venv 后台构建状态（供前端轮询）。"""
        return dict(_mineru_setup_state)

    @app.post("/api/setup/mineru/cuda")
    def setup_mineru_cuda() -> Dict[str, Any]:
        """后台把 MinerU 隔离 venv 的 torch 升级为 CUDA 版（启用 MinerU GPU）。

        桌面/冻结分发下 MinerU 装在与应用分离的隔离 venv 里，默认 torch 为
        CPU 版（PyPI 解析），请求 ``--backend cuda`` 也会被
        ``torch.cuda.is_available()=False`` 拒绝。本端点调用
        ``mineru_env.ensure_venv(cuda=True)`` 原位升级 CUDA torch/torchvision
        （不动 mineru 与模型缓存）；torch ~2GB，放守护线程，前端轮询
        :meth:`mineru_cuda_setup_status` 获取进度。
        """
        if _mineru_cuda_setup_state["running"]:
            return {"started": False, "reason": "already running"}
        _mineru_cuda_setup_state.update(
            running=True, done=False, error=None, interpreter=None
        )

        def _run() -> None:
            try:
                from pdf2zh.kernel.mineru_env import ensure_venv

                interpreter = ensure_venv(cuda=True)
                _mineru_cuda_setup_state["interpreter"] = interpreter
                _mineru_cuda_setup_state["done"] = True
            except Exception as exc:  # noqa: BLE001 -- 状态回显给前端
                logger.warning("mineru cuda setup failed: %s", exc)
                _mineru_cuda_setup_state["error"] = f"{type(exc).__name__}: {exc}"
            finally:
                _mineru_cuda_setup_state["running"] = False

        threading.Thread(target=_run, name="mineru-cuda-setup", daemon=True).start()
        return {"started": True}

    @app.get("/api/setup/mineru/cuda")
    def mineru_cuda_setup_status() -> Dict[str, Any]:
        """MinerU 隔离 venv 的 CUDA torch 升级状态（供前端轮询）。"""
        return dict(_mineru_cuda_setup_state)

    @app.get("/api/selftest/jina")
    def selftest_jina() -> Dict[str, Any]:
        from pdf2zh.kernel.jina_ocr_env import (
            default_venv_python,
            probe_jina_python,
        )

        interpreter = default_venv_python() or ""
        ready = probe_jina_python() is not None
        return {
            "ok": ready,
            "interpreter": interpreter,
            "hint": (
                "" if ready else "Install the isolated runtime with: pdf2zh-setup-jina"
            ),
        }

    @app.post("/api/setup/jina")
    def setup_jina() -> Dict[str, Any]:
        with _jina_setup_lock:
            if _jina_setup_state["running"]:
                return {"started": False, "reason": "already running"}
            _jina_setup_state.update(
                running=True, done=False, error=None, interpreter=None
            )

        def _run() -> None:
            try:
                from pdf2zh.kernel.jina_ocr_env import ensure_venv

                interpreter = ensure_venv()
                _jina_setup_state["interpreter"] = interpreter
                _jina_setup_state["done"] = True
            except Exception as exc:
                logger.warning("jina setup failed: %s", exc)
                _jina_setup_state["error"] = f"{type(exc).__name__}: {exc}"
            finally:
                _jina_setup_state["running"] = False

        threading.Thread(target=_run, name="jina-setup", daemon=True).start()
        return {"started": True}

    @app.get("/api/setup/jina")
    def jina_setup_status() -> Dict[str, Any]:
        return dict(_jina_setup_state)

    # ── submit ────────────────────────────────────────────────────────────
    def _submit(
        request: TranslationRequest,
        cleanup_paths: Optional[List[str]] = None,
    ) -> Dict[str, str]:
        task_id = svc.submit_task(request)
        paths = [Path(p) for p in (cleanup_paths or []) if p]
        if not paths:
            return {"task_id": task_id}

        def _cleanup() -> None:
            from pdf2zh.fs_utils import remove_path_robust

            for _ in range(86400):
                state = svc.get_task_state(task_id)
                if state is None or state.status in _TERMINAL_STATUSES:
                    time.sleep(1.0)
                    for path in paths:
                        remove_path_robust(path, defer=True)
                    return
                time.sleep(1.0)

        threading.Thread(
            target=_cleanup,
            name="pdf2zh-api-input-cleanup",
            daemon=True,
        ).start()
        return {"task_id": task_id}

    @app.post("/api/tasks")
    async def submit_task(
        request: Request,
        file: Optional[UploadFile] = File(default=None),
        files: Optional[List[UploadFile]] = File(default=None),
        source_path: str = Form(default=""),
        target_lang: str = Form(default="zh-CN"),
        source_lang: str = Form(default="auto"),
        engine: str = Form(default="google"),
        threads: int = Form(default=4),
        page_range: str = Form(default=""),
        parse_engine: str = Form(default="auto"),
        ingest_backend: str = Form(default="auto"),
        mode_choice: str = Form(default="auto"),
        ocr_mode: str = Form(default="auto"),
        backend: str = Form(default="auto"),
        output_dir: str = Form(default=""),
        ignore_cache: bool = Form(default=False),
        extra_config: str = Form(default=""),
        glossaries: Optional[List[UploadFile]] = File(default=None),
        glossary_files: str = Form(default=""),
        mineru_vram_size: str = Form(default=""),
        mineru_window_size: str = Form(default=""),
        mineru_parse_method: str = Form(default=""),
        mineru_backend: str = Form(default=""),
        jina_model: str = Form(default=JINA_MODEL_ID),
        jina_revision: str = Form(default=JINA_REVISION),
        jina_prompt: str = Form(default=JINA_PROMPT),
        jina_device: str = Form(default="auto"),
        jina_dpi: int = Form(default=JINA_DEFAULT_DPI),
        jina_max_pixels: int = Form(default=JINA_DEFAULT_MAX_PIXELS),
        jina_max_new_tokens: int = Form(default=JINA_DEFAULT_MAX_NEW_TOKENS),
        jina_timeout: float = Form(default=JINA_DEFAULT_TIMEOUT),
        jina_cache_dir: str = Form(default=""),
        jina_min_coverage: float = Form(default=JINA_MIN_COVERAGE),
        jina_offline: bool = Form(default=False),
        trace_enabled: bool = Form(default=False),
        trace_dir: str = Form(default=""),
    ) -> Dict[str, str]:
        # 本端点全部参数都是 Form/File。FastAPI 对"有默认值但类型是 Form"的
        # 参数在收到非表单 body 时**不会报错**，而是全部取默认值 —— 于是
        # `Content-Type: application/json` 的提交会拿到 200 + task_id，
        # 却把 source_path/target_lang 等字段静默丢弃，任务必然以
        # "No source files provided" 失败。显式拒绝，避免"看似成功实则空跑"。
        content_type = (request.headers.get("content-type") or "").split(";")[0].strip()
        if content_type and content_type not in (
            "multipart/form-data",
            "application/x-www-form-urlencoded",
        ):
            raise HTTPException(
                415,
                "POST /api/tasks only accepts multipart/form-data "
                f"(got Content-Type: {content_type}); submit files as form "
                "parts or pass source_path as a form field",
            )
        # 批量上传：``files``（可重复的 multipart 部件）优先；兼容旧的单文件
        # ``file`` 字段与 ``source_path`` 本地路径。所有路径统一进入
        # ``TranslationRequest.files``，由运行时服务按数量自动路由
        # 单文件/批量执行器（批量含逐文件进度、失败续行、结果 ZIP）。
        upload_parts: List[UploadFile] = [
            f for f in (files or []) if f is not None and f.filename
        ]
        cleanup_paths: List[str] = []
        if not upload_parts and file is not None and file.filename:
            upload_parts.append(file)

        resolved_paths: List[str] = []
        if upload_parts:
            upload_dir = Path(tempfile.gettempdir()) / "pdf2zh_api_uploads"
            upload_dir.mkdir(parents=True, exist_ok=True)
            # 批次子目录隔离不同提交（防同名覆盖）；文件保留原始文件名 ——
            # 翻译产物 ``{stem}-mono/-dual.pdf`` 因此与源文件一一对应
            # （此前无条件加 uuid 前缀导致产物名与源文件对不上）。
            batch_dir = upload_dir / (
                f"batch_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
            )
            batch_dir.mkdir(parents=True, exist_ok=True)
            cleanup_paths.append(str(batch_dir))
            used_names: set = set()
            try:
                for part in upload_parts:
                    safe_name = _safe_upload_name(part.filename, used_names)
                    dest = batch_dir / safe_name
                    await _save_upload(part, dest)
                    resolved_paths.append(str(dest))
            except Exception:
                from pdf2zh.fs_utils import remove_path_robust

                remove_path_robust(batch_dir, defer=True)
                raise

        legacy_path = source_path.strip()
        if not resolved_paths and legacy_path:
            resolved_paths = [legacy_path]
        elif resolved_paths and legacy_path:
            # 混合提交：本地路径追加到上传列表尾部，保持两者都参与批量。
            resolved_paths.append(legacy_path)

        for p in resolved_paths:
            _validate_source_path(p)
        resolved_path = resolved_paths[0] if resolved_paths else ""

        extra: Dict[str, Any] = {}
        if extra_config.strip():
            try:
                extra = json.loads(extra_config)
            except json.JSONDecodeError as exc:
                raise HTTPException(400, f"extra_config is not valid JSON: {exc}")
            if not isinstance(extra, dict):
                raise HTTPException(400, "extra_config must be a JSON object")
        if mode_choice and mode_choice != "auto":
            extra.setdefault("mode_choice", mode_choice)
        if ocr_mode and ocr_mode != "auto":
            extra.setdefault("ocr_mode", ocr_mode)
        # v3 flight-recorder trace（magicpdf 链路生效）：与 CLI --trace / --trace-dir
        # 语义一致，随 extra_config 透传到运行时（_execute_magicpdf 读取）。
        if trace_enabled:
            extra["trace_enabled"] = True
        if (trace_dir or "").strip():
            extra["trace_dir"] = trace_dir.strip()

        # 专业词表：multipart 直传（glossaries）和/或服务端已有路径
        # （glossary_files，JSON 数组或逗号分隔；支持词表库内名称）。
        resolved_glossaries: List[str] = []
        try:
            for gf in glossaries or []:
                if not gf.filename:
                    continue
                glossary_dir = Path(tempfile.gettempdir()) / "pdf2zh_api_glossaries"
                glossary_dir.mkdir(parents=True, exist_ok=True)
                gdest = (
                    glossary_dir / f"{uuid.uuid4().hex[:8]}_{Path(gf.filename).name}"
                )
                await _save_upload(gf, gdest)
                cleanup_paths.append(str(gdest))
                resolved_glossaries.append(str(gdest))
        except Exception:
            from pdf2zh.fs_utils import remove_path_robust

            for path in cleanup_paths:
                remove_path_robust(path, defer=True)
            raise
        if glossary_files.strip():
            from pdf2zh.glossary_store import resolve_store_names

            try:
                glossary_values = (
                    json.loads(glossary_files)
                    if glossary_files.lstrip().startswith("[")
                    else glossary_files.split(",")
                )
            except (TypeError, ValueError) as exc:
                raise HTTPException(400, f"invalid glossary_files: {exc}")
            if not isinstance(glossary_values, list) or any(
                not isinstance(p, str) for p in glossary_values
            ):
                raise HTTPException(
                    400, "glossary_files JSON must be an array of strings"
                )
            raw = [str(p).strip().strip('"') for p in glossary_values if str(p).strip()]
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

        normalized_backend = (ingest_backend or "auto").strip().lower()
        if normalized_backend not in INGEST_REQUEST_CHOICES:
            raise HTTPException(400, f"invalid ingest_backend: {ingest_backend!r}")
        try:
            normalized_parse_engine = normalize_parse_engine(parse_engine)
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, f"invalid parse_engine: {exc}")

        if page_range.strip():
            try:
                normalize_user_page_indices(page_range)
            except (TypeError, ValueError) as exc:
                raise HTTPException(400, f"invalid page_range: {exc}")

        validated_jina_options: Optional[JinaOcrOptions] = None
        if normalize_ingest_backend(ingest_backend) == "jina":
            custom_model = (jina_model or JINA_MODEL_ID).strip() != JINA_MODEL_ID or (
                jina_revision or JINA_REVISION
            ).strip() != JINA_REVISION
            if custom_model and os.environ.get("PDF2ZH_JINA_ALLOW_CUSTOM_MODEL") != "1":
                raise HTTPException(
                    400,
                    "custom Jina models require PDF2ZH_JINA_ALLOW_CUSTOM_MODEL=1",
                )
            try:
                validated_jina_options = JinaOcrOptions.from_mapping(
                    {
                        "model": jina_model,
                        "revision": jina_revision,
                        "prompt": jina_prompt,
                        "device": jina_device,
                        "dpi": jina_dpi,
                        "max_pixels": jina_max_pixels,
                        "max_new_tokens": jina_max_new_tokens,
                        "timeout": jina_timeout,
                        "cache_dir": jina_cache_dir,
                        "min_coverage": jina_min_coverage,
                        "offline": jina_offline,
                    }
                )
            except (TypeError, ValueError) as exc:
                raise HTTPException(400, f"invalid Jina OCR options: {exc}")

        request = TranslationRequest(
            source_path=resolved_path,
            files=resolved_paths,
            target_lang=target_lang,
            source_lang=source_lang,
            engine=engine,
            threads=max(1, min(int(threads), 32)),
            page_range=page_range or None,
            parse_engine=normalized_parse_engine,
            ingest_backend=normalized_backend,
            backend=(backend or "auto").strip().lower() or "auto",
            output_dir=(output_dir or "").strip(),
            ignore_cache=bool(ignore_cache),
            glossary_files=resolved_glossaries,
            extra_config=extra,
            mineru_vram_size=(mineru_vram_size or "").strip(),
            mineru_window_size=(mineru_window_size or "").strip(),
            mineru_parse_method=(mineru_parse_method or "").strip(),
            mineru_backend=(mineru_backend or "").strip(),
            jina_model=(
                validated_jina_options.model
                if validated_jina_options is not None
                else (jina_model or JINA_MODEL_ID).strip()
            ),
            jina_revision=(
                validated_jina_options.revision
                if validated_jina_options is not None
                else (jina_revision or "").strip()
            ),
            jina_prompt=(
                validated_jina_options.prompt
                if validated_jina_options is not None
                else (jina_prompt or JINA_PROMPT).strip()
            ),
            jina_device=(
                validated_jina_options.device
                if validated_jina_options is not None
                else (jina_device or "auto").strip()
            ),
            jina_dpi=(
                validated_jina_options.dpi
                if validated_jina_options is not None
                else jina_dpi
            ),
            jina_max_pixels=(
                validated_jina_options.max_pixels
                if validated_jina_options is not None
                else jina_max_pixels
            ),
            jina_max_new_tokens=(
                validated_jina_options.max_new_tokens
                if validated_jina_options is not None
                else jina_max_new_tokens
            ),
            jina_timeout=(
                validated_jina_options.timeout
                if validated_jina_options is not None
                else jina_timeout
            ),
            jina_cache_dir=(
                (validated_jina_options.cache_dir or "")
                if validated_jina_options is not None
                else (jina_cache_dir or "").strip()
            ),
            jina_min_coverage=(
                validated_jina_options.min_coverage
                if validated_jina_options is not None
                else jina_min_coverage
            ),
            jina_offline=(
                validated_jina_options.offline
                if validated_jina_options is not None
                else bool(jina_offline)
            ),
        )
        return _submit(request, cleanup_paths)

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
        await _save_upload(file, tmp)
        try:
            entries = parse_csv(tmp)
            dest = import_to_store(tmp, name=name or None)
        except GlossaryError as exc:
            raise HTTPException(400, str(exc))
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
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

        def _cleanup_export() -> None:
            try:
                export_tmp.unlink(missing_ok=True)
            except OSError:
                pass

        return FileResponse(
            export_tmp,
            filename=f"{name}.csv",
            media_type="text/csv",
            background=BackgroundTask(_cleanup_export),
        )

    # ── query / control ───────────────────────────────────────────────────
    def _require_state(task_id: str):
        state = svc.get_task_state(task_id)
        if state is None:
            raise HTTPException(404, f"Unknown task: {task_id}")
        return state

    @app.get("/api/tasks")
    def list_tasks() -> List[Dict[str, Any]]:
        """任务列表(P0-1 摘要模式):每任务仅剔除四个"全文档快照"巨字段
        (ir/processor/gate/toc),15s 历史轮询不再反复序列化数十 MB 的
        per-page 快照。详情(含巨字段)经 GET /api/tasks/{id} 单独拉取。
        """
        states = []
        for tid in svc.list_task_ids():
            state = svc.get_task_state(tid)
            if state is not None:
                states.append(state.to_dict(summary=True))
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
    #: 每条 SSE 连接独占一个 0.4s 轮询线程；用信号量限制并发流数，
    #: 防止前端重连风暴耗尽线程（超限时返回 503，客户端会自动退避重试）。
    _sse_slots = threading.BoundedSemaphore(16)

    @app.get("/api/tasks/{task_id}/events")
    async def stream_events(
        task_id: str,
        request: Request,
        since: int = 0,
    ) -> StreamingResponse:
        _require_state(task_id)
        if not _sse_slots.acquire(timeout=5.0):
            raise HTTPException(503, "too many concurrent event streams; retry later")
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
            _event_stream(
                svc,
                task_id,
                start_seq=max(0, last_event_id),
                release=_sse_slots.release,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ── artifacts ─────────────────────────────────────────────────────────
    @app.get("/api/tasks/{task_id}/artifacts")
    def list_artifacts(task_id: str) -> List[Dict[str, Any]]:
        state = _require_state(task_id)
        # index 与下载端点的 ``{index}``（int 路径参数）保持同一类型，
        # 客户端可直接把这里的值拼进 URL，无需再做一次类型转换。
        return [
            {
                "index": i,
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
            raise HTTPException(
                404,
                f"Artifact index out of range: {index} "
                f"(task has {len(files)} artifact(s))",
            )
        path = files[index].get("path", "")
        if not path or not Path(path).exists():
            # 只回文件名：绝对路径是服务端内部信息，不应出现在错误体里。
            raise HTTPException(
                404, f"Artifact file missing: {Path(path).name or '(unnamed)'}"
            )
        return FileResponse(path, filename=Path(path).name)

    @app.get("/api/tasks/{task_id}/result-zip")
    def download_result_zip(task_id: str) -> FileResponse:
        """批量任务「全部下载」：返回运行时打包好的结果 ZIP。"""
        state = _require_state(task_id)
        path = state.result_zip or ""
        if not path or not Path(path).exists():
            raise HTTPException(404, "result zip not available for this task")
        return FileResponse(
            path,
            # 下载名 = 打包时组合的防重复名（{stem}-translated-{时间戳}.zip）；
            # 旧任务 / 兜底回退沿用历史格式。
            filename=(state.result_zip_name or f"pdf2zh-{task_id}-results.zip"),
            media_type="application/zip",
        )

    _mount_spa(app)

    return app


async def _event_stream(
    svc: RuntimeService,
    task_id: str,
    start_seq: int = 0,
    release=None,
) -> AsyncIterator[str]:
    """SSE 桥（游标轮询泵）：帧携带绝对序号，天然支持 Last-Event-ID 续传。

    - 每个事件帧 ``id: <seq>``，seq = 该任务事件列表中的绝对位置（1-based）；
    - 浏览器 EventSource 自动重连时会回传 Last-Event-ID，服务端从
      ``get_task_events(since=seq)`` 重放缺失事件，零丢失；
    - 连接建立先发一帧完整 ``state`` 快照，再进入增量流；
    - ``release``：连接关闭时归还并发信号量名额（见 stream_events）。
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue(maxsize=512)
    stop = threading.Event()

    def _enqueue(frame: str) -> bool:
        """Hand one frame to the async side. False => the frame was NOT queued."""
        if loop.is_closed():
            return False
        try:
            asyncio.run_coroutine_threadsafe(queue.put(frame), loop).result(timeout=5)
            return True
        except (RuntimeError, TimeoutError, asyncio.TimeoutError):
            logger.debug("SSE enqueue stopped for task %s", task_id)
            return False

    def _pump() -> None:
        """轮询游标泵：把新事件翻译成带 id 的 SSE 帧。"""
        cursor = start_seq
        try:
            while not stop.is_set():
                state = svc.get_task_state(task_id)
                if state is None:
                    _enqueue(
                        _sse_frame("error", {"message": f"Unknown task: {task_id}"})
                    )
                    return
                events, next_cursor = svc.get_task_events_with_cursor(
                    task_id, since=cursor
                )
                for offset, evt in enumerate(events):
                    seq = cursor + offset + 1
                    payload = _event_payload(evt)
                    payload["seq"] = seq
                    name = (
                        "progress"
                        if type(evt).__name__ == "TaskProgressEvent"
                        else "log" if type(evt).__name__ == "TaskLogEvent" else "notice"
                    )
                    frame = _sse_frame(name, payload)
                    # 注入 SSE id 行（_sse_frame 只产出 event/data）
                    frame = frame.replace(
                        f"event: {name}\n", f"event: {name}\nid: {seq}\n", 1
                    )
                    if not _enqueue(frame):
                        # A slow/absent client must never silently lose a frame:
                        # end the stream so EventSource reconnects with its last
                        # received id and the ring replays the gap.
                        logger.info(
                            "SSE pump dropping connection for task %s at seq %d",
                            task_id,
                            seq,
                        )
                        return
                cursor = next_cursor
                if state.status in _TERMINAL_STATUSES:
                    return
                # P2-3: 事件驱动唤醒为主,超时仅作兜底(保留旧 0.4s 心跳),
                # 避免每连接每 0.4s 忙轮询空转。
                svc.wait_task_signal(task_id, 0.4)
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
        # keep-alive 注释帧按 ``_SSE_KEEPALIVE_SECONDS`` 节奏发（用于穿过中间
        # 代理的空闲超时）；此前这里每 0.5 s 空转就发一帧，与该常量声明的
        # 15 s 意图不符 —— 16 路并发即 32 帧/秒的无意义流量。
        idle_since = time.monotonic()
        while True:
            try:
                frame = await asyncio.wait_for(queue.get(), timeout=_SSE_POLL_SECONDS)
                idle_since = time.monotonic()
                yield frame
                continue
            except asyncio.TimeoutError:
                pass
            if pump_thread.is_alive():
                if time.monotonic() - idle_since >= _SSE_KEEPALIVE_SECONDS:
                    idle_since = time.monotonic()
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
        if release is not None:
            try:
                release()
            except ValueError:  # 信号量已满：防重复归还
                pass


def main() -> int:
    """`python -m pdf2zh.services.api` 直接启动 API 服务。"""
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="Run the pdf2zh REST/SSE API server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=11009)
    parser.add_argument(
        "--log-file",
        default="",
        help="Server runtime log file (rotating). Falls back to the "
        "PDF2ZH_LOG_FILE env var when omitted.",
    )
    args = parser.parse_args()

    # 服务端运行日志落盘（进程级，幂等；缺省走 PDF2ZH_LOG_FILE env）。
    try:
        from pdf2zh.logging_setup import install_log_file, resolve_log_file

        install_log_file(resolve_log_file(args.log_file) or "")
    except Exception:  # noqa: BLE001 -- 日志落盘失败不影响服务启动
        pass

    app = create_api_app()
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
