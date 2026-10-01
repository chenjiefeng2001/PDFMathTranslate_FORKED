"""magicpdf / BabelDOC OCR 开关解耦回归测试。

背景：``RuntimeService`` 曾在两个 magicpdf 站点用
``extra.get("ocr_mode") or request.magicpdf_ocr_mode or "auto"`` 解析 magicpdf 的
OCR 三态，而 ``extra_config["ocr_mode"]`` 其实是 **BabelDOC** 的 OCR 开关
（CLI ``--babeldoc-ocr``）。GUI 永远会把该字段填成 ``auto``/``on``/``off``
三者之一（永不为假），于是 ``request.magicpdf_ocr_mode`` 永远读不到：

- GUI 选 MagicPDF OCR = ``off`` → 实际变成 ``auto``，预检仍可强制开 OCR；
- BabelDOC 的 ``off`` 反而会关掉 magicpdf 的自动切换（语义交叉）。

本模块用 ``resolve_magicpdf_ocr_mode`` 钉死优先级与解耦行为。
"""

import pytest

from pdf2zh.services.runtime_service import (
    TranslationRequest,
    resolve_magicpdf_ocr_mode,
)


def _req(**kw) -> TranslationRequest:
    params = {
        "source_path": "/tmp/paper.pdf",
        "files": ["/tmp/paper.pdf"],
        "target_lang": "zh-CN",
        "engine": "google",
    }
    params.update(kw)
    return TranslationRequest(**params)


# ── GUI 的 MagicPDF OCR 单选必须真正生效 ──────────────────────────────────────


@pytest.mark.parametrize("mode", ["auto", "on", "off"])
def test_request_field_is_honored_despite_babeldoc_extra(mode):
    """GUI 同时设置两个字段时，magicpdf 侧只认自己的字段。"""
    req = _req(
        magicpdf_ocr_mode=mode,
        extra_config={"ocr_mode": "auto"},  # GUI 的 BabelDOC 单选
    )
    assert resolve_magicpdf_ocr_mode(req) == mode


def test_gui_off_is_not_shadowed_by_babeldoc_auto():
    """回归用例：GUI 选 off 时必须得到 off，而不是被 ocr_mode 覆盖成 auto。"""
    req = _req(magicpdf_ocr_mode="off", extra_config={"ocr_mode": "auto"})
    assert resolve_magicpdf_ocr_mode(req) == "off"


def test_gui_off_survives_babeldoc_on():
    """BabelDOC 开 OCR 不得反向强开 magicpdf 侧的 OCR 预检开关。"""
    req = _req(magicpdf_ocr_mode="off", extra_config={"ocr_mode": "on"})
    assert resolve_magicpdf_ocr_mode(req) == "off"


def test_gui_on_survives_babeldoc_off():
    """反向：magicpdf 显式 on 也不该被 BabelDOC 的 off 拉低。"""
    req = _req(magicpdf_ocr_mode="on", extra_config={"ocr_mode": "off"})
    assert resolve_magicpdf_ocr_mode(req) == "on"


# ── extra_config 专用 key 优先级最高 ──────────────────────────────────────────


@pytest.mark.parametrize("mode", ["auto", "on", "off"])
def test_extra_magicpdf_key_wins(mode):
    req = _req(
        magicpdf_ocr_mode="off",
        extra_config={"magicpdf_ocr_mode": mode, "ocr_mode": "off"},
    )
    assert resolve_magicpdf_ocr_mode(req) == mode


def test_invalid_extra_key_falls_back_to_request_field():
    req = _req(
        magicpdf_ocr_mode="on",
        extra_config={"magicpdf_ocr_mode": "bogus", "ocr_mode": "off"},
    )
    assert resolve_magicpdf_ocr_mode(req) == "on"


def test_invalid_request_field_falls_back_to_legacy_extra():
    req = _req(magicpdf_ocr_mode="nope", extra_config={"ocr_mode": "on"})
    assert resolve_magicpdf_ocr_mode(req) == "on"


# ── 旧单字段调用方仍可用（向后兼容） ──────────────────────────────────────────


def test_legacy_single_field_caller_still_works():
    """只发 ``ocr_mode`` 的旧 API 调用方不会静默失去 OCR 开关。"""
    assert resolve_magicpdf_ocr_mode(_req(extra_config={"ocr_mode": "on"})) == "on"
    assert resolve_magicpdf_ocr_mode(_req(extra_config={"ocr_mode": "off"})) == "off"


def test_no_config_defaults_to_auto():
    assert resolve_magicpdf_ocr_mode(_req()) == "auto"
    assert resolve_magicpdf_ocr_mode(_req(extra_config={})) == "auto"
    assert resolve_magicpdf_ocr_mode(_req(extra_config=None)) == "auto"


def test_babeldoc_auto_extra_does_not_leak_into_magicpdf():
    """BabelDOC 停在 auto（默认值）时不得影响 magicpdf 的 auto/on/off。"""
    for mode in ("auto", "on", "off"):
        req = _req(magicpdf_ocr_mode=mode, extra_config={"ocr_mode": "auto"})
        assert resolve_magicpdf_ocr_mode(req) == mode


# ── 两条链路确实读的是不同的字段 ──────────────────────────────────────────────


def test_babeldoc_path_still_reads_ocr_mode():
    """解耦不能反过来把 BabelDOC 的开关改掉：它仍读 ``extra['ocr_mode']``。"""
    from pdf2zh.babeldoc_ocr_mode import normalize_ocr_mode, resolve_ocr_flags

    extra = {"ocr_mode": "on", "magicpdf_ocr_mode": "off"}
    assert normalize_ocr_mode(extra.get("ocr_mode")) == "on"
    assert resolve_magicpdf_ocr_mode(_req(extra_config=extra)) == "off"
    # resolve_ocr_flags 在 mode="on" 时给出 ocr_workaround=True
    assert resolve_ocr_flags("on", None)[0] is True
