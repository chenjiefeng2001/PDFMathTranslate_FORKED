#!/usr/bin/env python3
"""A command line tool for extracting text and images from PDF and
output it to plain text, html, xml or tags.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from string import Template
from typing import List, Optional

from pdf2zh import __version__, log
from pdf2zh.converter_docx import convert_to_pdf, is_convertible

logger = logging.getLogger(__name__)


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, add_help=True)
    parser.add_argument(
        "files",
        type=str,
        default=None,
        nargs="*",
        help="One or more paths to PDF/Word files.",
    )
    parser.add_argument(
        "--version",
        "-v",
        action="version",
        version=f"pdf2zh v{__version__}",
    )
    parser.add_argument(
        "--debug",
        "-d",
        default=False,
        action="store_true",
        help="Use debug logging level.",
    )
    parse_params = parser.add_argument_group(
        "Parser",
        description="Used during PDF parsing",
    )
    parse_params.add_argument(
        "--pages",
        "-p",
        type=str,
        help="The list of page numbers to parse.",
    )
    parse_params.add_argument(
        "--vfont",
        "-f",
        type=str,
        default="",
        help="The regex to math font name of formula.",
    )
    parse_params.add_argument(
        "--vchar",
        "-c",
        type=str,
        default="",
        help="The regex to math character of formula.",
    )
    parse_params.add_argument(
        "--lang-in",
        "-li",
        type=str,
        default="en",
        help="The code of source language.",
    )
    parse_params.add_argument(
        "--lang-out",
        "-lo",
        type=str,
        default="zh",
        help="The code of target language.",
    )
    parse_params.add_argument(
        "--service",
        "-s",
        type=str,
        default="google",
        help="The service to use for translation.",
    )
    parse_params.add_argument(
        "--output",
        "-o",
        type=str,
        default="",
        help="Output directory for files.",
    )
    parse_params.add_argument(
        "--thread",
        "-t",
        type=int,
        default=4,
        help="The number of threads to execute translation.",
    )
    parse_params.add_argument(
        "--parallel-workers",
        type=int,
        default=None,
        help="Number of parallel page-processing worker processes (default 4). "
        "Lower it (e.g. 2) on memory-constrained machines; also honored via "
        "the PDF2ZH_PARALLEL_WORKERS env var.",
    )
    parse_params.add_argument(
        "--no-parallel",
        action="store_true",
        help="Disable parallel page processing (serial fallback). Also set via "
        "PDF2ZH_NO_PARALLEL=1; PDF2ZH_PARALLEL=1 forces it on.",
    )
    parse_params.add_argument(
        "--interactive",
        "-i",
        action="store_true",
        help="Interact with GUI.",
    )
    parse_params.add_argument(
        "--share",
        action="store_true",
        help="Enable Gradio Share",
    )
    parse_params.add_argument(
        "--flask",
        action="store_true",
        help="flask",
    )
    parse_params.add_argument(
        "--celery",
        action="store_true",
        help="celery",
    )
    parse_params.add_argument(
        "--authorized",
        type=str,
        nargs="+",
        help="user name and password.",
    )
    parse_params.add_argument(
        "--prompt",
        type=str,
        help="user custom prompt.",
    )

    parse_params.add_argument(
        "--compatible",
        "-cp",
        action="store_true",
        help="Convert the PDF file into PDF/A format to improve compatibility.",
    )

    parse_params.add_argument(
        "--onnx",
        type=str,
        help="custom onnx model path.",
    )

    parse_params.add_argument(
        "--backend",
        type=str,
        choices=["auto", "cpu", "cuda", "dml"],
        default="auto",
        help="ONNX Runtime execution provider: auto, cpu, cuda, dml.",
    )

    parse_params.add_argument(
        "--serverport",
        type=int,
        help="custom WebUI port.",
    )

    parse_params.add_argument(
        "--proxy",
        type=str,
        default="",
        help="HTTP(S) proxy for translation requests, e.g. http://127.0.0.1:7890. "
        "Also honored via the PDF2ZH_PROXY env var or the WebUI environment box.",
    )

    parse_params.add_argument(
        "--max-file-size",
        type=int,
        default=None,
        help="WebUI upload size limit in MB (default 100). "
        "Can also be set via the PDF2ZH_MAX_FILE_SIZE env var.",
    )

    parse_params.add_argument(
        "--dir",
        action="store_true",
        help="translate directory.",
    )

    parse_params.add_argument(
        "--config",
        type=str,
        help="config file.",
    )

    parse_params.add_argument(
        "--mode",
        type=str,
        choices=["fast", "precise"],
        default="fast",
        help="Translation mode: fast (v1) or precise (v2, requires pdf2zh_next).",
    )

    parse_params.add_argument(
        "--babeldoc",
        default=False,
        action="store_true",
        help="Use experimental backend babeldoc.",
    )

    parse_params.add_argument(
        "--skip-subset-fonts",
        action="store_true",
        help="Skip font subsetting. "
        "This option can improve compatibility "
        "but will increase the size of the output file.",
    )

    parse_params.add_argument(
        "--ignore-cache",
        action="store_true",
        help="Ignore cache and force retranslation.",
    )

    parse_params.add_argument(
        "--mcp", action="store_true", help="Launch pdf2zh MCP server in STDIO mode"
    )

    parse_params.add_argument(
        "--sse", action="store_true", help="Launch pdf2zh MCP server in SSE mode"
    )

    return parser


def parse_args(args: Optional[List[str]]) -> argparse.Namespace:
    parsed_args = create_parser().parse_args(args=args)

    if parsed_args.pages:
        pages = []
        for p in parsed_args.pages.split(","):
            if "-" in p:
                start, end = p.split("-")
                pages.extend(range(int(start) - 1, int(end)))
            else:
                pages.append(int(p) - 1)
        parsed_args.raw_pages = parsed_args.pages
        parsed_args.pages = pages

    return parsed_args


# Spawn 子进程会在 argv 中携带的进程级标志（Python spawn / 冻结启动器都会
# 把入口脚本作为 __main__ 重新执行并把这些参数连同原始声明一起传入）。
# 若入口不加保护，argparse 会在子进程中以 "-I --multiprocessing-fork ..."
# 二次解析而崩溃（"unrecognized arguments"），导致
# ProcessPoolExecutor 全部子进程启动即死 -> BrokenProcessPool。
_SPAWN_FORK_FLAG = "--multiprocessing-fork"


def is_spawn_child(argv: Optional[List[str]] = None) -> bool:
    """当前进程是否为 multiprocessing spawn 子进程（依据 argv 中的 fork 标志）。"""
    return _SPAWN_FORK_FLAG in (argv if argv is not None else sys.argv)


def spawn_child_yields_to(args: Optional[List[str]] = None) -> bool:
    """spawn 子进程将控制权让渡给 multiprocessing bootstrap；返回 True 表示不要再运行 argparse。

    标准/冻结形态（argv[1] == '--multiprocessing-fork'）交给
    ``multiprocessing.freeze_support()``（其内部会调用 ``spawn_main()`` 并
    ``sys.exit``）；其余启动器形态（如 ``-I --multiprocessing-fork`` 混在
    argv 中）此时由 spawn machinery 驱动，入口只需静默让路。
    """
    if not is_spawn_child(args):
        return False
    try:
        import multiprocessing

        multiprocessing.freeze_support()
    except Exception:  # noqa: BLE001 - 入口保护函数不允许让任何异常逃逸
        pass
    return True


def find_all_files_in_directory(directory_path):
    """
    Recursively search all PDF files in the given directory and return their paths as a list.

    :param directory_path: str, the path to the directory to search
    :return: list of PDF file paths
    """
    # Check if the provided path is a directory
    if not os.path.isdir(directory_path):
        raise ValueError(f"The provided path '{directory_path}' is not a directory.")

    file_paths = []

    # Walk through the directory recursively
    for root, _, files in os.walk(directory_path):
        for file in files:
            # Check if the file is a PDF
            if file.lower().endswith((".pdf", ".doc", ".docx")):
                # Append the full file path to the list
                file_paths.append(os.path.join(root, file))

    return file_paths


def main(args: Optional[List[str]] = None) -> int:
    if spawn_child_yields_to(args):
        # spawn 子进程再入口：控制权已归还 bootstrap，本进程不会再启动应用。
        return 0
    parsed_args = parse_args(args)

    from pdf2zh.parallel.interrupt import install_interrupt_guard

    # Ctrl+C 旗标：CLI 主线程仍按默认语义收到 KeyboardInterrupt；旗标供
    # coordinator 在 chunk 运行期间轮询感知，实现"立即短路、不进串行兜底"。
    install_interrupt_guard()

    from rich.logging import RichHandler

    logging.basicConfig(level=logging.INFO, handlers=[RichHandler()])

    # disable httpx, openai, httpcore, http11 logs
    logging.getLogger("httpx").setLevel("CRITICAL")
    logging.getLogger("httpx").propagate = False
    logging.getLogger("openai").setLevel("CRITICAL")
    logging.getLogger("openai").propagate = False
    logging.getLogger("httpcore").setLevel("CRITICAL")
    logging.getLogger("httpcore").propagate = False
    logging.getLogger("http11").setLevel("CRITICAL")
    logging.getLogger("http11").propagate = False

    if parsed_args.config:
        from pdf2zh.config import ConfigManager

        ConfigManager.custome_config(parsed_args.config)

    if getattr(parsed_args, "proxy", None):
        # 统一入口：--proxy 覆盖为标准变量，配合 PDF2ZH_PROXY 兜底（见 translator.py）
        os.environ.setdefault("PDF2ZH_PROXY", parsed_args.proxy)

    if parsed_args.debug:
        log.setLevel(logging.DEBUG)

    from pdf2zh.doclayout import ModelInstance, OnnxModel, set_backend

    set_backend(parsed_args.backend)

    if parsed_args.onnx:
        ModelInstance.value = OnnxModel(parsed_args.onnx)
    else:
        ModelInstance.value = OnnxModel.load_available()

    if parsed_args.interactive:
        from pdf2zh.gui.entry import setup_gui

        max_file_size = getattr(parsed_args, "max_file_size", None)
        if parsed_args.serverport:
            setup_gui(
                parsed_args.share, parsed_args.authorized, int(parsed_args.serverport),
                max_file_size=max_file_size,
            )
        else:
            setup_gui(
                parsed_args.share, parsed_args.authorized,
                max_file_size=max_file_size,
            )
        return 0

    if parsed_args.flask:
        from pdf2zh.backend import flask_app

        flask_app.run(port=11008)
        return 0

    if parsed_args.celery:
        from pdf2zh.backend import celery_app

        celery_app.start(argv=sys.argv[2:])
        return 0

    if parsed_args.prompt:
        try:
            with open(parsed_args.prompt, "r", encoding="utf-8") as file:
                content = file.read()
            parsed_args.prompt = Template(content)
        except Exception:
            raise ValueError("prompt error.")

    if parsed_args.mcp:
        logging.getLogger("mcp").setLevel(logging.ERROR)
        from pdf2zh.mcp_server import create_mcp_app, create_starlette_app

        mcp = create_mcp_app()
        if parsed_args.sse:
            import uvicorn

            starlette_app = create_starlette_app(mcp._mcp_server)
            uvicorn.run(starlette_app)
            return 0
        mcp.run()
        return 0

    print(parsed_args)

    if parsed_args.babeldoc:
        return yadt_main(parsed_args)

    # Unified kernel routing — both fast and precise modes go through the registry
    from pdf2zh.kernel import KernelRegistry
    from pdf2zh.kernel.protocol import TranslateRequest

    KernelRegistry.switch(parsed_args.mode)  # "fast" or "precise"
    kernel = KernelRegistry.get()

    if parsed_args.dir:
        parsed_args.files = find_all_files_in_directory(parsed_args.files[0])

    # Extract prompt text (may be a Template object from file reading above)
    prompt_text = None
    if parsed_args.prompt:
        prompt_text = (
            parsed_args.prompt.template
            if hasattr(parsed_args.prompt, "template")
            else parsed_args.prompt
        )

    request = TranslateRequest(
        files=parsed_args.files,
        output=parsed_args.output,
        pages=parsed_args.pages,
        lang_in=parsed_args.lang_in,
        lang_out=parsed_args.lang_out,
        service=parsed_args.service,
        thread=parsed_args.thread,
        parallel_pages=None if not parsed_args.no_parallel else False,
        parallel_workers=parsed_args.parallel_workers,
        vfont=parsed_args.vfont,
        vchar=parsed_args.vchar,
        envs={},
        prompt=prompt_text,
        skip_subset_fonts=parsed_args.skip_subset_fonts,
        ignore_cache=parsed_args.ignore_cache,
        compatible=parsed_args.compatible,
        debug=parsed_args.debug,
    )
    kernel.translate(request)
    return 0


def yadt_main(parsed_args) -> int:
    from babeldoc.format.pdf.high_level import async_translate as yadt_translate
    from babeldoc.format.pdf.high_level import init as yadt_init
    from babeldoc.main import create_progress_handler
    from babeldoc.format.pdf.translation_config import TranslationConfig as YadtConfig
    from pdf2zh.babeldoc_adapter import make_babeldoc_translator
    from pdf2zh.high_level import download_remote_fonts

    if parsed_args.dir:
        untranlate_file = find_all_files_in_directory(parsed_args.files[0])
    else:
        untranlate_file = parsed_args.files
    lang_in = parsed_args.lang_in
    lang_out = parsed_args.lang_out
    ignore_cache = parsed_args.ignore_cache
    outputdir = None
    if parsed_args.output:
        outputdir = parsed_args.output

    # yadt require init before translate
    yadt_init()
    font_path = download_remote_fonts(lang_out.lower())

    param = parsed_args.service.split(":", 1)
    service_name = param[0]
    service_model = param[1] if len(param) > 1 else None

    envs = {}
    prompt = []

    if parsed_args.prompt:
        try:
            with open(parsed_args.prompt, "r", encoding="utf-8") as file:
                content = file.read()
            prompt = Template(content)
        except Exception:
            raise ValueError("prompt error.")

    from pdf2zh.translator import (
        AzureOpenAITranslator,
        GoogleTranslator,
        BingTranslator,
        DeepLTranslator,
        DeepLXTranslator,
        OllamaTranslator,
        OpenAITranslator,
        ZhipuTranslator,
        ModelScopeTranslator,
        SiliconTranslator,
        GeminiTranslator,
        AzureTranslator,
        TencentTranslator,
        DifyTranslator,
        AnythingLLMTranslator,
        XinferenceTranslator,
        ArgosTranslator,
        GrokTranslator,
        GroqTranslator,
        DeepseekTranslator,
        OpenAIlikedTranslator,
        QwenMtTranslator,
        X302AITranslator,
    )

    for translator in [
        GoogleTranslator,
        BingTranslator,
        DeepLTranslator,
        DeepLXTranslator,
        OllamaTranslator,
        XinferenceTranslator,
        AzureOpenAITranslator,
        OpenAITranslator,
        ZhipuTranslator,
        ModelScopeTranslator,
        SiliconTranslator,
        GeminiTranslator,
        AzureTranslator,
        TencentTranslator,
        DifyTranslator,
        AnythingLLMTranslator,
        ArgosTranslator,
        GrokTranslator,
        GroqTranslator,
        DeepseekTranslator,
        OpenAIlikedTranslator,
        QwenMtTranslator,
        X302AITranslator,
    ]:
        if service_name == translator.name:
            translator = translator(
                lang_in,
                lang_out,
                service_model,
                envs=envs,
                prompt=prompt,
                ignore_cache=ignore_cache,
            )
            break
    else:
        raise ValueError("Unsupported translation service")
    # Bridge the pdf2zh engine into BabelDOC's translator interface so the
    # BabelDOC layout pipeline can call translate(text, rate_limit_params=...).
    babeldoc_translator = make_babeldoc_translator(
        translator, lang_in, lang_out, ignore_cache,
    )

    import asyncio

    for file in untranlate_file:
        file = file.strip("\"'")
        _converted_pdf = None
        if is_convertible(file):
            _converted_pdf = convert_to_pdf(file)
            file = _converted_pdf
        yadt_config = YadtConfig(
            input_file=file,
            font=font_path,
            pages=",".join((str(x) for x in getattr(parsed_args, "raw_pages", []))),
            output_dir=outputdir,
            doc_layout_model=None,
            translator=babeldoc_translator,
            debug=parsed_args.debug,
            lang_in=lang_in,
            lang_out=lang_out,
            no_dual=False,
            no_mono=False,
            qps=parsed_args.thread,
        )

        async def yadt_translate_coro(yadt_config):
            progress_context, progress_handler = create_progress_handler(yadt_config)
            # 开始翻译
            with progress_context:
                async for event in yadt_translate(yadt_config):
                    progress_handler(event)
                    if yadt_config.debug:
                        logger.debug(event)
                    if event["type"] == "finish":
                        result = event["translate_result"]
                        logger.info("Translation Result:")
                        logger.info(f"  Original PDF: {result.original_pdf_path}")
                        logger.info(f"  Time Cost: {result.total_seconds:.2f}s")
                        logger.info(f"  Mono PDF: {result.mono_pdf_path or 'None'}")
                        logger.info(f"  Dual PDF: {result.dual_pdf_path or 'None'}")
                        break

        asyncio.run(yadt_translate_coro(yadt_config))
        if _converted_pdf:
            try:
                os.unlink(_converted_pdf)
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
