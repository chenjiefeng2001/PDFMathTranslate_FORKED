"""BabelDOC (PDFMathTranslate-Next / YADT) engine adapter for pdf2zh.

BabelDOC 0.6.x exposes its own ``babeldoc.translator.translator.BaseTranslator``
contract (``translate(text, rate_limit_params=...)`` + an ``llm_translate``
capability probe for its automatic term extractor). pdf2zh translators expose
a simpler ``translate(text)`` contract instead, so the two cannot be plugged
together directly.

This module bridges that gap:

* :func:`make_babeldoc_translator` wraps any pdf2zh translator instance so it
  satisfies the BabelDOC ``BaseTranslator`` interface while proxying every
  call through the pdf2zh engine (its own cache + prompt handling included).
* :func:`run_babeldoc_translation` is the shared high-level runner used by
  both the CLI entry point (``yadt_main``) and the Gradio GUI
  (``RuntimeService._execute_babeldoc``). It drives BabelDOC's
  ``async_translate`` event stream, forwards progress to the caller, honours
  cancellation, and returns the generated result files.

The ``babeldoc`` package stays an *optional* dependency: everything in this
module imports it lazily and raises :class:`BabeldocNotInstalledError` with a
friendly message when it is missing.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
from string import Template
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class BabeldocNotInstalledError(RuntimeError):
    """Raised when the optional ``babeldoc`` package is unavailable.

    The GUI surfaces this as a per-file failure instead of a hard crash so
    users get an actionable hint (``pip install babeldoc``).
    """


class _BabeldocCancelledError(Exception):
    """Internal: the user cancelled the running BabelDOC task."""


#: BabelDOC pipeline stage name -> GUI work-graph stage. Unknown stages fall
#: back to ``translating`` so progress keeps flowing even across releases.
_BABELDOC_STAGE_MAP: Dict[str, str] = {
    "Parse PDF and Create Intermediate Representation": "parsing",
    "Detect Scanned File": "parsing",
    "Parse Page Layout": "analyzing",
    "Parse Table": "analyzing",
    "Parse Paragraphs": "analyzing",
    "Parse Formulas and Styles": "analyzing",
    "Extract Terms": "translating",
    "Translate Paragraphs": "translating",
    "Typesetting": "layouting",
    "Add Fonts": "rendering",
    "Generate drawing instructions": "rendering",
    "Subset font": "rendering",
    "Save PDF": "rendering",
}


def _map_babeldoc_stage(stage: str) -> str:
    """Map a BabelDOC stage name onto a GUI work-graph stage."""
    if not stage:
        return "translating"
    for key, gui_stage in _BABELDOC_STAGE_MAP.items():
        if key in stage:
            return gui_stage
    return "translating"


#: BabelDOC 阶段名 -> 计数单位（细粒度进度展示用，见
#: doc/granular_progress_feasibility_report.md）。匹配规则与
#: ``_BABELDOC_STAGE_MAP`` 一致（子串包含，按声明顺序）。
_BABELDOC_STAGE_UNITS: Dict[str, str] = {
    "Parse PDF and Create Intermediate Representation": "page",
    "Detect Scanned File": "page",
    "Parse Page Layout": "page",
    "Parse Table": "page",
    "Extract Terms": "term",
    "Translate Paragraphs": "paragraph",
}


def _babeldoc_stage_unit(stage: str) -> str:
    """阶段计数单位（page/paragraph/term），未知阶段返回空串。"""
    if not stage:
        return ""
    for key, unit in _BABELDOC_STAGE_UNITS.items():
        if key in stage:
            return unit
    return ""


def _progress_detail_from_event(event: dict) -> Optional[Dict[str, Any]]:
    """把 BabelDOC 进度事件的页级/段落级计数整理成结构化 ``detail``。

    ``async_translate`` 的事件自带 ``stage_current/stage_total``（LayoutParser、
    DetectScannedFile 逐页推进，ILTranslator 按段落推进）；此前适配器只取
    ``overall_progress``，细节被丢弃。字段缺失（旧版本/异常事件）时返回
    ``None``——调用方保持现状行为。
    """
    current = event.get("stage_current")
    total = event.get("stage_total")
    if current is None and total is None:
        return None
    stage_name = str(event.get("stage") or "")
    try:
        cur = int(current or 0)
        tot = int(total or 0)
    except (TypeError, ValueError):
        return None
    return {
        "engine": "babeldoc",
        "raw_stage": stage_name,
        "unit": _babeldoc_stage_unit(stage_name),
        "current": cur,
        "total": tot,
    }


def _resolve_prompt(prompt: Any) -> Optional[Template]:
    """Normalise the GUI/CLI prompt into a ``string.Template`` (or None).

    Accepts ``string.Template``, a raw prompt string, ``PROMPT=...`` prefixed
    text, or a ``{"prompt": ...}``/``{"PROMPT": ...}`` dict (as produced by the
    GUI's KEY=VALUE env parser). Never raises; malformed input -> None.
    """
    if prompt is None:
        return None
    if isinstance(prompt, Template):
        return prompt
    text = prompt
    if isinstance(prompt, dict):
        text = prompt.get("prompt") or prompt.get("PROMPT") or ""
    text = str(text).strip()
    if text.startswith("PROMPT="):
        text = text[len("PROMPT="):].strip()
    if not text:
        return None
    try:
        return Template(text)
    except Exception:  # noqa: BLE001 -- prompt is user input, never fatal
        logger.warning("Invalid prompt template ignored: %.40s", text)
        return None

def make_babeldoc_translator(
    pdf2zh_translator: Any,
    lang_in: str,
    lang_out: str,
    ignore_cache: bool = False,
) -> Any:
    """Wrap a pdf2zh translator instance as a BabelDOC ``BaseTranslator``.

    The returned object satisfies the BabelDOC 0.6.x translator contract
    (``translate`` / ``do_translate`` / ``do_llm_translate`` /
    ``llm_translate``) while proxying every call through the pdf2zh engine.
    ``do_llm_translate`` raises ``NotImplementedError`` so BabelDOC runs its
    paragraph pipeline through the plain ``translate`` path (pdf2zh engines
    are not LLM-prompt translators); ``llm_translate`` still works so the
    automatic term extractor's capability probe succeeds.
    """
    from babeldoc.translator.translator import BaseTranslator as YadtBaseTranslator

    class _Pdf2zhBabeldocTranslator(YadtBaseTranslator):
        """Duck-typed bridge: BabelDOC contract, pdf2zh engine underneath."""

        def __init__(self, inner: Any, lang_in: str, lang_out: str,
                     ignore_cache: bool = False) -> None:
            # Set the attributes BabelDOC's BaseTranslator.__init__ would set,
            # but skip super().__init__: it spins up BabelDOC's own SQLite
            # translation cache, which we deliberately bypass.
            self._inner = inner
            self.name = str(getattr(inner, "name", "pdf2zh"))[:20] or "pdf2zh"
            self.lang_in = lang_in
            self.lang_out = lang_out
            self.ignore_cache = bool(ignore_cache)
            self.translate_call_count = 0
            self.translate_cache_call_count = 0

        def add_cache_impact_parameters(self, k: str, v: Any) -> None:
            # Delegate to the pdf2zh engine's own cache impact tracking.
            return self._inner.add_cache_impact_parameters(k, v)

        def translate(
            self, text: str, ignore_cache: bool = False,
            rate_limit_params: Optional[Dict[str, Any]] = None,
        ) -> str:
            # BabelDOC calls translate(text, rate_limit_params=...); proxy to
            # the pdf2zh engine (own cache + do_translate pipeline).
            try:
                return self._inner.translate(text, ignore_cache=ignore_cache)
            except TypeError:
                return self._inner.translate(text)

        def do_translate(
            self, text: str,
            rate_limit_params: Optional[Dict[str, Any]] = None,
        ) -> str:
            return self._inner.do_translate(text)

        def do_llm_translate(
            self, text: str,
            rate_limit_params: Optional[Dict[str, Any]] = None,
        ) -> str:
            # Keep the LLM prompt path disabled: pdf2zh engines translate
            # plain text, not BabelDOC's structured LLM prompts.
            raise NotImplementedError

        def llm_translate(
            self, text: str,
            rate_limit_params: Optional[Dict[str, Any]] = None,
        ) -> str:
            # Satisfies the AutomaticTermExtractor capability probe while
            # still routing through the pdf2zh engine.
            return self.translate(text)

    return _Pdf2zhBabeldocTranslator(
        pdf2zh_translator, lang_in, lang_out, ignore_cache,
    )



def _build_doclayout_model(pdf_path: Optional[str] = None) -> Any:
    """Build the fused layout model for BabelDOC.

    Combines BabelDOC's default doclayout model with an ``algorithm`` detector
    (PP-DocLayoutV2, or MinerU VLM when ``pdf_path`` is given and
    magic-pdf/MinerU is available -- Step 1.2) so that algorithm blocks are
    skipped during translation (see :mod:`pdf2zh.doclayout_pseudocode`). Any
    failure degrades to ``None``, which makes BabelDOC fall back to its own
    default model -- the engine keeps working either way.
    """
    try:
        from pdf2zh.doclayout_pseudocode import (
            build_pseudo_code_protected_layout_model,
        )

        return build_pseudo_code_protected_layout_model(pdf_path=pdf_path)
    except Exception:  # noqa: BLE001 -- never break the BabelDOC pipeline
        logger.warning(
            "pseudo-code protection model unavailable; "
            "using BabelDOC default layout model",
            exc_info=True,
        )
        return None


def run_babeldoc_translation(
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
    """Translate a document with the BabelDOC layout engine.

    Reuses the pdf2zh translator factory (``build_translator``) so the engine
    dropdown / ``--service`` argument keeps working unchanged. Returns the
    list of ``{"name": ..., "path": ...}`` result files (mono + dual PDF).

    Args:
        source_path: Input PDF/DOCX path (DOCX is converted via LibreOffice).
        lang_in / lang_out: Source / target language codes.
        service: pdf2zh engine name, optionally ``engine:model``.
        pages: Page-range string in pdf2zh/BabelDOC format (``"1-5, 7"``).
        envs: Environment overrides for the engine (KEY -> value).
        prompt: Prompt template (raw string, ``PROMPT=...`` or Template).
        ignore_cache: Bypass the engine translation cache.
        qps: Translation request rate limit (maps to BabelDOC qps).
        output_dir: Output directory (defaults to the input file's directory).
        progress_cb: ``(gui_stage, overall_progress_0_100, babeldoc_stage)``.
        cancelled_check: Optional predicate; when it returns True the task is
        ocr_mode: Scanned-PDF / OCR handling mode (``auto``/``on``/``off``,
            see :mod:`pdf2zh.babeldoc_ocr_mode`). ``None`` falls back to the
            ``PDF2ZH_BABELDOC_OCR`` env var, then ``auto``.

            cancelled cooperatively through BabelDOC's own cancellation.
        debug: Forward to BabelDOC's TranslationConfig for verbose logging.
        glossary_files: Professional-term glossary CSV paths (columns
            ``source,target[,tgt_lng]``, see :mod:`pdf2zh.glossary_store`).
            Loaded via ``Glossary.from_csv`` and forwarded to BabelDOC's
            ``TranslationConfig.glossaries``. Invalid files raise before any
            translation work starts.

    Raises:
        BabeldocNotInstalledError: ``babeldoc`` is not importable.
    """
    try:
        from babeldoc.format.pdf.high_level import (
            async_translate as yadt_async_translate,
        )
        from babeldoc.format.pdf.high_level import init as yadt_init
        from babeldoc.format.pdf.translation_config import (
            TranslationConfig as YadtConfig,
        )
        from babeldoc.format.pdf.translation_config import (
            WatermarkOutputMode,
        )
    except Exception as exc:  # noqa: BLE001 -- surface an actionable message
        # 携带底层异常类型与缺失模块名：frozen 分发中该 ImportError 几乎
        # 都是打包缺件（而非真的没装），只有暴露 ModuleNotFoundError: No
        # module named 'X' 才能定位。
        raise BabeldocNotInstalledError(
            "BabelDOC engine not available "
            f"({type(exc).__name__}: {exc}). If this is a frozen/packaged "
            "build, the bundle is missing a dependency — report which module. "
            "For source installs: `pip install babeldoc` "
            "(or `pip install pdf2zh[babeldoc]`)."
        ) from exc

    # 把后端开关（--backend / PDF2ZH_BABELDOC_BACKEND）同步到 BabelDOC 内部
    # ONNX 会话（幂等）：显式 cuda/dml 时版面分析也走 GPU，而不是硬编码 CPU。
    from pdf2zh.babeldoc_onnx_backend import apply_babeldoc_backend

    apply_babeldoc_backend()

    # 数字编号列表项（1. XXX / 2. XXX）段落拆分（幂等）：避免整个列表被合并
    # 成单一段落整体翻译导致排版错乱。PDF2ZH_BABELDOC_SPLIT_LIST_ITEMS=0 关闭。
    from pdf2zh.babeldoc_list_split import apply_babeldoc_list_split

    apply_babeldoc_list_split()

    # 目录（TOC）行"点线引导 + 页码"公式保护（幂等）：复用 pdf2zh.toc 识别，
    # 把点线/页码拆成公式占位符，使其不参与翻译、重排时原位保留。
    # PDF2ZH_BABELDOC_TOC_PROTECT=0 关闭。
    from pdf2zh.babeldoc_toc_protect import apply_babeldoc_toc_protect

    apply_babeldoc_toc_protect()

    from pdf2zh.converter_docx import convert_to_pdf, is_convertible


    from pdf2zh.high_level import download_remote_fonts
    from pdf2zh.translator import build_translator


    cleanup_paths: List[str] = []
    result = None
    cancelled = False
    try:
        # BabelDOC requires an initialised cache folder before translating.
        yadt_init()
        font_path = download_remote_fonts(lang_out.lower())

        # BabelDOC only ingests PDF; convert DOCX/DOC via LibreOffice when needed.
        work_path = source_path
        if is_convertible(work_path):
            work_path = convert_to_pdf(work_path)
            cleanup_paths.append(work_path)

        pdf2zh_translator = build_translator(
            service,
            lang_in,
            lang_out,
            envs=envs or {},
            prompt=_resolve_prompt(prompt),
            ignore_cache=ignore_cache,
        )
        yadt_translator = make_babeldoc_translator(
            pdf2zh_translator, lang_in, lang_out, ignore_cache,
        )

        # 把 OCR 模式开关解析成 BabelDOC 的三个互斥扫描版开关。
        from pdf2zh.babeldoc_ocr_mode import resolve_ocr_flags

        ocr_workaround, auto_enable_ocr_workaround, skip_scanned_detection = (
            resolve_ocr_flags(ocr_mode, source_path=work_path)
        )

        out_dir = output_dir or os.path.dirname(os.path.abspath(work_path))

        # 专业词表：预检 + 装载（坏文件在翻译开始前快速失败）。
        from pdf2zh.glossary_store import load_babeldoc_glossaries

        glossaries = load_babeldoc_glossaries(glossary_files, lang_out)

        yadt_config = YadtConfig(
            translator=yadt_translator,
            input_file=work_path,
            font=font_path,
            pages=pages or None,
            output_dir=out_dir,
            doc_layout_model=_build_doclayout_model(pdf_path=work_path),
            debug=debug,
            lang_in=lang_in,
            lang_out=lang_out,
            no_dual=False,
            no_mono=False,
            qps=max(1, int(qps or 4)),
            # GUI/CLI render progress themselves; never spawn a rich/tqdm bar.
            use_rich_pbar=False,
            # pdf2zh outputs are plain PDFs, not AI-watermarked documents.
            watermark_output_mode=WatermarkOutputMode.NoWatermark,
            # 双语 PDF 使用交替页模式：原文页、译文页各自独立成页并交替排列，
            # 而不是把原文+译文合并到同一页（BabelDOC 默认 side-by-side 合并）。
            use_alternating_pages_dual=True,
            # 扫描版 PDF 自动启用 OCR workaround 继续翻译，而不是直接抛
            # "Scanned PDF detected" 失败（BabelDOC 默认行为会导致任务报错）。
            # 扫描版 / 无文本层 PDF 的 OCR 处理由 OCR 模式开关决定（不是
            # 硬编码）：auto 自动检测扫描并启用 OCR / on 强制 OCR / off
            # 跳过扫描检测，避免无文本层 PDF 直接抛 "Scanned PDF detected"
            # 失败。环境变量 PDF2ZH_BABELDOC_OCR 可覆盖 GUI/CLI 显式选择。
            ocr_workaround=ocr_workaround,
            auto_enable_ocr_workaround=auto_enable_ocr_workaround,
            skip_scanned_detection=skip_scanned_detection,
            # pdf2zh engines are mostly non-LLM; skip LLM-only term extraction.
            auto_extract_glossary=False,
            # 用户词表（CSV）注入 BabelDOC 的 hyperscan 术语管线。
            glossaries=glossaries or None,
            report_interval=0.2,
        )

        async def _drive() -> Optional[Any]:
            nonlocal cancelled
            # progress_cb 兼容 3 参（旧调用方）与 4 参（带 detail）两种签名
            try:
                cb_takes_detail = (
                    len(inspect.signature(progress_cb).parameters) >= 4
                    if progress_cb is not None else False
                )
            except (TypeError, ValueError):
                cb_takes_detail = False
            async for event in yadt_async_translate(yadt_config):
                if cancelled_check and cancelled_check() and not cancelled:
                    cancelled = True
                    if yadt_config.progress_monitor is not None:
                        # Cooperatively cancel: BabelDOC's pipeline raises at its
                        # next cancellation checkpoint and drains a terminal
                        # event; keep consuming until then.
                        yadt_config.cancel_translation()
                ev_type = event.get("type")
                if ev_type == "error":
                    if cancelled:
                        break  # terminal event emitted by the cancellation path
                    raise RuntimeError(
                        event.get("error") or "BabelDOC translation failed"
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
                                    _map_babeldoc_stage(stage_name), overall,
                                    stage_name, detail,
                                )
                            else:
                                progress_cb(
                                    _map_babeldoc_stage(stage_name), overall,
                                    stage_name,
                                )
                        except Exception:  # noqa: BLE001 -- progress never fatal
                            logger.debug(
                                "babeldoc progress callback failed", exc_info=True
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
        raise _BabeldocCancelledError()
    if result is None:
        return []
    return _collect_result_files(result)


def _collect_result_files(result: Any) -> List[Dict[str, str]]:
    """Turn a BabelDOC ``TranslateResult`` into GUI result-file entries."""
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

