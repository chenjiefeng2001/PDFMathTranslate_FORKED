"""Commit 7E-2c/d — list layout integration: semantic → layout → payload.

Verifies the four layers stay decoupled through ``build_page_list_plan``:

- marker never enters the translator (``translated_calls`` only content);
- a translator returning garbage still yields ``1. TRANSLATED`` /
  ``2. TRANSLATED`` — markers never swallowed or rewritten;
- long translated content wraps through the layout engine and every
  wrapped/continuation line lands at ``content_x``;
- renderer architecture: the list renderer module does not call
  ``detect_list`` / ``parse_list`` / ``calculate_level`` /
  ``calculate_indent`` / ``wrap_lines`` / ``measure_text``;
- render payload stays JSON-safe and backwards compatible
  (``commands`` / ``items`` / ``translated_calls``).
"""

import inspect

from pdf2zh.semantic.renderer.list import build_page_list_plan

TR = lambda s: f"TR[{s}]"


def _cmd_by_kind(cmds, kind):
    return [c for c in cmds if c["kind"] == kind]


# ── 1. translator decoupling ─────────────────────────────────────────────

def test_marker_never_translated_via_payload():
    seen: list[str] = []

    def spy(s: str) -> str:
        seen.append(s)
        return f"R[{s}]"

    plan = build_page_list_plan(["1. First item", "2. Second item"], translate=spy)
    assert seen == ["First item", "Second item"]
    assert "1." not in seen and "2." not in seen
    assert plan["translated_calls"] == seen


def test_garbage_translator_never_swallows_marker():
    """翻译器对一切 content 返回 ``TRANSLATED`` → 输出仍是 1./2. 前置。"""
    plan = build_page_list_plan(
        ["1. This is an item", "2. Second item"], translate=lambda s: "TRANSLATED"
    )
    markers = _cmd_by_kind(plan["commands"], "marker")
    texts = _cmd_by_kind(plan["commands"], "text")
    assert [c["text"] for c in markers] == ["1.", "2."]
    assert [c["text"] for c in texts] == ["TRANSLATED", "TRANSLATED"]
    # 严格顺序：marker 在对应 content 之前，绝不合并
    assert [c["text"] for c in plan["commands"]] == [
        "1.", "TRANSLATED", "2.", "TRANSLATED",
    ]
    assert all(m["text"].strip() in ("1.", "2.") for m in markers)


# ── 2. long content wraps through the layout engine ──────────────────────

def test_long_translated_item_wraps_to_multiple_text_commands():
    long = "This is a very long translated list item that cannot fit on one line"
    plan = build_page_list_plan(
        ["1. Original short item", "2. Another item"],
        geom=[
            {"x0": 40.0, "x1": 200.0, "y0": 700.0},  # 窄宽 → 强制 wrap
            {"x0": 40.0, "x1": 560.0, "y0": 680.0},
        ],
        translate=lambda s: long if "Original" in s else TR(s),
    )
    texts = _cmd_by_kind(plan["commands"], "text")
    first_item_lines = [c for c in texts if "TR[Another" not in c["text"]]
    second_item_lines = [c for c in texts if "TR[Another" in c["text"]]
    # 第一项 wrap 成多行；第二项单行
    assert len(first_item_lines) >= 2
    assert len(second_item_lines) == 1
    # 换行后文本内容完整（断在词边界，空格重建还原）
    assert " ".join(c["text"] for c in first_item_lines) == long
    # 所有行（含 wrap 后的换行）都锚定第一项 content_x
    item_x = plan["items"][0]["content_x"]
    assert all(c["x"] == item_x for c in first_item_lines)
    # y 递减（v3 y-up：换行向下）
    ys = [c["y"] for c in first_item_lines]
    assert ys[1] < ys[0]


def test_wrapped_continuation_x_equals_content_x():
    plan = build_page_list_plan(
        ["1. very long item", "     continuation line one", "     continuation line two"],
        geom=[
            {"x0": 40.0, "x1": 560.0, "y0": 700.0},
            {"x0": 60.0, "x1": 560.0, "y0": 685.0},
            {"x0": 60.0, "x1": 560.0, "y0": 670.0},
        ],
        translate=lambda s: s,
    )
    item = plan["items"][0]
    content_x = item["content_x"]
    texts = _cmd_by_kind(plan["commands"], "text")
    cont_cmds = [c for c in texts if c["text"].startswith("continuation")]
    assert len(cont_cmds) == 2
    assert all(c["x"] == content_x for c in cont_cmds)
    assert item["continuation"] == ["continuation line one", "continuation line two"]


# ── 3. nested payload geometry ───────────────────────────────────────────

def test_nested_payload_content_x_strictly_increasing():
    paras = ["1. Intro", "   a. Background", "      i. deep"]
    geom = [
        {"x0": 40.0, "x1": 300.0, "size": 12.0, "y0": 700.0},
        {"x0": 52.0, "x1": 300.0, "size": 12.0, "y0": 680.0},
        {"x0": 64.0, "x1": 300.0, "size": 12.0, "y0": 660.0},
    ]
    plan = build_page_list_plan(paras, geom=geom, translate=TR)
    cxs = [it["content_x"] for it in plan["items"]]
    assert cxs[2] > cxs[1] > cxs[0]
    # items 载荷同时带 continuation_x（== content_x）供下游消费
    assert all(it["content_x"] == it.get("continuation_x", it["content_x"]) for it in plan["items"])


# ── 4. payload JSON-safe + backwards compatible ──────────────────────────

def test_payload_json_safe():
    import json

    plan = build_page_list_plan(
        ["1. First item", "2. Second item"], translate=TR
    )
    json.dumps(plan)
    assert set(plan.keys()) == {"tree", "items", "commands", "translated_calls"}
    assert plan["items"][0]["marker"] == "1."
    assert plan["items"][0]["translated"] == "TR[First item]"


# ── 5. renderer architecture: draw-only ──────────────────────────────────

def test_list_renderer_forbidden_imports_absent():
    import pdf2zh.semantic.renderer.list as mod

    src = inspect.getsource(mod)
    for banned in (
        "detect_list(",
        "parse_list(",
        "calculate_level(",
        "calculate_indent(",
        "wrap_lines(",
        "measure_text(",
    ):
        assert banned not in src, banned
    # 布局经 layout_list_item 委托给 lay_out —— 渲染器不自己做 fit 决策
    assert "layout_list_item(" in src


def test_list_renderer_no_translator_import():
    import pdf2zh.semantic.renderer.list as mod

    src = inspect.getsource(mod)
    assert "from pdf2zh.translator" not in src
    assert "import translator" not in src
