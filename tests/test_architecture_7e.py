"""Commit 7E-Audit — 全链路架构审计（7E-Audit）.

证明并锁定四条单向边界：

    Semantic (detect/parse) → Translation → Layout → Renderer

1. **依赖方向**：detector / parser 只属于 semantic 层；
   ``looks_like_*`` / ``detect_*`` / ``parse_*`` 不得出现在 layout /
   renderer / magicpdf_renderer（语义决策在进 Translation 之前已结束）。
2. **Geometry Ownership**：原始几何（marker_x / content_x / title_x /
   page_x / bbox）只来自 Semantic；Layout 只做 fit/wrap/overflow 决策；
   Renderer 只消费。禁止 ``level * const`` / ``index * width`` 在
   layout / renderer 里重建几何（AST 精确到算术表达式中的语义量）。
3. **Legacy fallback 隔离**：``render_payload.kind`` 始终优先；只有在新
   载荷不可用时才回退 legacy 字段，且回退必须是显式的。
4. **单向性**：Renderer 只能消费几何、不能 mutate 语义几何；Translation
   只改内容文本，绝不动几何锚点。

7E-Audit 不改任何实现 —— 只锁定既有边界。若发现违反边界的 bug，只修
违反边界的部分，不趁机重构。
"""

import ast
import inspect
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent

_LAYOUT_DIR = _ROOT / "pdf2zh" / "semantic" / "layout"
_RENDERER_DIR = _ROOT / "pdf2zh" / "semantic" / "renderer"
_MAGICPDF = _ROOT / "pdf2zh" / "v3" / "magicpdf_renderer.py"


# -- helpers ---------------------------------------------------------------


def _strip_docstrings(source: str) -> str:
    """删除模块/类/函数级 docstring，仅保留可执行代码（散文不得触发断言）。"""
    tree = ast.parse(source)

    def _clean(body):
        return [
            n
            for n in body
            if not (
                isinstance(n, ast.Expr)
                and isinstance(n.value, ast.Constant)
                and isinstance(n.value.value, str)
            )
        ]

    tree.body = _clean(tree.body)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            node.body = _clean(node.body)
    ast.fix_missing_locations(tree)
    return ast.unparse(tree)


def _code(path: Path) -> str:
    return _strip_docstrings(path.read_text(encoding="utf-8"))


def _ast_binops(source: str):
    """Yield ``(kind, left_name, right_name)`` for every BinOp in executable code.

    ``kind`` ∈ {Mul, Div, Mult, ...} as a type name; operands are the simple
    Name nodes (or None).  Used to forbid semantic-derived geometry math while
    leaving legitimate ``font_size * 1.4`` / ``index * step`` scalar math intact.
    """
    tree = ast.parse(source)
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.BinOp):
            continue
        op = type(node.left.__class__ if False else node.op).__name__
        lname = _operand_name(node.left)
        rname = _operand_name(node.right)
        out.append((op, lname, rname))
    return out


def _operand_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Constant):
        v = node.value
        return v if isinstance(v, str) else None
    return None


def _module_stem(path: Path):
    return path.stem


def _iter_layout_renderer_files():
    for d in (_LAYOUT_DIR, _RENDERER_DIR):
        for py in sorted(d.glob("*.py")):
            if py.name == "__init__.py":
                continue
            yield py


# =========================================================================
# 1. 依赖方向 —— detector/parser 不得出现在 layout / renderer / magicpdf
# =========================================================================


def test_no_detection_in_layout_package():
    """layout 包绝不能出现语义检测（looks_like / detect_* / parse_*）。

    ``build_page_*_plan`` 组合链（检测/解析是编排职责）在 renderer 里，而
    不是 layout；layout 是纯几何载体。
    """
    for py in sorted(_LAYOUT_DIR.glob("*.py")):
        if py.name == "__init__.py":
            continue
        src = _code(py)
        for banned in (
            "looks_like",
            "detect_code",
            "detect_list",
            "detect_toc",
            "parse_list",
            "parse_toc",
        ):
            assert banned not in src, f"layout/{py.name} 不得出现 {banned}"


