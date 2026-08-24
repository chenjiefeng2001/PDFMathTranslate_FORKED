"""Tests for pdf2zh.networking proxy environment preparation.

Regression guard for the 2026-08-13 proxy regression: setting NO_PROXY
(loopback 502 fix) without importing the WinINET registry proxy into env vars
drops urllib's registry fallback, so every translator request goes direct and
times out on blocked hosts (``ConnectTimeout`` to translate.google.com).
"""

import os
import urllib.request

from pdf2zh import networking


class TestImportSystemProxyToEnv:
    def _clear_proxy_env(self, monkeypatch):
        for k in (
            "HTTP_PROXY",
            "http_proxy",
            "HTTPS_PROXY",
            "https_proxy",
            "ALL_PROXY",
            "all_proxy",
        ):
            monkeypatch.delenv(k, raising=False)

    def test_imports_registry_proxy_when_no_env_proxy(self, monkeypatch):
        self._clear_proxy_env(monkeypatch)
        monkeypatch.setattr(
            networking,
            "_read_wininet_proxy",
            lambda: (1, "127.0.0.1:7890", "localhost;<local>"),
        )
        networking.import_system_proxy_to_env()
        assert os.environ.get("HTTP_PROXY") == "http://127.0.0.1:7890"
        assert os.environ.get("HTTPS_PROXY") == "http://127.0.0.1:7890"
        assert os.environ.get("ALL_PROXY") == "http://127.0.0.1:7890"

    def test_respects_explicit_env_proxy(self, monkeypatch):
        self._clear_proxy_env(monkeypatch)
        monkeypatch.setenv("HTTP_PROXY", "http://explicit:3128")
        monkeypatch.setenv("HTTPS_PROXY", "http://explicit:3128")

        def _boom():
            raise AssertionError("registry must not be read when env proxy set")

        monkeypatch.setattr(networking, "_read_wininet_proxy", _boom)
        networking.import_system_proxy_to_env()
        assert os.environ.get("HTTP_PROXY") == "http://explicit:3128"
        assert os.environ.get("HTTPS_PROXY") == "http://explicit:3128"

    def test_noop_when_registry_proxy_disabled(self, monkeypatch):
        self._clear_proxy_env(monkeypatch)
        monkeypatch.setattr(networking, "_read_wininet_proxy", lambda: (0, None, None))
        networking.import_system_proxy_to_env()
        assert not os.environ.get("HTTP_PROXY")
        assert not os.environ.get("HTTPS_PROXY")

    def test_per_scheme_registry_value(self, monkeypatch):
        self._clear_proxy_env(monkeypatch)
        monkeypatch.setattr(
            networking,
            "_read_wininet_proxy",
            lambda: (
                1,
                "http=127.0.0.1:7890;https=127.0.0.1:7891;socks=127.0.0.1:7892",
                "",
            ),
        )
        networking.import_system_proxy_to_env()
        assert os.environ.get("HTTP_PROXY") == "http://127.0.0.1:7890"
        assert os.environ.get("HTTPS_PROXY") == "http://127.0.0.1:7891"
        assert os.environ.get("ALL_PROXY") == "http://127.0.0.1:7890"


class TestSanitizeLoopbackProxy:
    def _clear_proxy_env(self, monkeypatch):
        for k in (
            "HTTP_PROXY",
            "http_proxy",
            "HTTPS_PROXY",
            "https_proxy",
            "ALL_PROXY",
            "all_proxy",
            "NO_PROXY",
            "no_proxy",
        ):
            monkeypatch.delenv(k, raising=False)

    def test_builds_no_proxy_from_registry_override_and_loopback(self, monkeypatch):
        self._clear_proxy_env(monkeypatch)
        monkeypatch.setattr(
            networking,
            "_read_wininet_proxy",
            lambda: (1, "127.0.0.1:7890", "localhost;127.*;<local>"),
        )
        networking.sanitize_loopback_proxy()
        no_proxy = os.environ.get("NO_PROXY", "")
        for h in ("127.0.0.1", "localhost", "::1", "127.*"):
            assert h in no_proxy, f"{h} missing from NO_PROXY={no_proxy!r}"

    def test_keeps_existing_no_proxy_entries(self, monkeypatch):
        self._clear_proxy_env(monkeypatch)
        monkeypatch.setenv("NO_PROXY", "example.com")
        monkeypatch.setattr(networking, "_read_wininet_proxy", lambda: (0, None, None))
        networking.sanitize_loopback_proxy()
        no_proxy = os.environ.get("NO_PROXY", "")
        assert "example.com" in no_proxy
        assert "localhost" in no_proxy

    def test_translate_domain_proxied_and_loopback_bypassed(self, monkeypatch):
        """End-to-end: after sanitize, Google goes through the proxy, loopback does not."""
        self._clear_proxy_env(monkeypatch)
        monkeypatch.setattr(
            networking,
            "_read_wininet_proxy",
            lambda: (1, "127.0.0.1:7890", "localhost;127.*;<local>"),
        )
        networking.sanitize_loopback_proxy()
        proxies = urllib.request.getproxies()
        assert proxies.get("http") == "http://127.0.0.1:7890"
        assert proxies.get("https") == "http://127.0.0.1:7890"
        assert urllib.request.proxy_bypass("translate.google.com") is False
        assert urllib.request.proxy_bypass("127.0.0.1") is True
        assert urllib.request.proxy_bypass("localhost") is True
