from mcp.server import Server
from mcp.server.fastmcp import FastMCP, Context
from mcp.server.sse import SseServerTransport
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.routing import Mount, Route
from pdf2zh import translate_stream
from pdf2zh.converter_docx import convert_to_pdf, is_convertible
from pdf2zh.doclayout import ModelInstance
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
        if is_convertible(file):
            _converted_pdf = convert_to_pdf(file)
            original_name = os.path.splitext(os.path.basename(file))[0]
            file = _converted_pdf
        else:
            original_name = None

        with open(file, "rb") as f:
            file_bytes = f.read()
        await ctx.log(level="info", message=f"start translate {file} with {engine}")
        with contextlib.redirect_stdout(io.StringIO()):
            doc_mono_bytes, doc_dual_bytes = translate_stream(
                file_bytes,
                lang_in=lang_in,
                lang_out=lang_out,
                service=engine,
                model=ModelInstance.value,
                thread=4,
            )
        await ctx.log(level="info", message="translate complete")
        output_path = Path(os.path.dirname(file))
        filename = original_name or os.path.splitext(os.path.basename(file))[0]
        doc_mono = output_path / f"{filename}-mono.pdf"
        doc_dual = output_path / f"{filename}-dual.pdf"
        with open(doc_mono, "wb") as f:
            f.write(doc_mono_bytes)
        with open(doc_dual, "wb") as f:
            f.write(doc_dual_bytes)
        if _converted_pdf:
            try:
                os.unlink(_converted_pdf)
            except OSError:
                pass
        return f"""------------
    translate complete
    mono pdf file: {doc_mono.absolute()}
    dual pdf file: {doc_dual.absolute()}
    """

        return f"""------------
    translate complete
    mono pdf file: {doc_mono.absolute()}
    dual pdf file: {doc_dual.absolute()}
    """

    # ── V2 Tools using RuntimeService ─────────────────────────────────────

    @mcp.tool()
    async def translate_document(
        file: str,
        target_lang: str = "zh-CN",
        source_lang: str = "auto",
        engine: str = "google",
        output_format: str = "pdf",
    ) -> str:
        """
        Translate a document using RuntimeService (V4 pipeline).
        Supports PDF, DOCX input. Returns path to translated file.
        """
        from pdf2zh.services.runtime_service import RuntimeService, TranslationRequest

        resolved = file
        _converted_pdf = None
        if is_convertible(file):
            _converted_pdf = convert_to_pdf(file)
            resolved = _converted_pdf

        req = TranslationRequest(
            source_path=resolved,
            target_lang=target_lang,
            source_lang=source_lang,
            engine=engine,
        )

        svc = RuntimeService()
        task_id = svc.submit_task(req)

        import time
        while True:
            state = svc.get_task_state(task_id)
            if state is None:
                return "Error: task vanished"
            if state.status == "completed":
                result = state.result_files[0].get("path", "") if state.result_files else f"Completed (task: {task_id})"
                if _converted_pdf:
                    try: os.unlink(_converted_pdf)
                    except OSError: pass
                return result
            if state.status == "failed":
                return f"Failed: {state.error_message or state.message}"
            if state.status == "cancelled":
                return "Cancelled"
            time.sleep(1.0)

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
            summary = {"file": file, "pages": len(rt.graph.pages) if hasattr(rt.graph, 'pages') else 0, "status": "analyzed"}
            if hasattr(rt.graph, 'nodes'):
                counts = {}
                for node in rt.graph.nodes:
                    t = getattr(node, 'type', 'unknown')
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
            diagnostic.update({"status": "evaluated", "quality_scores": result.get("scores", {}), "issues": result.get("issues", [])})
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

        starlette_app = create_starlette_app(mcp_server, debug=True)
        uvicorn.run(starlette_app, host=args.host, port=args.port)
    else:
        mcp.run()
