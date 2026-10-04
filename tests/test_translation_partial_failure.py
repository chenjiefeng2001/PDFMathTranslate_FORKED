"""legacy/BabelDOC 链路「部分段翻译失败」的处理回归测试。

实测问题（325 页英文书 + 不可达的翻译服务）::

    Failed: magicpdf engine failed: Translation failed for 21 segment(s):
    RetryError: RetryError[<Future at 0x… raised ConnectionError>]; ...

两个缺陷：

1. **可用产物被丢弃。** ``converter._safe_worker`` 对失败段已经回落成原文，
   文档是完整可读的；但 ``high_level.translate`` 在收尾处只要 ``count > 0``
   就无条件 ``raise PDFValueError``，于是一份只挂 21 段的文档整份作废、
   任务判 FAILED。这与 magicpdf 链路的 ``EXIT_PARTIAL`` 是同一个反模式。
2. **报错无法操作。** tenacity 的 ``RetryError`` 只是重试耗尽的包装，真正
   原因（ConnectionError）藏在 ``str()`` 里 —— 用户既不知道是哪个引擎，也
   不知道是网络不通、Key 错还是配额耗尽。
"""

import pytest

from pdf2zh.high_level import (
    PDFValueError,
    _describe_total_translation_failure,
    _enforce_translation_error_policy,
)

RETRY_SAMPLE = (
    "RetryError: RetryError[<Future at 0x1d2c3306140 state=finished "
    "raised ConnectionError>]"
)


# ── 门禁：部分失败放行 ───────────────────────────────────────────────────────


def test_no_errors_is_a_noop():
    """没有错误时什么都不做（无 attempted 字段 = 旧调用方，语义不变）。"""
    _enforce_translation_error_policy("openai", {})
    _enforce_translation_error_policy("openai", {"count": 0})
    _enforce_translation_error_policy("openai", None)
    # attempted=0 表示"根本没尝试翻译"（纯扫描件 passthrough），不该 raise
    _enforce_translation_error_policy("openai", {"count": 0, "ok": 0, "attempted": 0})


def test_partial_failure_does_not_raise(caplog):
    """核心回归：21 段失败 / 1200 段成功时不得抛异常，产物要继续写出。"""
    with caplog.at_level("WARNING", logger="pdf2zh.high_level"):
        _enforce_translation_error_policy(
            "openai",
            {"count": 21, "ok": 1200, "samples": [RETRY_SAMPLE]},
        )
    # 必须留下可见告警（静默容忍 = 又一次「静默失败」）
    assert any("段翻译失败" in r.message for r in caplog.records)
    # 告警要带上比例，让用户知道只是个别段
    assert any("21/1221" in r.message for r in caplog.records)


def test_partial_failure_with_a_single_success_still_passes():
    """哪怕只成功 1 段，产物也有价值，不该整份作废。"""
    _enforce_translation_error_policy("openai", {"count": 500, "ok": 1})


def test_cache_only_success_counts_as_success():
    """全部命中缓存（ok>0、无网络调用）也算「有译文」，不该判死。"""
    _enforce_translation_error_policy("openai", {"count": 3, "ok": 900})


# ── 门禁：全部失败才 raise，且消息可操作 ─────────────────────────────────────


def test_total_failure_raises():
    with pytest.raises(PDFValueError):
        _enforce_translation_error_policy(
            "openai", {"count": 21, "ok": 0, "samples": [RETRY_SAMPLE]}
        )


def test_identity_translation_raises():
    """尝试过、一段都没译出（翻译器全程返回原文）→ 必须 raise。

    这类失败没有任何异常，所以 ``count == 0``；旧逻辑直接 return，
    于是一份逐字等于原文的「译文」被当成功交付（实测 325 页扫描件）。
    """
    with pytest.raises(PDFValueError) as exc:
        _enforce_translation_error_policy(
            "google", {"count": 0, "ok": 0, "attempted": 1200}
        )
    message = str(exc.value)
    assert "identical to the source" in message
    assert "google" in message, "报错要指名服务，避免用户去查网络"
    assert "1200" in message


