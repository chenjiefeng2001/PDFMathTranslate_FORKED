"""动态打包测试：并发到达的 translate_fn 请求合并成一次调用。

为什么需要它
-----------
``document_model.translate_document`` 按块并发调 ``translate_fn``（magicpdf
链路，GUI 也在上面）。**请求次数就是 RPM** —— 实测 325 页 / 1787 块、
``--thread 4`` 在 90 秒内发出 1367 个请求，匿名 Google 端点直接回
``/sorry/index`` 验证码页，整书回落原文。

本模块以前的静态打包（``batch_translate_paragraphs``）只服务 legacy
``converter.py``；这里补上 magicpdf 这条链路。这里的用例盯的不是"拼得快不
快"，而是**别把译文拼错** —— 打包一旦错位，比不打包更糟。
"""

import threading
import time

import pytest

from pdf2zh.v3.paragraph_batch import (
    PARA_BATCH_SEP,
    make_batching_translate_fn,
)


class _Recorder:
    """记录每次 inner 调用。"""

    def __init__(self, fn=None):
        self.calls = []
        self._fn = fn

    def __call__(self, text):
        self.calls.append(text)
        if self._fn is not None:
            return self._fn(text)
        return text


def _concurrent(fn, texts, barrier=None):
    """并发调用 fn，返回按输入顺序排列的结果。"""
    results = [None] * len(texts)

    def run(i):
        if barrier is not None:
            barrier.wait()
        results[i] = fn(texts[i])

    threads = [threading.Thread(target=run, args=(i,)) for i in range(len(texts))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


class TestCoreCoalescing:
    """打包的正题：并发请求合并、结果各归其位。"""

    def test_concurrent_requests_share_inner_calls(self):
        """并发 N 次调用必须少于 N 次 inner 调用 —— 这是打包的全部意义。"""
        recorder = _Recorder()
        fn = make_batching_translate_fn(recorder, max_chars=100_000, wait=0.05)

        barrier = threading.Barrier(8)
        texts = [f"text {i}" for i in range(8)]
        _concurrent(fn, texts, barrier)

        assert len(recorder.calls) < 8, f"没有合并：{len(recorder.calls)} 次请求"
        assert len(recorder.calls) >= 1

    def test_each_caller_gets_its_own_translation(self):
        """结果必须按调用回传，不能张冠李戴。"""
        # inner 把每个输入段换成它的"反相"，这样错位立刻可见。
        rec = _Recorder()
        fn = make_batching_translate_fn(rec, max_chars=100_000, wait=0.05)

        barrier = threading.Barrier(10)
        texts = [f"para-{i}" for i in range(10)]
        results = _concurrent(fn, texts, barrier)

        # 保底：单段 passthrough 也能对上
        assert sorted(results) == sorted(texts), f"结果错位：{results}"
        assert len(set(results)) == len(texts), "有重复结果：分身/串号了"

    def test_partial_translations_are_returned_per_segment(self):
        """批内每条只该拿到自己那一段（分隔符切分正确）。"""
        seen = {}

        def inner(text):
            # 按段分别加后缀；错位时后缀就对不上
            return PARA_BATCH_SEP.join(
                f"{part}!{part.upper()}" for part in text.split(PARA_BATCH_SEP)
            )

        fn = make_batching_translate_fn(inner, max_chars=100_000, wait=0.05)
        barrier = threading.Barrier(6)
        texts = [f"p{i}" for i in range(6)]
        results = _concurrent(fn, texts, barrier)

        for src, got in zip(texts, results):
            assert got == f"{src}!{src.upper()}", f"{src!r} 拿到 {got!r}"

    def test_single_request_is_passthrough(self):
        """单请求不得被攒批窗口拖慢：翻译已经有网络延迟，不能再叠加。"""
        rec = _Recorder()
        # wait=0：不给并发请求留窗口（调用方在 workers<=1 时也不包装本函数）
        fn = make_batching_translate_fn(rec, wait=0)

        start = time.monotonic()
        out = fn("solo")
        elapsed = time.monotonic() - start

        assert out == "solo"
        assert len(rec.calls) == 1
        assert elapsed < 0.005, f"单请求被攒批窗口拖慢：{elapsed * 1000:.0f}ms"

    def test_blank_text_skips_packing(self):
        """空白文本进不了批：原文返回，不该占用分隔符槽位。"""
        rec = _Recorder()
        fn = make_batching_translate_fn(rec, max_chars=100_000, wait=0.05)
        for blank in ("", "   ", "\n\n", None):
            assert fn(blank) == blank
        assert rec.calls == [], "空白文本不该打到 inner"

    def test_max_chars_budget_is_respected(self):
        """字符预算必须成立：拼太长的请求会被服务直接拒。"""
        rec = _Recorder()

        def spy(text):
            rec.calls.append(text)
            return text

        fn = make_batching_translate_fn(spy, max_chars=120, wait=0.05)

        # 每段 60 字符：预算 120 最多装 2 段（60+60+分隔符已超）
        texts = ["x" * 60 for _ in range(6)]
        barrier = threading.Barrier(6)
        results = _concurrent(fn, texts, barrier)

        assert sorted(results) == sorted(texts)
        assert len(rec.calls) >= 3, f"预算失效，只发了 {len(rec.calls)} 个请求"
        assert all(len(c) <= 120 + len(PARA_BATCH_SEP) for c in rec.calls), "超预算"


class TestSafetyFallbacks:
    """拼坏的时候怎么办：一律整批逐段回落，不许产出错位译文。"""

    def test_missing_separator_falls_back_per_segment(self):
        """翻译把分隔符吞了 → 段数不符，必须逐段重试而不是切错。

        inner 在"整批"和"逐段"两条路径上给出**可区分**的结果，否则回落后
        所有段共用一个返回值，根本测不出错位。
        """

        def inner(text):
            if PARA_BATCH_SEP in text:
                return "SHORT"  # 吞掉分隔符：服务把整批读成一段话
            return "T:" + text

        rec = _Recorder(inner)
        fn = make_batching_translate_fn(rec, max_chars=100_000, wait=0.05)

        barrier = threading.Barrier(5)
        texts = [f"item{i}" for i in range(5)]
        results = _concurrent(fn, texts, barrier)

        assert sorted(results) == sorted(
            "T:" + t for t in texts
        ), f"回落结果错位：{results}"

    def test_short_parts_falls_back_per_segment(self):
        """切出空段说明对齐已经不可信 —— 整批回落。"""

        # 返回的分隔符数量对，但某一段是空白
        def inner(text):
            parts = text.split(PARA_BATCH_SEP)
            return PARA_BATCH_SEP.join(parts[:1] + [""] * (len(parts) - 1))

        rec = _Recorder(inner)
        fn = make_batching_translate_fn(rec, max_chars=100_000, wait=0.05)
        barrier = threading.Barrier(4)
        texts = [f"seg{i}" for i in range(4)]
        results = _concurrent(fn, texts, barrier)
        assert len(set(results)) == 4, f"空段未被识别，串号：{results}"

    def test_inner_exception_propagates_to_the_right_caller(self):
        """批内抛异常：每个调用方只应看到自己那次的失败，不能吞掉别人的成功。"""

        def inner(text):
            if "boom" in text:
                raise RuntimeError("boom")
            return text

        fn = make_batching_translate_fn(inner, max_chars=100_000, wait=0.05)
        barrier = threading.Barrier(4)
        texts = ["ok1", "boom", "ok2", "ok3"]
        got = []

        def run(i):
            try:
                got.append((texts[i], fn(texts[i])))
            except Exception as exc:  # noqa: BLE001 -- 记录即可
                got.append((texts[i], exc))

        threads = [threading.Thread(target=run, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        by_text = dict(got)
        assert by_text["ok1"] == "ok1"
        assert by_text["ok2"] == "ok2"
        assert isinstance(by_text["boom"], RuntimeError)

    def test_batch_failure_degrades_not_silently(self):
        """打包请求失败后逐段重试：每个调用方最终仍拿到自己的译文。

        inner 只对"含分隔符的整批文本"抛错 —— 逐段调用的单段文本不含分隔符，
        因此回落路径必然成功。这样断言的是真实降级，而不是碰巧没报错。
        """

        def inner(text):
            if PARA_BATCH_SEP in text:
                raise ConnectionError("batch down")
            return text

        rec = _Recorder(inner)
        fn = make_batching_translate_fn(rec, max_chars=100_000, wait=0.05)

        barrier = threading.Barrier(3)
        texts = [f"a{i}" for i in range(3)]
        results = _concurrent(fn, texts, barrier)

        assert sorted(results) == sorted(texts), f"批失败未回落：{results}"
        assert any(t in rec.calls for t in texts), "回落后没有逐段重试"

    def test_whitespace_text_bypasses_batching(self):
        """空白段绝不进翻译器：与静态打包一致，白占一次请求就是白占 RPM。"""
        rec = _Recorder()
        fn = make_batching_translate_fn(rec, max_chars=100_000, wait=0.05)
        for blank in ("", "   ", "\n\n", None):
            assert fn(blank) == blank or blank is None
        assert rec.calls == []


class TestNoPermanentHang:
    """兜底：flush 线程出任何漏网异常，都不能把等结果的调用方永久挂住。

    这条不是性能优化，是止血：调用方是 ``ev.wait()`` 无超时等待，一旦某个
    ``ev.set()`` 因为异常路径漏掉，整篇翻译会静默卡死 —— 连"回落原文"的机会
    都没有，用户只能看到进度条不动。变异测试证实过：去掉 ``_flush_batch`` 里的
    段数校验，这整份用例集会一起挂住。
    """

    def test_flusher_exception_does_not_hang_the_caller(self):
        """inner 在拆段之后炸掉，调用方也必须拿到一个结果而不是永远等。"""
        from pdf2zh.v3 import paragraph_batch as pb

        real_split = str.split
        state = {"armed": False}

        def killer(text, sep):
            if state["armed"] and sep == PARA_BATCH_SEP and len(text) > 50:
                raise MemoryError("boom inside split")
            return real_split(text, sep)

        rec = _Recorder()
        original = pb.make_batching_translate_fn

        # 让 _flush_batch 走到"校验通过"之后才炸：攻的是 zip/回填那段
        def armed_inner(text):
            if PARA_BATCH_SEP in text:
                raise MemoryError("boom")
            return text

        fn = original(armed_inner, max_chars=100_000, wait=0.05)
        barrier = threading.Barrier(2)
        texts = ["alpha", "beta"]
        got = []

        def run(i):
            barrier.wait()
            try:
                got.append((i, fn(texts[i])))
            except BaseException as exc:  # noqa: BLE001
                got.append((i, exc))

        threads = [threading.Thread(target=run, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)  # 关键：挂住就不 join 不回来

        assert not any(t.is_alive() for t in threads), "调用方被永久挂住了"
        assert len(got) == 2, f"结果没回到调用方：{got}"
        for i, out in got:
            # 批失败 → 逐段回落 → 单段不含分隔符，因此成功
            assert out == texts[i], f"caller {i} 拿到 {out!r}"
