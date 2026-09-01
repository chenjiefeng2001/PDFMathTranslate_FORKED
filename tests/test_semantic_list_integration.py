"""Commit 4 集成测试：detect → List AST → translation → renderer 完整打通。

覆盖 ``pdf2zh.semantic.renderer.list.build_page_list_plan`` 的整条链：

- decimal / alphabetic / bullet 三类 marker；
- 延续行从原始 content_x 开始（非 marker_x）；
- 嵌套列表（1 → a → i）保持层级，indent/level 来自节点；
- 列表后的普通 paragraph 不被误认为列表（也不成为列表 item）；
- marker 永不进入 translation（``translated_calls`` 不含任何 marker）；
- translated content 保持 content_x。

全部是纯数据断言，不触碰 PDF，也不依赖翻译引擎（translate 用恒等/标记回调）。
"""

from pdf2zh.semantic.renderer.list import build_page_list_plan

TR = lambda s: f"TR[{s}]"  # noqa: E731


def _plan(paras, **kw):
    return build_page_list_plan(
        paras, geom=kw.get("geom"), translate=kw.get("translate", TR)
    )


def _cmd_by_kind(cmds, kind):
    return [c for c in cmds if c["kind"] == kind]


# ── 1. decimal list ─────────────────────────────────────────────────
def test_decimal_list_chain():
    paras = ["1. First item", "2. Second item", "3. Third item"]
    plan = _plan(paras)
    assert [i["marker"] for i in plan["items"]] == ["1.", "2.", "3."]
    assert [i["translated"] for i in plan["items"]] == [
        "TR[First item]",
        "TR[Second item]",
        "TR[Third item]",
    ]
    # 几何：content_x == marker_x + marker_width（保持原始列的起点）
    for it in plan["items"]:
        assert abs(it["content_x"] - it["marker_x"]) > 0
    # marker 命令原样，content 命令已翻译
    markers = _cmd_by_kind(plan["commands"], "marker")
    texts = _cmd_by_kind(plan["commands"], "text")
    assert [c["text"] for c in markers] == ["1.", "2.", "3."]
    assert [c["text"] for c in texts] == [
        "TR[First item]",
        "TR[Second item]",
        "TR[Third item]",
    ]


def test_decimal_contents_at_content_x():
    paras = ["1. Alpha", "2. Beta"]
    plan = _plan(paras)
    texts = _cmd_by_kind(plan["commands"], "text")
    items = plan["items"]
    assert texts[0]["x"] == items[0]["content_x"]
    assert texts[1]["x"] == items[1]["content_x"]
    # content 不贴在 marker_x
    assert texts[0]["x"] != items[0]["marker_x"]


# ── 2. alphabetic list ──────────────────────────────────────────────
def test_alphabetic_parens_list():
    paras = ["(a) first", "(b) second", "(c) third"]
    plan = _plan(paras)
    assert [i["marker"] for i in plan["items"]] == ["(a)", "(b)", "(c)"]
    assert [i["marker_type"] for i in plan["items"]] == ["lower_alpha"] * 3
    assert [i["translated"] for i in plan["items"]] == [
        "TR[first]",
        "TR[second]",
        "TR[third]",
    ]


# ── 3. bullet list ──────────────────────────────────────────────────
def test_bullet_list_preserves_marker_glyph():
    paras = ["• item one", "• item two", "• item three"]
    plan = _plan(paras)
    assert [i["marker"] for i in plan["items"]] == ["•", "•", "•"]
    assert all(i["marker_type"] == "bullet" for i in plan["items"])
    markers = _cmd_by_kind(plan["commands"], "marker")
    assert all(c["text"] == "•" for c in markers)


# ── 4. continuation lines alignment ─────────────────────────────────
def test_continuation_at_content_x():
    paras = [
        "1. very long item that wraps",
        "     continuation one",
        "     continuation two",
    ]
    plan = _plan(paras)
    items = plan["items"]
    assert len(items) == 1
    assert items[0]["continuation"] == ["continuation one", "continuation two"]
    content_x = items[0]["content_x"]
    texts = _cmd_by_kind(plan["commands"], "text")
    cont_cmds = [c for c in texts if c["text"].startswith("TR[continuation")]
    assert len(cont_cmds) == 2
    # 延续行 x == content_x（不是 marker_x）
    assert all(c["x"] == content_x for c in cont_cmds)
    assert content_x != items[0]["marker_x"]


