"""请求限速器测试（PDF2ZH_MIN_REQUEST_INTERVAL / ConfigManager min_request_interval）。

为什么需要它
-----------
**请求次数就是 RPM**。mp2e 之外的 Structural holes 325 页 / 1787 块、
``--thread 4`` 实测 90 秒内发出 1367 个请求（约 15 次/秒），匿名 Google 端点
直接从限流升级成 ``/sorry/index`` 验证码页，整书翻成原文。

打包（``pdf2zh.v3.paragraph_batch``）负责减少请求次数，限速器负责把**剩下的**
请求摊平到出口 IP 能承受的速率。两者互补：前者治本，后者兜底。

GUI 场景是被一桩真实 bug 咬过才进来的：初版把间隔算成**模块导入时**的常量，
桌面端是在进程起来之后才写配置，于是用户改了设置永远不生效、只能重启进程。
所以这里断言的是「运行时读数」。

为什么用虚拟时钟
---------------
并发用例测的是「6 个线程抢预约」的算术，而真实时钟会把**机器调度抖动**混进
断言：全量测试把 CPU 压满时，一个线程被拖延 1.5 秒是常事，跨度判据于是随机
红 —— 而它声称在保护的东西早就坏了都没人发现。换成虚拟时钟（``monotonic``
读时刻、``sleep`` 推进时刻）后，断言落在预约本身上，既确定又不用真睡 1.2 秒。
"""

import os
import threading
import time as _real_time

import pytest

from pdf2zh import translator


class _VirtualClock:
    """线程安全的虚拟时钟：``monotonic`` 读时刻，``sleep`` 推进时刻。

    只替换 ``translator`` 看到的那个 ``time``（见 ``_FakeTime``），不碰全局
    ``time`` 模块 —— 满仓库替掉 ``sleep`` 会让别的后台线程空转。
    """

    def __init__(self):
        # 非零起点：真实 monotonic 的 epoch 是个任意大数，而限速器把
        # ``_last_request_at=0.0`` 当作"过去某时刻"。虚拟时钟若也从 0 起算，
        # 第一个请求就会白等一个满间隔 —— 那是测试自造的误差，不是真行为。
        self._now = 1000.0
        self._lock = threading.Lock()

    def monotonic(self) -> float:
        with self._lock:
            return self._now

    def sleep(self, delay: float) -> None:
        with self._lock:
            self._now += max(0.0, delay)


class _FakeTime:
    """``translator.time`` 的替身：时钟走虚拟的，其余委托给真模块。"""

    def __init__(self, clock: _VirtualClock):
        self._clock = clock

    def monotonic(self) -> float:
        return self._clock.monotonic()

    def sleep(self, delay: float) -> None:
        self._clock.sleep(delay)

    def __getattr__(self, name):
        return getattr(_real_time, name)


class _FakeTranslator(translator.BaseTranslator):
    """只记录 do_translate 被调了几次、什么时候进的，不真的发请求。"""

    name = "fake_throttle"
    lang_map = {}

    def __init__(self, clock=None):
        self.delay = 0.0
        self.calls = 0
        self._started = []
        self._ended = []
        self._clock = clock if clock is not None else _real_time
        # 通篇用真 cache：某些用例直接 new 而不走 _make_translator，cache=None
        # 会让 do_translate 之后的 cache.set 抛 AttributeError —— 那是在线程里
        # 抛的，pytest 只报一条警告，容易把真问题埋掉。
        from types import SimpleNamespace

        self.cache = SimpleNamespace(
            get=lambda text, **kw: None,
            set=lambda text, val, **kw: None,
        )
        self.ignore_cache = True

    def do_translate(self, text):
        clk = self._clock
        self.calls += 1
        self._started.append(clk.monotonic())
        if self.delay:
            clk.sleep(self.delay)
        self._ended.append(clk.monotonic())
        return "x"


def _make_translator(clock=None) -> _FakeTranslator:
    t = _FakeTranslator(clock)
    return t


