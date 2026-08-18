"""BabelDOC 扫描版（OCR）PDF 处理模式开关测试。

覆盖 ``pdf2zh/babeldoc_ocr_mode.py``：

- ``normalize_ocr_mode``：合法/非法/空取值归一化为三态之一。
- ``get_babeldoc_ocr_mode``：``PDF2ZH_BABELDOC_OCR`` 环境变量优先级 >
  显式参数 > 默认 ``auto``；非法环境变量回退显式参数。
- ``resolve_ocr_flags``：三态到 BabelDOC 三个互斥字段的映射，保证任一
  模式最多只打开其中一个开关（与 pdf2zh_next 内核 ``validate_settings``
  的约束一致）。
- （可选）与 pdf2zh_next 内核 ``PDFSettings.validate_settings`` 集成验证：
  三态组合都不会被内核覆盖。
"""

import os

import pytest

import pdf2zh.babeldoc_ocr_mode as bom


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """每个测试清掉 ``PDF2ZH_BABELDOC_OCR``，避免跨测试泄漏。"""
    monkeypatch.delenv(bom._ENV_OCR_MODE, raising=False)


# ── normalize_ocr_mode ────────────────────────────────────────────────────────


def test_normalize_valid_modes():
    assert bom.normalize_ocr_mode("auto") == "auto"
    assert bom.normalize_ocr_mode("on") == "on"
    assert bom.normalize_ocr_mode("off") == "off"
    assert bom.normalize_ocr_mode("ON") == "on"
    assert bom.normalize_ocr_mode("  auto  ") == "auto"


def test_normalize_invalid_falls_back_to_auto():
    assert bom.normalize_ocr_mode("bogus") == "auto"
    assert bom.normalize_ocr_mode("") == "auto"
    assert bom.normalize_ocr_mode(None) == "auto"


# ── get_babeldoc_ocr_mode ─────────────────────────────────────────────────────


def test_default_is_auto():
    assert bom.get_babeldoc_ocr_mode() == "auto"


def test_explicit_argument_wins_over_default():
    assert bom.get_babeldoc_ocr_mode("on") == "on"
    assert bom.get_babeldoc_ocr_mode("off") == "off"


def test_env_var_overrides_explicit_argument():
    os.environ[bom._ENV_OCR_MODE] = "on"
    assert bom.get_babeldoc_ocr_mode("auto") == "on"
    assert bom.get_babeldoc_ocr_mode(None) == "on"


def test_invalid_env_var_falls_back_to_argument():
    os.environ[bom._ENV_OCR_MODE] = "garbage"
    assert bom.get_babeldoc_ocr_mode("off") == "off"
    assert bom.get_babeldoc_ocr_mode(None) == "auto"


def test_env_var_case_insensitive():
    os.environ[bom._ENV_OCR_MODE] = "OFF"
    assert bom.get_babeldoc_ocr_mode() == "off"


# ── resolve_ocr_flags ─────────────────────────────────────────────────────────


def test_auto_flags():
    flags = bom.resolve_ocr_flags("auto")
    assert flags == (False, True, False)
    assert sum(1 for f in flags if f) == 1  # 互斥：最多一个 True


def test_on_flags_force_ocr():
    flags = bom.resolve_ocr_flags("on")
    assert flags == (True, False, False)
    assert sum(1 for f in flags if f) == 1


def test_off_flags_skip_scanned_detection():
    flags = bom.resolve_ocr_flags("off")
    assert flags == (False, False, True)
    assert sum(1 for f in flags if f) == 1


def test_resolve_uses_env():
    os.environ[bom._ENV_OCR_MODE] = "off"
    assert bom.resolve_ocr_flags("auto") == (False, False, True)


# ── 与 pdf2zh_next 内核 validate_settings 的集成约束 ───────────────────────


def test_flags_survive_next_kernel_validate(monkeypatch):
    """三态组合必须与内核 ``PDFSettings.validate_settings`` 的强制规则兼容。

    pdf2zh_next 内核在 ``auto_enable_ocr_workaround`` 开启时会强制覆盖
    ``ocr_workaround`` / ``skip_scanned_detection``；我们的三态映射保证任一
    模式只打开一个开关，因此 validate 后语义不被意外改写。
    """
    try:
        from pdf2zh_next.config.model import PDFSettings
    except Exception:  # noqa: BLE001 -- 内核缺失时跳过集成验证
        pytest.skip("pdf2zh_next kernel not available")

    for mode in ("auto", "on", "off"):
        ocr_w, auto_ocr, skip_scan = bom.resolve_ocr_flags(mode)
        settings = PDFSettings(
            ocr_workaround=ocr_w,
            auto_enable_ocr_workaround=auto_ocr,
            skip_scanned_detection=skip_scan,
        )
        settings.validate_settings()
        assert settings.auto_enable_ocr_workaround is auto_ocr
        assert settings.ocr_workaround is ocr_w
        assert settings.skip_scanned_detection is skip_scan
