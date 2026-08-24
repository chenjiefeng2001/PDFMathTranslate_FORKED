"""Regression tests for the GUI entry-point launcher (entry.py).

Covers the two real-world launch failures fixed on 2026-08-08:

* F12a: ``setup_gui()`` used to call ``launch()`` WITHOUT
        ``prevent_thread_lock=True``, so the main thread blocked forever and
        the custom FastAPI routes (``/gui/events`` SSE, ``/gui/logs``,
        ``/pdf-preview``) were never registered - the browser's EventSource
        hit 404 and the whole event-driven frontend went silent.
* F12b: a stale instance holding the preferred port made ``launch()`` raise
        OSError and the CLI died with a traceback; ``_find_free_port`` now
        slides to the next free port before launching.
"""

import socket

import pytest

from pdf2zh.gui.entry import _find_free_port, resolve_max_file_size


class TestFindFreePort:
    def test_returns_first_free_port(self):
        assert _find_free_port("127.0.0.1", 30000, budget=5) == 30000

    def test_skips_occupied_port(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
            held.bind(("127.0.0.1", 0))
            held.listen(1)
            busy = held.getsockname()[1]
            assert _find_free_port("127.0.0.1", busy, budget=5) != busy

    def test_returns_last_when_budget_exhausted(self):
        assert _find_free_port("127.0.0.1", 65400, budget=1) == 65400


class TestResolveMaxFileSize:
    def test_explicit_arg_wins(self):
        assert resolve_max_file_size("5mb") == "5mb"

    def test_integer_means_megabytes(self):
        assert resolve_max_file_size("42") == "42mb"

    def test_blank_falls_back_to_env(self, monkeypatch):
        monkeypatch.delenv("PDF2ZH_MAX_FILE_SIZE", raising=False)
        monkeypatch.setenv("PDF2ZH_MAX_FILE_SIZE", "7mb")
        assert resolve_max_file_size("") == "7mb"

    def test_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.delenv("PDF2ZH_MAX_FILE_SIZE", raising=False)
        assert resolve_max_file_size("") == "100mb"


class TestStartupEventsHandshakeTolerance:
    """F13 (2026-08-09): gradio 5.20-5.35's boot handshake can 502 transiently
    on Windows; a dead-looking handshake must not kill an already-serving app."""

    def _fake_gui(self, error=None):
        class FakeGui:
            def __init__(self):
                self.called = 0

            def launch(self, **kw):
                self.called += 1
                if error:
                    raise error

        return FakeGui()

    def test_handshake_502_tolerated_when_server_alive(self, monkeypatch):
        from pdf2zh.gui import entry

        gui = self._fake_gui(
            error=Exception(
                "Couldn’t start the app because 'http://localhost:7860/gradio_api/"
                "startup-events' failed (code 502)."
            )
        )
        monkeypatch.setattr(entry, "_startup_events_ok", lambda port: True)
        entry._launch(
            gui, port=7860, share=False, debug=False, max_file_size="100mb", akw={}
        )
        assert gui.called == 1

    def test_unrelated_launch_error_still_raises(self, monkeypatch):
        from pdf2zh.gui import entry

        gui = self._fake_gui(error=OSError("address already in use"))
        monkeypatch.setattr(entry, "_startup_events_ok", lambda port: True)
        try:
            entry._launch(
                gui, port=7860, share=False, debug=False, max_file_size="100mb", akw={}
            )
        except OSError:
            pass
        else:
            raise AssertionError("unrelated launch error must re-raise")

    def test_handshake_failure_with_dead_server_re_raises(self, monkeypatch):
        from pdf2zh.gui import entry

        gui = self._fake_gui(error=Exception("startup-events failed (code 502)"))
        monkeypatch.setattr(entry, "_startup_events_ok", lambda port: False)
        raised = False
        try:
            entry._launch(
                gui, port=7860, share=False, debug=False, max_file_size="100mb", akw={}
            )
        except Exception:
            raised = True
        assert raised

    def test_startup_events_ok_probes_port(self, monkeypatch):
        from pdf2zh.gui import entry
        import httpx

        monkeypatch.setattr(httpx, "get", lambda url, timeout=10: httpx.Response(200))
        assert entry._startup_events_ok(7860) is True

    def test_url_ok_valueerror_alive_skips_share_fallback(self, monkeypatch):
        """gradio 5.21 的 url_ok(local_url) 二次检查与握手同根因：端口实测活着
        时不得走 share 回退（该回退会二度 launch 且打满日志）。"""
        from pdf2zh.gui import entry
        import pdf2zh.gui.app as app_module
        from unittest.mock import MagicMock

        gui = MagicMock()
        gui.launch.side_effect = ValueError(
            "When localhost is not accessible, a shareable link must be created. "
            "Please set share=True or check your proxy settings..."
        )
        gui.block_thread = MagicMock()
        monkeypatch.setattr(app_module, "create_gui", lambda: gui)
        monkeypatch.setattr(entry, "_startup_events_ok", lambda port: True)
        monkeypatch.setattr(entry, "_find_free_port", lambda host, port: port)
        register_calls = []
        monkeypatch.setattr(
            entry, "_register_custom_routes", lambda g: register_calls.append("routes")
        )
        entry.setup_gui(server_port=7870)
        assert gui.launch.call_count == 1
        assert register_calls == ["routes"]

    def test_url_ok_valueerror_dead_server_still_raises(self, monkeypatch):
        from pdf2zh.gui import entry
        from unittest.mock import MagicMock

        gui = MagicMock()
        gui.launch.side_effect = ValueError(
            "When localhost is not accessible, a shareable link must be created..."
        )
        monkeypatch.setattr(entry, "_startup_events_ok", lambda port: False)
        with pytest.raises(ValueError):
            entry._launch(
                gui, port=7870, share=False, debug=False, max_file_size="100mb", akw={}
            )
