"""Post-install patch: Gradio blocks.py startup-events boot handshake tolerance.

Windows boot problem in gradio 5.20-5.35: launch() performs a synchronous
``httpx.get(.../gradio_api/startup-events)`` right after uvicorn binds. Two
independent transport failures hit Windows:

1. ``self.local_api_url`` is built from server_name ("0.0.0.0") -- a valid
   BIND address but NOT a valid client address on Windows: connecting to it
   raises WinError 10049 immediately, so the handshake can NEVER succeed and
   the page auto-open (inbrowser=True -> http://0.0.0.0:7860) never loads.
2. Environment proxies (Clash / mihomo / corporate VPN exporting HTTP(S)_PROXY)
   intercept loopback requests unless NO_PROXY covers localhost; the proxy then
   answers with an empty-body 502/404 and gradio turns it into a fatal raise
   that kills `pdf2zh.exe` ("Couldn't start the app ...").

This patch rewrites the handshake to: probe 127.0.0.1 (0.0.0.0 -> 127.0.0.1),
inject NO_PROXY entries for loopback hosts, retry boundedly (8 x 5s) with real
interpolation in the logs, and on persistent failure START THE QUEUE LOCALLY
(``self.run_startup_events()``) and continue instead of raising -- the frontend
stays functional in every environment.

Usage: python patch_gradio_startup_events.py <path_to_blocks.py>
Idempotent: a second run no-ops; v1/v2 patched files are upgraded in place.
"""

import re
import sys
import pathlib

PATTERN_MARKER = "# [pdf2zh patch] startup-events handshake"
V3_MARKER = (
    "# [pdf2zh patch] startup-events handshake v3 (loopback probe + NO_PROXY injection)"
)

VARIANTS = [
    # gradio 5.21 (verified) / any 5.2x with timeout=None
    """            if not wasm_utils.IS_WASM:
                # Cannot run async functions in background other than app's scope.
                # Workaround by triggering the app endpoint
                resp = httpx.get(
                    f"{self.local_api_url}startup-events",
                    verify=ssl_verify,
                    timeout=None,
                )
                if not resp.is_success:
                    raise Exception(
                        f"Couldn\N{RIGHT SINGLE QUOTATION MARK}t start the app because '{resp.url}' failed (code {resp.status_code}). Check your network or proxy settings to ensure localhost is accessible."
                    )
""",
    # 5.3x variant (timeout parameter, different wording)
    """            if not wasm_utils.IS_WASM:
                # Cannot run async functions in background other than app's scope.
                # Workaround by triggering the app endpoint
                resp = httpx.get(
                    f"{self.local_api_url}startup-events",
                    verify=ssl_verify,
                    timeout=60,
                )
                if not resp.is_success:
                    raise Exception(
                        f"Couldn\N{RIGHT SINGLE QUOTATION MARK}t start the app because '{resp.url}' failed (code {resp.status_code}). Check your network or proxy settings to ensure localhost is accessible."
                    )
""",
]

# NOTE: single braces {_i + 1} on purpose -- this text is spliced into the
# target file verbatim (plain str.replace, no .format, no escaping needed).
REPLACEMENT = """            if not wasm_utils.IS_WASM:
                # Cannot run async functions in background other than app's scope.
                # Workaround by triggering the app endpoint
                # V3_MARKER: probe loopback (0.0.0.0 is bind-only on Windows ->
                # WinError 10049), bypass env proxies for loopback, bounded
                # retries, then start the queue LOCALLY and continue.
                import time as _time
                import os as _os
                _np = [x.strip() for x in _os.environ.get("NO_PROXY", "").split(",") if x.strip()]
                for _h in ("127.0.0.1", "localhost", "::1", "0.0.0.0"):
                    if _h not in _np:
                        _np.append(_h)
                _os.environ["NO_PROXY"] = ",".join(_np)
                _url = f"{self.local_api_url}startup-events".replace("0.0.0.0", "127.0.0.1")
                resp = None
                for _i in range(8):
                    try:
                        resp = httpx.get(
                            _url,
                            verify=ssl_verify,
                            timeout=5.0,
                        )
                    except Exception as _exc:  # noqa: BLE001
                        print(
                            f"[pdf2zh] startup-events probe error (attempt {_i + 1}): {_exc}",
                            flush=True,
                        )
                    if resp is not None and resp.is_success:
                        print(f"[pdf2zh] startup-events handshake succeeded (attempt {_i + 1})", flush=True)
                        break
                    if resp is not None:
                        print(
                            f"[pdf2zh] startup-events attempt {_i + 1}: code={resp.status_code} "
                            f"url={resp.url} server={resp.headers.get('server') or '?'}",
                            flush=True,
                        )
                    _time.sleep(0.5)
                if resp is None or not resp.is_success:
                    _code = -1 if resp is None else resp.status_code
                    _body = "" if resp is None else resp.text[:200]
                    print(
                        f"[pdf2zh] startup-events handshake failed (code={_code}) "
                        f"after 8 attempts; starting queue locally. body={_body}",
                        flush=True,
                    )
                    try:
                        # sync startup = queue.start() + stopped=False + is_running=True
                        # + create_limiter(); extra_startup_events is empty in pdf2zh.
                        self.run_startup_events()
                        print("[pdf2zh] startup queue started locally (fallback)", flush=True)
                    except Exception as _e2:  # noqa: BLE001
                        print(f"[pdf2zh] local startup fallback failed: {_e2}", flush=True)
""".replace("V3_MARKER", V3_MARKER)