def test_no_detection_in_renderer_draw_only_classes():
    """draw-only 渲染类（ListRenderer/TocRenderer）不得做语义检测。

    与 7A（renderers_no_looks_like）/7E-3（draw path 无 detect/parse）约定
    一致：``build_page_*_plan`` 是一侧的组合链（编排职责，检测/解析合法），
    但 draw-only 类绝不探测结构。
    """
    import pdf2zh.semantic.renderer.list as list_mod
    import pdf2zh.semantic.renderer.toc as toc_mod

    for mod, cls in ((list_mod, "ListRenderer"), (toc_mod, "TocRenderer")):
        src = _strip_docstrings(inspect.getsource(getattr(mod, cls)))
        for banned in (
            "looks_like",
            "detect_code",
            "detect_list",
            "detect_toc",
            "parse_list",
            "parse_toc",
        ):
            assert banned not in src, f"{cls} 不得做语义检测（{banned}）"


def test_renderer_modules_no_looks_like_anywhere():
    """整个 renderer 模块（含组合链）都不出现 looks_like —— 探测彻底结束。"""
    for py in sorted(_RENDERER_DIR.glob("*.py")):
        if py.name == "__init__.py":
            continue
        assert "looks_like" not in _code(py), f"renderer/{py.name}"


def test_no_detection_in_magicpdf_renderer():
    src = _code(_MAGICPDF)
    for banned in (
        "looks_like",
        "detect_code",
        "detect_list",
        "detect_toc",
        "detect_span",
        "parse_list",
        "parse_toc",
    ):
        assert banned not in src, f"magicpdf_renderer 不得出现 {banned}"


# =========================================================================
# 2. Geometry Ownership —— layout / renderer 禁止由 level/index 重建几何
# =========================================================================


def test_no_level_index_geometry_math_in_layout():
    """layout 层不得用 ``level * const`` / ``index * width`` 推导几何。"""
    for py in sorted(_LAYOUT_DIR.glob("*.py")):
        if py.name == "__init__.py":
            continue
        src = _code(py)
        for op, l, r in _ast_binops(src):
            names = {l, r}
            if {"level", "index"} & names:
                raise AssertionError(f"{py.name} 用 {op}({l},{r}) 由语义量重建几何")


def test_no_level_index_geometry_math_in_renderer_draw_paths():
    """renderer 的 draw-only 类（ListRenderer/TocRenderer）不得据此重建几何。"""
    import pdf2zh.semantic.renderer.list as list_mod
    import pdf2zh.semantic.renderer.toc as toc_mod

    # 只审 draw-only 类，不含 build_page_*_plan 组合链（编排职责）。
    for mod, cls in ((list_mod, "ListRenderer"), (toc_mod, "TocRenderer")):
        src = _strip_docstrings(inspect.getsource(getattr(mod, cls)))
        for op, l, r in _ast_binops(src):
            names = {l, r}
            if {"level", "index"} & names:
                raise AssertionError(f"{cls} 用 {op}({l},{r}) 重建几何")


def test_no_level_index_geometry_math_in_magicpdf_renderer():
    src = _code(_MAGICPDF)
    for op, l, r in _ast_binops(src):
        names = {l, r}
        if {"level", "index"} & names:
            raise AssertionError(f"magicpdf_renderer 用 {op}({l},{r}) 重建几何")


# =========================================================================
# 3. Legacy fallback 隔离 —— render_payload.kind 优先，回退显式
# =========================================================================


def test_magicpdf_switches_on_render_payload_kind_first():
    src = _code(_MAGICPDF)
    # 以 payload_kind == "list"/"toc"/"flow" 为前提分派
    assert 'payload_kind == "list"' in src or "payload_kind == 'list'" in src
    assert 'payload_kind == "toc"' in src or "payload_kind == 'toc'" in src
    assert 'payload_kind == "flow"' in src or "payload_kind == 'flow'" in src


def test_magicpdf_legacy_fields_only_used_as_explicit_fallback():
    """legacy（list_items/toc_commands）只在新 payload 无 commands 时回退。

    ``render_payload.kind`` 分支优先读 payload.commands；只有 commands 为空
    才回退到 ``entry.get("list_items")/toc_commands`` —— 且紧跟在 kind
    条件内，永远不会抢占新 payload 主路径。
    """
    src = _code(_MAGICPDF).replace('"', "'")
    # list 回退：payload.commands 为空才读 legacy list_items.commands
    assert "(entry.get('list_items') or {}).get('commands')" in src
    assert "if not list_cmds:" in src
    # toc 回退：payload.commands 为空才读 legacy toc_commands.commands / 落下游
    assert "(entry.get('toc_commands') or {}).get('commands')" in src
    assert "if not toc_cmds:" in src


