"""pdf2zh_next-kernel adapter for the BabelDOC layout engine (GUI path).

The Gradio GUI's BabelDOC mode historically ran on the *legacy* pipeline:
``RuntimeService._execute_babeldoc`` wrapped a ``pdf2zh.build_translator``
instance in a BabelDOC ``BaseTranslator`` shim (see
:mod:`pdf2zh.babeldoc_adapter`) and drove BabelDOC's layout engine directly.
That meant BabelDOC mode never exercised the *modified* pdf2zh_next kernel
shipped in ``pdf2zh/kernel/PDFMathTranslate-next.git`` (fonts, layout,
rendering fixes), so BabelDOC jobs silently bypassed the new pipeline.

This module bridges that gap: it maps the GUI engine selection (``service``
plus the KEY=VALUE env lines) onto a ``pdf2zh_next.SettingsModel`` and drives
the exact pipeline the modified kernel uses --
``pdf2zh_next.high_level.create_babeldoc_config`` + BabelDOC ``async_translate``.

The public entry point :func:`run_babeldoc_next_translation` mirrors the
signature and result contract of
``babeldoc_adapter.run_babeldoc_translation`` so the runtime service can prefer
the new kernel and fall back to the legacy shim only when the kernel is
genuinely unavailable.
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

#: Bundled modified pdf2zh_next kernel directory (relative to this module).
_NEXT_KERNEL_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "kernel",
    "PDFMathTranslate-next.git",
)

_KERNEL_INSERTED = False
_KERNEL_LOCK = threading.Lock()


class BabeldocNextUnavailableError(RuntimeError):
    """Raised when the modified pdf2zh_next kernel cannot be loaded.

    The runtime service treats this as a *soft* failure and falls back to the
    legacy BabelDOC pipeline instead of aborting the task.
    """


class _BabeldocNextCancelledError(Exception):
    """Internal: the user cancelled a running BabelDOC-next task."""


def is_next_kernel_available() -> bool:
    """Whether the modified pdf2zh_next kernel can be loaded here.

    Mirrors :func:`_ensure_next_kernel` without side effects: bundled kernel
    directory present, or a PyPI-installed ``pdf2zh_next`` package. Used by
    tests to skip cleanly on fresh checkouts — the nested git repo under
    ``pdf2zh/kernel/`` ships empty in plain clones.
    """
    if os.path.isdir(os.path.join(_NEXT_KERNEL_DIR, "pdf2zh_next")):
        return True
    try:
        return importlib.util.find_spec("pdf2zh_next") is not None
    except (ImportError, ValueError):
        return False


def _ensure_next_kernel() -> str:
    """Idempotently expose the bundled pdf2zh_next kernel on ``sys.path``.

    Prefers the fork's modified kernel directory over any PyPI-installed
    ``pdf2zh_next`` package; falls back to an installed package when the
    bundled kernel is missing (zipapp / packaged builds).
    """
    global _KERNEL_INSERTED
    with _KERNEL_LOCK:
        if _KERNEL_INSERTED:
            return _NEXT_KERNEL_DIR
        if os.path.isdir(os.path.join(_NEXT_KERNEL_DIR, "pdf2zh_next")):
            if _NEXT_KERNEL_DIR not in sys.path:
                sys.path.insert(0, _NEXT_KERNEL_DIR)
            _KERNEL_INSERTED = True
            return _NEXT_KERNEL_DIR
        if importlib.util.find_spec("pdf2zh_next") is not None:
            _KERNEL_INSERTED = True
            return "<installed>"
        raise BabeldocNextUnavailableError(
            "pdf2zh_next kernel not found: bundled kernel missing at "
            f"{_NEXT_KERNEL_DIR!r} and no installed pdf2zh_next package."
        )


def _env_get(
    envs: Optional[Dict[str, Any]],
    *names: str,
    default: Optional[str] = None,
) -> Optional[str]:
    """Case-insensitive env lookup against request envs, then ``os.environ``.

    The GUI lower-cases every KEY=VALUE env line (see
    ``gui.worker._parse_env_lines``), so lookups must tolerate ``OpenAI_API_Key``
    as well as ``OPENAI_API_KEY``.
    """
    if envs:
        wanted = {name.lower() for name in names}
        for key, value in envs.items():
            if str(key).lower() in wanted and value not in (None, ""):
                return str(value)
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return default


def _resolve_prompt_text(prompt: Any) -> Optional[str]:
    """Normalise a GUI/CLI prompt into a plain string (or None)."""
    if prompt is None:
        return None
    if isinstance(prompt, dict):
        text = prompt.get("prompt") or prompt.get("PROMPT") or ""
    else:
        text = str(prompt)
    text = text.strip()
    if text.startswith("PROMPT="):
        text = text[len("PROMPT=") :].strip()
    return text or None


def _build_engine_settings(service: str, envs: Optional[Dict[str, Any]]) -> Any:
    """Map a GUI ``service`` selection onto a pdf2zh_next engine settings.

    Unknown engines raise :class:`BabeldocNextUnavailableError` so the runtime
    service can fall back to the legacy pipeline for engines the modified
    kernel does not implement.
    """
    from pdf2zh_next.config.translate_engine_model import (
        AnythingLLMSettings,
        AzureOpenAISettings,
        AzureSettings,
        BingSettings,
        ClaudeCodeSettings,
        DeepLSettings,
        DeepSeekSettings,
        DifySettings,
        GeminiSettings,
        GoogleSettings,
        GrokSettings,
        GroqSettings,
        ModelScopeSettings,
        OllamaSettings,
        OpenAISettings,
        OpenAICompatibleSettings,
        QwenMtSettings,
        SiliconFlowSettings,
        TencentSettings,
        XinferenceSettings,
        ZhipuSettings,
    )

    name, sep, model = (service or "google").strip().partition(":")
    name = name.strip().lower()
    model = (model.strip() or None) if sep else None

    if name == "google":
        return GoogleSettings()
    if name == "bing":
        return BingSettings()
    if name == "deepl":
        return DeepLSettings(
            deepl_auth_key=_env_get(envs, "DEEPL_AUTH_KEY"),
        )
    if name == "ollama":
        return OllamaSettings(
            ollama_host=_env_get(envs, "OLLAMA_HOST") or "http://localhost:11434",
            ollama_model=model or _env_get(envs, "OLLAMA_MODEL") or "gemma2",
        )
    if name == "xinference":
        return XinferenceSettings(
            xinference_host=_env_get(envs, "XINFERENCE_HOST"),
            xinference_model=model
            or _env_get(envs, "XINFERENCE_MODEL")
            or "gemma-2-it",
        )
    if name == "openai":
        return OpenAISettings(
            openai_api_key=_env_get(envs, "OPENAI_API_KEY"),
            openai_model=model or _env_get(envs, "OPENAI_MODEL") or "gpt-4o-mini",
            openai_base_url=_env_get(envs, "OPENAI_BASE_URL"),
        )
    if name in ("azure_openai", "azureopenai"):
        return AzureOpenAISettings(
            azure_openai_api_key=_env_get(envs, "AZURE_OPENAI_API_KEY"),
            azure_openai_model=model
            or _env_get(envs, "AZURE_OPENAI_MODEL")
            or "gpt-4o-mini",
            azure_openai_base_url=_env_get(envs, "AZURE_OPENAI_BASE_URL"),
            azure_openai_api_version=(
                _env_get(envs, "AZURE_OPENAI_API_VERSION") or "2024-06-01"
            ),
        )
    if name in ("microsoft", "azure"):
        # GUI "Microsoft" = Azure Translator service.
        return AzureSettings(
            azure_api_key=_env_get(envs, "AZURE_API_KEY"),
            azure_endpoint=(
                _env_get(envs, "AZURE_ENDPOINT") or "https://api.translator.azure.cn"
            ),
        )
    if name == "gemini":
        return GeminiSettings(
            gemini_api_key=_env_get(envs, "GEMINI_API_KEY"),
            gemini_model=model or _env_get(envs, "GEMINI_MODEL") or "gemini-1.5-flash",
        )
    if name == "deepseek":
        return DeepSeekSettings(
            deepseek_api_key=_env_get(envs, "DEEPSEEK_API_KEY"),
            deepseek_model=model or _env_get(envs, "DEEPSEEK_MODEL") or "deepseek-chat",
        )
    if name == "zhipu":
        return ZhipuSettings(
            zhipu_api_key=_env_get(envs, "ZHIPU_API_KEY"),
            zhipu_model=model or _env_get(envs, "ZHIPU_MODEL") or "glm-4-flash",
        )
    if name == "modelscope":
        return ModelScopeSettings(
            modelscope_api_key=_env_get(envs, "MODELSCOPE_API_KEY"),
            modelscope_model=model
            or _env_get(envs, "MODELSCOPE_MODEL")
            or "Qwen/Qwen2.5-32B-Instruct",
        )
    if name in ("silicon", "siliconflow"):
        return SiliconFlowSettings(
            siliconflow_api_key=_env_get(
                envs,
                "SILICONFLOW_API_KEY",
                "SILICON_API_KEY",
            ),
            siliconflow_model=model
            or _env_get(
                envs,
                "SILICONFLOW_MODEL",
                "SILICON_MODEL",
            )
            or "Qwen/Qwen2.5-7B-Instruct",
            siliconflow_base_url=_env_get(envs, "SILICONFLOW_BASE_URL"),
        )
    if name == "tencent":
        return TencentSettings(
            tencentcloud_secret_id=_env_get(envs, "TENCENT_SECRET_ID"),
            tencentcloud_secret_key=_env_get(envs, "TENCENT_SECRET_KEY"),
        )
    if name == "dify":
        return DifySettings(
            dify_url=_env_get(envs, "DIFY_API_URL"),
            dify_apikey=_env_get(envs, "DIFY_API_KEY"),
        )
    if name == "anythingllm":
        return AnythingLLMSettings(
            anythingllm_url=_env_get(envs, "ANYTHINGLLM_API_URL"),
            anythingllm_apikey=_env_get(envs, "ANYTHINGLLM_API_KEY"),
        )

    if name == "grok":
        return GrokSettings(
            grok_api_key=_env_get(envs, "GROK_API_KEY"),
            grok_model=model or _env_get(envs, "GROK_MODEL") or "grok-2-1212",
        )
    if name == "groq":
        return GroqSettings(
            groq_api_key=_env_get(envs, "GROQ_API_KEY"),
            groq_model=model
            or _env_get(envs, "GROQ_MODEL")
            or "llama-3-3-70b-versatile",
        )
    if name == "qwen":
        # GUI "Qwen" maps onto the QwenMT engine (ALI_* legacy env names too).
        return QwenMtSettings(
            qwenmt_api_key=_env_get(envs, "QWENMT_API_KEY", "ALI_API_KEY"),
            qwenmt_model=model
            or _env_get(envs, "QWENMT_MODEL", "ALI_MODEL")
            or "qwen-mt-plus",
            qwenmt_base_url=_env_get(envs, "QWENMT_BASE_URL", "ALI_BASE_URL"),
        )
    if name in ("aliyun", "aliyun-dashscope"):
        from pdf2zh_next.config.translate_engine_model import AliyunDashScopeSettings

        return AliyunDashScopeSettings(
            aliyun_dashscope_api_key=_env_get(
                envs,
                "ALIYUN_DASHSCOPE_API_KEY",
                "ALI_API_KEY",
            ),
            aliyun_dashscope_model=model
            or _env_get(
                envs,
                "ALIYUN_DASHSCOPE_MODEL",
                "ALI_MODEL",
            )
            or "qwen-plus-latest",
            aliyun_dashscope_base_url=_env_get(
                envs,
                "ALIYUN_DASHSCOPE_BASE_URL",
                "ALI_BASE_URL",
            ),
        )
    if name == "claude":
        # pdf2zh_next implements Claude through the `claude` Code CLI.
        return ClaudeCodeSettings(
            claude_code_path=_env_get(envs, "CLAUDE_CODE_PATH") or "claude",
            claude_code_model=model or _env_get(envs, "CLAUDE_CODE_MODEL") or "sonnet",
        )
    if name == "openai-compatible":
        return OpenAICompatibleSettings(
            openai_compatible_api_key=_env_get(
                envs,
                "OPENAI_COMPATIBLE_API_KEY",
            ),
            openai_compatible_base_url=_env_get(
                envs,
                "OPENAI_COMPATIBLE_BASE_URL",
            ),
            openai_compatible_model=model
            or _env_get(
                envs,
                "OPENAI_COMPATIBLE_MODEL",
            )
            or "gpt-4o-mini",
        )

    raise BabeldocNextUnavailableError(
        f"Engine {name!r} has no pdf2zh_next kernel mapping"
    )


def build_next_settings(
    service: str,
    lang_in: str,
    lang_out: str,
    envs: Optional[Dict[str, Any]] = None,
    prompt: Optional[Any] = None,
    pages: Optional[str] = None,
    qps: int = 4,
    output_dir: Optional[str] = None,
    ignore_cache: bool = False,
    debug: bool = False,
    ocr_mode: Optional[str] = None,
    source_path: Optional[str] = None,
    glossary_files: Optional[List[str]] = None,
) -> Any:
    """Assemble a ``pdf2zh_next.SettingsModel`` from GUI request fields.

    Mirrors the flags pdf2zh_next's CLI applies for a PDF job: no watermark,
    auto term extraction disabled, table text translation left off (avoids
    loading the RapidOCR model on the GUI path), and ``basic.debug`` enabled so
    the async stream runs in-process (no subprocess spawn under Gradio).

    ``ocr_mode`` (``auto``/``on``/``off``) controls BabelDOC's scanned-PDF
    handling: ``auto`` auto-detects heavily-scanned pages and enables OCR,
    ``on`` forces OCR for every PDF, ``off`` skips scan detection entirely.

    ``glossary_files`` maps onto the kernel's ``translation.glossaries``
    (comma-separated CSV paths; consumed by ``pdf2zh_next.high_level
    ._get_glossaries`` → ``Glossary.from_csv``, filtered by target language).
    Paths are pre-validated here so bad files fail before the kernel spins up.
    """
    _ensure_next_kernel()
    from pdf2zh_next.config.model import (
        BasicSettings,
        PDFSettings,
        SettingsModel,
        TranslationSettings,
    )

    from pdf2zh.babeldoc_ocr_mode import resolve_ocr_flags

    ocr_workaround, auto_enable_ocr_workaround, skip_scanned_detection = (
        resolve_ocr_flags(ocr_mode, source_path=source_path)
    )

    engine_settings = _build_engine_settings(service, envs)

    # 专业词表：内核 translation.glossaries 为逗号分隔 CSV 路径串。
    glossary_csvs: Optional[str] = None
    if glossary_files:
        from pdf2zh.glossary_store import parse_csv

        for p in glossary_files:
            parse_csv(p)  # 预检：坏文件在进入内核前给出可读错误
        glossary_csvs = ",".join(str(p) for p in glossary_files)

    return SettingsModel(
        basic=BasicSettings(debug=bool(debug), input_files=set()),
        translation=TranslationSettings(
            lang_in=lang_in or "en",
            lang_out=lang_out or "zh",
            qps=max(1, int(qps or 4)),
            ignore_cache=bool(ignore_cache),
            output=output_dir,
            custom_system_prompt=_resolve_prompt_text(prompt),
            no_auto_extract_glossary=True,
            glossaries=glossary_csvs,
        ),
        pdf=PDFSettings(
            pages=pages or None,
            watermark_output_mode="no_watermark",
            translate_table_text=False,
            # 双语 PDF 使用交替页模式：原文页、译文页各自独立成页并交替排列，
            # 而不是把原文+译文合并到同一页（BabelDOC 默认 side-by-side 合并）。
            use_alternating_pages_dual=True,
            # 扫描版 / 无文本层 PDF 的 OCR 处理由 OCR 模式开关决定（不是
            # 硬编码）：auto 自动检测扫描并启用 OCR / on 强制 OCR / off
            # 跳过扫描检测，避免无文本层 PDF 直接抛 "Scanned PDF detected"
            # 失败。环境变量 PDF2ZH_BABELDOC_OCR 可覆盖 GUI/CLI 显式选择。
            ocr_workaround=ocr_workaround,
            auto_enable_ocr_workaround=auto_enable_ocr_workaround,
            skip_scanned_detection=skip_scanned_detection,
        ),
        translate_engine_settings=engine_settings,
    )


def run_babeldoc_next_translation(
    source_path: str,
    lang_in: str,
    lang_out: str,
    service: str,
    pages: Optional[str] = None,
    envs: Optional[Dict[str, Any]] = None,
    prompt: Optional[Any] = None,
    ignore_cache: bool = False,
    qps: int = 4,
    output_dir: Optional[str] = None,
    progress_cb: Optional[Callable[[str, float, str], None]] = None,
    cancelled_check: Optional[Callable[[], bool]] = None,
    debug: bool = False,
    ocr_mode: Optional[str] = None,
    glossary_files: Optional[List[str]] = None,
) -> List[Dict[str, str]]:
    """Translate a document with the modified pdf2zh_next kernel pipeline.

    This is the drop-in counterpart of
    :func:`babeldoc_adapter.run_babeldoc_translation` with the same arguments
    and return contract (``[{"name": ..., "path": ...}]``), but it drives the
    *modified* kernel: engine settings -> ``SettingsModel`` ->
    ``create_babeldoc_config`` -> BabelDOC ``async_translate`` event stream.

    Raises:
        BabeldocNextUnavailableError: the pdf2zh_next kernel (or a required
            BabelDOC import) is unavailable; callers fall back to the legacy
            pipeline.
        _BabeldocNextCancelledError: the user cancelled the task (internal).
    """
    _ensure_next_kernel()
    try:
        from babeldoc.format.pdf.high_level import (
            async_translate as babeldoc_translate,
        )
        from pdf2zh_next.high_level import create_babeldoc_config
    except Exception as exc:  # noqa: BLE001 -- surface an actionable message
        raise BabeldocNextUnavailableError(
            "pdf2zh_next kernel / BabelDOC engine not available: " f"{exc}"
        ) from exc

    try:
        from pdf2zh.babeldoc_adapter import (
            _collect_result_files,
            _map_babeldoc_stage,
            _progress_detail_from_event,
        )
    except Exception:  # noqa: BLE001 -- keep the adapter self-contained
        from pdf2zh.babeldoc_adapter import (  # noqa: F401
            _map_babeldoc_stage,
            _progress_detail_from_event,
        )

        def _collect_result_files(result: Any) -> List[Dict[str, str]]:
            files: List[Dict[str, str]] = []
            seen: set = set()
            for attr in (
                "mono_pdf_path",
                "dual_pdf_path",
                "no_watermark_mono_pdf_path",
                "no_watermark_dual_pdf_path",
            ):
                path = getattr(result, attr, None)
                if not path:
                    continue
                path = os.fspath(path)
                if path in seen or not os.path.exists(path):
                    continue
                seen.add(path)
                files.append({"name": os.path.basename(path), "path": path})
            return files

    from pdf2zh.converter_docx import convert_to_pdf, is_convertible

    # 把后端开关（--backend / PDF2ZH_BABELDOC_BACKEND）同步到 BabelDOC 内部
    # ONNX 会话（幂等）：显式 cuda/dml 时版面分析也走 GPU，而不是硬编码 CPU。
    from pdf2zh.babeldoc_onnx_backend import apply_babeldoc_backend

    apply_babeldoc_backend()

    # 进程本地线程预算（有界并发而非串行）：保留两边多线程/多任务能力，
    # 同时把 CPU/GPU 并发乘积封顶，避免 oversubscription 表现为“卡死”。
    # 仅设线程预算，不做进程级 CUDA 重置（进程内路径可能与 pdf2zh 共享进程）。
    from pdf2zh.gpu_governor import (  # noqa: PLC0415
        apply_process_local_thread_budget,
    )

    apply_process_local_thread_budget("babeldoc-adapter")

    # 数字编号列表项（1. XXX / 2. XXX）段落拆分（幂等）：避免整个列表被合并
    # 成单一段落整体翻译导致排版错乱。PDF2ZH_BABELDOC_SPLIT_LIST_ITEMS=0 关闭。
    from pdf2zh.babeldoc_list_split import apply_babeldoc_list_split

    apply_babeldoc_list_split()

    # 目录（TOC）行"点线引导 + 页码"公式保护（幂等）：复用 pdf2zh.toc 识别，
    # 把点线/页码拆成公式占位符，使其不参与翻译、重排时原位保留。
    # PDF2ZH_BABELDOC_TOC_PROTECT=0 关闭。
    from pdf2zh.babeldoc_toc_protect import apply_babeldoc_toc_protect

    apply_babeldoc_toc_protect()

    # 公式检测过度识别缓解（幂等）：BabelDOC 把色块/嵌入字体上的「含公式文本块」
    # 整块判为公式而漏翻并污染后续段落；含普通文本信号的公式块转回翻译。
    # PDF2ZH_BABELDOC_FORMULA_PROTECT=0 关闭。
    from pdf2zh.babeldoc_formula_protect import apply_babeldoc_formula_protect

    apply_babeldoc_formula_protect()

    cleanup_paths: List[str] = []
    result = None
    cancelled = False

    try:
        # BabelDOC only ingests PDF; convert DOCX/DOC via LibreOffice when needed.
        work_path = source_path
        if is_convertible(work_path):
            work_path = convert_to_pdf(work_path)
            cleanup_paths.append(work_path)

        out_dir = output_dir or os.path.dirname(os.path.abspath(work_path))

        settings = build_next_settings(
            service=service,
            lang_in=lang_in,
            lang_out=lang_out,
            envs=envs,
            prompt=prompt,
            pages=pages,
            qps=qps,
            output_dir=out_dir,
            ignore_cache=ignore_cache,
            debug=debug,
            ocr_mode=ocr_mode,
            source_path=work_path,
            glossary_files=glossary_files,
        )
        settings.validate_settings()
        config = create_babeldoc_config(settings, Path(work_path))
        # Fuse the default doclayout model with a PP-DocLayoutV2 pseudo-code
        # (``algorithm``) detector so algorithm blocks survive translation
        # untouched. The fused builder returns ``None`` when protection is
        # disabled (e.g. documents above the 30-page auto-skip cap); in that
        # case — and on any build error — fall back to BabelDOC's own default
        # layout model, because ``create_babeldoc_config`` deliberately leaves
        # ``doc_layout_model=None`` and the pipeline would otherwise crash
        # with ``'NoneType' object has no attribute 'handle_document'``.
        try:
            from pdf2zh.doclayout_pseudocode import (
                build_pseudo_code_protected_layout_model,
            )

            fused_model = build_pseudo_code_protected_layout_model(pdf_path=work_path)
            if fused_model is not None:
                config.doc_layout_model = fused_model
        except Exception:  # noqa: BLE001 -- never break the BabelDOC pipeline
            logger.warning(
                "pseudo-code protection model unavailable; "
                "using BabelDOC default layout model",
                exc_info=True,
            )
        if config.doc_layout_model is None:
            try:
                from babeldoc.docvision.doclayout import (
                    DocLayoutModel as _BabelDocDocLayoutModel,
                )

                config.doc_layout_model = _BabelDocDocLayoutModel.load_onnx()
            except Exception:  # noqa: BLE001 -- last-resort diagnostics only
                logger.error(
                    "failed to load BabelDOC default doclayout model; "
                    "the translation will fail",
                    exc_info=True,
                )

        async def _drive() -> Optional[Any]:
            nonlocal cancelled
            # progress_cb 兼容 3 参（旧调用方）与 4 参（带 detail）两种签名
            try:
                cb_takes_detail = (
                    len(inspect.signature(progress_cb).parameters) >= 4
                    if progress_cb is not None
                    else False
                )
            except (TypeError, ValueError):
                cb_takes_detail = False
            async for event in babeldoc_translate(config):
                if cancelled_check and cancelled_check() and not cancelled:
                    cancelled = True
                    try:
                        # Cooperatively cancel: BabelDOC's pipeline raises at its
                        # next cancellation checkpoint; keep consuming until then.
                        config.cancel_translation()
                    except Exception:  # noqa: BLE001 -- best effort
                        logger.debug("babeldoc-next cancel failed", exc_info=True)
                ev_type = event.get("type")
                if ev_type == "error":
                    if cancelled:
                        break  # terminal event emitted by the cancellation path
                    raise RuntimeError(
                        event.get("error") or "BabelDOC-next translation failed"
                    )
                if ev_type == "finish":
                    return event.get("translate_result")
                if ev_type in ("progress_start", "progress_update", "progress_end"):
                    stage_name = event.get("stage") or ""
                    overall = float(event.get("overall_progress") or 0.0)
                    if progress_cb:
                        try:
                            detail = _progress_detail_from_event(event)
                            if cb_takes_detail:
                                progress_cb(
                                    _map_babeldoc_stage(stage_name),
                                    overall,
                                    stage_name,
                                    detail,
                                )
                            else:
                                progress_cb(
                                    _map_babeldoc_stage(stage_name),
                                    overall,
                                    stage_name,
                                )
                        except Exception:  # noqa: BLE001 -- progress never fatal
                            logger.debug(
                                "babeldoc-next progress callback failed",
                                exc_info=True,
                            )
            return None

        result = asyncio.run(_drive())
    except asyncio.CancelledError:
        cancelled = True
    finally:
        # NOTE: We deliberately do NOT call ``babeldoc.const.close_process_pool``
        # here. BabelDOC keeps a *process-global* multiprocessing pool that all
        # concurrent translations share, and it closes it itself after the
        # layout stage (``babeldoc/format/pdf/high_level.py``). If one task
        # fails (or finishes) while another task is still using the pool, an
        # explicit close here would hang / break the surviving task -- which
        # manifested as "translation stopped working after an error".
        for path in cleanup_paths:
            try:
                os.unlink(path)
            except OSError:
                pass

    if cancelled:
        raise _BabeldocNextCancelledError()
    if result is None:
        return []
    return _collect_result_files(result)


# ── 子进程隔离（性能基准报告 P0 #3 / Bug #4）───────────────────────────────
#
# babeldoc 内核在进程内连续任务 RSS 稳定增长（实测 6 次 +2GB，ONNX 会话 /
# il_creater / BabelDOC 内部缓存无法确定性释放）。PDF2ZH_BABELDOC_SUBPROCESS=1
# 时改走 :func:`run_babeldoc_next_translation_subprocess`：每任务一个全新
# 子进程（NDJSON 协议，见 pdf2zh.babeldoc_next_worker），进程退出即把全部
# 原生内存归还 OS。签名/返回契约与进程内版本完全一致，运行时服务可无感切换。

#: 子进程模式下轮询取消信号的间隔（秒）。
_SUBPROCESS_CANCEL_POLL = 1.0


def run_babeldoc_next_translation_subprocess(
    source_path: str,
    lang_in: str,
    lang_out: str,
    service: str,
    pages: Optional[str] = None,
    envs: Optional[Dict[str, Any]] = None,
    prompt: Optional[Any] = None,
    ignore_cache: bool = False,
    qps: int = 4,
    output_dir: Optional[str] = None,
    progress_cb: Optional[Callable[[str, float, str], None]] = None,
    cancelled_check: Optional[Callable[[], bool]] = None,
    debug: bool = False,
    ocr_mode: Optional[str] = None,
    glossary_files: Optional[List[str]] = None,
) -> List[Dict[str, str]]:
    """在一次性子进程中执行 :func:`run_babeldoc_next_translation`。

    契约与进程内版本一致（含 ``BabeldocNextUnavailableError`` 软失败语义
    与取消行为）；差异：
      - ``cancelled_check`` 由看门狗线程轮询，命中即 kill 子进程并以
        ``_BabeldocNextCancelledError`` 结束（内核协作式取消点不可跨进程）；
      - 进度经子进程 NDJSON 帧转发（0.2s 事件节奏不变）。
    """

    import json as _json
    import subprocess as _subprocess

    payload = {
        "source_path": source_path,
        "lang_in": lang_in,
        "lang_out": lang_out,
        "service": service,
        "pages": pages,
        "envs": envs,
        "prompt": prompt if isinstance(prompt, (str, type(None))) else str(prompt),
        "ignore_cache": bool(ignore_cache),
        "qps": int(qps or 4),
        "output_dir": output_dir,
        "debug": bool(debug),
        "ocr_mode": ocr_mode,
        "glossary_files": list(glossary_files or []),
    }

    proc = _subprocess.Popen(
        [
            sys.executable,
            "-m",
            # 测试可注入轻量 stub worker（避免真实内核导入成本）。
            os.environ.get(
                "PDF2ZH_BABELDOC_WORKER_MODULE", "pdf2zh.babeldoc_next_worker"
            ),
        ],
        stdin=_subprocess.PIPE,
        stdout=_subprocess.PIPE,
        text=True,
        encoding="utf-8",
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )
    final: Dict[str, Any] = {}
    reader_err: List[str] = []

    def _read_stdout() -> None:
        try:
            for line in proc.stdout or []:
                line = line.strip()
                if not line:
                    continue
                try:
                    frame = _json.loads(line)
                except ValueError:
                    continue
                if frame.get("progress"):
                    if progress_cb is not None:
                        try:
                            progress_cb(
                                str(frame.get("stage") or ""),
                                float(frame.get("pct") or 0.0),
                                str(frame.get("msg") or ""),
                                frame.get("detail"),
                            )
                        except Exception:  # noqa: BLE001 -- progress never fatal
                            pass
                else:
                    final.update(frame)
                    return
        except Exception as exc:  # noqa: BLE001 -- 读流失败记录后按退出码处理
            reader_err.append(str(exc))

    reader = threading.Thread(
        target=_read_stdout, name="babeldoc-worker-reader", daemon=True
    )
    reader.start()

    def _cancel_watcher() -> None:
        while proc.poll() is None:
            try:
                if cancelled_check is not None and cancelled_check():
                    proc.kill()
                    return
            except Exception:  # noqa: BLE001 -- 取消探测失败不致命
                pass
            threading.Event().wait(_SUBPROCESS_CANCEL_POLL)

    watcher = threading.Thread(
        target=_cancel_watcher, name="babeldoc-worker-watch", daemon=True
    )
    watcher.start()

    try:
        try:
            proc.stdin.write(_json.dumps(payload, ensure_ascii=False, default=str))
            proc.stdin.close()
        except Exception:  # noqa: BLE001 -- worker 提前退出时写 stdin 失败
            pass
        rc = proc.wait()
        reader.join(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()

    if cancelled_check is not None:
        try:
            if cancelled_check():
                raise _BabeldocNextCancelledError()
        except _BabeldocNextCancelledError:
            raise
        except Exception:  # noqa: BLE001
            pass

    if not final:
        raise RuntimeError(
            "babeldoc-next subprocess produced no result "
            f"(exit={rc}, reader_error={reader_err[:1]})"
        )
    if final.get("ok"):
        return [dict(f) for f in (final.get("files") or [])]
    error_type = str(final.get("error_type") or "")
    message = str(final.get("error") or "babeldoc-next subprocess failed")
    if error_type == "BabeldocNextUnavailableError":
        raise BabeldocNextUnavailableError(message)
    raise RuntimeError(message)
