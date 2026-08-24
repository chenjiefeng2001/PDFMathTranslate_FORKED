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
        "--api",
        action="store_true",
        help="Run the REST/SSE API server (FastAPI) for SPA / third-party clients.",
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
        "--parse-engine",
        type=str,
        choices=["auto", "legacy", "babeldoc", "magicpdf"],
        default="auto",
        help="PDF parse engine (Stage 2.3): auto (default; babeldoc iff "
        "--babeldoc), legacy (pdfminer kernel), babeldoc (YADT), magicpdf "
        "(MinerU/magic-pdf as parse layer, bridged into the v3 canonical "
        "model; falls back to legacy when the engine is unavailable).",
    )

    parse_params.add_argument(
        "--magicpdf-ocr",
        action="store_true",
        help="Enable OCR in the magicpdf parse engine (magic-pdf 1.x "
        "pipe_ocr_merge). Equivalent to --magicpdf-ocr-mode on.",
    )
    parse_params.add_argument(
        "--magicpdf-ocr-mode",
        type=str,
        choices=["auto", "on", "off"],
        default="auto",
        help="OCR mode for the magicpdf parse engine: auto (default) "
        "auto-enables OCR when the text-layer quality preflight flags "
        "scanned/damaged signals; on forces OCR for every PDF; off "
        "disables OCR entirely (the preflight never overrides it). "
        "--magicpdf-ocr is equivalent to --magicpdf-ocr-mode on.",
    )

    parse_params.add_argument(
        "--magicpdf-render",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Render the magicpdf parse result into a translated mono PDF "
        "(default: on). Use --no-magicpdf-render to keep JSON dumps only.",
    )

    parse_params.add_argument(
        "--babeldoc-ocr",
        type=str,
        choices=["auto", "on", "off"],
        default="auto",
        help="Scanned-PDF / OCR handling for the BabelDOC layout engine: "
        "auto auto-detects heavily-scanned pages and enables OCR; on forces "
        "OCR for every PDF (best for textless PDFs); off skips scan "
        "detection (no OCR). Can also be set via the PDF2ZH_BABELDOC_OCR "
        "env var.",
    )

    parse_params.add_argument(
        "--glossary-files",
        nargs="+",
        metavar="CSV",
        default=[],
        help="Professional-term glossary CSV files (columns: "
        "source,target[,tgt_lng]; tgt_lng filters entries by target "
        "language). Applied on the BabelDOC parse engine (--parse-engine "
        "babeldoc / --babeldoc); ignored with a warning on other engines. "
        "Manage the glossary store via `python -m pdf2zh.glossary_store`.",
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


def parse_args(args: list[str] | None) -> argparse.Namespace:
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


def is_spawn_child(argv: list[str] | None = None) -> bool:
    """当前进程是否为 multiprocessing spawn 子进程（依据 argv 中的 fork 标志）。"""
    return _SPAWN_FORK_FLAG in (argv if argv is not None else sys.argv)


def spawn_child_yields_to(args: list[str] | None = None) -> bool:
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


def main(args: list[str] | None = None) -> int:
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

    # 系统代理（WinINET 注册表 Clash/VPN）导入环境变量 + NO_PROXY 构建。必须在任何
    # 翻译 HTTP 请求发出前执行：仅设置 NO_PROXY 会使 urllib/requests 放弃注册表
    # 代理回退，翻译直连被墙站点而 ConnectTimeout（如 translate.google.com）。
    from pdf2zh.networking import sanitize_loopback_proxy

    sanitize_loopback_proxy()

    if parsed_args.debug:
        log.setLevel(logging.DEBUG)

    from pdf2zh.doclayout import set_backend

    # 版面分析模型不在全局入口加载（改由 legacy/babeldoc 轨按需懒加载）：
    # ``OnnxModel.load_available()`` 会立即创建 onnxruntime CUDA/TensorRT
    # 会话，Windows 上 ORT 先加载自带的 cuDNN DLL 后，同一进程再导入
    # torch 会因 DLL 冲突报 WinError 127（cudnn_cnn64_9.dll），导致全
    # PyTorch 实现的 magic-pdf 引擎必然失败。magicpdf 轨不消费该模型。
    set_backend(parsed_args.backend)

    if parsed_args.interactive:
        from pdf2zh.gui.entry import setup_gui

        max_file_size = getattr(parsed_args, "max_file_size", None)
        if parsed_args.serverport:
            setup_gui(
                parsed_args.share,
                parsed_args.authorized,
                int(parsed_args.serverport),
                max_file_size=max_file_size,
            )
        else:
            setup_gui(
                parsed_args.share,
                parsed_args.authorized,
                max_file_size=max_file_size,
            )
        return 0

    if parsed_args.flask:
        from pdf2zh.backend import flask_app

        flask_app.run(port=11008)
        return 0

    if parsed_args.api:
        import uvicorn

        from pdf2zh.services.api import create_api_app
        from pdf2zh.services.runtime_singleton import get_runtime_service

        # CORS 仅对显式声明的源开启（如 Vite 5173 联调时设
        # allow_origins=["http://localhost:5173"]）；通配 "*" 已被入站
        # Host/Origin 守卫取代，防止网页 drive-by 访问本地 API。
        api_app = create_api_app(service=get_runtime_service())
        uvicorn.run(api_app, host="127.0.0.1", port=11009)
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

    # 输入存在性校验（--dir 模式的首项是待扫描目录，跳过）：不存在或不是
    # 普通文件的输入在引擎路由前给出明确错误，而不是等到下游 open() 时抛出
    # 令人困惑的 PermissionError/IsADirectoryError 调用栈。
    if not parsed_args.dir:
        invalid = [f for f in (parsed_args.files or []) if not os.path.isfile(f)]
        if invalid:
            raise FileNotFoundError(
                "Input PDF not found or not a regular file: " + ", ".join(invalid)
            )

    # 解析引擎路由（Stage 2.3）：auto 时维持历史语义（--babeldoc → YADT，
    # 否则 legacy kernel）；显式 magicpdf 走 MinerU/magic-pdf 解析链路。
    parse_engine = resolve_parse_engine(parsed_args)
    if parse_engine != "babeldoc" and getattr(parsed_args, "glossary_files", None):
        logger.warning(
            "--glossary-files 仅在 --parse-engine babeldoc 链路生效，"
            "当前引擎 %s 将忽略词表（legacy 链支持见 roadmap Phase 3）",
            parse_engine,
        )
    if parse_engine == "babeldoc":
        return yadt_main(parsed_args)

    if parse_engine == "magicpdf":
        from pdf2zh.magicpdf_cli import run_magicpdf_main

        return run_magicpdf_main(parsed_args)

    return _run_legacy_kernel(parsed_args)


def resolve_parse_engine(parsed_args) -> str:
    """解析引擎路由决策（Stage 2.3）。

    ``--parse-engine auto``（缺省）维持历史语义：``--babeldoc`` → YADT，
    否则 legacy kernel；显式指定值直接生效。
    """
    engine = getattr(parsed_args, "parse_engine", "auto")
    if engine == "auto" and parsed_args.babeldoc:
        return "babeldoc"
    return engine


def resolve_magicpdf_ocr_mode(parsed_args) -> str:
    """解析 magicpdf 引擎的 OCR 模式（auto/on/off）。

    ``--magicpdf-ocr``（历史 bool 开关）等价 ``--magicpdf-ocr-mode on``；
    二者都未指定时默认 ``auto``（预检命中扫描/损坏信号才自动开启 OCR）。
    ``off`` 表示用户显式关闭 OCR——预检命中也绝不强制开启。
    """
    if getattr(parsed_args, "magicpdf_ocr", False):
        return "on"
    mode = getattr(parsed_args, "magicpdf_ocr_mode", "auto") or "auto"
    return mode if mode in ("auto", "on", "off") else "auto"


def _try_auto_switch_magicpdf(parsed_args) -> bool:
    """legacy 内核的文本层预检 + magicpdf 自动切换（报告 §6.1）。

    对每个输入 PDF 跑多信号融合预检（preflight_scan_check）：命中扫描/损坏
    信号且 magic-pdf/MinerU 可用时自动切换 ``--parse-engine magicpdf`` 并
    开启 OCR；否则输出强警告并继续 legacy。环境变量
    ``PDF2ZH_AUTO_SWITCH_MAGICPDF=0`` 关闭自动切换；``parsed_args."
    "_auto_switch_attempted`` 防重入（自动切换失败回退 legacy 时不再循环）。

    Returns:
        True 表示已切换并交给 magicpdf 引擎；False 表示继续 legacy。
    """
    if getattr(parsed_args, "_auto_switch_attempted", False):
        return False
    # 刚从 magicpdf 引擎熔断降级回来：本次运行中 magic-pdf 已被证实不可用
    # （解析异常 / 未安装），不再自动切回，避免 magicpdf → legacy → magicpdf
    # 的乒乓循环与重复的引擎冷启动开销。
    if getattr(parsed_args, "_magicpdf_fallback", False):
        return False
    parsed_args._auto_switch_attempted = True
    if os.environ.get("PDF2ZH_AUTO_SWITCH_MAGICPDF", "1") == "0":
        return False
    # 用户显式关闭 OCR（--magicpdf-ocr-mode off）时，即使预检命中扫描/损坏
    # 信号也不自动切换 magicpdf 引擎并强制开启 OCR——尊重用户的显式选择。
    if resolve_magicpdf_ocr_mode(parsed_args) == "off":
        logger.info(
            "MagicPDF OCR mode is 'off'; skipping auto-switch to "
            "--parse-engine magicpdf with OCR."
        )
        return False
    files = list(parsed_args.files or [])
    if not files:
        return False
    from pdf2zh.scanned_detection import preflight_scan_check

    for f in files:
        if not f.lower().endswith(".pdf"):
            continue
        try:
            decision = preflight_scan_check(f)
        except Exception as exc:  # noqa: BLE001 -- 预检失败不阻断
            logger.debug("legacy preflight skipped %s: %s", f, exc)
            continue
        if not decision.is_scanned:
            continue
        reasons = "; ".join(decision.reasons) or "unknown"
        try:
            from pdf2zh.engine_env import available_backend

            backend, ok = available_backend()
        except Exception:  # noqa: BLE001
            backend, ok = None, False
        if ok:
            logger.warning(
                "%s 文本层质量预检命中扫描/损坏信号（%s）；magic-pdf/MinerU "
                "可用，已自动切换 --parse-engine magicpdf --magicpdf-ocr。",
                f,
                reasons,
            )
            parsed_args.parse_engine = "magicpdf"
            parsed_args.magicpdf_ocr = True
            return True
        logger.warning(
            "%s 文本层质量预检命中扫描/损坏信号（%s）。legacy 内核无 OCR "
            "兜底，译文可能基于乱码输出；建议改用 --parse-engine magicpdf "
            "--magicpdf-ocr 或 --babeldoc-ocr on。",
            f,
            reasons,
        )
    return False


def _ensure_doclayout_model(parsed_args) -> None:
    """版面分析模型懒加载（legacy / babeldoc 轨入口调用，幂等）。

    ``--onnx`` 显式指定模型路径时始终重建；否则仅在全局单例为空时加载。
    从 CLI 全局入口下沉到此处的原因见 :func:`main` 中 set_backend 处注释
    （onnxruntime CUDA 会话会污染进程 DLL 环境，阻断后续 torch 导入）。
    """
    from pdf2zh.doclayout import ModelInstance, OnnxModel

    if parsed_args.onnx:
        ModelInstance.value = OnnxModel(parsed_args.onnx)
        return
    if ModelInstance.value is None:
        ModelInstance.value = OnnxModel.load_available()


def _run_legacy_kernel(parsed_args) -> int:
    """既有内核路由（fast/precise，不涉及 magicpdf/babeldoc）。"""
    _ensure_doclayout_model(parsed_args)

    # Unified kernel routing — both fast and precise modes go through the registry
    from pdf2zh.kernel import KernelRegistry
    from pdf2zh.kernel.protocol import TranslateRequest

    KernelRegistry.switch(parsed_args.mode)  # "fast" or "precise"
    kernel = KernelRegistry.get()

    if parsed_args.dir:
        parsed_args.files = find_all_files_in_directory(parsed_args.files[0])

    # 文本层质量预检（scan_damaged 报告 §6.1 长期实现）：legacy 内核无 OCR
    # 兜底，命中扫描/损坏信号时若 magic-pdf/MinerU 可用则自动切换 magicpdf
    # 引擎并开启 OCR（PDF2ZH_AUTO_SWITCH_MAGICPDF=0 关闭）；否则输出强警告。
    if _try_auto_switch_magicpdf(parsed_args):
        from pdf2zh.magicpdf_cli import run_magicpdf_main

        return run_magicpdf_main(parsed_args)

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
    _ensure_doclayout_model(parsed_args)

    # 把后端开关（--backend / PDF2ZH_BABELDOC_BACKEND）同步到 BabelDOC 内部
    # ONNX 会话（幂等）：显式 cuda/dml 时 BabelDOC 版面分析也走 GPU，而不是
    # 其硬编码的 CPU-only。
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

    # 扫描版 / 无文本层 PDF 的 OCR 模式开关（auto/on/off，见
    # pdf2zh.babeldoc_ocr_mode）：解析出 BabelDOC 的三个互斥扫描版字段。
    from babeldoc.format.pdf.high_level import async_translate as yadt_translate
    from babeldoc.format.pdf.high_level import init as yadt_init
    from babeldoc.format.pdf.translation_config import TranslationConfig as YadtConfig
    from babeldoc.main import create_progress_handler

    from pdf2zh.babeldoc_adapter import _build_doclayout_model, make_babeldoc_translator
    from pdf2zh.babeldoc_ocr_mode import resolve_ocr_flags
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

    # 专业词表（--glossary-files）：预检 + 装载，坏文件在翻译前快速失败。
    from pdf2zh.glossary_store import load_babeldoc_glossaries

    glossaries = load_babeldoc_glossaries(
        getattr(parsed_args, "glossary_files", None),
        lang_out,
    )
    if glossaries:
        logger.info("Loaded %d glossary file(s)", len(glossaries))

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
        AnythingLLMTranslator,
        ArgosTranslator,
        AzureOpenAITranslator,
        AzureTranslator,
        BingTranslator,
        DeepLTranslator,
        DeepLXTranslator,
        DeepseekTranslator,
        DifyTranslator,
        GeminiTranslator,
        GoogleTranslator,
        GrokTranslator,
        GroqTranslator,
        ModelScopeTranslator,
        OllamaTranslator,
        OpenAIlikedTranslator,
        OpenAITranslator,
        OpenCodeTranslator,
        QwenMtTranslator,
        SiliconTranslator,
        TencentTranslator,
        X302AITranslator,
        XinferenceTranslator,
        ZhipuTranslator,
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
        OpenCodeTranslator,
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
        translator,
        lang_in,
        lang_out,
        ignore_cache,
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
            pages=",".join(str(x) for x in getattr(parsed_args, "raw_pages", [])),
            output_dir=outputdir,
            doc_layout_model=_build_doclayout_model(),
            translator=babeldoc_translator,
            debug=parsed_args.debug,
            lang_in=lang_in,
            lang_out=lang_out,
            no_dual=False,
            no_mono=False,
            qps=parsed_args.thread,
            # 双语 PDF 使用交替页模式（与 RuntimeService 的
            # babeldoc_adapter 路由保持一致）：原文页、译文页各自独立成页
            # 并交替排列，而不是 BabelDOC 默认的原文+译文 side-by-side 合并。
            use_alternating_pages_dual=True,
            # 扫描版 / 无文本层 PDF 的 OCR 处理由 --babeldoc-ocr / 环境变量
            # PDF2ZH_BABELDOC_OCR 决定（auto 自动检测扫描并启用 OCR / on
            # 强制 OCR / off 跳过扫描检测），而不是保持 BabelDOC 默认的
            # 扫描检测失败行为。
            **dict(
                zip(
                    (
                        "ocr_workaround",
                        "auto_enable_ocr_workaround",
                        "skip_scanned_detection",
                    ),
                    resolve_ocr_flags(parsed_args.babeldoc_ocr),
                )
            ),
            glossaries=glossaries or None,
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
