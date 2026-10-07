"""runner 必须把 ``--ignore-cache`` 真正传进 CLI 的 argv。

为什么这是必须钉住的
-------------------
``pdf2zh/pdf2zh.py`` 本身**早就支持** ``--ignore-cache``（:448 定义、:1070 传给引擎）。
缺的是 runner 这一层的透传 —— 少了它，50 页"真跑"全部命中 ``~/.cache/pdf2zh``，
测出来的 66s / 130s 之类全是缓存时间，与真实吞吐无关。这正是并发收益一直只有
微基准、拿不到端到端墙钟的根因。

顺带钉住两个容易出错的方向：**不带该开关时不能漏传**（否则对照实验悄悄变成缓存对照），
以及 run-config / run-summary 要留下痕迹，否则事后无法证明那一轮到底有没有走缓存。
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


RUNNER = _load("bench_run", "doc/7p0_real_load_run.py")
VERIFY = _load("bench_verify", "doc/7p2_verify_run.py")


@pytest.fixture
def captured_argv(tmp_path, monkeypatch):
    """跑一次 runner 的 main()，抓下它交给 pdf2zh 的 argv 与写出的配置。

    两个坑，都踩过：

    - **不能**用 ``monkeypatch.setitem(sys.modules, "pdf2zh.pdf2zh", stub)``。
      runner 里写的是 ``import pdf2zh.pdf2zh as cli``，而 ``import a.b as c`` 会**先
      取父包属性** ``getattr(a, "b")`` —— 只要真实模块此前被导入过，拿到的就是真身，
      stub 被静默绕过（表现为 ``KeyError: 'argv'``：``main`` 根本没被调用）。
      直接改真身对象的 ``main`` 属性才可靠。
    - runner 的 ``main()`` 会**直接写进程环境**（``OPENCODE_TIMEOUT=180`` 等）。
      这会泄漏到同一次 pytest 进程里的其它测试：实测 ``test_env_defaults`` 因此
      看到 ``'180' != '300'`` 而失败。所以这里必须快照并还原整个环境。
    """
    import pdf2zh.pdf2zh as real_cli

    seen: dict = {}
    monkeypatch.setattr(
        real_cli,
        "main",
        staticmethod(lambda argv: seen.__setitem__("argv", list(argv)) or 0),
    )

    before = dict(os.environ)

    class _EnvGuard:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            os.environ.clear()
            os.environ.update(before)
            return False

    out = tmp_path / "run"

    def _main_with(extra):
        # main() 走 sys.argv 解析，所以直接替换它。
        monkeypatch.setattr(sys, "argv", ["runner", "--out", str(out)] + extra)
        with _EnvGuard():
            return RUNNER.main()

    return out, seen, _main_with


# --------------------------------------------------------------------------
# runner 层
# --------------------------------------------------------------------------
def test_ignore_cache_reaches_the_cli_argv(captured_argv):
    out, seen, main_with = captured_argv
    main_with(["--ignore-cache"])

    assert "--ignore-cache" in seen["argv"], seen["argv"]
    cfg = json.loads((out / "run-config.json").read_text(encoding="utf-8"))
    assert cfg["ignore_cache"] is True
    assert "--ignore-cache" in cfg["argv"]


def test_the_flag_is_appended_from_the_flag_not_hardcoded(captured_argv):
    """回归：``argv.append`` 必须由 ``args.ignore_cache`` 把守。

    钉的是**条件判断本身**。若有人把 ``if args.ignore_cache:`` 改成 ``if True:``，
    不带开关的那一轮也会偷偷绕过缓存 —— 对照实验就此变成"两次都无缓存"，
    thread=1 与 thread=4 的差值失去了意义，而且这个错误在日志里完全看不出来。
    """
    out, seen, main_with = captured_argv
    main_with([])

    assert (
        "--ignore-cache" not in seen["argv"]
    ), "the flag leaked into a run that did not ask for it"
    cfg = json.loads((out / "run-config.json").read_text(encoding="utf-8"))
    assert cfg["ignore_cache"] is False


def test_no_flag_means_no_flag(captured_argv):
    """不带开关时**不能**漏传 —— 否则对照实验会悄悄退化成缓存对照。"""
    out, seen, main_with = captured_argv
    main_with([])

    assert "--ignore-cache" not in seen["argv"], seen["argv"]
    cfg = json.loads((out / "run-config.json").read_text(encoding="utf-8"))
    assert cfg["ignore_cache"] is False


def test_summary_records_the_cache_setting(captured_argv):
    out, _seen, main_with = captured_argv
    main_with(["--ignore-cache"])

    summary = json.loads((out / "run-summary.json").read_text(encoding="utf-8"))
    assert summary["ignore_cache"] is True, (
        "run-summary must record it, otherwise a later reader cannot tell "
        "which runs were cache hits"
    )
    assert summary["thread"] == 4


def test_thread_is_still_recorded(captured_argv):
    out, _seen, main_with = captured_argv
    main_with(["--thread", "1", "--ignore-cache"])

    cfg = json.loads((out / "run-config.json").read_text(encoding="utf-8"))
    assert cfg["thread"] == 1
    assert "--thread" in cfg["argv"]


# --------------------------------------------------------------------------
# launcher 层（心跳脚本）
# --------------------------------------------------------------------------
def test_launcher_forwards_ignore_cache_to_the_runner(tmp_path, monkeypatch):
    seen: dict = {}

    class _Proc:
        pid = 4242

        def poll(self):
            return None

    def fake_popen(cmd, **kwargs):
        seen["cmd"] = list(cmd)
        return _Proc()

    monkeypatch.setattr(VERIFY.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(VERIFY, "_hb", lambda *a, **k: None)

    VERIFY.launch(tmp_path / "out", "1-50", 4, ignore_cache=True)

    assert "--ignore-cache" in seen["cmd"], seen["cmd"]
    assert str(ROOT / "doc" / "7p0_real_load_run.py") in " ".join(seen["cmd"])


def test_launcher_omits_the_flag_by_default(tmp_path, monkeypatch):
    seen: dict = {}

    class _Proc:
        pid = 4242

        def poll(self):
            return None

    monkeypatch.setattr(
        VERIFY.subprocess,
        "Popen",
        lambda cmd, **kw: (seen.__setitem__("cmd", list(cmd)), _Proc())[1],
    )
    monkeypatch.setattr(VERIFY, "_hb", lambda *a, **k: None)

    VERIFY.launch(tmp_path / "out", "1-50", 4)

    assert "--ignore-cache" not in seen["cmd"], seen["cmd"]


def test_launcher_argument_parser_exposes_the_flag():
    """CLI 表面也要有这个开关，否则没法从命令行触发无缓存对照。"""
    src = (ROOT / "doc" / "7p2_verify_run.py").read_text(encoding="utf-8")
    assert '"--ignore-cache"' in src
    assert "ignore_cache=args.ignore_cache" in src


# --------------------------------------------------------------------------
# 与产品 CLI 的契约
# --------------------------------------------------------------------------
def test_the_product_cli_really_supports_the_flag():
    """runner 传的是一个**真实存在**的选项 —— 否则实跑会直接 argparse 报错退出。"""
    src = (ROOT / "pdf2zh" / "pdf2zh.py").read_text(encoding="utf-8")
    assert '"--ignore-cache"' in src