@pytest.fixture
def reset_rate_state(monkeypatch):
    """每个用例还原限速器的**预约**状态。

    间隔值本身由 conftest 的 ``_isolated_translation_settings`` 负责，并且那边
    收尾时**故意不还原** —— 这里原先会把"进入时的值"当 old_cfg 写回去，于是
    任何上游用例（``test_services_api.py`` 走真实端点写 ConfigManager）留下的
    值都会被一轮轮固化，把同进程后续所有 ``translate()`` 都真的节流上。
    实测代价：``TestTranslator::test_cache`` 从 0.06s 涨到 27s，而单跑看不出来。
    """
    from pdf2zh.config import ConfigManager

    monkeypatch.delenv("PDF2ZH_MIN_REQUEST_INTERVAL", raising=False)
    ConfigManager.set(translator._MIN_REQUEST_INTERVAL_KEY, None)
    translator._last_request_at = 0.0
    yield
    ConfigManager.set(translator._MIN_REQUEST_INTERVAL_KEY, None)
    translator._last_request_at = 0.0


def _set_interval(seconds):
    """设置间隔：环境变量 + ConfigManager，模拟 GUI 与 CLI 两种入口。

    环境变量走 ``os.environ`` 而不是 monkeypatch：这个文件自己的 fixture 会在
    收尾时还原，但**别的文件**（test_services_api）在同一个会话里读它，写进去
    不还原就是给它们埋偶发失败。真正的清理在 ``reset_rate_state`` 里。
    """
    from pdf2zh.config import ConfigManager

    os.environ[translator._MIN_REQUEST_INTERVAL_ENV] = str(seconds)
    ConfigManager.set(translator._MIN_REQUEST_INTERVAL_KEY, seconds)


def _install_clock(monkeypatch) -> _VirtualClock:
    """给 translator 换上虚拟时钟，返回它（用例据此读数）。"""
    clock = _VirtualClock()
    monkeypatch.setattr(translator, "time", _FakeTime(clock))
    return clock


# --------------------------------------------------------------------------
# 间隔来源：GUI / CLI / 配错
# --------------------------------------------------------------------------
def test_no_interval_means_no_throttle(reset_rate_state):
    """默认 0 = 不节流：不能给 LLM 服务凭空加延迟。"""
    assert translator._request_interval() == 0.0
    t = _make_translator()
    for _ in range(5):
        t.translate("a")
    gaps = [b - a for a, b in zip(t._started, t._started[1:])]
    assert all(g < 0.05 for g in gaps), f"默认不应节流，实测间隔 {gaps}"


def test_interval_read_at_request_time_not_import_time(reset_rate_state):
    """GUI 的设置在进程起来之后才写入 —— 间隔必须运行时读，不能导入期冻结。

    这桩 bug 真实发生过：初版是 ``_MIN_REQUEST_INTERVAL = float(os.environ...)``
    模块级常量，桌面端写完配置也不会被读，用户以为限速没接上。
    """
    assert translator._request_interval() == 0.0  # 导入时确实是 0

    from pdf2zh.config import ConfigManager

    ConfigManager.set(translator._MIN_REQUEST_INTERVAL_KEY, 0.4)

    assert translator._request_interval() == 0.4, "改完配置应当立刻生效"


def test_config_beats_env_var(reset_rate_state):
    """两处都配时 ConfigManager 优先：GUI 的写入应当盖过遗留环境变量。"""
    from pdf2zh.config import ConfigManager

    os.environ[translator._MIN_REQUEST_INTERVAL_ENV] = "9.0"
    assert translator._request_interval() == 9.0

    ConfigManager.set(translator._MIN_REQUEST_INTERVAL_KEY, 0.5)
    assert translator._request_interval() == 0.5

    ConfigManager.set(translator._MIN_REQUEST_INTERVAL_KEY, None)
    assert translator._request_interval() == 9.0  # 清掉后退回环境变量


@pytest.mark.parametrize("bad", ["abc", "  ", "-3"])
def test_bad_config_falls_back_to_no_throttle(reset_rate_state, bad):
    """配错一个数不该让翻译崩掉 —— 忽略就是回到不限流。"""
    from pdf2zh.config import ConfigManager

    ConfigManager.set(translator._MIN_REQUEST_INTERVAL_KEY, bad)
    assert translator._request_interval() == 0.0