def test_identity_translation_names_the_real_cause():
    """报错必须指向缓存 / 同语 / passthrough，而不是网络超时。"""
    with pytest.raises(PDFValueError) as exc:
        _enforce_translation_error_policy(
            "openai", {"count": 0, "ok": 0, "attempted": 5}
        )
    message = str(exc.value).lower()
    assert "cache" in message
    assert "passthrough" in message
    assert "retryerror" not in message, "不该把用户引向 tenacity 重试排查"


def test_partial_failure_with_attempted_still_passes():
    """有成功段时 attempted 的存在不应改变「部分失败只告警」的行为。"""
    _enforce_translation_error_policy(
        "openai", {"count": 21, "ok": 1200, "attempted": 1221}
    )


def test_missing_ok_key_is_treated_as_total_failure():
    """旧调用方 / 旧缓存没有 ``ok`` 字段时按全部失败处理（保守）。"""
    with pytest.raises(PDFValueError):
        _enforce_translation_error_policy("openai", {"count": 3})


def test_total_failure_names_engine_and_is_actionable():
    with pytest.raises(PDFValueError) as exc:
        _enforce_translation_error_policy(
            "openai", {"count": 21, "ok": 0, "samples": [RETRY_SAMPLE]}
        )
    message = str(exc.value)
    assert "openai" in message
    assert "21" in message
    assert "不可达" in message
    assert "PDF2ZH_PROXY" in message
    assert "RetryError" in message  # 原始错误保留


# ── 根因归类 ─────────────────────────────────────────────────────────────────


def test_auth_failure_is_not_reported_as_network():
    """鉴权/配额问题不能被误报成「网络不通」—— 处置方式完全不同。"""
    message = _describe_total_translation_failure(
        "openai", 5, "Unauthorized: 401 invalid_api_key"
    )
    assert "鉴权" in message or "配额" in message
    assert "不可达" not in message


def test_quota_exhaustion_is_named():
    message = _describe_total_translation_failure(
        "openai", 5, "RateLimitError: insufficient_quota"
    )
    assert "配额" in message


@pytest.mark.parametrize(
    "sample",
    [
        "RetryError: [raised ConnectTimeout]",
        "requests.exceptions.ConnectionError: HTTPSConnectionPool",
        "NewConnectionError: <urllib3.connection.HTTPSConnection>",
        "MaxRetryError: too many retries",
        "SSLError: certificate verify failed",
        "NameResolutionError: getaddrinfo failed",
    ],
)
def test_transport_failures_are_classified(sample):
    assert "不可达" in _describe_total_translation_failure("openai", 1, sample)


@pytest.mark.parametrize(
    "sample",
    ["Unauthorized", "401 invalid_api_key", "insufficient_quota", "PermissionDenied"],
)
def test_auth_failures_are_classified(sample):
    message = _describe_total_translation_failure("openai", 1, sample)
    assert "鉴权" in message or "配额" in message


def test_unclassifiable_error_still_shows_raw_text():
    message = _describe_total_translation_failure("weird", 2, "SomeVendorError: boom")
    assert "SomeVendorError: boom" in message
    assert "weird" in message


def test_missing_engine_name_is_reported_explicitly():
    assert "未指定引擎" in _describe_total_translation_failure("", 3, "boom")


def test_empty_samples_do_not_crash():
    message = _describe_total_translation_failure("openai", 4, "")
    assert "openai" in message
    assert "n/a" in message


# ── converter 的成功计数 ─────────────────────────────────────────────────────


def test_converter_initialises_both_counters():
    """converter 必须同时暴露失败数与成功数，上层才能区分部分/全部失败。

    计数初始化在 ``TranslateConverter.__init__``（``PDFConverterEx`` 的子类）。
    """
    import inspect

    from pdf2zh.converter import TranslateConverter

    source = inspect.getsource(TranslateConverter.__init__)
    assert "_translation_ok" in source
    assert "_translation_errors" in source
