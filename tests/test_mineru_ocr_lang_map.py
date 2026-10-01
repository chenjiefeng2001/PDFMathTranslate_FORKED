"""MinerU OCR 语言映射表回归测试。

背景：``pdf2zh.magicpdf_adapter._LANG_TO_MINERU`` 历史上直接透传 pdf2zh 的
语言代码（``ja`` / ``ko`` / ``fr`` / ``de`` / ``es`` / ``pt`` / ``it`` / ``vi``），
而 MinerU 的 ``normalize_ocr_model_lang`` 既不认这些标识符、
``models_config.yml`` 也没有对应的单语言模型，于是
``PytorchPaddleOCR.__init__`` 抛 ``ValueError("Language ... not supported")``
并拖垮整个解析阶段 —— 且异常被降级逻辑吞掉，用户只看到「翻译完了但没翻译」。

本模块把「pdf2zh 语言代码 → MinerU 模型 key」这条不变量钉死：

- 每个映射值都必须是 MinerU ``models_config.yml`` ``lang:`` 段的真实 key；
- 每个 pdf2zh 语言代码经 MinerU 自己的 ``normalize_ocr_model_lang`` 归一化后
  仍落在合法集合内（即不抛 ``ValueError``）；
- GUI 暴露的源语言全部覆盖。
"""

import importlib.util
import pathlib

import pytest

