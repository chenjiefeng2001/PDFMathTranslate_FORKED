"""翻译服务下拉框的显示契约：注册表 ↔ 前端 i18n 标签必须一一对应。

背景：`/api/engines` 的 `label` 字段是 Python 类名（`cls.__name__`，见
`pdf2zh/services/api.py`），而 `cls.__name__` 永远不可能等于 `cls.name`。
前端那句 `e.label !== e.name ? "类名 (id)" : e.name` 的 fallback 于是是死代码，
25 个服务无一例外显示成 `OpenAITranslator (openai)`。

现在标签改由前端 i18n 提供（`ui.engine_<id>`），类名只在缺键时兜底。缺键不会
报错 —— i18next 没设 `parseMissingKeyHandler`，会用 defaultValue 静默顶掉 ——
所以唯一能发现「新服务忘了加标签」的办法就是本测试：把注册表和两个 locale
文件对齐着比。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from pdf2zh.translator import build_translator_registry

FRONTEND = Path(__file__).resolve().parent.parent / "frontend" / "src"
I18N_TS = FRONTEND / "i18n" / "index.ts"
ENGINE_OPTIONS_TS = FRONTEND / "components" / "engineOptions.ts"
DASHBOARD_TSX = FRONTEND / "pages" / "Dashboard.tsx"
SETTINGS_TSX = FRONTEND / "pages" / "SettingsDrawer.tsx"

#: SPA_OVERLAY 里两段 locale 的边界。键名在两段里同名，用切分而不是正则全局抓，
#: 免得把 en 段的值当成 zh 的键。
_LOCALES = ("zh-CN", "en")

#: `engine_` 前缀下**不是服务标签**的键：下拉框自身的状态文案，以及历史上就存在
#: 的解析引擎标签（`engine_label` 指 parse_engine，不是翻译服务）。反向校验要把
#: 它们排除掉，否则会把这些正常键误报成「服务已删除」。
_NON_SERVICE_ENGINE_KEYS = {
    "engine_label",
    "engine_label_magicpdf",
    "engine_select",
    "engine_loading",
    "engine_none",
    "engine_unavailable",
}


def _strip_comments(source: str) -> str:
    """去掉注释，只留可执行代码。

    这些断言是对源码做文本校验的，而源码里大量中文注释**正在描述被修掉的那些
    缺陷**（「原先两处都是 optionFilterProp="value"」）。不剥注释的话，注释本身
    就会把断言触发，测试变成自相矛盾。
    """
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"//[^\n]*", "", source)


def _overlay_sections() -> dict[str, str]:
    """切出 SPA_OVERLAY 的 zh-CN / en 两段原文。"""
    text = I18N_TS.read_text(encoding="utf-8")
    start = text.index("const SPA_OVERLAY")
    body = text[start:]
    bounds = {
        "zh-CN": (body.index('"zh-CN": {'), body.index("  en: {")),
        "en": (body.index("  en: {"), body.index("} as const")),
    }
    return {name: body[a:b] for name, (a, b) in bounds.items()}


def _service_label_keys(section: str) -> dict[str, str]:
    """抽出 ``engine_*`` 里**属于服务标签**的键及其字面值。"""
    out: dict[str, str] = {}
    for key, value in re.findall(r'(engine_[A-Za-z0-9_]+):\s*"([^"]*)"', section):
        if key not in _NON_SERVICE_ENGINE_KEYS:
            out[key] = value
    return out


def _label_key(service_name: str) -> str:
    """前端 `engineLabelKey()` 的 Python 镜像：连字符转下划线。"""
    return "engine_" + service_name.replace("-", "_")


@pytest.fixture(scope="module")
def sections() -> dict[str, str]:
    return _overlay_sections()


def test_both_locales_have_the_same_service_label_keys(sections):
    """两个 locale 的 engine_* 键集合必须完全一致。"""
    zh = set(_service_label_keys(sections["zh-CN"]))
    en = set(_service_label_keys(sections["en"]))
    assert zh == en, f"缺 zh: {sorted(zh - en)} 缺 en: {sorted(en - zh)}"


def test_every_registered_service_has_a_label_in_both_locales(sections):
    """后端注册表里每个服务都要有中英文标签。

    新增翻译器却忘记加标签时，只有这里能发现：缺键不会报错，前端会用类名兜底
    然后安静地显示 `FooTranslator (foo)`。
    """
    zh = _service_label_keys(sections["zh-CN"])
    en = _service_label_keys(sections["en"])
    missing_zh, missing_en, empty = [], [], []
    for cls in build_translator_registry():
        key = _label_key(cls.name)
        if key not in zh:
            missing_zh.append(cls.name)
        if key not in en:
            missing_en.append(cls.name)
        for table, bag in ((zh, empty), (en, empty)):
            if key in table and not table[key].strip():
                bag.append(cls.name)
    assert not missing_zh, f"zh-CN 缺标签: {missing_zh}"
    assert not missing_en, f"en 缺标签: {missing_en}"
    assert not empty, f"标签为空: {sorted(set(empty))}"


def test_no_orphan_service_label_keys(sections):
    """反向：locale 里有标签但注册表没有对应服务 → 有人改过 id 或服务被删了。"""
    registered = {_label_key(cls.name) for cls in build_translator_registry()}
    zh = set(_service_label_keys(sections["zh-CN"]))
    assert (
        not zh - registered
    ), f"多余的标签键（服务可能已删除/改名）: {sorted(zh - registered)}"


def test_ui_state_strings_exist_in_both_locales(sections):
    """加载/空态/失败态文案也必须双语齐全。

    这些键原先是被误用的（`ui.waiting_task` 当加载态、`ui.settings_engines`
    当 placeholder），漏了就又是一次「缺键把 key 本身当文案渲染」。
    """
    required = {
        "engine_select",
        "engine_loading",
        "engine_none",
        "settings_engines_loading",
        "settings_engines_failed",
        "settings_engines_none",
        "settings_engines_select_ph",
    }
    for locale, section in sections.items():
        # 这里要含被 _NON_SERVICE_ENGINE_KEYS 排除掉的状态键本身。
        keys = set(re.findall(r"\b(engine_[a-z0-9_]+):", section)) | set(
            re.findall(r"\b(settings_engines_[a-z_]+):", section)
        )
        missing = required - keys
        assert not missing, f"{locale} 缺文案键: {sorted(missing)}"


def test_label_lookup_always_carries_a_default_value():
    """``t()`` 必须带 defaultValue：i18next 缺键时会把 key 本身渲染出来。"""
    text = _strip_comments(ENGINE_OPTIONS_TS.read_text(encoding="utf-8"))
    assert re.search(
        r"t\(\s*engineLabelKey\(e\.name\)\s*,\s*\{\s*defaultValue:\s*fallback\s*\}\s*\)",
        text,
    ), "标签 t() 丢了 defaultValue 兜底，缺键时会渲染 ui.engine_xxx"
    # 兜底必须是去掉 Translator 后缀的类名，而不是原样类名。
    assert "label.endsWith(suffix)" in text, "兜底仍在显示 FooTranslator 这类类名"
    assert (
        "optionFilterProp" not in text
    ), "engineOptions 里不该再出现 optionFilterProp —— 搜索要靠 filterOption"


def test_search_text_covers_id_english_and_localized_names():
    """搜索串必须含本地化名，且整体小写化，否则中文界面搜「谷歌」搜不到。"""
    text = _strip_comments(ENGINE_OPTIONS_TS.read_text(encoding="utf-8"))
    match = re.search(r"searchText:\s*`([^`]*)`(\.toLowerCase\(\))?", text)
    assert match, "找不到 searchText 构造"
    for part, why in (
        ("e.name", "id"),
        ("label", "本地化名"),
        ("fallback", "英文名/类名兜底"),
    ):
        assert part in match.group(1), f"搜索串缺{why}"
    assert match.group(2), "搜索串未统一小写，大小写不同的输入就搜不到"


def test_credentials_drawer_uses_its_own_wording():
    """凭据抽屉的加载/失败/空态与 placeholder 必须各有自己的文案键。

    原先加载态用的是 ``ui.waiting_task``（「等待翻译任务…」，与本节毫无关系），
    placeholder 用的是章节标题 ``ui.settings_engines``（「翻译引擎凭据」）；而
    那个 catch 是静默的，于是请求一失败这两句错文案就永远停在那里。
    """
    text = _strip_comments(SETTINGS_TSX.read_text(encoding="utf-8"))
    assert "ui.waiting_task" not in text, (
        "凭据抽屉又用 ui.waiting_task 当加载态；请求失败时这句与本节无关的话"
        "会一直显示"
    )
    assert re.search(r"if\s*\(credEngines\.length === 0\)", text), (
        "空态判据必须是过滤后的 credEngines：用未过滤的 engines 会让"
        "「全都免凭据」绕过判据，渲染出 options=[] 的空下拉框"
    )
    assert (
        "setFailed(true)" in text
    ), "getEngines 的 catch 又变回静默了，失败没有任何提示"
    for key in (
        "settings_engines_loading",
        "settings_engines_failed",
        "settings_engines_none",
    ):
        assert key in text, f"缺少 {key} 分支"
    placeholder = re.search(r"placeholder=\{t\(\"ui\.([a-z_]+)\"\)\}", text)
    assert placeholder, "找不到凭据抽屉的 placeholder"
    assert placeholder.group(1) == "settings_engines_select_ph", (
        f"placeholder 用了 ui.{placeholder.group(1)}；章节标题不是"
        "「请选择一个引擎」的意思"
    )


@pytest.mark.parametrize(
    "path", [DASHBOARD_TSX, SETTINGS_TSX], ids=["Dashboard", "SettingsDrawer"]
)
def test_engine_selects_do_not_render_the_class_name_pair(path):
    """两处下拉框都不能再出现 ``${label} (${name})`` 的「类名 (id)」拼接。"""
    text = _strip_comments(path.read_text(encoding="utf-8"))
    assert "${e.name}" not in text, "仍在拼接 `label (id)`，类名会漏给用户"
    assert "optionFilterProp" not in text, (
        '引擎下拉框用 optionFilterProp="value" 时只能搜 id，'
        "中文界面搜「谷歌」/「OpenAI」都搜不到；须改用 engineFilterOption"
    )
    assert "engineFilterOption" in text, "引擎下拉框没有接上共用的搜索匹配"


def test_dashboard_validates_the_hardcoded_default_engine():
    """硬编码的 "google" 默认值必须和注册表核对过。

    ``initialValues.engine`` 曾经直接写死 "google"：注册表到达前下拉框显示这个
    原始 id，注册表里没有它时还会被原样提交给后端。
    """
    text = _strip_comments(DASHBOARD_TSX.read_text(encoding="utf-8"))
    initial = text[text.index("initialValues={{") :]
    assert not re.search(
        r"\bengine\s*:", initial[: initial.index("}}")]
    ), "initialValues 里又写死了 engine 初值，会显示/提交一个未经校验的 id"
    assert (
        'setFieldValue("engine"' in text
    ), "engines 到达后没有校正 engine 字段，默认值仍可能不存在于选项中"


def test_store_exposes_an_engines_loading_flag():
    """必须有 enginesLoading，否则「还没加载完」和「加载完就是空的」分不开。"""
    store = _strip_comments(
        (FRONTEND / "stores" / "taskStore.ts").read_text(encoding="utf-8")
    )
    assert "enginesLoading: boolean" in store
    assert "set({ enginesLoading: true })" in store, "bootstrap 未标记加载中"
    assert (
        "set({ enginesLoading: false })" in store
    ), "加载失败时 enginesLoading 不落下，下拉框会永远转圈"
