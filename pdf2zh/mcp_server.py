from mcp.server import Server
from mcp.server.fastmcp import FastMCP, Context
from mcp.server.sse import SseServerTransport
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.routing import Mount, Route
from pdf2zh import translate_stream
from pdf2zh.converter_docx import cleanup_converted_pdf, convert_to_pdf, is_convertible
from pdf2zh.doclayout import ModelInstance
from pdf2zh.v3.ingestion.config import (
    JINA_DEFAULT_DPI,
    JINA_DEFAULT_MAX_NEW_TOKENS,
    JINA_DEFAULT_MAX_PIXELS,
    JINA_DEFAULT_TIMEOUT,
    JINA_MIN_COVERAGE,
    JINA_MODEL_ID,
    JINA_PROMPT,
    JINA_REVISION,
    JinaOcrOptions,
    normalize_ingest_backend,
)
from pathlib import Path

import contextlib
import io
import os


def create_mcp_app() -> FastMCP:
    mcp = FastMCP("pdf2zh")

    @mcp.tool()
    async def translate_pdf(
        file: str, lang_in: str, lang_out: str, ctx: Context, engine: str = "google"
    ) -> str:
        """
        translate given pdf or word document. Argument `file` is absolute path
        of input pdf/doc/docx, `lang_in` and `lang_out` is translate from and
        to language, and should be like google translate lang_code. `lang_in`
        can be `auto` if you can't determine input language. `engine` selects
        the translation service (e.g. google, openai, deepl, opencode); engine
        credentials are resolved from config.json / environment variables.
        """

        _converted_pdf = None
        output_dir = Path(file).resolve().parent
        if is_convertible(file):
            _converted_pdf = convert_to_pdf(file)
            original_name = os.path.splitext(os.path.basename(file))[0]
            file = _converted_pdf
        else:
            original_name = None

        try:
            with open(file, "rb") as f:
                file_bytes = f.read()
            await ctx.log(level="info", message=f"start translate {file} with {engine}")
            with contextlib.redirect_stdout(io.StringIO()):
                doc_dual_bytes, doc_mono_bytes = translate_stream(
                    file_bytes,
                    lang_in=lang_in,
                    lang_out=lang_out,
                    service=engine,
                    model=ModelInstance.value,
                    thread=4,
                )
            await ctx.log(level="info", message="translate complete")
            if not doc_mono_bytes or not doc_dual_bytes:
                raise RuntimeError("translate_stream returned empty output")
            output_path = output_dir
            filename = original_name or os.path.splitext(os.path.basename(file))[0]
            doc_mono = output_path / f"{filename}-mono.pdf"
            doc_dual = output_path / f"{filename}-dual.pdf"
            with open(doc_mono, "wb") as f:
                f.write(doc_mono_bytes)
            with open(doc_dual, "wb") as f:
                f.write(doc_dual_bytes)
            return f"""------------
    translate complete
    mono pdf file: {doc_mono.absolute()}
    dual pdf file: {doc_dual.absolute()}
    """
        finally:
            if _converted_pdf:
                try:
                    os.unlink(_converted_pdf)
                except OSError:
                    pass
                cleanup_converted_pdf(_converted_pdf)

    # ── V2 Tools using RuntimeService ─────────────────────────────────────

    @mcp.tool()
    async def translate_document(
        file: str,
        target_lang: str = "zh-CN",
        source_lang: str = "auto",
        engine: str = "google",
        output_format: str = "pdf",
        page_range: str = "",
        parse_engine: str = "auto",
        ingest_backend: str = "auto",
        jina_model: str = JINA_MODEL_ID,
        jina_revision: str = JINA_REVISION,
        jina_prompt: str = JINA_PROMPT,
        jina_device: str = "auto",
        jina_dpi: int = JINA_DEFAULT_DPI,
        jina_max_pixels: int = JINA_DEFAULT_MAX_PIXELS,
        jina_max_new_tokens: int = JINA_DEFAULT_MAX_NEW_TOKENS,
        jina_timeout: float = JINA_DEFAULT_TIMEOUT,
        jina_cache_dir: str = "",
        jina_min_coverage: float = JINA_MIN_COVERAGE,
        jina_offline: bool = False,
    ) -> str:
        """
        Translate a document using RuntimeService (V4 pipeline).
        Supports PDF, DOCX input. Returns path to translated file.
        """
        from pdf2zh.services.runtime_service import TranslationRequest

        resolved = file
        _converted_pdf = None

        jina_options = None
        if normalize_ingest_backend(ingest_backend) == "jina":
            if (
                (jina_model or JINA_MODEL_ID).strip() != JINA_MODEL_ID
                or (jina_revision or JINA_REVISION).strip() != JINA_REVISION
            ) and os.environ.get("PDF2ZH_JINA_ALLOW_CUSTOM_MODEL") != "1":
                return (
                    "Error: custom Jina models require PDF2ZH_JINA_ALLOW_CUSTOM_MODEL=1"
                )
            try:
                jina_options = JinaOcrOptions.from_mapping(
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
                return f"Error: invalid Jina OCR options: {exc}"

        req = TranslationRequest(
            source_path=resolved,
            target_lang=target_lang,
            source_lang=source_lang,
            engine=engine,
            page_range=page_range or None,
            parse_engine=parse_engine,
            ingest_backend=ingest_backend,
            jina_model=jina_options.model if jina_options is not None else jina_model,
            jina_revision=(
                jina_options.revision if jina_options is not None else jina_revision
            ),
            jina_prompt=(
                jina_options.prompt if jina_options is not None else jina_prompt
            ),
            jina_device=(
                jina_options.device if jina_options is not None else jina_device
            ),
            jina_dpi=jina_options.dpi if jina_options is not None else jina_dpi,
            jina_max_pixels=(
                jina_options.max_pixels if jina_options is not None else jina_max_pixels
            ),
            jina_max_new_tokens=(
                jina_options.max_new_tokens
                if jina_options is not None
                else jina_max_new_tokens
            ),
            jina_timeout=(
                jina_options.timeout if jina_options is not None else jina_timeout
            ),
            jina_cache_dir=(
                jina_options.cache_dir if jina_options is not None else jina_cache_dir
            ),
            jina_min_coverage=(
                jina_options.min_coverage
                if jina_options is not None
                else jina_min_coverage
            ),
            jina_offline=(
                jina_options.offline if jina_options is not None else jina_offline
            ),
        )

        if is_convertible(file):
            _converted_pdf = convert_to_pdf(file)
            req.source_path = _converted_pdf

        from pdf2zh.services.runtime_singleton import get_runtime_service

        try:
            svc = get_runtime_service()
            task_id = svc.submit_task(req)
        except Exception:
            if _converted_pdf:
                try:
                    os.unlink(_converted_pdf)
                except OSError:
                    pass
                cleanup_converted_pdf(_converted_pdf)
            raise

        import asyncio
        import time

        deadline = time.monotonic() + 3600.0

        async def _cleanup_converted_input() -> None:
            if not _converted_pdf:
                return
            for _ in range(50):
                current = svc.get_task_state(task_id)
                if current is None or current.status in {
                    "completed",
                    "failed",
                    "cancelled",
                }:
                    break
                await asyncio.sleep(0.1)
            try:
                os.unlink(_converted_pdf)
            except OSError:
                pass
            cleanup_converted_pdf(_converted_pdf)

        try:
            while True:
                state = svc.get_task_state(task_id)
                if state is None:
                    return "Error: task vanished"
                if state.status == "completed":
                    return (
                        state.result_files[0].get("path", "")
                        if state.result_files
                        else f"Completed (task: {task_id})"
                    )
                if state.status == "failed":
                    return f"Failed: {state.error_message or state.message}"
                if state.status == "cancelled":
                    return "Cancelled"
                if time.monotonic() >= deadline:
                    svc.cancel_task(task_id)
                    return "Error: translation timed out"
                await asyncio.sleep(1.0)
        finally:
            await _cleanup_converted_input()

    @mcp.tool()
    async def inspect_document_structure(file: str) -> str:
        """
        Analyze a PDF document's structure using V4 Document Graph.
        Returns JSON summary of pages, headings, paragraphs, figures, etc.
        """
        import json

        try:
            from pdf2zh.v3.runtime import RuntimeFacade
        except ImportError:
            return json.dumps({"error": "V4 engine not available"}, indent=2)
        try:
            rt = RuntimeFacade()
            rt.load(file)
            rt.analyze()
            summary = {
                "file": file,
                "pages": len(rt.graph.pages) if hasattr(rt.graph, "pages") else 0,
                "status": "analyzed",
            }
            if hasattr(rt.graph, "nodes"):
                counts = {}
                for node in rt.graph.nodes:
                    t = getattr(node, "type", "unknown")
                    counts[t] = counts.get(t, 0) + 1
                summary["node_counts"] = counts
            return json.dumps(summary, indent=2)
        except Exception as exc:
            return json.dumps({"error": str(exc), "status": "failed"}, indent=2)

    @mcp.tool()
    async def get_document_diagnostics(file: str) -> str:
        """Get translation quality diagnostics and evaluation scores."""
        import json

        diagnostic = {"file": file, "status": "not_evaluated"}
        try:
            from pdf2zh.v3.evaluator import Evaluator

            ev = Evaluator()
            result = ev.evaluate(file)
            diagnostic.update(
                {
                    "status": "evaluated",
                    "quality_scores": result.get("scores", {}),
                    "issues": result.get("issues", []),
                }
            )
        except ImportError:
            pass
        return json.dumps(diagnostic, indent=2)

    return mcp


def create_starlette_app(mcp_server: Server, *, debug: bool = False) -> Starlette:
    sse = SseServerTransport("/messages/")

    async def handle_sse(request: Request) -> None:
        async with sse.connect_sse(request.scope, request.receive, request._send) as (
            read_stream,
            write_stream,
        ):
            await mcp_server.run(
                read_stream, write_stream, mcp_server.create_initialization_options()
            )

    return Starlette(
        debug=debug,
        routes=[
            Route("/sse", endpoint=handle_sse),
            Mount("/messages/", app=sse.handle_post_message),
        ],
    )


if __name__ == "__main__":
    import argparse

    from pdf2zh.pdf2zh import spawn_child_yields_to

    if spawn_child_yields_to():
        raise SystemExit(0)

    mcp = create_mcp_app()
    mcp_server = mcp._mcp_server
    parser = argparse.ArgumentParser(description="Run MCP SSE-based PDF2ZH server")

    parser.add_argument(
        "--sse",
        default=False,
        action="store_true",
        help="Run the server with SSE transport or STDIO",
    )
    parser.add_argument(
        "--host", type=str, default="127.0.0.1", required=False, help="Host to bind"
    )
    parser.add_argument(
        "--port", type=int, default=3001, required=False, help="Port to bind"
    )

    args = parser.parse_args()
    if args.sse and args.host and args.port:
        import uvicorn

        # debug=False：不向客户端回显异常堆栈（信息泄露）
        starlette_app = create_starlette_app(mcp_server, debug=False)
        uvicorn.run(starlette_app, host=args.host, port=args.port)
    else:
        mcp.run()