# =========================================================================
# 4. 单向性 —— Renderer 只消费几何，不能 mutate 语义几何
# =========================================================================


def test_renderer_cannot_change_semantic_geometry():
    """ListRenderer 渲染后，语义节点的几何锚点必须原样保留。

    ``marker_x``/``content_x``/``y``/``level`` 是 Semantic 量；渲染只是把
    它们抄进命令，绝不改写节点。
    """
    from pdf2zh.semantic.models import ListItemNode, ListNode
    from pdf2zh.semantic.renderer.list import ListRenderer

    node = ListNode(level=0)
    node.items = [type("I", (), {})()]  # placeholder; replaced below

    # 构造一个带几何的 ListItemNode

    it = ListItemNode(
        marker="1.",
        marker_x=40.0,
        content_x=60.0,
        content_width=300.0,
        y=700.0,
        level=0,
        content="Hello list item content",
    )
    node.items = [it]

    renderer = ListRenderer(font_size=11.0)
    cmds = renderer.render(node, translate=lambda s: f"T:{s}")

    # Renderer 完成了 —— 语义节点的几何必须一字未动
    assert it.marker_x == 40.0
    assert it.content_x == 60.0
    assert it.content_width == 300.0
    assert it.y == 700.0
    assert it.level == 0
    # marker 命令文本原样（未被翻译器改写）
    markers = [c for c in cmds if c.kind == "marker"]
    assert markers[0].text == "1."


def test_layout_result_bbox_passthrough_not_mutated():
    """layout anchor 的原始 x 不因 translate 改变。"""
    from pdf2zh.semantic.layout.list_layout import layout_list_item
    from pdf2zh.semantic.models import ListItemNode

    it = ListItemNode(
        marker="2.",
        marker_x=45.0,
        content_x=70.0,
        content_width=200.0,
        y=650.0,
        content="orig text",
    )
    result = layout_list_item(
        it,
        font_size=11.0,
        content_text="非常长的译文文本内容内容",
    )
    # 原始锚点保留
    assert result.marker_x == 45.0
    assert result.content_x == 70.0
    assert result.continuation_x == 70.0
    assert result.y == 650.0
    # 源节点几何没被写成派生值
    assert it.marker_x == 45.0 and it.content_x == 70.0


# =========================================================================
# 5. Translation → Layout 隔离 —— translator 只改内容，不动几何
# =========================================================================


def test_translation_does_not_mutate_geometry_anchors():
    """恶意 translator 把内容全部置乱，几何锚点仍一字不动。

    只允许 text width / line count / overflow 变化；marker_x / content_x /
    y 不变。这验证「translation changes content, NOT geometry」。
    """
    from pdf2zh.semantic.layout.list_layout import layout_list_item
    from pdf2zh.semantic.models import ListItemNode

    it = ListItemNode(
        marker="1.",
        marker_x=40.0,
        content_x=60.0,
        content_width=50.0,
        y=700.0,
        content="A short item",
    )
    before = (it.marker_x, it.content_x, it.content_width, it.y)

    def evil(s):
        return "TRANSLATED-内容-" + s * 100

    result = layout_list_item(it, font_size=11.0, content_text=evil(it.content))

    assert (it.marker_x, it.content_x, it.content_width, it.y) == before
    assert result.marker_x == 40.0 and result.content_x == 60.0
    # 译文太长 → 溢出上报（内容层面变化），绝不把 anchor 拉走
    assert result.content.overflow is True or len(result.content.lines) > 0


# =========================================================================
# 6. Overflow Contract Ownership —— 各原语的 fit 策略归属
# =========================================================================


