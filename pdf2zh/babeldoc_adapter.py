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
            cancelled cooperatively through BabelDOC's own cancellation.
        debug: Forward to BabelDOC's TranslationConfig for verbose logging.

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
        raise BabeldocNotInstalledError(
            "BabelDOC engine not available. Install it with "
            "`pip install babeldoc` (or `pip install pdf2zh[babeldoc]`) to "
            "use the BabelDOC layout mode."
        ) from exc

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

        out_dir = output_dir or os.path.dirname(os.path.abspath(work_path))
        yadt_config = YadtConfig(
            translator=yadt_translator,
            input_file=work_path,
            font=font_path,
            pages=pages or None,
            output_dir=out_dir,
            doc_layout_model=None,
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
            auto_enable_ocr_workaround=True,
            # pdf2zh engines are mostly non-LLM; skip LLM-only term extraction.
            auto_extract_glossary=False,
            report_interval=0.2,
        )

        async def _drive() -> Optional[Any]:
            nonlocal cancelled
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
                            progress_cb(
                                _map_babeldoc_stage(stage_name), overall, stage_name,
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

