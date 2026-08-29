"""单元测试：语义代码检测（Phase 1）。

覆盖 ``pdf2zh.semantic.code_detector``：
- 等宽/代码字体名识别；
- 分数制判定：等宽字体 + 缩进 + 符号/关键字密度 + 行结构；
- 关键不变量：普通正文（无等宽字体证据）绝不误判为代码；
- require_mono 开关的行为差异。
"""

from pdf2zh.semantic.code_detector import (
    CODE_THRESHOLD,
    CodeProfile,
    detect_code,
    detect_code_block,
    is_monospace_font,
    score_code,
)


# ── 字体识别 ────────────────────────────────────────────────
def test_is_monospace_font_positives():
    for name in ["CourierNew", "Consolas", "Menlo", "SourceCodePro",
                 "DejaVuSansMono", "ABC+RobotoMono-Bold", "FiraCode"]:
        assert is_monospace_font(name)


def test_is_monospace_font_negatives():
    for name in ["TimesNewRoman", "ArialMT", "Helvetica", "Georgia", None, ""]:
        assert not is_monospace_font(name)


# ── 代码判定（默认 require_mono）───────────────────────────────
_CODE = "def foo():\n    x = 1\n    return x"


def test_indented_code_with_mono_font_is_code():
    is_code, score, reasons = detect_code(_CODE, ["CourierNew"])
    assert is_code is True
    assert score >= CODE_THRESHOLD
    # 等宽 + 缩进是主要证据
    assert "monospace_font" in reasons
    assert "indentation" in reasons


def test_code_without_mono_font_is_not_code():
    # 即使信号很强，无等宽字体也不判定为代码（防御性：宁可漏报不可误skip正文）
    is_code, score, reasons = detect_code(_CODE, ["TimesNewRoman"])
    assert is_code is False
    assert "monospace_font" not in reasons


def test_body_prose_not_code():
    text = ("This paper studies the effect of translation quality on "
            "reader comprehension and proposes a new evaluation metric.")
    is_code, score, reasons = detect_code(text, ["TimesNewRoman"])
    assert is_code is False


def test_code_with_require_mono_off_can_trigger():
    # require_mono=False：缩进 + 符号 + 关键字三重信号也能命中
    text = "for x in range(10):\n    print(x)\n    x += 1"
    is_code_off, score, reasons = detect_code(
        text, ["ArialMT"], require_mono=False
    )
    if score >= CODE_THRESHOLD:
        assert is_code_off is True
        assert "monospace_font" not in reasons  # 无等宽，靠其余信号


def test_empty_or_blank_never_code():
    for t in ["", "   ", "\n\n"]:
        is_code, score, reasons = detect_code(t, ["CourierNew"])
        assert is_code is False


# ── 分数明细 ────────────────────────────────────────────────
def test_score_reasons_accumulate_with_mono():
    # 单行、无缩进/少符号 → 只有等宽字体命中（5 分，低于阈值 6 → 不判定 code）
    text = "just some mono font text on one line"
    score, reasons = score_code(text, ["Consolas"])
    assert "monospace_font" in reasons
    assert score == 5.0
    is_code, _, _ = detect_code(text, ["Consolas"])
    assert is_code is False


def test_indent_plus_mono_crosses_threshold():
    text = "    x = 1\n    return 2"  # 等宽 + 缩进 → 达标
    is_code, score, reasons = detect_code(text, ["CourierNew"])
    assert is_code is True
    assert score >= CODE_THRESHOLD


# ── 检测 profile（strict / technical）────────────────────────
_FAKE_CODE = (
    "for x in range(10):\n"
    "    if x % 2 == 0:\n"
    "        print(x)\n"
    "        x += 1"
)


def test_strict_profile_requires_mono():
    # strict：无等宽字体 → 即使信号强也不算 code
    is_code, score, reasons = detect_code(
        _FAKE_CODE, ["TimesNewRoman"], profile=CodeProfile.STRICT
    )
    assert is_code is False
    assert "monospace_font" not in reasons


def test_technical_profile_allows_proportional_code():
    # technical：不要求等宽，但需要缩进+符号+关键字+行结构达到更高阈值
    is_code, score, reasons = detect_code(
        _FAKE_CODE, ["TimesNewRoman"], profile=CodeProfile.TECHNICAL
    )
    assert is_code is True
    assert "monospace_font" not in reasons
    assert score >= 8.0  # technical 阈值


def test_technical_profile_still_rejects_prose():
    prose = ("This paper studies translation quality and reader "
             "comprehension in a controlled experiment.")
    is_code, _, _ = detect_code(
        prose, ["TimesNewRoman"], profile=CodeProfile.TECHNICAL
    )
    assert is_code is False


# ── 节点化迁移：detect_code_block ────────────────────────────
def test_detect_code_block_returns_node():
    node = detect_code_block(_CODE, ["CourierNew"])
    assert node is not None
    assert node.lines == ["def foo():", "    x = 1", "    return x"]
    assert node.region_type.value == "code"


def test_detect_code_block_none_for_prose():
    assert detect_code_block("just prose", ["TimesNewRoman"]) is None