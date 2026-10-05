"""打包产物的代理链路：sidecar 入口必须自己 bootstrap 一次系统代理。

缺陷
----
浏览器级代理（Clash/VPN）只存在于 WinINET 注册表，而 ``requests`` /
``httpx`` 只认环境变量。``pdf2zh.networking.sanitize_loopback_proxy()`` 负责
两者之间的搬运，外加把回环地址写进 ``NO_PROXY``。

CLI（``pdf2zh.pdf2zh``）与 Gradio GUI（``pdf2zh.gui.entry``）都调用了它，
**只有 sidecar 入口漏了** —— 而 sidecar 正是 Tauri 桌面应用的翻译进程本体。
症状是「源码直跑能走代理、打包后翻译请求直连超时」，而且只在打包产物里出现。

顺序也是契约的一部分：translator 的 ``set_envs`` 从 ``os.environ`` 拷值，
所以 bootstrap 必须早于 runtime service（及其预热的 translator registry）。
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SIDECAR_PY = ROOT / "deploy" / "pdf2zh_sidecar.py"


@pytest.fixture()
def sidecar():
    """按文件路径加载 sidecar 入口（它不是包的一部分）。"""
    spec = importlib.util.spec_from_file_location("_sidecar_under_test", SIDECAR_PY)
    assert spec and spec.loader, f"cannot load {SIDECAR_PY}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def clean_proxy_env(monkeypatch):
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        monkeypatch.delenv(k, raising=False)
        # lowercase variants matter too: requests reads both
        monkeypatch.delenv(k.lower(), raising=False)


def _fake_registry(monkeypatch, enable=1, server="127.0.0.1:7890", override="<local>"):
    import pdf2zh.networking as nw

    monkeypatch.setattr(
        nw, "_read_wininet_proxy", lambda: (enable, server, override), raising=False
    )


# --------------------------------------------------------------------------
# bootstrap_network 本身
# --------------------------------------------------------------------------
def test_bootstrap_imports_registry_proxy(sidecar, monkeypatch, clean_proxy_env):
    _fake_registry(monkeypatch)
    sidecar.bootstrap_network()
    assert os.environ["HTTP_PROXY"] == "http://127.0.0.1:7890"
    assert os.environ["HTTPS_PROXY"] == "http://127.0.0.1:7890"


def test_bootstrap_keeps_loopback_out_of_the_proxy(
    sidecar, monkeypatch, clean_proxy_env
):
    """桌面壳就是走 127.0.0.1 与 sidecar 通信的。

    只导入代理而不写 NO_PROXY，会把壳↔sidecar 的本地请求也塞进代理 ——
    那时翻译能不能走代理已经不重要了，应用先连不上自己的后端。
    """
    _fake_registry(monkeypatch)
    sidecar.bootstrap_network()
    noproxy = os.environ["NO_PROXY"]
    for host in ("127.0.0.1", "localhost", "::1"):
        assert host in noproxy, f"{host} must bypass the proxy, NO_PROXY={noproxy!r}"


def test_bootstrap_respects_an_explicit_proxy(sidecar, monkeypatch, clean_proxy_env):
    """用户显式配了 env 变量时，注册表不得覆盖（``import_system_proxy_to_env``
    是 setdefault 语义；这里钉住 sidecar 走的是同一条路径）。"""
    _fake_registry(monkeypatch)
    monkeypatch.setenv("HTTPS_PROXY", "http://explicit:1080")
    sidecar.bootstrap_network()
    assert os.environ["HTTPS_PROXY"] == "http://explicit:1080"


def test_bootstrap_failure_does_not_raise(sidecar, monkeypatch, clean_proxy_env):
    """代理探测失败只降级为直连，绝不能让整个应用起不来。"""
    import pdf2zh.networking as nw

    def boom():
        raise OSError("registry unavailable")

    monkeypatch.setattr(nw, "_read_wininet_proxy", boom, raising=False)
    sidecar.bootstrap_network()  # must not raise


# --------------------------------------------------------------------------
# 调用顺序：必须早于 runtime service 构造
# --------------------------------------------------------------------------
class _Stop(BaseException):
    """Sentinel: uvicorn.run 不会真的起服务。"""


def test_bootstrap_runs_before_the_runtime_service_is_built(
    sidecar, monkeypatch, clean_proxy_env
):
    """translator 的 set_envs 从 os.environ 拷值 —— 晚一步就拿不到代理。

    这里不打桩源码，而是把 ``uvicorn`` / 服务模块换成假模块，让 ``main()``
    真跑一遍：假 ``get_runtime_service`` 会记下它被调用那一刻的环境。
    """
    _fake_registry(monkeypatch)
    monkeypatch.setattr(sidecar.multiprocessing, "freeze_support", lambda: None)
    monkeypatch.setattr(sys, "argv", ["pdf2zh-api-sidecar", "--port", "1"])

    seen = {}

    fake_uvicorn = types.ModuleType("uvicorn")

    def _run(*a, **k):
        raise _Stop()

    fake_uvicorn.run = _run
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)

    fake_api = types.ModuleType("pdf2zh.services.api")
    fake_api.create_api_app = lambda **k: types.SimpleNamespace()
    monkeypatch.setitem(sys.modules, "pdf2zh.services.api", fake_api)

    fake_rt = types.ModuleType("pdf2zh.services.runtime_singleton")

    def _svc():
        seen["http_proxy_at_service_construction"] = os.environ.get("HTTP_PROXY")
        seen["no_proxy_at_service_construction"] = os.environ.get("NO_PROXY")
        return object()

    fake_rt.get_runtime_service = _svc
    monkeypatch.setitem(sys.modules, "pdf2zh.services.runtime_singleton", fake_rt)

    with pytest.raises(_Stop):
        sidecar.main()

    assert seen.get("http_proxy_at_service_construction") == (
        "http://127.0.0.1:7890"
    ), (
        "runtime service 构造时环境里还没有代理 —— 预热的 translator registry "
        "会永久拿不到它（translator 的 set_envs 只在构造时拷一次 os.environ）"
    )
    noproxy = seen.get("no_proxy_at_service_construction") or ""
    assert "127.0.0.1" in noproxy, "NO_PROXY 未在服务构造前就绪"


def test_spec_collects_the_network_module():
    """``pdf2zh.networking`` 在 main() 里是延迟导入，漏收会被 except 吞掉，
    表现为打包后静默不走代理 —— 没有任何报错。"""
    spec = (ROOT / "deploy" / "pdf2zh-api-sidecar.spec").read_text(encoding="utf-8")
    assert "'pdf2zh.networking'" in spec, (
        "pdf2zh.networking must be an explicit hiddenimport: it is imported "
        "inside main(), and a missing module is swallowed by bootstrap_network"
    )