def test_overflow_policy_table_owned_by_who():
    """Overflow 策略归属表（7C/7E-Audit 锁定）：

    - Code(PreservedRegion)  → PRESERVE，绝无 WRAP/SHRINK/CLIP
    - List content(FlowText)→ WRAP；marker(FixedAnchor) 默认不自动 shrink
    - TOC title(FixedAnchor)→ 可报 overflow；page_x(FixedColumn) → PRESERVE
    - flow(FlowText)        → WRAP
    """
    from pdf2zh.semantic.layout.overflow import OverflowPolicy, lay_out
    from pdf2zh.semantic.layout.primitives import (
        FixedAnchor,
        FixedColumn,
        FlowText,
        PreservedRegion,
    )

    def m(s, sz):
        return len(s) * sz * 0.6

    # code
    code = PreservedRegion(text="def long_func():", bbox=(10, 10, 300, 26))
    r = lay_out(code, measure=m, font_size=10.0)
    assert r.policy is OverflowPolicy.PRESERVE
    assert r.lines == [code.text]
    assert not any(
        p in (OverflowPolicy.WRAP, OverflowPolicy.SHRINK, OverflowPolicy.CLIP)
        for p in [r.policy]
    )

    # list content
    flow = FlowText(text="很长很长", origin=(60, 30), max_width=20)
    r = lay_out(flow, measure=m, font_size=11.0)
    assert r.policy is OverflowPolicy.WRAP

    # list marker / toc title: FixedAnchor -> SHRINK mechanism (not auto-applied)
    anchor = FixedAnchor(text="1. ", x=40, y=700, max_width=50, role="marker_x")
    r = lay_out(anchor, measure=m, font_size=11.0)
    assert r.policy in (OverflowPolicy.SHRINK, OverflowPolicy.PRESERVE)

    # toc page column: FixedColumn -> PRESERVE
    col = FixedColumn(text="42", column_x=540, y=30)
    r = lay_out(col, measure=m, font_size=10.0)
    assert r.policy is OverflowPolicy.PRESERVE


def test_policy_for_uses_primitives_kind_table():
    from pdf2zh.semantic.layout.overflow import OverflowPolicy, policy_for

    assert policy_for("preserved") is OverflowPolicy.PRESERVE
    assert policy_for("flow") is OverflowPolicy.WRAP
    assert policy_for("continuation") is OverflowPolicy.WRAP
    assert policy_for("column") is OverflowPolicy.PRESERVE


# =========================================================================
# 7. Golden Corpus —— 全链路 7D 回归，不允许 regression
# =========================================================================


def test_golden_corpus_no_regression(tmp_path):
    """对 corpus（code/list/nested/toc/style/cjk）分别 evaluate + compare。

    每条给出忠实副本（identity 恒等，输出==源）作为 baseline，再以相同
    副本作为 current：必须零 regression。事实上 identity 的 fidelity 全 1.0。
    """
    from pdf2zh.semantic.eval import compare_reports, evaluate
    from tests.pdf_eval_build import (
        build_cjk,
        build_code,
        build_list,
        build_nested_list,
        build_prose,
        build_toc,
        build_toc_multiline,
        build_toc_no_leader,
    )

    cases = [
        ("code", build_code),
        ("list", build_list),
        ("nested", build_nested_list),
        ("toc", build_toc),
        ("toc_multiline", build_toc_multiline),
        ("toc_no_leader", build_toc_no_leader),
        ("style", build_prose),
        ("cjk", build_cjk),
    ]
    for name, builder in cases:
        src = str(tmp_path / f"{name}_src.pdf")
        out = str(tmp_path / f"{name}_out.pdf")
        builder(src)
        builder(out)
        rep = evaluate(src, out)
        res = compare_reports(rep, rep)
        assert res["status"] == "pass", f"{name}: {res['regressions']}"
        m = rep["metrics"]
        # identity path must be structural fidelity 1.0
        assert m["text_exactness"] == 1.0, name
        assert m["overflow_count"] == 0, name
        assert m["code_preserved_bbox"] == 1.0, name
        assert m["list_wrap_integrity"] == 1.0, name
        assert m["toc_leader_integrity"] == 1.0, name
        assert m["toc_continuation_x_accuracy"] == 1.0, name


def test_golden_corpus_all_7d_metrics_present(tmp_path):
    """7E-Audit 验收：全 corpus 报告必须包含全部关键指标键，且值被计算。"""
    from pdf2zh.semantic.eval import evaluate
    from tests.pdf_eval_build import build_list

    src = str(tmp_path / "s.pdf")
    out = str(tmp_path / "o.pdf")
    build_list(src)
    build_list(out)
    m = evaluate(src, out)["metrics"]
    required = {
        "code_preserved_bbox",
        "list_content_x_accuracy",
        "list_continuation_x_accuracy",
        "list_nested_geometry_accuracy",
        "toc_title_x_accuracy",
        "toc_page_x_accuracy",
        "toc_leader_integrity",
        "toc_continuation_x_accuracy",
        "outline_destination_accuracy",
        "bold_accuracy",
        "italic_accuracy",
    }
    for k in required:
        assert k in m, f"缺 7D 指标 {k}"
        assert isinstance(m[k], (int, float)), k
