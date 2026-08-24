"""包入口第三方噪音过滤回归（pdf2zh/__init__._quiet_third_party_noise）。

覆盖：
- pymupdf 消息被路由到 ``pymupdf.message`` logger（fitz 弃用提示不走
  warnings 模块，旧 DeprecationWarning 过滤对它无效）；
- 该 logger 的过滤器只丢弃 fitz 弃用文本，其余消息保留；
- sklearn delayed/Parallel UserWarning 被 filterwarnings 静音。
"""

from __future__ import annotations

import logging
import warnings

import pdf2zh  # noqa: F401  -- 触发包入口的噪音过滤装配


def _message_logger() -> logging.Logger:
    return logging.getLogger("pymupdf.message")


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record.getMessage())


def test_pymupdf_messages_routed_to_named_logger():
    # set_messages 已在包入口调用：pymupdf.message logger 存在且消息走 logging。
    import pymupdf

    cap = _Capture()
    lg = _message_logger()
    lg.addHandler(cap)
    try:
        pymupdf.message("routing-check")
        assert "routing-check" in cap.records
    finally:
        lg.removeHandler(cap)


def test_fitz_deprecation_message_filtered_others_kept():
    cap = _Capture()
    lg = _message_logger()
    lg.addHandler(cap)
    try:
        lg.warning("The `fitz` API is deprecated and will be removed in future.")
        lg.warning("some other pymupdf notice")
        assert not any("API is deprecated" in r for r in cap.records)
        assert "some other pymupdf notice" in cap.records
    finally:
        lg.removeHandler(cap)


def test_sklearn_parallel_userwarning_filter_registered():
    # 注意：不能在测试进程内断言 warnings.filters / 直接 warn 验证——
    # pytest 的 warning 插件会在每个测试外层 resetwarnings() 并套用自身
    # 配置，包入口注册的 ignore 会被遮蔽。端到端行为由下面的子进程测试
    # （干净解释器）覆盖。
    import subprocess
    import sys

    code = (
        "import sys, warnings\n"
        "import pdf2zh\n"
        "assert any(\n"
        "    e[0] == 'ignore'\n"
        "    and e[2] is UserWarning\n"
        "    and 'parallel\\\\.delayed' in getattr(e[1], 'pattern', '')\n"
        "    for e in warnings.filters\n"
        "), 'sklearn delayed ignore filter missing'\n"
        "warnings.warn(\n"
        "    '`sklearn.utils.parallel.delayed` should be used with '\n"
        "    '`sklearn.utils.parallel.Parallel`.', UserWarning)\n"
        "print('OK')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr.strip() == "", proc.stderr
    assert "OK" in proc.stdout


def test_sklearn_warning_silent_in_fresh_interpreter():
    import subprocess
    import sys

    code = (
        "import sys, warnings\n"
        "import pdf2zh\n"
        "warnings.warn(\n"
        "    '`sklearn.utils.parallel.delayed` should be used with '\n"
        "    '`sklearn.utils.parallel.Parallel`.', UserWarning)\n"
        "print('OK')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout
    assert "should be used with" not in proc.stderr


def test_package_import_does_not_print_fitz_warning(capsys):
    # 全新解释器里 `import pdf2zh` 后再 `import fitz` 不应产生任何输出。
    import subprocess
    import sys

    code = (
        "import io, sys\n"
        "err = io.StringIO()\n"
        "old = sys.stderr\n"
        "sys.stderr = err\n"
        "try:\n"
        "    import pdf2zh, fitz\n"
        "finally:\n"
        "    sys.stderr = old\n"
        "assert 'API is deprecated' not in err.getvalue(), err.getvalue()\n"
        "print('OK')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout
