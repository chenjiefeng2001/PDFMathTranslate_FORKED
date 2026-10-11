"""翻译节流的运行时生效：GUI 改完设置，正在跑的任务立刻按新间隔走。

为什么值得单独验
---------------
节流间隔是从 ConfigManager **运行时**读取的（``_request_interval()``）。这个
性质是 GUI 设置抽屉的全部意义：桌面端在进程起来之后才写配置。如果哪天有人图
省事把读取挪回模块导入期，症状不是报错而是 ——

    用户在 GUI 里填了 1 秒，点了保存，后端返回 200，然后什么都不变。
    已经启动的任务继续按老速率打爆出口 IP，新任务也要等下次重启。

单元测试覆盖不到这一层：它们调 ``_request_interval()`` 时进程里根本没有任务
在跑。这里跑真链路并在任务执行途中改设置，看后面的请求是否照新间隔走。

为什么在 ``translate_document`` 这一层测，而不是 ``run_magicpdf_main``
---------------------------------------------------------------
先按整条 CLI 管线写了一版，结果它把"文档块数"当成了"请求数"，而这是错的：
渲染阶段（BabelDOC / render takeover）自己也会调翻译器，实测一次 50 块的文档
最终发出 486 次请求。节流器管的是 ``BaseTranslator.translate``，而
``translate_document`` 正好就是它的翻译阶段 —— 在那一层测量，量到的就是节流
器管辖的东西，且不受渲染阶段怎么刷新的影响。

管线接线由 :func:`test_cli_pipeline_goes_through_the_throttled_translate`
单独用静态断言钉住。
"""

from __future__ import annotations

import threading
import time

import pytest

from pdf2zh import translator
from pdf2zh.v3.canonical_page import BlockModel, LineModel, PageModel
from pdf2zh.v3.document_model import DocumentModel, translate_document


def _block(text: str) -> BlockModel:
    return BlockModel(
        text=text,
        kind="paragraph",
        x0=72.0,
        y0=700.0,
        x1=540.0,
        y1=722.0,
        lines=[
            LineModel(text=text, baseline=0.0, x0=72.0, y0=700.0, x1=540.0, y1=722.0)
        ],
        metadata={"font_size": 12.0},
    )


def _doc(pages: int, blocks_per_page: int) -> DocumentModel:
    model = DocumentModel()
    for p in range(pages):
        page = PageModel(page_num=p, width=612.0, height=792.0)
        page.blocks = [
            _block(f"page {p} block {b} body text") for b in range(blocks_per_page)
        ]
        model.pages.append(page)
    return model


class _NoopCache:
    """什么都不做的缓存替身。

    ``BaseTranslator.translate`` 在 ``do_translate`` 之后还会 ``cache.set``；
    共享的 SQLite 缓存在别的用例跑完后常常是坏的，重试一次要吃掉约 0.4 秒 ——
    那段耗时会被算进"请求间隔"，于是把缓存故障误判成节流。换掉最干净。
    """

    def get(self, *a, **kw):
        return None

    def set(self, *a, **kw):
        return None

    def add_params(self, *a, **kw):
        return None


class _SlowTranslator(translator.BaseTranslator):
    """每次都真的发一次请求的假译者：``translate`` 走的是真身。

    不用 Mock：一旦 Mock 掉 ``translate``，``_throttle`` 也跟着被绕过去，那条
    链路等于没测。这里只替换最深处不发网络的部分（``do_translate``）。
    """

    # 名字必须 <20 字符：TranslationCache 用它拼缓存键，且有硬断言
    name = "slowthrottle"
    lang_map = {}

    def __init__(self, delay: float):
        super().__init__("en", "zh", "", True)
        self.delay = delay
        self.starts: list[float] = []
        self._lock = threading.Lock()
        self._saw_enough = threading.Event()
        self.flip_at = 5
        self.cache = _NoopCache()

    def do_translate(self, text):
        with self._lock:
            self.starts.append(time.monotonic())
            reached = len(self.starts) >= self.flip_at
        if reached:
            self._saw_enough.set()
        time.sleep(self.delay)
        return "译:" + text


@pytest.fixture()
def throttle_env(monkeypatch):
    """干净的限速状态：无环境变量、无用户配置、无历史预约。"""
    from pdf2zh.config import ConfigManager

    monkeypatch.delenv("PDF2ZH_MIN_REQUEST_INTERVAL", raising=False)
    ConfigManager.set(translator._MIN_REQUEST_INTERVAL_KEY, None)
    translator._last_request_at = 0.0
    yield ConfigManager
    # 收尾：否则这条用例的间隔会漏给同会话里后面的用例
    ConfigManager.set(translator._MIN_REQUEST_INTERVAL_KEY, None)
    translator._last_request_at = 0.0


