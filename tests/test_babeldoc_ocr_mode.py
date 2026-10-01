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


# ── ocr_workaround 不是 OCR：纯扫描件必须告警而不是静默产出空译文 ────────────


def _make_pdf(tmp_path, name, pages_text):
    """pages_text: 每页写入的字符串（空串 = 纯图片页，无文本层）。"""
    pymupdf = pytest.importorskip("pymupdf")
    path = tmp_path / name
    doc = pymupdf.open()
    for text in pages_text:
        page = doc.new_page()
        if text:
            page.insert_text((72, 72), text, fontsize=11)
    doc.save(str(path))
    doc.close()
    return str(path)


def test_document_has_text_layer_true(tmp_path):
    path = _make_pdf(tmp_path, "text.pdf", ["Hello world, this page has text."])
    assert bom.document_has_text_layer(path) is True


def test_document_has_text_layer_false_for_blank_pages(tmp_path):
    path = _make_pdf(tmp_path, "blank.pdf", ["", ""])
    assert bom.document_has_text_layer(path) is False


def test_document_has_text_layer_true_if_any_page_has_text(tmp_path):
    """任一页达标即算「有文本层」——混合文档不该被当成纯扫描件。"""
    long_text = "This second page carries a real extractable text layer."
    assert len(long_text) >= bom._MIN_TEXT_CHARS_PER_PAGE
    path = _make_pdf(tmp_path, "mixed.pdf", ["", long_text])
    assert bom.document_has_text_layer(path) is True


def test_document_has_text_layer_unreadable_is_true(tmp_path):
    """读不了就当「有文本层」：绝不因探测失败打扰用户。"""
    missing = str(tmp_path / "does-not-exist.pdf")
    assert bom.document_has_text_layer(missing) is True


def test_warn_on_textless_pdf_when_ocr_requested(tmp_path, caplog):
    """回归：--babeldoc-ocr on 打在纯扫描件上必须可见地告警。"""
    path = _make_pdf(tmp_path, "scan.pdf", ["", ""])
    with caplog.at_level("ERROR", logger="pdf2zh.babeldoc_ocr_mode"):
        assert bom.warn_if_babeldoc_ocr_is_a_noop(path, "on") is True
    assert any("parse-engine magicpdf" in r.message for r in caplog.records)


def test_no_warn_when_text_layer_exists(tmp_path, caplog):
    path = _make_pdf(tmp_path, "ok.pdf", ["A page with a real extractable text."])
    with caplog.at_level("ERROR", logger="pdf2zh.babeldoc_ocr_mode"):
        assert bom.warn_if_babeldoc_ocr_is_a_noop(path, "on") is False
    assert not [r for r in caplog.records if r.levelname == "ERROR"]


def test_no_warn_in_off_mode(tmp_path, caplog):
    """off 模式本就不做处理，不该告警。"""
    path = _make_pdf(tmp_path, "scan2.pdf", ["", ""])
    with caplog.at_level("ERROR", logger="pdf2zh.babeldoc_ocr_mode"):
        assert bom.warn_if_babeldoc_ocr_is_a_noop(path, "off") is False
    assert not [r for r in caplog.records if r.levelname == "ERROR"]


def test_no_warn_for_non_pdf(tmp_path, caplog):
    docx = tmp_path / "a.docx"
    docx.write_bytes(b"not a pdf")
    with caplog.at_level("ERROR", logger="pdf2zh.babeldoc_ocr_mode"):
        assert bom.warn_if_babeldoc_ocr_is_a_noop(str(docx), "on") is False


def test_resolve_ocr_flags_warns_but_still_returns_flags(tmp_path, caplog):
    """告警不是异常：flags 仍要照常返回，链路不被打断。"""
    path = _make_pdf(tmp_path, "scan3.pdf", ["", ""])
    with caplog.at_level("ERROR", logger="pdf2zh.babeldoc_ocr_mode"):
        flags = bom.resolve_ocr_flags("on", path)
    assert flags == (True, False, False)
    assert any("magicpdf" in r.message for r in caplog.records)