# --------------------------------------------------------------------------
# 并发下的间隔
# --------------------------------------------------------------------------
def test_interval_is_enforced_across_threads(reset_rate_state, monkeypatch):
    """并发下全局间隔必须成立 —— 限流管的是出口 IP 总速率，不是单线程速率。"""
    interval = 0.2
    _set_interval(interval)
    clock = _install_clock(monkeypatch)

    t = _make_translator(clock)
    n = 6

    def run():
        t.translate("a")

    threads = [threading.Thread(target=run) for _ in range(n)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert t.calls == n
    gaps = [b - a for a, b in zip(t._started, t._started[1:])]
    assert gaps, "没有间隔样本"
    # 虚拟时钟下没有调度抖动：每个间隔必须恰好是配置值，一个不多一个不少。
    # 少了就说明并发把间隔吞了；多了说明预约基准错了（见下一条）。
    assert all(g >= interval - 1e-9 for g in gaps), f"间隔被并发破坏：{gaps}"
    assert all(g <= interval + 1e-9 for g in gaps), f"间隔被放大：{gaps}"


def test_slow_requests_are_not_delayed_twice(reset_rate_state, monkeypatch):
    """请求本身比间隔慢时不能叠加睡眠：那会让快服务也变慢。"""
    interval = 0.3
    _set_interval(interval)
    clock = _install_clock(monkeypatch)

    t = _make_translator(clock)
    t.delay = interval * 2  # 请求本身已超过间隔

    t.translate("a")
    t.translate("a")
    assert len(t._ended) == 2 and len(t._started) == 2
    # 要断言的是"第二个请求没有额外多等"，不是总耗时：假翻译器对每次调用都收
    # delay，两次请求本身就要 2×delay。若限流以"结束时刻"记账，第二个请求会
    # 多等一个满间隔（实测总耗时被拖成 1.2s）。
    gap = t._started[1] - t._ended[0]
    assert gap < 1e-9, f"慢请求之后被重复限速：多等了 {gap * 1000:.0f}ms"


def test_throttle_is_not_more_aggressive_than_configured(reset_rate_state, monkeypatch):
    """间隔不能超配 —— 反向也要钉住。

    下限有测试盯着，上限没有：限速器突然比配置凶狠一倍，症状只是"翻译莫名变
    慢"，没人会怀疑到限速器头上，于是它一直坏着。
    """
    interval = 0.15
    _set_interval(interval)
    clock = _install_clock(monkeypatch)

    t = _make_translator(clock)
    n = 5

    start = clock.monotonic()
    for _ in range(n):
        t.translate("a")
    elapsed = clock.monotonic() - start

    # 串行发出 n 个请求：正常就是 (n-1) 个间隔，没有别的来源
    ceiling = (n - 1) * interval + 1e-9
    assert (
        elapsed < ceiling
    ), f"限速器比配置的更狠：{n} 个请求花了 {elapsed:.3f}s，上限 {ceiling:.3f}s"


def test_cache_hit_bypasses_the_throttle(reset_rate_state, monkeypatch):
    """缓存命中不发请求，也就不该被限速拖慢。"""
    from types import SimpleNamespace

    _set_interval(5.0)  # 极大：一旦走限速测试立刻能发现
    clock = _install_clock(monkeypatch)

    t = _make_translator(clock)
    t.ignore_cache = False
    store = {}
    t.cache = SimpleNamespace(
        get=lambda text, **kw: store.get(text),
        set=lambda text, val, **kw: store.__setitem__(text, val),
    )

    t.translate("hello")  # 第一次：真请求（会被限速一次）
    start = clock.monotonic()
    t.translate("hello")  # 第二次：命中缓存
    elapsed = clock.monotonic() - start

    assert elapsed < 5.0, f"缓存命中被限速拖慢：{elapsed:.3f}s"
    assert t.calls == 1, "缓存命中不应再触发 do_translate"