@pytest.mark.parametrize("packed", [False, True], ids=["per_block", "batched"])
def test_running_task_picks_up_the_new_interval(monkeypatch, throttle_env, packed):
    """任务跑到一半改设置，后面的请求必须按新间隔走。

    规模取 50 块 / 4 线程，翻转点落在第 5 次请求。文档再小就会"翻转即收尾"，
    前后两段各自只剩两三个样本 —— 实测过：两个请求的跨度量不到 0.05s，与
    "不限速"和"限速 0.1s"都只差一点点，判据失效。

    间隔取 0.25s：打包模式下每个请求本身就带着约 25ms 攒批窗口 + 20ms 假延迟，
    5 个请求铺开 0.2s 是常态。间隔只取 0.1s 的话前后两段差不到两倍，判据分不
    开（实测就是这么偶发失败的）；0.25s 下差一个数量级。
    """
    interval = 0.25
    monkeypatch.setenv("PDF2ZH_PARAGRAPH_BATCH", "1" if packed else "0")

    fake = _SlowTranslator(delay=0.02)
    model = _doc(pages=5, blocks_per_page=10)

    worker = threading.Thread(
        target=lambda: translate_document(
            model, lambda t: fake.translate(t), lang_out="zh", thread=4
        ),
        daemon=True,
    )
    worker.start()

    # 等任务真的跑起来：前若干次请求不限速（间隔 0），随后改设置。
    # 这一步是整条用例的关键 —— 只等不改的话，前后两段都不限速，任何断言都会
    # 通过，测试变成摆设。
    got_window = fake._saw_enough.wait(timeout=20)
    throttle_env.set(translator._MIN_REQUEST_INTERVAL_KEY, interval)
    flipped_at = time.monotonic()

    worker.join(timeout=60)
    assert not worker.is_alive(), "翻译任务在 60s 内没跑完"

    with fake._lock:
        starts = sorted(fake.starts)
    assert got_window, "任务在设定的观察点之前就跑完了，没有可比窗口"
    # 打包模式下请求数少于块数（这正是打包的目的），但两种模式都必须有足够
    # 样本：拿不到样本说明没能插进手，那是测试环境的时序问题，不能算"验过了"。
    assert len(starts) >= 10, f"样本太少（{len(starts)} 次请求），前后无法对比"

    before = [s for s in starts if s < flipped_at]
    after = [s for s in starts if s >= flipped_at]
    assert len(before) >= 2, f"改动前的样本不足：{len(before)}"
    assert (
        len(after) >= 4
    ), f"改动后的样本不足：{len(after)} —— 设置改了却没观察到后续请求"

    # 判据用**跨度**而不是逐个间隔：逐个间隔要求"每一次都规整"，而满载机器上
    # 单次请求被 GC / 线程调度拖延几十毫秒是常态；跨度只关心总时长，对个别
    # 异常免疫。两者区分度依然巨大：不限速时 50 个请求总共几毫秒到几十毫秒，
    # 限流到 0.25s 则要几秒。
    span_before = before[-1] - before[0]
    span_after = after[-1] - after[0]

    early_bound = interval * max(len(before) - 1, 1) * 0.5
    assert span_before < early_bound, (
        f"改动前就已经在节流了：{len(before)} 个请求铺开 {span_before:.3f}s，"
        f"上限 {early_bound:.3f}s"
    )
    floor = (len(after) - 1) * (interval - 0.05)
    assert span_after >= floor, (
        f"改动后没有按新间隔 {interval}s 走：{len(after)} 个请求只铺开 "
        f"{span_after:.3f}s，期望至少 {floor:.3f}s"
    )


def test_settings_change_actually_takes_effect_mid_run(monkeypatch, throttle_env):
    """正向对照：不改设置时全程都不该节流。

    上一条证明"改了会变"，这一条证明"没改不会变"—— 否则上一条可能只是在量
    某个恒定存在的间隔，与设置无关。
    """
    interval_that_should_not_apply = 0.5
    monkeypatch.setenv("PDF2ZH_PARAGRAPH_BATCH", "0")
    throttle_env.set(translator._MIN_REQUEST_INTERVAL_KEY, None)
    assert translator._request_interval() == 0.0

    fake = _SlowTranslator(delay=0.02)
    translate_document(
        _doc(pages=5, blocks_per_page=10),
        lambda t: fake.translate(t),
        lang_out="zh",
        thread=4,
    )

    starts = sorted(fake.starts)
    assert len(starts) >= 40, f"样本太少：{len(starts)}"
    span = starts[-1] - starts[0]
    assert span < interval_that_should_not_apply * 0.5 * len(
        starts
    ), f"没有设置间隔却被节流了：{len(starts)} 个请求铺开 {span:.3f}s"