#: 升级路径用：REPLACEMENT 去掉首行前导缩进（由捕获的原始缩进补回）
REPLACEMENT_UNINDENTED = REPLACEMENT.lstrip()

# v1 升级路径：早期补丁（无本地启动回退）→ 失败分支换成带本地队列启动的
OLD_FAILURE_BLOCK = """                if resp is None or not resp.is_success:
                    _code = -1 if resp is None else resp.status_code
                    _body = "" if resp is None else resp.text[:200]
                    print(
                        f"[pdf2zh] startup-events handshake failed (code={{_code}}) "
                        f"after 8 attempts; continuing anyway. body={{_body}}",
                        flush=True,
                    )
"""

NEW_FAILURE_BLOCK = """                if resp is None or not resp.is_success:
                    _code = -1 if resp is None else resp.status_code
                    _body = "" if resp is None else resp.text[:200]
                    print(
                        f"[pdf2zh] startup-events handshake failed (code={{_code}}) "
                        f"after 8 attempts; starting queue locally. body={{_body}}",
                        flush=True,
                    )
                    try:
                        # sync startup = queue.start() + stopped=False + is_running=True
                        # + create_limiter(); extra_startup_events is empty in pdf2zh.
                        self.run_startup_events()
                        print("[pdf2zh] startup queue started locally (fallback)", flush=True)
                    except Exception as _e2:  # noqa: BLE001
                        print(f"[pdf2zh] local startup fallback failed: {{_e2}}", flush=True)
"""

#: v2 → v3 升级：整块替换（if not wasm_utils.IS_WASM: 起，到下一个 else: 止）。
#: 该块正是 v1/v2 补丁整体替换过的范围，含 `# Cannot run async functions` 锚点。
UPGRADE_RE = re.compile(
    r"(?sm)^([ \t]*)if not wasm_utils\.IS_WASM:\n"
    r"[ \t]*# Cannot run async functions.*?(?=\n[ \t]*else:)"
)
UPGRADE_REPL = r"\1" + REPLACEMENT_UNINDENTED


def patch_file(path: str) -> bool:
    p = pathlib.Path(path)
    if not p.exists():
        print(f"ERROR: {p} not found")
        return False

    content = p.read_text(encoding="utf-8")

    if V3_MARKER in content:
        print(f"{p.name} already patched (v3, marker present); skipping")
        return True

    if PATTERN_MARKER in content:
        content, n = UPGRADE_RE.subn(UPGRADE_REPL, content, count=1)
        if n > 0:
            p.write_text(content, encoding="utf-8")
            print(
                f"Upgraded {p.name}: v1/v2 -> v3 (loopback probe + NO_PROXY injection + braces fix)"
            )
            return True
        if OLD_FAILURE_BLOCK in content:  # v1 残余（无本地启动回退）→ 失败分支替换
            content = content.replace(OLD_FAILURE_BLOCK, NEW_FAILURE_BLOCK, 1)
            p.write_text(content, encoding="utf-8")
            print(f"Upgraded {p.name}: added local queue-start fallback (v2)")
            return True
        print(f"{p.name} already patched but not upgradeable; skipping")
        return True

    for idx, variant in enumerate(VARIANTS):
        if variant in content:  # exact hit
            content = content.replace(variant, REPLACEMENT, 1)
            p.write_text(content, encoding="utf-8")
            print(f"Patched {p.name} (matched variant {idx + 1})")
            return True
        # tolerate curly/straight apostrophe differences in the raise message
        norm_v = variant.replace("\u2019", "'").replace("\u2018", "'")
        norm_c = content.replace("\u2019", "'").replace("\u2018", "'")
        if norm_v in norm_c:
            norm_c = norm_c.replace(norm_v, REPLACEMENT, 1)
            p.write_text(norm_c, encoding="utf-8")
            print(f"Patched {p.name} (matched variant {idx + 1}, apostrophe-tolerant)")
            return True

    # regex fallback: swap the fatal raise inside launch() for a log-and-continue
    pat = re.compile(
        r"([ \t]*)if not resp\.is_success:\n"
        r"[ \t]*raise Exception\([^\n]*startup-events[^\n]*\n",
    )
    repl = (
        r"\1if not resp.is_success:\n"
        r"\1    print("
        r'f"[pdf2zh] startup-events handshake non-200 (code={resp.status_code}); '
        r'continuing anyway. # pdf2zh patch", flush=True)\n'
    )
    content, count = pat.subn(repl, content, count=1)
    if count > 0:
        p.write_text(content, encoding="utf-8")
        print(f"Patched {p.name} (regex fallback)")
        return True

    print("ERROR: Could not find the startup-events handshake block in blocks.py")
    print("       gradio may have changed; bump the build pin or update this script.")
    return False


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python patch_gradio_startup_events.py <path_to_blocks.py>")
        sys.exit(1)
    success = patch_file(sys.argv[1])
    sys.exit(0 if success else 1)