# ── 5. nested list (1 → a → i) ──────────────────────────────────────
def test_nested_list_levels_preserved():
    paras = ["1. Intro", "   a. Background", "      i. deep", "2. Method"]
    plan = _plan(paras)
    items = plan["items"]
    # 深度遍历：Intro(0), Background(1), deep(2), Method(0)
    assert [it["level"] for it in items] == [0, 1, 2, 0]
    assert [it["marker"] for it in items] == ["1.", "a.", "i.", "2."]
    # 嵌套 marker 的 x 来自解析节点缩进（逐级更大）
    xs = [it["marker_x"] for it in items]
    assert xs[2] > xs[1] > xs[0]
    assert xs[3] == xs[0]


def test_nested_content_x_differ():
    """Commit 4.1：嵌套层级最终的 content_x 必须真实不同（不只 JSON level）。

    三个嵌套层必须有三个递增 content_x，说明渲染时内容列逐级右移；
    同时同层（Method 与 Intro）的 content_x 一致。
    """
    paras = ["1. Intro", "   a. Background", "      i. deep", "2. Method"]
    plan = _plan(paras)
    cxs = [it["content_x"] for it in plan["items"]]
    assert cxs[2] > cxs[1] > cxs[0]
    assert cxs[3] == cxs[0]
    # content_x 与 marker_x 严格分开（内容不贴在 marker 上）
    for it in plan["items"]:
        assert it["content_x"] > it["marker_x"]


# ── 6. paragraph after list is not a list ───────────────────────────
def test_paragraph_after_list_not_misdetected():
    paras = ["1. First item", "2. Second item", "A normal following paragraph."]
    plan = _plan(paras)
    # 只有 2 个列表项；普通段不成为 item
    assert len(plan["items"]) == 2
    # 无列表的输入返回空载荷
    empty = build_page_list_plan(["plain text no markers", "more plain"])
    assert empty["items"] == [] and empty["commands"] == []
    assert empty["tree"] is None


# ── 7. marker never enters translation ──────────────────────────────
def test_marker_never_translated():
    paras = ["1. First item", "2. Second item"]
    seen: list[str] = []

    def spy(s: str) -> str:
        seen.append(s)
        return f"R[{s}]"

    plan = build_page_list_plan(paras, translate=spy)
    # translate 只见过 content；marker 文本绝不在其中
    assert seen == ["First item", "Second item"]
    assert "1." not in seen and "2." not in seen
    assert plan["translated_calls"] == seen
    # marker 命令文本仍是原样（未经 transform）
    markers = _cmd_by_kind(plan["commands"], "marker")
    assert [c["text"] for c in markers] == ["1.", "2."]


# ── 7.1 Commit 4.1：翻译器恶意返回垃圾串，marker 仍与译文通道解耦 ───
def test_translator_garbage_never_swallows_marker():
    """翻译器对所有 content 一律返回同一串 ``TRANSLATED``。

    最终布局必须是 ``1. TRANSLATED`` / ``2. TRANSLATED`` —— marker 原样
    前置、垃圾译文作为 content 紧随其后。绝不 merge 成单个 ``TRANSLATED``
    （marker 被吞）、也绝不出现 ``1.1 TRANSLATED`` 这类 marker 被改写的
    结果（marker 与 translation channel 真正解耦）。
    """
    paras = ["1. This is an item", "2. Second item"]
    garbage = lambda s: "TRANSLATED"  # noqa: E731 -- 刻意返回与输入无关的串
    plan = build_page_list_plan(paras, translate=garbage)
    # 翻译器仍只见过 content（marker 从不由它改写）
    assert plan["translated_calls"] == ["This is an item", "Second item"]
    markers = _cmd_by_kind(plan["commands"], "marker")
    texts = _cmd_by_kind(plan["commands"], "text")
    assert [c["text"] for c in markers] == ["1.", "2."]
    assert [c["text"] for c in texts] == ["TRANSLATED", "TRANSLATED"]
    # 严格顺序：marker 必须在对应 content 之前（"1.", "TRANSLATED", "2.", "TRANSLATED"）
    assert [c["text"] for c in plan["commands"]] == [
        "1.",
        "TRANSLATED",
        "2.",
        "TRANSLATED",
    ]
    # marker 是逐字保留的原文（"1."），不含任何翻译产物（绝不 "1.1"）
    assert all(m["text"].strip() in ("1.", "2.") for m in markers)


# ── 8. content keeps content_x across translation ───────────────────
def test_translated_content_keeps_content_x():
    paras = ["1. item", "2. item"]
    plan = _plan(paras)
    items = plan["items"]
    texts = _cmd_by_kind(plan["commands"], "text")
    # 翻译后 content 起点仍为原始 content_x
    assert texts[0]["x"] == items[0]["content_x"]
    assert texts[0]["x"] >= items[0]["marker_x"]
    # translated 已应用翻译回调，但几何未变
    assert items[0]["translated"] != items[0]["content"]
    assert abs(items[0]["content_x"] - items[0]["marker_x"]) > 0


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__]))
