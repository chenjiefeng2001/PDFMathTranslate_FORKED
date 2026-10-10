"""请求限速器测试（PDF2ZH_MIN_REQUEST_INTERVAL）。

为什么需要它
------------
**请求次数就是 RPM**。mp2e 之外的 Structural holes 325 页 / 1787 块、
``--thread 4`` 实测 90 秒内发出 1367 个请求（约 15 次/秒），匿名 Google 端点
直接从限流升级成 ``/sorry/index`` 验证码页，整书翻成原文。

打包（``pdf2zh.v3.paragraph_batch``）负责减少请求次数，限速器负责把**剩下的**
请求摊平到出口 IP 能承受的速率。两者互补：前者治本，后者兜底。
"""

import os
import time
from types import SimpleNamespace

import pytest

from pdf2zh import translator


@pytest.fixture
def reset_rate_state():
    """每个用例还原限速器的全局状态，避免互相污染。"""
    old_interval = translator._MIN_REQUEST_INTERVAL
    translator._MIN_REQUEST_INTERVAL = 0.0
    translator._last_request_at = 0.0
    yield
    translator._MIN_REQUEST_INTERVAL = old_interval
    translator._last_request_at = 0.0


class _FakeTranslator(translator.BaseTranslator):
    """只记录 do_translate 被调了几次，不真的发请求。"""

    name = "fake_throttle"
    lang_map = {}

    def __init__(self):
        self.delay = 0.0
        self.calls = 0
        self._started = []
        self._ended = []
        self.cache = None
        self.ignore_cache = True

    def do_translate(self, text):
        import time as _t

        self.calls += 1
        self._started.append(_t.monotonic())
        if self.delay:
            _t.sleep(self.delay)
        self._ended.append(_t.monotonic())
        return "x"


def _make_translator() -> _FakeTranslator:
    t = _FakeTranslator()
    t.cache = SimpleNamespace(
        get=lambda text, **kw: None,
        set=lambda text, val, **kw: None,
    )
    return t


def test_no_interval_means_no_throttle(reset_rate_state):
    """默认 0 = 不节流：不能给 LLM 服务凭空加延迟。"""
    assert translator._MIN_REQUEST_INTERVAL == 0.0
    t = _make_translator()
    for _ in range(5):
        t.translate("a")
    gaps = [b - a for a, b in zip(t._started, t._started[1:])]
    assert all(g < 0.05 for g in gaps), f"默认不应节流，实测间隔 {gaps}"


def test_interval_is_enforced_across_threads(reset_rate_state):
    """并发下全局间隔必须成立 —— 限流管的是出口 IP 总速率，不是单线程速率。"""
    import threading

    interval = 0.2
    translator._MIN_REQUEST_INTERVAL = interval
    t = _FakeTranslator()
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
    # 允许 20ms 的调度抖动；少了就说明并发把间隔吞了
    assert all(g >= interval - 0.02 for g in gaps), f"间隔被并发破坏：{gaps}"


def test_slow_requests_are_not_delayed_twice(reset_rate_state):
    """请求本身比间隔慢时不能叠加睡眠：那会让快服务也变慢。"""
    interval = 0.3
    translator._MIN_REQUEST_INTERVAL = interval
    t = _make_translator()
    t.delay = interval * 2  # 请求本身已超过间隔

    t.translate("a")
    t.translate("a")
    assert len(t._ended) == 2 and len(t._started) == 2
    # 要断言的是"第二个请求没有额外多等"，不是总耗时：假翻译器对每次调用都收
    # delay，两次请求本身就要 2×delay。若限流以"结束时刻"记账，第二个请求会
    # 多等一个满间隔（实测总耗时被拖成 1.2s）。
    gap = t._started[1] - t._ended[0]
    assert gap < 0.05, f"慢请求之后被重复限速：多等了 {gap * 1000:.0f}ms"


def test_cache_hit_bypasses_the_throttle(reset_rate_state, monkeypatch):
    """缓存命中不发请求，也就不该被限速拖慢。"""
    translator._MIN_REQUEST_INTERVAL = 5.0  # 极大：一旦走限速测试立刻能发现
    t = _FakeTranslator()
    t.ignore_cache = False
    store = {}
    t.cache = SimpleNamespace(
        get=lambda text, **kw: store.get(text),
        set=lambda text, val, **kw: store.__setitem__(text, val),
    )

    t.translate("hello")  # 第一次：真请求（会被限速一次）
    start = time.monotonic()
    t.translate("hello")  # 第二次：命中缓存
    elapsed = time.monotonic() - start

    assert elapsed < 1.0, f"缓存命中被限速拖慢：{elapsed:.2f}s"
    assert t.calls == 1, "缓存命中不应再触发 do_translate"
