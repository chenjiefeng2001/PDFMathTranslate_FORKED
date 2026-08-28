"""MinerU 隔离环境解析 worker（由 :mod:`pdf2zh.kernel.mineru_env` 构建的
隔离 venv 调用；也可指向任意装有 ``mineru[pipeline]`` 的解释器）。

契约::

    python mineru_worker.py <pdf_path> <output_dir> <parse_method> <lang> [device]

``device``（可选，缺省 ``auto``）：``auto``/``cpu``/``cuda``。MinerU 3.x 的
设备决策在 :func:`mineru.utils.config_reader.get_device` —— 优先读环境变量
``MINERU_DEVICE_MODE``，其次 ``torch.cuda.is_available()``。因此这里在
``import mineru`` **之前**把 device 映射为 ``MINERU_DEVICE_MODE``：
- ``cuda`` → ``MINERU_DEVICE_MODE=cuda``（venv torch 必须为 CUDA 版，否则
  MinerU 模型加载到 cuda 会报 ``CUDA not available``，由上层先做预检）；
- ``cpu`` → ``MINERU_DEVICE_MODE=cpu``；
- ``auto``/空 → 不设置，让 MinerU 按 torch 能力自行探测（CUDA 可用即 cuda）；
- ``dml`` → MinerU 3.x 的 torch 模型不认 DirectML，按 ``auto`` 处理。

成功退出码 0 且在 ``output_dir`` 内产出 ``*_middle.json``；失败非零并把
诊断写到 stderr。本脚本必须保持 **stdlib-only**（目标 venv 里没有 pdf2zh），
且驱动代码必须在 ``__main__`` 防护内 —— MinerU pipeline 用
ProcessPoolExecutor 渲染页面图像，Windows spawn 会重新导入本模块。
"""

from __future__ import annotations

import inspect
import os
import sys

#: 上层设备名 → ``MINERU_DEVICE_MODE`` 合法值（None = 不设置，交给 MinerU 探测）。
_DEVICE_MODE_MAP = {
    "cuda": "cuda",
    "gpu": "cuda",
    "cpu": "cpu",
    "mps": "mps",
    # dml/auto/空：不设置（MinerU 的 torch 模型不认 DirectML）。
    "dml": None,
    "auto": None,
}


def _apply_device_mode(device: str) -> None:
    """在 import mineru 前设置 ``MINERU_DEVICE_MODE``（幂等、可被用户覆盖）。"""
    mode = _DEVICE_MODE_MAP.get(str(device or "auto").strip().lower())
    if mode is None:
        return
    os.environ["MINERU_DEVICE_MODE"] = mode


def _apply_conservative_vram_budget() -> None:
    """按物理显存总量做保守 batch 预算，规避小显存卡 CUDA OOM。

    MinerU 3.x 的 ``batch_ratio`` 由 ``get_vram`` 的**物理显存总量**决定
    （8GB→4、16GB→8…），未扣除系统 UI 桌面合成（Windows 常占 ~2GB）与模型
    权重本身的占用。8GB 卡实际只剩 ~6GB 空闲时 batch_ratio=4 会让 OCR 等
    批处理（base_batch_size×4）瞬间张量超限 → OOM，与 PDF 页数无关。

    通过 MinerU 官方覆盖项 ``MINERU_VIRTUAL_VRAM_SIZE`` 把估算压到保守档：
    16GB+→16（ratio=8）、8GB→6（ratio=2）、<8GB→5（ratio=1）。仅当用户未
    显式设置该变量时注入；设备非 CUDA 或探测失败时保持原样。
    """
    if os.environ.get("MINERU_VIRTUAL_VRAM_SIZE", "").strip():
        return  # 用户显式配置优先
    try:
        import torch  # noqa: PLC0415 -- venv 内必有 torch

        if not torch.cuda.is_available():
            return
        total_gb = round(
            torch.cuda.get_device_properties(0).total_memory / (1024 ** 3)
        )
    except Exception:  # noqa: BLE001 -- 探测失败则按设备模式保守处理
        total_gb = 0
    if total_gb >= 16:
        budget = "16"
    elif total_gb >= 8:
        budget = "6"  # 8GB 卡按 6GB 估算 → batch_ratio=2，显著降 OOM
    elif total_gb >= 6:
        budget = "5"  # 6GB 卡按 5GB → ratio=1
    else:
        budget = "5"
    os.environ["MINERU_VIRTUAL_VRAM_SIZE"] = budget


def main(argv: list[str]) -> int:
    if len(argv) not in (4, 5):
        print(
            "usage: mineru_worker.py <pdf_path> <output_dir> "
            "<parse_method> <lang> [device]",
            file=sys.stderr,
        )
        return 2
    pdf_path, output_dir, parse_method, lang = argv[:4]
    device = argv[4] if len(argv) >= 5 else "auto"
    os.makedirs(output_dir, exist_ok=True)

    _apply_device_mode(device)

    from mineru.cli.common import do_parse, read_fn

    try:
        pdf_bytes = read_fn(pdf_path)
    except Exception:  # noqa: BLE001 -- read_fn 兼容图片输入，失败回退直读
        with open(pdf_path, "rb") as fh:
            pdf_bytes = fh.read()

    stem = os.path.splitext(os.path.basename(pdf_path))[0]
    # CUDA 时按显存做保守 batch 预算（do_parse 内部 pipeline_analyze 才读
    # get_vram，此处设置仍先于它）。
    if _DEVICE_MODE_MAP.get(str(device or "auto").strip().lower()) == "cuda":
        _apply_conservative_vram_budget()
    wanted = {
        "output_dir": output_dir,
        "pdf_file_names": [stem],
        "pdf_bytes_list": [pdf_bytes],
        "p_lang_list": [lang],
        "backend": "pipeline",
        "parse_method": parse_method,
        "f_dump_md": False,
        "f_dump_content_list": False,
        "f_draw_layout_bbox": False,
        "f_draw_span_bbox": False,
        "f_dump_middle_json": True,
    }
    try:
        params = inspect.signature(do_parse).parameters
    except (TypeError, ValueError):  # pragma: no cover - 内置类兜底
        params = None
    if params is not None and not any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
    ):
        wanted = {k: v for k, v in wanted.items() if k in params}

    do_parse(**wanted)

    produced = any(
        name.endswith("_middle.json")
        for _root, _dirs, files in os.walk(output_dir)
        for name in files
    )
    if not produced:
        print(
            f"mineru_worker: no *_middle.json produced under {output_dir}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
