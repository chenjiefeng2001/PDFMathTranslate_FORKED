"""MinerU 隔离环境解析 worker（由 :mod:`pdf2zh.kernel.mineru_env` 构建的
隔离 venv 调用；也可指向任意装有 ``mineru[pipeline]`` 的解释器）。

契约::

    python mineru_worker.py <pdf_path> <output_dir> <parse_method> <lang>

成功退出码 0 且在 ``output_dir`` 内产出 ``*_middle.json``；失败非零并把
诊断写到 stderr。本脚本必须保持 **stdlib-only**（目标 venv 里没有 pdf2zh），
且驱动代码必须在 ``__main__`` 防护内 —— MinerU pipeline 用
ProcessPoolExecutor 渲染页面图像，Windows spawn 会重新导入本模块。
"""

from __future__ import annotations

import inspect
import os
import sys


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(
            "usage: mineru_worker.py <pdf_path> <output_dir> "
            "<parse_method> <lang>",
            file=sys.stderr,
        )
        return 2
    pdf_path, output_dir, parse_method, lang = argv
    os.makedirs(output_dir, exist_ok=True)

    from mineru.cli.common import do_parse, read_fn

    try:
        pdf_bytes = read_fn(pdf_path)
    except Exception:  # noqa: BLE001 -- read_fn 兼容图片输入，失败回退直读
        with open(pdf_path, "rb") as fh:
            pdf_bytes = fh.read()

    stem = os.path.splitext(os.path.basename(pdf_path))[0]
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
