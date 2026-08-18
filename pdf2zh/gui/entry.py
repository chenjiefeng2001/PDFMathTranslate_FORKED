"""Entry point for the modular Gradio GUI — drop-in replacement for Legacy setup_gui().

Provides setup_gui() with the same API signature as the 909-line gui.py,
but delegates to the modular pdf2zh.gui.app.create_gui().
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from typing import Optional


logger = logging.getLogger(__name__)

#: Default upload cap. The 5MB legacy default rejected real-world papers
#: (e.g. MurrayII.pdf > 5MB); raised and made configurable via
#: ``PDF2ZH_MAX_FILE_SIZE`` or the ``--max-file-size`` CLI flag.
DEFAULT_MAX_FILE_SIZE = "100mb"

#: Ports scanned when the preferred port is occupied by a stale instance.
PORT_SCAN_BUDGET = 10


def _find_free_port(host: str, start_port: int, budget: int = PORT_SCAN_BUDGET) -> int:
    """Return the first free port in ``[start_port, start_port + budget)``.

    Gradio's ``launch(server_port=...)`` raises OSError when the chosen port is
    already in use (e.g. a stale pdf2zh instance still holding 7860), and the
    CLI entry point would otherwise die with a traceback before the browser
    ever opens -- exactly the "UI is alive but nothing communicates" failure
    this helper prevents.
    """
    last = start_port
    for port in range(start_port, start_port + budget):
        last = port
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((host, port))
            except OSError:
                continue
        return port
    return last


def resolve_max_file_size(explicit: Optional[str] = None) -> str:
    """Resolve the Gradio upload cap from explicit arg / env / default.

    Precedence: explicit argument > ``PDF2ZH_MAX_FILE_SIZE`` env > default.
    The value must be a gradio byte-string like ``"5mb"``/``"100mb"``;
    plain integers are interpreted as megabytes for convenience.
    """
    raw = explicit or os.environ.get("PDF2ZH_MAX_FILE_SIZE") or DEFAULT_MAX_FILE_SIZE
    raw = str(raw).strip()
    if not raw:
        return DEFAULT_MAX_FILE_SIZE
    if raw.isdigit():
        return f"{raw}mb"
    return raw


def _sanitize_loopback_proxy() -> None:
    """Prepare env proxy settings for the GUI + translation workers.

    Delegates to ``pdf2zh.networking.sanitize_loopback_proxy()`` which does
    two things:
      1. imports the WinINET system proxy (Clash/VPN) into HTTP(S)_PROXY so
         translator requests actually use it -- setting NO_PROXY without this
         drops urllib's registry fallback and every request goes direct
         (ConnectTimeout on blocked hosts like translate.google.com);
      2. makes loopback bypass env proxies; without NO_PROXY the
         startup-events handshake and browser requests to 127.0.0.1 can be
         hijacked into an empty-body 502.
    """
    from pdf2zh.networking import sanitize_loopback_proxy as _do

    _do()


def _startup_events_ok(port: int, timeout: float = 10.0) -> bool:
    """Verify the running Gradio server actually serves the API.

    Gradio 5.20-5.35 has a known boot race: ``launch()`` performs a synchronous
    ``httpx.get(.../gradio_api/startup-events)`` handshake immediately after
    uvicorn binds; on Windows the server can transiently answer 502 while it
    is still warming up, and ``launch()`` raises even though the server is
    (or becomes) fully functional. Trust the live server over the handshake.
    """
    try:
        import httpx
        resp = httpx.get(f"http://127.0.0.1:{port}/gradio_api/startup-events", timeout=timeout)
        return resp.status_code == 200
    except Exception:
        return False


def _launch(gui, *, port: int, share: bool, debug: bool, max_file_size: str, akw: dict):
    """Run gui.launch with the transient startup-events handshake tolerated.

    When the handshake 502s but the server is already serving (verified by a
    separate probe), the launch is treated as successful; otherwise the
    original exception propagates. Without this, `pdf2zh.exe` (built with
    gradio 5.21) exits setup_gui with a traceback, `_register_custom_routes`
    never runs, and the process lingers as a zombie with a dead frontend.
    """
    try:
        gui.launch(
            server_name="0.0.0.0",
            server_port=port,
            debug=debug,
            inbrowser=False,
            share=share,
            max_file_size=max_file_size,
            prevent_thread_lock=True,
            **akw,
        )
    except Exception as exc:
        if "startup-events" in str(exc) and _startup_events_ok(port):
            logger.info(
                "Gradio startup-events handshake failed transiently (%s) but the "
                "server is alive on port %d; continuing.", str(exc)[:120], port,
            )
        else:
            raise


def _ensure_queue_running(gui) -> None:
    """Start the Gradio event queue if launch() did not.

    The v1 blocks.py patch tolerated a failed startup-events handshake but
    skipped run_startup_events() entirely: the UI was served while every
    button click sat in the queue thread-pool that was never started -- the
    frontend looks alive but nothing ever executes ("卡死/无响应").

    This heals that state from our own code, whatever blocks.py variant the
    build ships (original / v1-patched / v2-patched): a started 5.x Queue
    always has ``active_jobs == [None] * max_thread_count`` while an unstarted
    one keeps it empty. Safe to call unconditionally after launch.
    """
    try:
        queue = getattr(gui, "_queue", None)
        if queue is None:
            logger.warning("[pdf2zh] queue-heal: no Gradio queue object; skipping")
            return
        if getattr(queue, "active_jobs", None):
            logger.debug("[pdf2zh] queue-heal: queue already running")
            return
        if not hasattr(gui, "run_startup_events"):
            logger.warning("[pdf2zh] queue-heal: run_startup_events unavailable; skipping")
            return
        logger.warning("[pdf2zh] queue-heal: queue never started; starting it now")
        gui.run_startup_events()
    except Exception as exc:  # noqa: BLE001 -- never kill boot over this
        logger.error("[pdf2zh] queue-heal failed: %s", exc)


def _queue_is_dead(gui) -> bool:
    """True when the Gradio event queue is absent, unstarted or closed.

    A 5.x queue is alive iff ``active_jobs`` is a non-empty list and
    ``stopped`` is False. Anything else means every button click sits in a
    dead queue -- the UI serves but nothing ever executes ("卡死/无响应").
    """
    queue = getattr(gui, "_queue", None)
    if queue is None:
        return True
    return not getattr(queue, "active_jobs", None) or bool(getattr(queue, "stopped", True))


def _start_queue_watchdog(gui, interval: float = 60.0) -> None:
    """S0: runtime watchdog that heals a died Gradio event queue.

    If the queue thread-pool dies mid-session, the frontend looks alive but
    every button click never executes. Every ``interval`` seconds this daemon
    checks queue liveness and restarts it via ``run_startup_events()`` when
    dead (idempotent-ish: only fires when actually dead; a healthy queue is a
    cheap attribute check).
    """

    def _watch() -> None:
        while True:
            time.sleep(interval)
            try:
                if not _queue_is_dead(gui):
                    continue
                if not hasattr(gui, "run_startup_events"):
                    continue
                logger.warning("[pdf2zh] queue-watchdog: queue dead; restarting it")
                gui.run_startup_events()
            except Exception:  # noqa: BLE001 -- watchdog must never die
                logger.exception("[pdf2zh] queue-watchdog error")

    threading.Thread(
        target=_watch, name="pdf2zh-queue-watchdog", daemon=True,
    ).start()


def setup_gui(
    share: bool = False,
    auth_file: list[str] | None = None,
    server_port: int = 7860,
    debug: bool = False,
    max_file_size: Optional[str] = None,
) -> None:
    """Launch the modular Gradio Web UI (V4/V5 capable).

    Args:
        share: Whether to create a public share link via Gradio.
        auth_file: List of [username, password] for authentication.
        server_port: Port to bind the server to (default 7860).
        debug: Enable Gradio debug mode (dev only; default False).
        max_file_size: Upload size cap (gradio string like ``"100mb"``, or
            a plain integer meaning megabytes). Falls back to the
            ``PDF2ZH_MAX_FILE_SIZE`` env var, then ``100mb``.

    Notes:
        ``launch(prevent_thread_lock=True)`` is REQUIRED here: without it
        ``launch()`` blocks the main thread forever and the custom FastAPI
        routes (``/gui/events`` SSE, ``/gui/logs``, ``/pdf-preview``) below
        are never registered -- Gradio 5 rebuilds the ASGI app inside
        ``launch()``, so registering before launch would also be dropped.
        The browser keeps an EventSource on ``/gui/events``; if that route is
        missing, the whole event-driven frontend loses server push and looks
        completely dead.
    """
    from pdf2zh.gui.app import create_gui
    _sanitize_loopback_proxy()
    gui = create_gui()
    akw: dict = {}
    if auth_file and len(auth_file) == 2 and auth_file[0]:
        akw["auth"] = auth_file
        akw["auth_message"] = "Enter credentials to access the translation service."

    gui.queue(default_concurrency_limit=2, max_size=20, status_update_rate=0.1)

    requested = server_port
    port = _find_free_port("0.0.0.0", requested)
    if port != requested:
        logger.warning(
            "Port %s is occupied by another instance; the GUI will listen on %s instead.",
            requested, port,
        )

    try:
        _launch(gui, port=port, share=share, debug=debug,
                max_file_size=resolve_max_file_size(max_file_size), akw=akw)
    except ValueError as exc:
        msg = str(exc)
        if "localhost" in msg.lower() or "share" in msg.lower():
            # gradio 5.21 的 url_ok(local_url) 二次检查与 startup-events 握手
            # 是同一根因（localhost 瞬时 502）；先实测端口，活着就别折腾 share
            if _startup_events_ok(port):
                logger.info(
                    "Gradio reported 'localhost not accessible' (%s) but the "
                    "server is already serving on port %d; continuing without "
                    "share fallback.", str(exc)[:100], port,
                )
            else:
                logger.warning("Localhost not accessible, falling back to share=True")
                try:
                    _launch(gui, port=port, share=True, debug=debug,
                            max_file_size=resolve_max_file_size(max_file_size), akw=akw)
                except Exception as exc2:
                    logger.error("Failed to launch with share=True: %s", exc2)
                    raise exc2 from exc
        else:
            raise
    _ensure_queue_running(gui)
    _start_queue_watchdog(gui)
    _register_custom_routes(gui)
    _open_browser(port)

    try:
        gui.block_thread()
    except KeyboardInterrupt:
        pass


def _open_browser(port: int) -> None:
    """Open the UI on 127.0.0.1: 0.0.0.0 is bind-only on Windows (WinError
    10049 as a client address), so gradio's own inbrowser=True auto-open
    (local_url -> http://0.0.0.0:port) would spin forever."""
    try:
        import webbrowser

        webbrowser.open(f"http://127.0.0.1:{port}")
    except Exception as exc:  # noqa: BLE001
        logger.debug("Failed to open browser: %s", exc)


def _register_custom_routes(gui) -> None:
    """Register /pdf-preview/, /gui/events and /gui/logs AFTER launch (Gradio 5
    rebuilds the FastAPI app inside launch(), dropping pre-launch routes)."""
    from pdf2zh.gui.app import (
        _register_events_route,
        _register_logs_route,
        _register_preview_route,
    )

    _register_preview_route(gui)
    _register_events_route(gui)
    _register_logs_route(gui)


if __name__ == "__main__":
    from pdf2zh.pdf2zh import spawn_child_yields_to

    if spawn_child_yields_to():
        raise SystemExit(0)
    logging.basicConfig(level=logging.DEBUG)
    setup_gui()
