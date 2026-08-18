"""Network environment preparation shared by the GUI and CLI entry points.

Windows proxy reality
---------------------
The browser-grade proxy (Clash/VPN) lives in the WinINET registry
(``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings``), not
in environment variables. ``requests``/``httpx`` only honor env vars, while
``urllib.request.getproxies()`` has a registry fallback.

The fallback is silently DROPPED the moment ANY proxy-related env var is
present: ``getproxies_environment()`` returns a non-empty dict (e.g.
``{'no': '127.0.0.1,localhost'}`` when NO_PROXY is set) and ``getproxies()``
then treats the environment as complete and never looks at the registry.
Consequence: a naive ``NO_PROXY`` patch -- the standard fix for gradio's
loopback startup-events 502 -- strips the registry proxy from every
translator request, so requests go direct and time out on blocked hosts
(``ConnectTimeout`` to ``translate.google.com``).

``sanitize_loopback_proxy()`` does both correctly:
  1. imports the WinINET registry proxy into ``HTTP(S)_PROXY`` first (unless
     the user already configured explicit proxy env vars);
  2. builds NO_PROXY from the registry ``ProxyOverride`` list plus loopback
     hosts, keeping loopback bypassed (gradio startup handshake, browser)
     without affecting real proxied traffic.
"""

from __future__ import annotations

import logging
import os
import re
import sys

logger = logging.getLogger(__name__)

_WININET_KEY = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"

_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")

#: Loopback hosts that must always bypass the proxy.
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def _read_wininet_proxy() -> tuple[int, str | None, str | None]:
    """Read the WinINET registry proxy as ``(ProxyEnable, ProxyServer, ProxyOverride)``.

    Returns ``(0, None, None)`` on non-Windows platforms or when the registry
    key/values are absent.
    """
    if sys.platform != "win32":
        return 0, None, None
    try:
        import winreg
    except ImportError:  # pragma: no cover - Windows always has winreg
        return 0, None, None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WININET_KEY) as key:
            enable = winreg.QueryValueEx(key, "ProxyEnable")[0]
            server = winreg.QueryValueEx(key, "ProxyServer")[0]
            override = winreg.QueryValueEx(key, "ProxyOverride")[0]
    except OSError:
        return 0, None, None
    return int(enable or 0), server, override


def _normalize_proxy_url(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if not _SCHEME_RE.match(value):
        return f"http://{value}"
    return value


def import_system_proxy_to_env() -> None:
    """Copy the WinINET registry proxy into ``HTTP(S)_PROXY`` env vars.

    No-op when the user already configured ``HTTP_PROXY``/``HTTPS_PROXY``/
    ``ALL_PROXY`` (explicit config wins) or on non-Windows platforms.
    Lowercase and uppercase variants are both set so requests/httpx/urllib
    agree on every platform.
    """
    if (
        os.environ.get("HTTP_PROXY")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("ALL_PROXY")
    ):
        return
    enable, server, _override = _read_wininet_proxy()
    if not enable or not server:
        return

    http_proxy: str | None = None
    https_proxy: str | None = None
    if "=" in server:
        # Per-scheme form: "http=host:port;https=host:port;socks=host:port".
        for part in server.split(";"):
            scheme, _, value = part.partition("=")
            value = value.strip()
            if not value:
                continue
            if scheme.strip().lower() == "http":
                http_proxy = value
            elif scheme.strip().lower() == "https":
                https_proxy = value
    else:
        http_proxy = https_proxy = server

    http_url = _normalize_proxy_url(http_proxy or "")
    https_url = _normalize_proxy_url(https_proxy or "")
    if http_url:
        os.environ.setdefault("HTTP_PROXY", http_url)
        os.environ.setdefault("http_proxy", http_url)
    if https_url:
        os.environ.setdefault("HTTPS_PROXY", https_url)
        os.environ.setdefault("https_proxy", https_url)
    if http_url or https_url:
        all_url = http_url or https_url
        os.environ.setdefault("ALL_PROXY", all_url)
        os.environ.setdefault("all_proxy", all_url)
        logger.info(
            "Imported system proxy from WinINET registry: http=%s https=%s",
            http_url or "-",
            https_url or "-",
        )


def _registry_bypass_hosts() -> list[str]:
    """Return the registry ``ProxyOverride`` list as NO_PROXY host entries.

    ``<local>`` is expanded to the loopback hosts; the remaining entries
    (``localhost``, ``127.*``, ``192.168.*``, ...) are kept verbatim.
    """
    _enable, _server, override = _read_wininet_proxy()
    if not override:
        return []
    hosts: list[str] = []
    for entry in override.split(";"):
        entry = entry.strip()
        if not entry:
            continue
        if entry == "<local>":
            for h in _LOOPBACK_HOSTS:
                if h not in hosts:
                    hosts.append(h)
        elif entry not in hosts:
            hosts.append(entry)
    return hosts


def sanitize_loopback_proxy() -> None:
    """Prepare env proxy settings for the whole app (GUI + workers + CLI).

    Safe to call repeatedly (idempotent): existing NO_PROXY entries are kept,
    proxy env vars are only added when unset.
    """
    import_system_proxy_to_env()

    hosts = [h.strip() for h in os.environ.get("NO_PROXY", "").split(",") if h.strip()]
    for h in _registry_bypass_hosts():
        if h not in hosts:
            hosts.append(h)
    for h in _LOOPBACK_HOSTS:
        if h not in hosts:
            hosts.append(h)
    if hosts:
        os.environ["NO_PROXY"] = ",".join(hosts)