from pdf2zh.magicpdf_adapter import (
    _LANG_TO_MINERU,
    _MINERU_DEFAULT_LANG,
    _MINERU_MODEL_LANGS,
    _lang_to_mineru,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_MODELS_CONFIG = (
    _REPO_ROOT
    / "vendor"
    / "MinerU"
    / "mineru"
    / "model"
    / "utils"
    / "pytorchocr"
    / "utils"
    / "resources"
    / "models_config.yml"
)
_OCR_LANG_MODULE = (
    _REPO_ROOT / "vendor" / "MinerU" / "mineru" / "utils" / "ocr_language.py"
)


def _require_vendor_mineru() -> None:
    """vendor/MinerU 未检出时跳过（CI 不初始化 submodule）。

    本地开发机有 submodule，CI 的 checkout 不带 —— 断言「映射表 ⊆ MinerU
    实际提供的模型」需要读 MinerU 自己的 models_config.yml，缺了无从对照。
    这类「需要 vendor 副模块」的断言随副模块一起跳过；不依赖它的断言
    （映射值非空、幂等、前缀剥离等）仍照常运行。
    """
    if not _MODELS_CONFIG.exists() or not _OCR_LANG_MODULE.exists():
        pytest.skip("vendor/MinerU submodule not checked out")


def _mineru_supported_langs() -> set:
    """MinerU 实际随包分发的 OCR 语言模型 key 集合。"""
    _require_vendor_mineru()
    yaml = pytest.importorskip("yaml")
    config = yaml.safe_load(_MODELS_CONFIG.read_text(encoding="utf-8"))
    return set(config["lang"].keys())


def _mineru_normalize():
    """MinerU 的 OCR 语言归一化函数（vendor 为可选 submodule）。"""
    _require_vendor_mineru()
    spec = importlib.util.spec_from_file_location(
        "pdf2zh_test_mineru_ocr_language", _OCR_LANG_MODULE
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.normalize_ocr_model_lang


# GUI ``config_panel.py`` 暴露的源语言 + CLI 常见取值。
# 修复前 ja/ko/fr/de/es/pt/it/vi 全部抛 ValueError。
_GUI_SOURCE_LANGS = ("zh", "en", "ja", "ko", "fr", "de", "es", "pt", "it", "vi")
_EXTRA_SOURCE_LANGS = (
    "zh-CN",
    "zh-TW",
    "en-US",
    "ru",
    "ar",
    "th",
    "hi",
    "auto",
    "",
)


def test_model_lang_set_matches_mineru_models_config():
    """硬编码的合法集合不能与 MinerU 实际分发的模型漂移。

    需要 vendor/MinerU 副模块 → CI 无 submodule 时跳过（漂移检测在本地与
    装了副模块的环境生效）。
    """
    assert _MINERU_MODEL_LANGS == _mineru_supported_langs()


def test_default_lang_is_a_real_mineru_model():
    assert _MINERU_DEFAULT_LANG in _MINERU_MODEL_LANGS


def test_every_mapping_value_is_a_shipped_mineru_model():
    """映射表字面量里的每个值都必须是 MinerU 提供的模型 key。

    这是最直接的防回归断言：往 ``_LANG_TO_MINERU`` 里加一个 MinerU 没有的
    单语言模型（如历史上的 ``ja``）会在这里立刻失败。

    刻意**不**依赖 vendor/MinerU 副模块 —— 用模块内硬编码的
    ``_MINERU_MODEL_LANGS`` 作判据，这样 CI（不初始化 submodule）也能守住
    「不要再写回 ja/ko/fr」这条不变量；与副模块的一致性由
    :func:`test_model_lang_set_matches_mineru_models_config` 单独把关。
    """
    unknown = {
        code: model
        for code, model in _LANG_TO_MINERU.items()
        if model not in _MINERU_MODEL_LANGS
    }
    assert (
        not unknown
    ), f"_LANG_TO_MINERU maps to models MinerU does not ship: {unknown}"


@pytest.mark.parametrize("lang", _GUI_SOURCE_LANGS + _EXTRA_SOURCE_LANGS)
def test_lang_to_mineru_survives_mineru_normalization(lang):
    """pdf2zh 的输出必须能通过 MinerU 自己的归一化且仍是合法模型。"""
    normalize = _mineru_normalize()
    supported = _mineru_supported_langs()
    mapped = _lang_to_mineru(lang)
    normalized = normalize(mapped, device="cpu", supported_langs=supported)
    assert normalized in supported


@pytest.mark.parametrize("lang", [None, "", "auto", "xx-YY", "zz"])
def test_unknown_lang_falls_back_to_default(lang):
    assert _lang_to_mineru(lang) == _MINERU_DEFAULT_LANG


def test_regional_prefix_is_stripped():
    """``zh-CN`` / ``en-US`` / ``ja-JP`` 按前缀归一，不因地区后缀落空。"""
    assert _lang_to_mineru("zh-CN") == _lang_to_mineru("zh")
    assert _lang_to_mineru("en-US") == _lang_to_mineru("en")
    assert _lang_to_mineru("ja-JP") == _lang_to_mineru("ja")
    assert _lang_to_mineru("ko-KR") == _lang_to_mineru("ko")


def test_korean_uses_the_korean_model_not_ch():
    """韩语有专用模型，不应退化到 ch（ch 的字符集不含谚文）。"""
    assert _lang_to_mineru("ko") == "korean"


def test_japanese_uses_ch_model():
    """MinerU 没有日文单语言模型；ch 模型字符集覆盖日文。

    参考 ``_PUBLIC_OCR_LANGUAGE_DESCRIPTIONS["ch"]``：
    "Chinese, English, Japanese, Chinese Traditional, Latin"。
    """
    assert _lang_to_mineru("ja") == "ch"


def test_latin_languages_share_the_ch_model():
    """fr/de/es/pt/it/vi 无专用模型，统一走字符集含 Latin 的 ch 模型。"""
    for lang in ("fr", "de", "es", "pt", "it", "vi", "nl", "pl"):
        assert _lang_to_mineru(lang) == "ch", lang


def test_cyrillic_family_split():
    """ru/be/uk 走 east_slavic，其余西里尔文种走 cyrillic。"""
    assert _lang_to_mineru("ru") == "east_slavic"
    assert _lang_to_mineru("bg") == "cyrillic"


def test_lang_to_mineru_is_idempotent():
    """映射幂等：已经是 MinerU 模型 key 的输入原样返回。"""
    for model in _MINERU_MODEL_LANGS:
        assert _lang_to_mineru(model) == model
