"""ListRenderer 单元测试（Commit 4 验收：marker 不翻译 / 几何保留）。

覆盖 ``pdf2zh.semantic.renderer.list``：
- marker 永不进入 translate 回调；
- content 走 translate（TRANSLATE_KEEP_GEOMETRY）；
- 延续行 x == content_x（不是 marker_x）；
- 几何来自节点（indent/marker_x/content_x），renderer 不重算 level；
- renderer 内部不出现 translator（translate 由调用方注入）。
"""

from pdf2zh.semantic.list_parser import parse_list_tree
from pdf2zh.semantic.renderer.list import ListRenderer, RenderCommand


def _markers(cmds):
    return [c for c in cmds if c.kind == "marker"]


def _texts(cmds):
    return [c for c in cmds if c.kind == "text"]


def test_marker_never_translated_content_is():
    paras = ["1. First item", "2. Second item"]
    tree = parse_list_tree(paras)
    assert tree is not None

    renderer = ListRenderer()
    cmds = renderer.render(tree, translate=lambda s: f"TR[{s}]")

    markers = _markers(cmds)
    texts = _texts(cmds)
    # marker 原样保留（绝不进 translator）
    assert [c.text for c in markers] == ["1.", "2."]
    # content 是唯一进入翻译的部分
    assert [c.text for c in texts] == ["TR[First item]", "TR[Second item]"]


def test_marker_and_content_positions_from_geometry():
    paras = ["1. First item", "2. Second item"]
    tree = parse_list_tree(paras)
    renderer = ListRenderer()
    cmds = renderer.render(tree)

    markers = _markers(cmds)
    texts = _texts(cmds)
    # 几何全部来自节点（列单位）：marker_x == indent，content_x == indent + marker 宽
    assert markers[0].x == tree.items[0].marker_x == tree.items[0].indent
    assert texts[0].x == tree.items[0].content_x
    assert tree.items[0].content_x == tree.items[0].marker_x + tree.items[0].marker_width


def test_continuation_renders_at_content_x():
    paras = ["1. very long item", "   continuation one", "   continuation two"]
    tree = parse_list_tree(paras)
    assert tree is not None and len(tree.items[0].continuation) == 2

    renderer = ListRenderer(line_height=12.0)
    cmds = renderer.render(tree, translate=lambda s: s)
    texts = _texts(cmds)
    cont_cmds = [c for c in texts if c.text.startswith("continuation")]
    # 延续行 x 必须等于 content_x，而不是 marker_x
    item = tree.items[0]
    assert len(cont_cmds) == 2
    assert all(c.x == item.content_x for c in cont_cmds)
    assert all(c.x != item.marker_x for c in cont_cmds)
    # 行距推进
    assert cont_cmds[1].y > cont_cmds[0].y


def test_nested_list_recurses_with_node_geometry():
    paras = ["1. Intro", "   a. Background", "   b. Motivation"]
    tree = parse_list_tree(paras)
    assert tree is not None
    renderer = ListRenderer()
    cmds = renderer.render(tree)
    markers = _markers(cmds)
    # 嵌套 item 的 marker_x 来自节点 indent（3 列），不重新计算
    nested_markers = [c for c in markers if c.x > tree.items[0].marker_x]
    assert len(nested_markers) == 2
    assert all(c.x == 3.0 for c in nested_markers)  # "   a." 的缩进


def test_render_plan_json_serializable():
    paras = ["1. First item", "2. Second item"]
    tree = parse_list_tree(paras)
    renderer = ListRenderer()
    plan = renderer.render_plan(tree, translate=lambda s: s)
    assert len(plan["commands"]) == 4
    assert plan["commands"][0]["kind"] == "marker"


def test_renderer_has_no_translator_import():
    # ListRenderer 只接收 translate 回调；模块内不 import translator 模块
    import inspect

    import pdf2zh.semantic.renderer.list as mod

    src = inspect.getsource(mod)
    assert "from pdf2zh.translator" not in src
    assert "import pdf2zh.translator" not in src
    assert "import translator" not in src