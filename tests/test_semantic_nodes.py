"""单元测试：语义节点模型（SemanticNode 层级 + ProtectionPolicy）。

覆盖 ``pdf2zh.semantic.models``：
- 各节点类型的默认 region_type / policy（计划中的 policy 表）；
- REGION_POLICY 默认映射；
- 嵌套结构（List → ListItem → List）与 walk 深度优先遍历。
"""

from pdf2zh.semantic.models import (
    CodeBlockNode,
    HeadingNode,
    ListNode,
    ListItemNode,
    ParagraphNode,
    ProtectionPolicy,
    REGION_POLICY,
    RegionType,
)


def test_paragraph_node_defaults():
    n = ParagraphNode(text="hello")
    assert n.region_type == RegionType.TEXT
    assert n.policy == ProtectionPolicy.TRANSLATE


def test_heading_node_policy():
    n = HeadingNode(text="Introduction", level=1)
    assert n.region_type == RegionType.TITLE
    assert n.policy == ProtectionPolicy.TRANSLATE_KEEP_STYLE


def test_code_block_node_preserve():
    n = CodeBlockNode(lines=["def f():", "    return 1"])
    assert n.region_type == RegionType.CODE
    assert n.policy == ProtectionPolicy.PRESERVE
    assert n.text == "def f():\n    return 1"


def test_region_policy_table():
    # 计划中的核心映射
    assert REGION_POLICY[RegionType.TEXT] == ProtectionPolicy.TRANSLATE
    assert REGION_POLICY[RegionType.TITLE] == ProtectionPolicy.TRANSLATE_KEEP_STYLE
    assert REGION_POLICY[RegionType.CODE] == ProtectionPolicy.PRESERVE
    assert REGION_POLICY[RegionType.FORMULA] == ProtectionPolicy.PRESERVE
    assert REGION_POLICY[RegionType.FIGURE] == ProtectionPolicy.PRESERVE
    assert REGION_POLICY[RegionType.LIST] == ProtectionPolicy.TRANSLATE_KEEP_GEOMETRY
    assert REGION_POLICY[RegionType.TOC] == ProtectionPolicy.TRANSLATE_KEEP_GEOMETRY


def test_nested_list_structure_walk():
    root = ListNode(level=0)
    item1 = ListItemNode(marker="1.", marker_type="decimal", content="Intro", level=0)
    child_list = ListNode(level=1)
    child_list.items.append(
        ListItemNode(marker="a.", marker_type="lower_alpha", content="Background", level=1)
    )
    item1.children.append(child_list)
    root.items.append(item1)

    kinds = [n.region_type for n in root.walk()]
    assert kinds == [
        RegionType.LIST,   # root
        RegionType.LIST,   # item1 本身
        RegionType.LIST,   # 嵌套 child_list
        RegionType.LIST,   # child item
    ]


def test_list_item_to_dict():
    it = ListItemNode(marker="1.", marker_type="decimal", content="Data",
                      continuation=["continuation line"], level=0)
    d = it.to_dict()
    assert d["marker"] == "1."
    assert d["continuation"] == ["continuation line"]
    assert d["level"] == 0


def test_list_node_to_dict():
    lst = ListNode(level=0)
    lst.items.append(ListItemNode(marker="•", marker_type="bullet", content="x"))
    d = lst.to_dict()
    assert d["level"] == 0
    assert len(d["items"]) == 1
    assert d["items"][0]["marker"] == "•"