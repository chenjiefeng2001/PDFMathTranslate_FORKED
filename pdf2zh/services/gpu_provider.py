"""onnxruntime GPU execution provider（CUDA）按需下载安装。

背景
----
PyInstaller 打包 ``onnxruntime-gpu`` 时会把 ``onnxruntime_providers_cuda.dll``
（解压后约 164MB）连同核心 onnxruntime.dll 一起收进 sidecar —— 绝大多数桌面
用户没有 NVIDIA CUDA 运行时，用不到却也照单全收，安装包被白白撑大。因此
sidecar 本体只内置 CPU provider；需要 GPU 版面加速的用户在应用内触发本模块：

    1. 从 PyPI 拉取与内置 onnxruntime 严格同版本的 ``onnxruntime-gpu`` wheel；
    2. 用 ``zipfile`` 解压出 CUDA provider DLL（wheel 即 zip，不依赖 pip）；
    3. 放置到运行时 ``onnxruntime/capi/`` 目录，后续任务创建的
       InferenceSession 自动探测到该 provider。

安全降级
--------
CUDA provider 能否加载取决于系统 CUDA/cuDNN 运行时。缺失时 onnxruntime
静默跳过该 provider，``get_available_providers()`` 不含 CUDA，
``pdf2zh.babeldoc_onnx_backend`` 的 CPU 兜底逻辑原样生效，绝不抛错。

支持 PyPI 镜像：环境变量 ``PDF2ZH_PYPI_MIRROR``（形如
``https://pypi.tuna.tsinghua.edu.cn``）覆盖默认源，便于国内网络环境。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import tempfile
import threading
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)

#: 从 wheel 中解压出来的 provider DLL 文件名。
_PROVIDER_DLL = "onnxruntime_providers_cuda.dll"

#: 下载状态（端点轮询读它；下载线程写它）。
_state: Dict[str, Any] = {
    "running": False,
    "done": False,
    "error": None,
    "downloaded_bytes": 0,
    "total_bytes": 0,
}
_lock = threading.Lock()

#: onnxruntime-gpu wheel 前缀（PyPI 上为 onnxruntime_gpu-<ver>-cpXX-...）。
_WHEEL_PREFIX = "onnxruntime_gpu-"


def _capi_dir() -> Path:
    """内置 onnxruntime 的 capi 目录（frozen 下为 _internal/onnxruntime/capi）。"""
    import onnxruntime  # noqa: PLC0415 -- 懒加载，避免模块顶层依赖重包

    return Path(onnxruntime.__file__).parent / "capi"


def provider_dll_path() -> Path:
    return _capi_dir() / _PROVIDER_DLL


def _pypi_json_url(version: str) -> str:
    mirror = os.environ.get("PDF2ZH_PYPI_MIRROR", "").strip().rstrip("/")
    base = mirror or "https://pypi.org"
    return f"{base}/pypi/onnxruntime-gpu/{version}/json"


def _open_url(url: str, timeout: float = 120.0):
    req = urllib.request.Request(url, headers={"User-Agent": "pdf2zh-gpu-provider/1"})
    return urllib.request.urlopen(req, timeout=timeout)


def _find_win_amd64_wheel_url(version: str) -> str:
    """返回与当前解释器匹配的 onnxruntime-gpu wheel 下载地址。"""
    cp_tag = f"cp{sys.version_info.major}{sys.version_info.minor}"
    # 完整双 tag（如 cp313-cp313）匹配，避免误命中 free-threaded 的
    # cp313-cp313t（其 abi tag 不同）。
    dual_tag = f"-{cp_tag}-{cp_tag}-"
    with _open_url(_pypi_json_url(version), timeout=60) as resp:
        data = json.load(resp)
    candidates = []
    for f in data.get("urls", []):
        fn = f.get("filename", "")
        if not fn.endswith(".whl") or "win_amd64" not in fn:
            continue
        if fn.startswith(f"{_WHEEL_PREFIX}{version}-") and dual_tag in fn:
            candidates.append(f)
    if not candidates:
        raise RuntimeError(
            f"PyPI 上找不到 onnxruntime-gpu=={version} 的 {cp_tag} win_amd64 wheel；"
            f"内置 onnxruntime 版本与可用发行版不匹配，或镜像不可用"
        )
    # 同 tag 下一般只有一个发行版；若多个，取文件名最长的。
    candidates.sort(key=lambda f: f["filename"], reverse=True)
    return candidates[0]["url"]


def _download_file(url: str, dest: Path) -> None:
    with _open_url(url) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        _state["total_bytes"] = total
        got = 0
        with open(dest, "wb") as fh:
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                fh.write(chunk)
                got += len(chunk)
                _state["downloaded_bytes"] = got


def _extract_cuda_provider(wheel_path: Path) -> None:
    with zipfile.ZipFile(wheel_path) as zf:
        member = next(
            (n for n in zf.namelist() if n.endswith(f"/capi/{_PROVIDER_DLL}")),
            None,
        )
        if member is None:
            raise RuntimeError(f"wheel 中未找到 onnxruntime/capi/{_PROVIDER_DLL}")
        target = provider_dll_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".part")
        try:
            with zf.open(member) as src, open(tmp, "wb") as out:
                shutil.copyfileobj(src, out)
            os.replace(tmp, target)
        finally:
            if tmp.exists():
                tmp.unlink(missing_ok=True)


# ════════════════════════════════════════════════════════════════════
# 对外 API（端点/前端调用）
# ════════════════════════════════════════════════════════════════════


def get_provider_status() -> Dict[str, Any]:
    """CUDA provider 状态：内置版本 / DLL 存在性 / 实际可用 provider 列表。"""
    import onnxruntime  # noqa: PLC0415

    dll = provider_dll_path()
    try:
        providers = list(onnxruntime.get_available_providers())
    except Exception as exc:  # noqa: BLE001 -- 状态端点只回显
        logger.warning("onnxruntime.get_available_providers() failed: %s", exc)
        providers = []
    return {
        "onnxruntime_version": getattr(onnxruntime, "__version__", ""),
        "target_path": str(dll),
        "cuda_dll_present": dll.is_file(),
        "cuda_dll_size_bytes": dll.stat().st_size if dll.is_file() else 0,
        "available_providers": providers,
        "cuda_active": any(p.startswith("CUDA") for p in providers),
        "downloading": _state["running"],
        "progress_bytes": _state["downloaded_bytes"],
        "total_bytes": _state["total_bytes"],
        "done": _state["done"],
        "last_error": _state["error"],
    }


def start_download() -> tuple[bool, str]:
    """后台线程下载并安装 CUDA provider；已运行时不重复启动。

    返回 ``(started, reason)``。
    """
    if not _lock.acquire(blocking=False):
        return False, "already running"
    if _state["running"]:
        _lock.release()
        return False, "already running"

    def _run() -> None:
        _state.update(
            running=True, done=False, error=None, downloaded_bytes=0, total_bytes=0
        )
        try:
            _install_cuda_provider()
            _state["done"] = True
        except Exception as exc:  # noqa: BLE001 -- 状态回显给前端
            logger.warning("cuda provider download failed: %s", exc)
            _state["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            _state["running"] = False
            _lock.release()

    threading.Thread(target=_run, name="gpu-provider-download", daemon=True).start()
    return True, ""


def remove_cuda_provider() -> bool:
    """删除已安装的 CUDA provider DLL，还原 CPU-only；返回是否实际删除了文件。"""
    dll = provider_dll_path()
    if dll.is_file():
        try:
            dll.unlink()
            logger.info("removed cuda provider: %s", dll)
            return True
        except OSError as exc:
            logger.warning("failed to remove %s: %s", dll, exc)
            return False
    return False


def _install_cuda_provider() -> None:
    import onnxruntime  # noqa: PLC0415

    version = getattr(onnxruntime, "__version__", None)
    if not version:
        raise RuntimeError("无法确定内置 onnxruntime 版本")
    url = _find_win_amd64_wheel_url(version)
    logger.info("downloading onnxruntime-gpu==%s wheel: %s", version, url)
    tmpdir = Path(tempfile.mkdtemp(prefix="pdf2zh-gpu-provider-"))
    try:
        wheel = tmpdir / "onnxruntime_gpu.whl"
        _download_file(url, wheel)
        _extract_cuda_provider(wheel)
        logger.info("cuda provider installed to %s", provider_dll_path())
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

