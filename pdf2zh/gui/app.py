"""Main Gradio Web UI entry point for pdf2zh.

Usage:
    python -m pdf2zh.gui.app
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, List

import gradio as gr

from pdf2zh.gui.components.upload_panel import create_upload_panel
from pdf2zh.gui.components.config_panel import create_config_panel
from pdf2zh.gui.components.progress_panel import create_progress_panel
from pdf2zh.gui.components.preview_panel import create_preview_panel
from pdf2zh.gui.components.diagnostic_panel import (
    create_diagnostic_panel,
    build_diagnostic_markdown,
)
from pdf2zh.gui.state import GLOBAL_TASK_STORE
from pdf2zh.gui.logger import get_handler
from pdf2zh.gui.worker import (
    get_runtime_service,
    submit_translation_task,
    _clean_stale_in_flight,
)

logger = logging.getLogger(__name__)

CUSTOM_CSS = (
    # note: :root CSS --vars removed for Gradio 5 compat
    ".section-header { margin-top: 0.5rem; margin-bottom: 0.25rem; }\n"
    ".upload-area { border: 2px dashed #E5E6EB; border-radius: 8px; padding: 1rem; }\n"
    ".progress-bar { min-height: 40px; padding: 8px; background: #FFFFFF; border-radius: 6px; }\n"
    ".status-text { padding: 4px 0; }\n"
    ".log-output { max-height: 300px; overflow-y: auto; background: #1D2129; "
    "color: #E5E6EB; padding: 12px; border-radius: 6px; font-family: monospace; font-size: 12px; }\n"
    ".preview-empty { display: flex; align-items: center; justify-content: center; "
    "min-height: 400px; background: #F7F8FA; border-radius: 6px; color: #86909C; }\n"
    ".diagnostic-overview, .quality-scores, .diagnostic-status { "
    "min-height: 60px; padding: 8px; background: #F7F8FA; border-radius: 4px; }"
    ".pdf-iframe-container { width: 100%; min-height: 500px; border: 1px solid #E5E6EB; border-radius: 6px; overflow: hidden; }\n"
    ".pdf-iframe-container iframe { width: 100%; min-height: 500px; border: none; }"
)

SESSION_JS = """<script>
(function() {
    var cid = localStorage.getItem("pdf2zh_client_id");
    if (!cid) { cid = "client_" + Math.random().toString(36).substr(2, 12); localStorage.setItem("pdf2zh_client_id", cid); }
    window.__pdf2zh_client_id = cid;

    var savedTaskId = localStorage.getItem("pdf2zh_last_task_id");
    if (savedTaskId) {
        window.__pdf2zh_last_task_id = savedTaskId;
        window.__pdf2zh_last_preview = localStorage.getItem("pdf2zh_last_preview_path") || "";
        try {
            window.__pdf2zh_last_results = JSON.parse(localStorage.getItem("pdf2zh_last_results") || "[]");
        } catch(e) { window.__pdf2zh_last_results = []; }
    }

    // Persist config panel values to localStorage when changed
    var configKeys = ["service","lang_from","lang_to","mode_choice","threads","skip_subset_fonts","ignore_cache","vfont","vchar","page_range","prompt_env","env0","env1","env2"];
    document.addEventListener("change", function(e) {
        var t = e.target;
        if (!t || !t.id) return;
        for (var i=0; i<configKeys.length; i++) {
            var k = configKeys[i];
            if (t.id.indexOf(k) !== -1) {
                try { localStorage.setItem("pdf2zh_config_" + k, t.value); } catch(ex) {}
                break;
            }
        }
    });
})();
</script>"""


def _sanitize_html(text: str) -> str:
    """Sanitize text for safe HTML embedding. Removes surrogate characters."""
    cleaned = "".join(c if ord(c) < 0xD800 or ord(c) > 0xDFFF else "\ufffd" for c in text)
    return cleaned.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def on_translate(
    client_id: str, file_type: str, file_input: Any, link_input: str,
    service: str, lang_from: str, lang_to: str, page_range: str,
    page_input: Any, threads: int, skip_subset_fonts: bool,
    ignore_cache: bool, vfont: str, vchar: str, mode_choice: str,
    recaptcha_response: str, fl_state: List[str],
    env0: str, env1: str, env2: str, prompt_env: str,
    current_task_id: str,
) -> tuple:
    """Handle translate button with double-click prevention."""
    # Resolve effective client_id (supports both Gradio State and JS-side global)
    import __main__ as _main_mod
    effective_cid = client_id if client_id else getattr(_main_mod, "__pdf2zh_client_id", "")
    if not effective_cid:
        effective_cid = "client_default"
    if current_task_id:
        svc = get_runtime_service()
        ts = svc.get_task_state(current_task_id)
        if ts and ts.status in (
            "pending", "parsing", "normalizing", "analyzing",
            "planning", "translating", "layouting", "rendering",
            "evaluating", "repairing",
        ):
            return current_task_id, gr.update()
    try:
        task_id = submit_translation_task(
            client_id=effective_cid, file_type=file_type,
            file_input=file_input, link_input=link_input,
            service=service, lang_from=lang_from, lang_to=lang_to,
            page_range=page_range, page_input=page_input,
            threads=threads, skip_subset_fonts=skip_subset_fonts,
            ignore_cache=ignore_cache, vfont=vfont, vchar=vchar,
            mode_choice=mode_choice,
            recaptcha_response=recaptcha_response,
            fl_state=fl_state, env0=env0, env1=env1, env2=env2,
            prompt_env=prompt_env,
        )
    except Exception as exc:
        logger.error("Failed to submit task: %s", exc)
        return "", gr.update()
    return task_id, gr.update(interactive=False)


def on_cancel(current_task_id: str) -> str:
    if current_task_id:
        get_runtime_service().cancel_task(current_task_id)
    return ""


def on_pause(current_task_id: str) -> str:
    if current_task_id:
        get_runtime_service().pause_task(current_task_id)
    return current_task_id

def on_resume(current_task_id: str) -> str:
    if current_task_id:
        get_runtime_service().resume_task(current_task_id)
    return current_task_id


def on_skip(current_task_id: str) -> str:
    if current_task_id:
        get_runtime_service().skip_task(current_task_id)
    return current_task_id




def _persist_state_to_storage(task_id: str, ts) -> str:
    """Persist current task state to localStorage for cross-session recovery."""
    import json
    try:
        results_json = json.dumps(
            [{"name": r["name"], "path": r["path"]}
             for r in (getattr(ts, "result_files", None) or [])],
            ensure_ascii=False,
        )
        preview = getattr(ts, "preview_path", None) or ""
        sel = getattr(ts, "selected_file", None) or ""
        safe_tid = json.dumps(task_id)
        safe_preview = json.dumps(preview)
        safe_results = json.dumps(results_json)
        script = "<script>"
        script += 'localStorage.setItem("pdf2zh_last_task_id", ' + safe_tid + ');'
        script += 'localStorage.setItem("pdf2zh_last_preview_path", ' + safe_preview + ');'
        script += 'localStorage.setItem("pdf2zh_last_results", ' + safe_results + ');'
        script += 'window.__pdf2zh_last_task_id = ' + safe_tid + ';'
        script += 'window.__pdf2zh_last_preview = ' + safe_preview + ';'
        script += "</script>"
        return script
    except Exception:
        return ""
def on_download_single(current_task_id: str):
    if not current_task_id:
        return None
    ts = get_runtime_service().get_task_state(current_task_id)
    if ts is None or not ts.result_files:
        return None
    sel = getattr(ts, "selected_file", None)
    for rf in ts.result_files:
        if rf["name"] == sel and os.path.exists(rf["path"]):
            return rf["path"]
    if ts.result_files and os.path.exists(ts.result_files[0]["path"]):
        return ts.result_files[0]["path"]
    return None


def on_download_all(current_task_id: str):
    if not current_task_id:
        return None
    ts = get_runtime_service().get_task_state(current_task_id)
    if ts is None:
        return None
    zp = getattr(ts, "result_zip", None)
    if zp and os.path.exists(zp):
        return zp
    return None


def _collect_logs(max_lines: int = 50) -> str:
    try:
        handler = get_handler()
        handler.register_thread()
        q = handler.get_queue()
        if q is None:
            return ""
        lines = []
        while not q.empty() and len(lines) < max_lines:
            try:
                lines.append(q.get_nowait())
            except Exception:
                break
        if lines:
            return "\n".join(
                _sanitize_html(line)
                for line in lines[-max_lines:]
            )
        return ""
    except Exception:
        return ""



def sync_status(current_task_id: str) -> tuple:
    """Periodic sync - updates all dynamic UI components."""
    svc = get_runtime_service()
    tid = current_task_id

    if not tid:
        tasks = GLOBAL_TASK_STORE.list_tasks()
        if tasks:
            tid = tasks[-1]
    else:
        ts = svc.get_task_state(tid)
        if ts is None:
            tasks = GLOBAL_TASK_STORE.list_tasks()
            tid = tasks[-1] if tasks else ""

    if not tid:
        return _idle_updates()

    ts = svc.get_task_state(tid)
    if ts is None:
        return _idle_updates()

    _clean_stale_in_flight()
    running = ts.status in (
        "pending", "parsing", "normalizing", "analyzing",
        "planning", "translating", "layouting", "rendering",
        "evaluating", "repairing",
    )
    finished = ts.status in ("completed", "cancelled", "failed")
    done = ts.status == "completed"
    pct = ts.progress

    bar = _build_bar(ts.stage or "running", pct, ts.message or "")
    st = f"**Status**: `{_clean_surrogates(ts.status)}` | **Progress**: {pct:.1f}%"
    if ts.error_message:
        st += f"\n\nError: {ts.error_message}"
    btn_upd = gr.update(interactive=not running)

    qs = (ts.quality_scores or {}) if isinstance(ts.quality_scores, dict) else {}
    ds = ts.diagnostic_summary or ""
    node_ov = {}
    if ts.current_file_name:
        node_ov["pages"] = 1
    qs_md = build_diagnostic_markdown(quality_scores=qs)
    diag_md = build_diagnostic_markdown(diagnostic_summary=ds, node_overview=node_ov)
    ov = f"**Document**: {ts.current_file_name or 'N/A'}"
    if ts.file_list:
        ov += f" | **Nodes**: {len(ts.file_list)}"
    if ts.message:
        safe_msg = _sanitize_html(ts.message)
        ov += f"\n\n**Message**: {safe_msg}"
    choices = []
    sval = None
    if ts.result_files:
        choices = [r["name"] for r in ts.result_files]
        sval = ts.selected_file or choices[0]

    download_single_val = None
    download_zip_val = None
    if done and ts.result_files:
        selected_path = None
        for rf in ts.result_files:
            if rf["name"] == sval and os.path.exists(rf.get("path", "")):
                selected_path = rf["path"]
                break
        if not selected_path:
            for rf in ts.result_files:
                if os.path.exists(rf.get("path", "")):
                    selected_path = rf["path"]
                    break
        download_single_val = selected_path
    if done and ts.result_zip and os.path.exists(ts.result_zip):
        download_zip_val = ts.result_zip

    ph = """<div class='preview-empty'>等待翻译完成后显示预览</div>"""
    if done:
        preview_path = None
        if ts.preview_path and os.path.exists(ts.preview_path):
            preview_path = ts.preview_path
        elif download_single_val and os.path.exists(download_single_val):
            preview_path = download_single_val
        if preview_path is None and ts.result_files:
            for rf in ts.result_files:
                p = rf.get("path", "")
                if p and os.path.exists(p):
                    preview_path = p
                    break
        if preview_path:
            import urllib.parse as _pp
            encoded = _pp.quote(preview_path)
            ph = '<div class=\"pdf-iframe-container\"><iframe src="/pdf-preview/' + encoded + '" type="application/pdf"></iframe></div>'

    if done:
        _persist_state_to_storage(tid, ts)

    lh = "<pre class='log-output'>[System ready]</pre>"
    logs = _collect_logs()
    if logs:
        lh = f"<pre class='log-output'>{logs}</pre>"

    return (
        gr.update(value=bar),
        gr.update(value=st),
        btn_upd,
        gr.update(interactive=running),
        gr.update(interactive=running),
        gr.update(interactive=running),
        gr.update(interactive=running),
        gr.update(value=ov),
        gr.update(value=qs_md),
        gr.update(value=diag_md),
        gr.update(choices=choices, value=sval, visible=bool(choices)),
        gr.update(value=download_single_val, visible=done and bool(download_single_val)),
        gr.update(value=download_zip_val, visible=done and bool(download_zip_val)),
        gr.update(value=lh),
        gr.update(value=ph),
        tid,
    )


def _idle_updates() -> tuple:
    return (
        gr.update(value="<div class='progress-idle'>Ready</div>"),
        gr.update(value="**Status**: Ready"),
        gr.update(interactive=True),
        gr.update(interactive=False),
        gr.update(interactive=False),
        gr.update(interactive=False),
        gr.update(interactive=False),
        gr.update(value="*Waiting for translation task...*"),
        gr.update(value="*Quality scores will appear after translation*"),
        gr.update(value="*No diagnostic analysis yet*"),
        gr.update(choices=[], value=None, visible=False),
        gr.update(value=None, visible=False),
        gr.update(value=None, visible=False),
        gr.update(value="<pre class='log-output'>[System ready]</pre>"),
        gr.update(value=None),
        "",
    )


def _build_bar(stage: str, pct: float, msg: str) -> str:
    if stage == "completed" and pct >= 100:
        return (
            "<div class='progress-active' style='border-left: 3px solid #00B42A;'>"
            "<div style='display:flex;justify-content:space-between'>"
            "<span>Complete</span><span>100%</span></div>"
            "<div style='background:#E5E6EB;border-radius:4px;height:8px;margin:4px 0'>"
            "<div style='width:100%;background:#00B42A;height:8px;border-radius:4px'></div></div>"
            f"<div style='font-size:0.9em;color:#86909C'>{msg}</div></div>"
        )
    return (
        "<div class='progress-active'>"
        "<div style='display:flex;justify-content:space-between'>"
        f"<span>{stage}</span><span>{pct:.1f}%</span></div>"
        "<div style='background:#E5E6EB;border-radius:4px;height:8px;margin:4px 0'>"
        f"<div style='width:{pct}%;background:#165DFF;height:8px;border-radius:4px'></div></div>"
        f"<div style='font-size:0.9em;color:#86909C'>{msg}</div></div>"
    )


def _clean_surrogates(text: str) -> str:
    """Remove surrogate characters that crash orjson serialization in Gradio."""
    if not isinstance(text, str):
        return str(text) if text is not None else ""
    return "".join(c if ord(c) < 0xD800 or ord(c) > 0xDFFF else "\ufffd" for c in text)


def create_gui() -> gr.Blocks:
    """Create the main Gradio Block UI."""
    get_handler()

    with gr.Blocks(
        css=CUSTOM_CSS, title="PDFMathTranslate",
        theme=gr.themes.Soft(primary_hue="blue"), head=SESSION_JS,
    ) as gui:

        gr.HTML(
            '<div class="app-header">'
            "<h1>PDFMathTranslate</h1>"
            '<p style="color: #86909C;">'
            "Document Intelligence Runtime</p></div>"
        )

        file_state = gr.State([])
        task_id_state = gr.State("")

        with gr.Row():
            with gr.Column(scale=4):
                uc = create_upload_panel()
                cc = create_config_panel()
                pc = create_progress_panel()
                dc = create_diagnostic_panel()

            with gr.Column(scale=7):
                prc = create_preview_panel()

        t_inputs = [
            gr.State(""), gr.State("file"), uc["file_input"],
            uc["link_input"], cc["service"], cc["lang_from"],
            cc["lang_to"], cc["page_range"], gr.State(None),
            cc["threads"], cc["skip_subset_fonts"],
            cc["ignore_cache"], cc["vfont"], cc["vchar"],
            cc["mode_choice"], gr.State(""), file_state,
            cc["env0"], cc["env1"], cc["env2"], cc["prompt_env"],
            task_id_state,
        ]

        pc["translate_btn"].click(
            fn=on_translate,
            inputs=t_inputs,
            outputs=[task_id_state, pc["translate_btn"]],
        )

        pc["cancel_btn"].click(
            fn=on_cancel,
            inputs=[task_id_state],
            outputs=[task_id_state],
        )
        pc["pause_btn"].click(
            fn=on_pause,
            inputs=[task_id_state],
            outputs=[task_id_state],
        )
        pc["resume_btn"].click(
            fn=on_resume,
            inputs=[task_id_state],
            outputs=[task_id_state],
        )
        pc["skip_btn"].click(
            fn=on_skip,
            inputs=[task_id_state],
            outputs=[task_id_state],
        )

        prc["download_btn"].click(
            fn=on_download_single,
            inputs=[task_id_state],
            outputs=[prc["download_single"]],
        )
        prc["download_all_btn"].click(
            fn=on_download_all,
            inputs=[task_id_state],
            outputs=[prc["download_zip"]],
        )

        def _on_select(tid: str, val: str) -> None:
            if tid:
                GLOBAL_TASK_STORE.update(tid, selected_file=val)

        prc["result_files_dropdown"].change(
            fn=_on_select,
            inputs=[task_id_state, prc["result_files_dropdown"]],
            outputs=[],
        )

        sync_outputs = [
            pc["progress_bar"],
            pc["status_markdown"],
            pc["translate_btn"],
            pc["cancel_btn"],
            pc["pause_btn"],
            pc["resume_btn"],
            pc["skip_btn"],
            dc["node_overview"],
            dc["quality_scores"],
            dc["diagnostic_status"],
            prc["result_files_dropdown"],
            prc["download_single"],
            prc["download_zip"],
            pc["log_output"],
            prc["pdf_preview"],
            task_id_state,
        ]

        gr.Timer(value=1.5, active=True).tick(
            fn=sync_status,
            inputs=[task_id_state],
            outputs=sync_outputs,
        )

        def _on_page_load():
            svc = get_runtime_service()
            tasks = GLOBAL_TASK_STORE.list_tasks()
            if tasks:
                tid = tasks[-1]
                return sync_status(tid)
            return _idle_updates()

        gui.load(
            fn=_on_page_load,
            inputs=None,
            outputs=sync_outputs,
        )

    return gui


def _register_preview_route(gui: "gr.Blocks") -> None:
    """Register the /pdf-preview/ route on the LIVE FastAPI app.

    IMPORTANT: Gradio 5's Blocks.launch() recreates the FastAPI app
    (self.server_app = self.app = App.create_app(...)), so any route
    registered *before* launch is silently dropped and the preview iframe
    receives a {"detail": "Not Found"} 404 response. This helper must
    therefore be called AFTER gui.launch() has started.
    """
    try:
        from starlette.responses import FileResponse, Response

        app = gui.app
        if app is None or not hasattr(app, "get"):
            logger.warning("Could not register /pdf-preview/ route: no FastAPI app available")
            return

        @app.get("/pdf-preview/{path:path}")
        async def serve_preview(path: str):
            if os.path.exists(path):
                return FileResponse(
                    path,
                    media_type="application/pdf",
                    filename=os.path.basename(path),
                    content_disposition_type="inline",
                )
            return Response("File not found", status_code=404)
    except Exception as route_err:
        logger.warning("Could not register /pdf-preview/ route: %s", route_err)


def main() -> None:
    gui = create_gui()
    gui.queue(default_concurrency_limit=2, max_size=10, status_update_rate=0.1)
    gui.launch(
        server_name="0.0.0.0",
        server_port=7860,
        show_error=True,
        prevent_thread_lock=True,
    )
    # Register custom route for PDF preview iframe AFTER launch (Gradio 5
    # rebuilds the FastAPI app inside launch(), dropping pre-launch routes).
    _register_preview_route(gui)
    gui.block_thread()


if __name__ == "__main__":
    main()
