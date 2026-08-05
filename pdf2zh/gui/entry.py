"""Entry point for the modular Gradio GUI — drop-in replacement for Legacy setup_gui().

Provides setup_gui() with the same API signature as the 909-line gui.py,
but delegates to the modular pdf2zh.gui.app.create_gui().
"""

from __future__ import annotations

import logging


logger = logging.getLogger(__name__)


def setup_gui(
    share: bool = False,
    auth_file: list[str] | None = None,
    server_port: int = 7860,
    debug: bool = False,
) -> None:
    """Launch the modular Gradio Web UI (V4/V5 capable).

    Args:
        share: Whether to create a public share link via Gradio.
        auth_file: List of [username, password] for authentication.
        server_port: Port to bind the server to (default 7860).
        debug: Enable Gradio debug mode (dev only; default False).
    """
    from pdf2zh.gui.app import create_gui
    gui = create_gui()
    akw: dict = {}
    if auth_file and len(auth_file) == 2 and auth_file[0]:
        akw["auth"] = auth_file
        akw["auth_message"] = "Enter credentials to access the translation service."

    gui.queue(default_concurrency_limit=2, max_size=10, status_update_rate=0.1)

    try:
        gui.launch(
            server_name="0.0.0.0",
            server_port=server_port,
            debug=debug,
            inbrowser=True,
            share=share,
            max_file_size="5mb",
            **akw,
        )
    except ValueError as exc:
        msg = str(exc)
        if "localhost" in msg.lower() or "share" in msg.lower():
            logger.warning("Localhost not accessible, falling back to share=True")
            try:
                gui.launch(
                    server_name="0.0.0.0",
                    server_port=server_port,
                    debug=debug,
                    inbrowser=True,
                    share=True,
                    **akw,
                )
            except Exception as exc2:
                logger.error("Failed to launch with share=True: %s", exc2)
                raise exc2 from exc
        else:
            raise
    _register_custom_routes(gui)


def _register_custom_routes(gui) -> None:
    """Register /pdf-preview/ and /gui/events AFTER launch (Gradio 5 rebuilds
    the FastAPI app inside launch(), dropping pre-launch routes)."""
    from pdf2zh.gui.app import (
        _register_events_route,
        _register_preview_route,
    )

    _register_preview_route(gui)
    _register_events_route(gui)


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    setup_gui()
