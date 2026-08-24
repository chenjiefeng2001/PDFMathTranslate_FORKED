"""Worker 进程硬化（迁移自 high_level，加固 bootstrap 语义 + ORT 线程 + DLL 注册）。

不变量：
- worker 进程除了 ``multiprocessing.Event``（取消）与进程池管道外，
  不得持有或继承任何主进程同步原语。
- bootstrap 失败必须可见：``[Worker FATAL]`` 写 stderr + 抛
  ``WorkerBootstrapError``，不再静默降级为 ``ModelInstance=None`` 继续跑。
- ORT 线程受限（``PDF2ZH_WORKER_ORT_THREADS=1`` 门控，默认行为不变），
  避免多 worker × 全核导致的 CPU 争抢。

``execute_chunk`` 是原 ``high_level._translate_parallel_chunk`` 的迁移实现，
返回 ``ChunkResult``（异常不回传而封装为 error_message，保持池稳定）。
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from typing import Any, Dict, Optional

from pdf2zh.parallel.chunk import ChunkResult, ChunkTask
from pdf2zh.parallel.errors import WorkerBootstrapError

logger = logging.getLogger(__name__)

#: worker bootstrap 中 onnxruntime 导入失败的兜底提示
_ORT_DLL_HINT = (
    "onnxruntime unavailable in worker process; parallel layout model disabled. "
    "Set PDF2ZH_PARALLEL_WORKERS=1 / --backend cpu if this repeats."
)


def _emit_worker_fatal(message: str) -> None:
    """Bootstrap 失败必须可见：`[Worker FATAL]` 写 stderr（尽力而为）。"""
    try:
        print(f"[Worker FATAL] {message}", file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001 -- never block bootstrap on stderr failure
        pass


def _register_ort_dll_dir() -> None:
    """防御：把 ``onnxruntime.__file__`` 所在目录加入 DLL 搜索路径。

    PyStand / 打包版中 worker 的 PATH 继承可能不完整，显式注册模块目录
    避免 ``DLL load failed`` 类冷启动失败（仅 Windows 生效，失败静默）。
    """
    if not sys.platform.startswith("win"):
        return
    try:
        import onnxruntime  # noqa: PLC0415

        pkg_dir = os.path.dirname(onnxruntime.__file__)
        if pkg_dir and os.path.isdir(pkg_dir) and hasattr(os, "add_dll_directory"):
            os.add_dll_directory(pkg_dir)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Worker DLL dir registration skipped: %s", exc)


def _ignore_ctrl_c_in_worker() -> None:
    """worker 进程对控制台 Ctrl+C 免疫。

    Windows 的 CTRL_C_EVENT 会广播给同一控制台的所有进程：用户在主进程按
    Ctrl+C 时，正在加载模型的 worker 若不去忽略，就会被 KeyboardInterrupt
    杀死 → ``BrokenProcessPool`` → 整文档串行兜底（Ctrl+C 反而触发最长路径，
    见 ``pdf2zh.parallel.interrupt`` 模块 docstring）。忽略后由主进程侧
    （interrupt 旗标 + coordinator 短路）统一 ``shutdown`` 回收本池。

    POSIX 上 ``concurrent.futures`` 本就会在 fork 后设置 ``SIG_IGN``，
    此处幂等补齐 spawn（Windows）路径。
    """
    try:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    except (ValueError, OSError, TypeError):
        # 非主线程或环境不支持：忽略（保持默认）。
        pass


def init_worker_process(backend: Optional[str] = None) -> None:
    """Worker 冷启动（ProcessPoolExecutor initializer）。

    与旧 ``high_level._init_worker_process`` 签名一致（供现有/新 executor 复用）：
    1) DLL 预注册（防御，仅 onnxruntime 模块目录）；
    2) ``set_backend(backend)`` 传播父进程执行 provider 选择；
    3) bootstrap 语义化：onnxruntime 导入失败 → ``WorkerBootstrapError``；
       模型加载失败但文件缺失/后端允许 → 保持 ``ModelInstance=None``（版面侧
       天然降级）仅加日志。
    """
    _t_start = time.perf_counter()
    # 控制台 Ctrl+C 免疫（必须在任何模型加载之前，覆盖最危险的“加载中途被杀”窗口）。
    _ignore_ctrl_c_in_worker()
    _register_ort_dll_dir()

    from pdf2zh.doclayout import (
        ModelInstance,
        OnnxModel,
        get_backend,
        set_backend,
    )  # noqa: PLC0415

    if backend:
        set_backend(backend)
    _providers: list[str] = []
    try:
        from pdf2zh.doclayout import _ort_available_providers  # noqa: PLC0415

        _providers = list(_ort_available_providers())
    except Exception as exc:  # noqa: BLE001 -- bootstrap 失败必须可见
        _emit_worker_fatal(
            f"onnxruntime import failed: {type(exc).__name__}: {str(exc)[:200]}"
        )
        raise WorkerBootstrapError(_ORT_DLL_HINT) from exc

    if ModelInstance.value is None:
        try:
            ModelInstance.value = OnnxModel.load_available()
        except WorkerBootstrapError:
            raise
        except Exception as exc:  # noqa: BLE001 -- 模型缺失属可恢复降级
            logger.warning(
                "Worker model load failed (%s: %s), continuing without layout model.",
                type(exc).__name__,
                str(exc)[:120],
            )
            ModelInstance.value = None
    logger.info(
        "Layout worker pid=%d backend=%s available_providers=%s model_load=%.2fs",
        os.getpid(),
        get_backend(),
        _providers,
        time.perf_counter() - _t_start,
    )


def execute_chunk(task: ChunkTask) -> ChunkResult:
    """在 worker 进程内执行一个 chunk（迁移自 ``_translate_parallel_chunk``）。

    重对象（pymupdf.Document / OnnxModel / FontResolver / TextMetrics 等）在
    worker 内从 ``fp_bytes`` 与全局 ``ModelInstance`` 单例重建，规避
    SwigPyObject pickle 错误。单 chunk 异常封装进 ``ChunkResult.error_message``
    （``KeyboardInterrupt`` 除外 —— 它是取消信号，必须传播给 coordinator 短路）。
    """
    import io as _io
    import json
    from string import Template

    import pymupdf as _fitz

    from pdf2zh.collision_resolver import CollisionResolver  # noqa: PLC0415
    from pdf2zh.doclayout import ModelInstance  # noqa: PLC0415
    from pdf2zh.font_resolver import FontResolver  # noqa: PLC0415
    from pdf2zh.layout_graph import LayoutGraph  # noqa: PLC0415

    t0 = time.perf_counter()
    chunk_pages = list(task.chunk_pages)
    # 8.1.2: pages 第二道防线 —— 与 chunk_pages 取交集；交集为空直接返回
    # 空结果（绝不回落成全量翻译：translate_patch 的 `if pages and` 会把空
    # 列表当“未过滤”，旧代码在 pages=[] 时会全量处理）。
    if task.pages is not None:
        pages_set = set(task.pages)
        chunk_pages = [p for p in chunk_pages if p in pages_set]
    if not chunk_pages:
        return ChunkResult(
            obj_patch={},
            obs_bundle=None,
            elapsed=time.perf_counter() - t0,
        )
    try:
        # 从共享字节流重建文档（pickle-safe：worker 内打开）
        doc_zh = _fitz.open(stream=task.fp_bytes, filetype="pdf")
        doc_en = _fitz.open(stream=task.fp_bytes, filetype="pdf")

        # 模型来自 initializer 设置的全局单例
        model = ModelInstance.value

        collision_resolver = CollisionResolver()
        layout_graph = LayoutGraph()
        font_resolver = FontResolver(task.lang_out)

        noto = _fitz.Font(task.noto_name, task.font_path) if task.font_path else None

        text_metrics: Dict[str, Any] = {}
        if task.use_text_metrics and task.font_path:
            try:
                from pdf2zh.text_metrics import TextMetrics as _TM  # noqa: PLC0415

                text_metrics[task.noto_name] = _TM(task.font_path)
            except Exception:  # noqa: BLE001
                pass

        translation_cache_obj = None
        if task.use_translation_cache and not task.ignore_cache:
            try:
                from pdf2zh.translation_cache import TranslationCache  # noqa: PLC0415

                translation_cache_obj = TranslationCache()
            except Exception:  # noqa: BLE001
                pass

        prompt = Template(task.prompt_template) if task.prompt_template else None
        envs = json.loads(task.envs_str) if isinstance(task.envs_str, str) else {}

        from pdf2zh.high_level import translate_patch  # noqa: PLC0415

        result = translate_patch(
            _io.BytesIO(task.fp_bytes),
            pages=chunk_pages,
            doc_zh=doc_zh,
            doc_en=doc_en,
            model=model,
            lang_in=task.lang_in,
            lang_out=task.lang_out,
            service=task.service,
            thread=task.thread,
            vfont=task.vfont,
            vchar=task.vchar,
            noto_name=task.noto_name,
            noto=noto,
            envs=envs,
            prompt=prompt,
            ignore_cache=task.ignore_cache,
            skip_subset_fonts=task.skip_subset_fonts,
            text_metrics=text_metrics,
            font_resolver=font_resolver,
            layout_graph=layout_graph,
            collision_resolver=collision_resolver,
            translation_cache=translation_cache_obj,
            page_xref_map=task.page_xref_map,
            apply_page_xrefs=False,
            cancellation_event=task.cancel_event,
            processor_channels=task.processor_channels,
            render_takeover=task.render_takeover,
            translation_qa=task.translation_qa,
            geometry_cluster=task.geometry_cluster,
            toc_split=task.toc_split,
            pipeline_dump=task.pipeline_dump,
            document_model=task.document_model,
            observability=task.observability,
            reconstruction_channel=task.reconstruction_channel,
            reconstruction_adopt=task.reconstruction_adopt,
        )
        obs = None
        if isinstance(result, dict) and "__obs__" in result:
            obs = result.pop("__obs__")
            result = dict(result)
        return ChunkResult(
            obj_patch=result,
            obs_bundle=obs,
            elapsed=time.perf_counter() - t0,
        )
    except KeyboardInterrupt:
        # 取消信号：传播给 coordinator → shutdown 短路，不进入串行兜底
        raise
    except Exception as exc:  # noqa: BLE001 -- 单 chunk 计算异常封装回传
        return ChunkResult(
            obj_patch=None,
            obs_bundle=None,
            elapsed=time.perf_counter() - t0,
            error_message=f"{type(exc).__name__}: {str(exc)[:400]}",
            is_fatal=False,
        )
