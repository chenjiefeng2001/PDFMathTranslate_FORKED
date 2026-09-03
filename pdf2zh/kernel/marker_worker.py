"""Marker 隔离环境转换 worker（由 :mod:`pdf2zh.kernel.marker_env` 构建的
隔离 venv 调用；也可指向任意装有 marker 的解释器）。

契约::

    python marker_worker.py <pdf_path> <output_dir> [mode]

- ``mode``（可选，缺省 ``fast``）：marker 2.x 转换模式 ``fast`` / ``balanced``。
  fast 用轻量 rf-detr/onnx 检测器 + 按需 OCR（CPU 默认）；balanced 用 VLM
  layout + 全页 OCR（GPU 默认）。marker 自身按设备选默认，未显式请求时不设。

成功退出码 0 且在 ``output_dir`` 内产出 ``{pdf_stem}.json``（marker 的
``JSONRenderer`` 输出，``JSONOutput`` schema）+ ``{pdf_stem}_meta.json``；
失败非零并把诊断写到 stderr。产物由上层 ``MarkerBackend.ingest_json``
离线摄入 —— 主进程零 marker 依赖。

本脚本必须保持 **stdlib-only**（目标 venv 里没有 pdf2zh），且驱动代码在
``__main__`` 防护内。环境变量透传 marker 官方配置（``TORCH_DEVICE``、
``FORCE_OCR`` 等，见 vendor/marker/marker/settings.py），worker 不做二次
封装；Windows 下强制 UTF-8 stdio（locale 默认 cp936 会炸中文输出，同
MinerU 子进程先例）。
"""

from __future__ import annotations

import os
import sys

#: 上层模式请求 → marker ``mode`` 配置（None = 不设置，交给 marker 按设备选）。
_MODE_MAP = {
    "fast": "fast",
    "balanced": "balanced",
    "auto": None,
    "": None,
}


def _force_utf8_stdio() -> None:
    """Windows locale（cp936 等）下强制 UTF-8 stdio（同 MinerU 子进程先例）。"""
    if sys.platform != "win32":
        return
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 -- 重配置失败不致命
        pass


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3):
        print(
            "usage: marker_worker.py <pdf_path> <output_dir> [mode]",
            file=sys.stderr,
        )
        return 2
    pdf_path, output_dir = argv[0], argv[1]
    mode = argv[2] if len(argv) >= 3 else ""
    if not os.path.exists(pdf_path):
        print(f"marker_worker: pdf not found: {pdf_path}", file=sys.stderr)
        return 2
    os.makedirs(output_dir, exist_ok=True)

    _force_utf8_stdio()

    # marker 2.x 官方 single-file 路径（convert_single_cli 的编程等价形）：
    # create_model_dict -> ConfigParser -> converter_cls -> save_output。
    from marker.config.parser import ConfigParser
    from marker.models import create_model_dict
    from marker.output import save_output

    cli_options = {"output_dir": output_dir, "output_format": "json"}
    marker_mode = _MODE_MAP.get(str(mode).strip().lower())
    if marker_mode:
        cli_options["mode"] = marker_mode
    config_parser = ConfigParser(cli_options)

    models = create_model_dict()
    converter_cls = config_parser.get_converter_cls()
    converter = converter_cls(
        config=config_parser.generate_config_dict(),
        artifact_dict=models,
        processor_list=config_parser.get_processors(),
        renderer=config_parser.get_renderer(),
        llm_service=config_parser.get_llm_service(),
    )
    rendered = converter(pdf_path)
    save_output(
        rendered,
        config_parser.get_output_folder(pdf_path),
        config_parser.get_base_filename(pdf_path),
    )

    # save_output 落在 output_dir/<stem>/ 子目录（get_output_folder 追加
    # stem），与 MinerU do_parse 的 `{stem}/{parse_method}/` 子目录惯例一致；
    # 这里显式校验产物存在，缺失即失败（绝不静默成功）。
    stem = os.path.splitext(os.path.basename(pdf_path))[0]
    produced = os.path.join(output_dir, stem, f"{stem}.json")
    if not os.path.exists(produced):
        print(
            f"marker_worker: no {stem}.json produced under {output_dir}",
            file=sys.stderr,
        )
        return 1
    print(produced)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