def test_new_task_uses_the_interval_immediately(monkeypatch, throttle_env):
    """写入后必须立刻被读出来 —— 有人把 ``_request_interval()`` 换成缓存值或
    模块常量时，它会立刻红，而不必等一次偶发的时序窗口。
    """
    assert translator._request_interval() == 0.0

    throttle_env.set(translator._MIN_REQUEST_INTERVAL_KEY, 0.8)
    assert translator._request_interval() == 0.8

    throttle_env.set(translator._MIN_REQUEST_INTERVAL_KEY, None)
    assert translator._request_interval() == 0.0

    # 环境变量是后备：用户没配时它说了算
    monkeypatch.setenv("PDF2ZH_MIN_REQUEST_INTERVAL", "0.3")
    assert translator._request_interval() == 0.3

    # 写盘后仍可读回 —— ConfigManager 是文件持久化的单例
    assert translator._request_interval() == 0.3


def test_interval_survives_a_fresh_translator_instance(monkeypatch, throttle_env):
    """新构造的翻译器实例也必须读到新间隔。

    翻译器是按 document 建的；若有人把间隔缓存进实例属性，下一个文件就会
    悄悄退回不限速，而 GUI 显示的一直是"已保存"。
    """
    throttle_env.set(translator._MIN_REQUEST_INTERVAL_KEY, 0.6)

    seen = []

    class _Probe(translator.BaseTranslator):
        name = "probeinstthrottle"
        lang_map = {}

        def __init__(self):
            super().__init__("en", "zh", "", True)
            self.ignore_cache = True
            self.cache = _NoopCache()
            seen.append(translator._request_interval())

        def do_translate(self, text):
            return text

    _Probe()
    assert seen == [0.6], f"新实例没读到用户设置：{seen}"


# --------------------------------------------------------------------------
# 管线接线
# --------------------------------------------------------------------------
def test_setting_survives_a_restart(monkeypatch, throttle_env, tmp_path):
    """设置必须落盘 —— 否则重启 sidecar 后用户的配置悄悄消失。

    桌面端是长驻进程，但用户会重启应用；悄悄丢配置的症状是"我明明设过"，
    而没有任何报错。这里把 ``Path.home`` 指到临时目录（``_config_path`` 是在
    ``__init__`` 里按 home 定死的实例属性，只能从源头改），检查写入真的到了
    文件里、并且换一个实例（等价于重启进程）能读回来。
    """
    import json
    from pathlib import Path as _Path

    import pdf2zh.config as config_mod

    real_home = _Path.home
    monkeypatch.setattr(_Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(config_mod.ConfigManager, "_instance", None, raising=False)
    try:
        config_mod.ConfigManager.set(translator._MIN_REQUEST_INTERVAL_KEY, 0.9)
        cfg_file = tmp_path / ".config" / "PDFMathTranslate" / "config.json"
        assert cfg_file.is_file(), f"配置没有写进文件：{cfg_file}"
        on_disk = json.loads(cfg_file.read_text(encoding="utf-8"))
        assert on_disk.get("min_request_interval") == 0.9

        # 换个实例：ConfigManager 只在首次使用时读盘，等价于"重启进程"
        config_mod.ConfigManager._instance = None
        assert translator._request_interval() == 0.9, "重启后读不回用户设置"
    finally:
        monkeypatch.setattr(_Path, "home", real_home)
        config_mod.ConfigManager._instance = None


def test_cli_pipeline_goes_through_the_throttled_translate():
    """CLI/GUI 管线必须经过 ``BaseTranslator.translate`` —— 节流就在那里。

    上面几条在 ``translate_document`` 层量间隔；这一条补上"真实任务确实会走到
    那条路上"的证明：``magicpdf_cli`` 的 ``translate_with_tracking`` 直接调
    ``translator.translate``，而 ``_throttle()`` 在 ``translate`` 内部。
    """

    from pathlib import Path

    cli = Path(__file__).resolve().parent.parent / "pdf2zh" / "magicpdf_cli.py"
    src = cli.read_text(encoding="utf-8")
    assert (
        "translator.translate(" in src
    ), "管线不再调 translator.translate —— 节流（以及缓存）就被绕过去了"
    assert "_throttle()" in (
        Path(__file__).resolve().parent.parent / "pdf2zh" / "translator.py"
    ).read_text(encoding="utf-8"), "translate 内部不再节流"
