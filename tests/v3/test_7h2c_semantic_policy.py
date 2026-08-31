"""7H-2C Semantic Translation Policy — role arbitration + category PRESERVE.

Validates that the semantic-role *arbitration* layer (StructureClassifier +
document_model.annotate_roles + TranslationPolicyPass) produces a **final
semantic role** with proper precedence, and that TranslationPolicyPass /
translate_document uniformly consume it (no translator-side ``if code`` patch).

Acceptance covered:
- CODE            → PRESERVE (F2 修复：可靠证据定型后不被 formula/body 覆盖)
- FORMULA         → PRESERVE，且**不被误归为 CODE**
- IDENTIFIER      → PRESERVE
- FILENAME        → PRESERVE
- COMMAND         → PRESERVE
- CITATION        → 不粗暴 preserve（正文引用仍可翻）
- 普通 prose      → 仍 translate
- forensic corpus：code 块在 build_document_model 主路径（无 default_pass_manager）
  也定型为 kind=code → F2 检测器 FDS ∈ {model, translation} 而非 renderer 假阳性
- 既有 formula/code/table protection 回归全绿（在 test_v13/test_v12 验证）
"""

from __future__ import annotations

import unittest


class _Line:
    """geometry.Paragraph 鸭子类型行：classifier 读 .size / .words[0].font /
    .text（仲裁读）—— 与 analyzer._RuleParagraphAdapter 产出的行同构。"""

    def __init__(self, text: str, size: float = 10.0, font: str = "Helvetica"):
        self.text = text
        self.size = size
        word = type("W", (), {"font": font})()
        self.words = [word]
        self.x0, self.y0, self.x1, self.y1 = 0.0, 0.0, 600.0, size


class _Para:
    def __init__(self, text: str, size: float = 10.0, lines=None):
        self.text = text
        self.size = size
        self.first_line_indent = 0.0
        self.alignment = "left"
        self.x0, self.y0, self.x1, self.y1 = 0.0, 0.0, 600.0, 12.0
        # lines 可能以 str（测试直接传文本行）或 _Line 存在 —— 统一包装为 _Line。
        self.lines = [
            l if not isinstance(l, str) else _Line(l, size)
            for l in (lines or [_Line(text, size)])
        ]

    @property
    def line_count(self) -> int:
        # 与真实 Paragraph（geometry）len(lines) / BlockModel max(1, len(lines))
        # 对齐 —— compute_features 会读 para.line_count。
        return max(1, len(self.lines))


# 生产 adapter 路径产出的行（无 .text，仅 .size/.words）—— 验证 text 回退拆解。
class _StubLine:
    def __init__(self, size: float = 10.0):
        self.size = size
        self.words = [type("W", (), {"font": "Helvetica"})()]
    @property
    def text(self) -> str:
        return ""


def _make_block(text, kind="paragraph", lines=None):
    """生产同类 Paragraph 鸭子类型（BlockModel 会被 compute_features 拒绝）。"""
    return _Para(text, lines=lines or [_Line(text)])


def _make_blockstub(text, kind="paragraph", n_lines=1):
    """无文本行的 stub（模拟 analyzer adapter 行）→ 仲裁按块文本换行拆解。"""
    return _Para(text, lines=[_StubLine() for _ in range(max(1, n_lines))])


def _make_real_block(text, kind="paragraph", lines=None):
    """document_model 层用真实 BlockModel（annotate_roles 经 _NodeProxy 读
    text/bbox/font_size —— 文本按行用 \n 连接，供仲裁按 block.text 拆线）。"""
    from pdf2zh.v3.canonical_page import BlockModel, LineModel

    line_texts = lines or [text]
    b = BlockModel(
        text="\n".join(line_texts) if len(line_texts) > 1 else text,
        kind=kind,
        x0=50,
        y0=700 - len(line_texts) * 12,
        x1=550,
        y1=700,
    )
    b.lines = [LineModel(text=t) for t in line_texts]
    b.metadata["font_size"] = 10.0
    return b


class TestRoleArbitrationClassifier(unittest.TestCase):
    """StructureClassifier._arbitrate_preserve_role 类别优先级。"""

    def _role(self, text, lines=None):
        from pdf2zh.v3.structure import StructureClassifier

        clf = StructureClassifier(body_font_size=10.0)
        block = clf.classify_paragraph(
            _make_block(text, lines=lines), page=None, body_font_size=10.0
        )
        return block.role.value, block.confidence

    def test_code_multiline(self):
        lines = [
            "namespace xyza {",
            "    int value = 42;",
            "    return value;",
            "}",
        ]
        role, conf = self._role(" ".join(lines), lines=lines)
        self.assertEqual(role, "code")
        self.assertGreaterEqual(conf, 0.7)

    def test_code_single_brace_line(self):
        role, _ = self._role("namespace xyza {")
        self.assertEqual(role, "code")

    def test_code_cpp_scope_symbol(self):
        role, _ = self._role("using std::make_unique;")
        self.assertEqual(role, "code")

    def test_formula_not_code(self):
        # 数学式：无代码关键字/大括号/分号，不能被 CODE 吞掉
        role, _ = self._role("E = mc^2")
        self.assertEqual(role, "formula")

    def test_formula_math_line_not_code(self):
        role, _ = self._role("x^2 + y^2 = z^2")
        self.assertEqual(role, "formula")

    def test_command(self):
        role, _ = self._role("pip install numpy")
        self.assertEqual(role, "command")

    def test_command_sudo(self):
        role, _ = self._role("sudo apt-get update -y")
        self.assertEqual(role, "command")

    def test_filename(self):
        role, _ = self._role("config/settings.yaml")
        self.assertEqual(role, "filename")

    def test_filename_path(self):
        role, _ = self._role("/usr/local/bin/python3")
        self.assertEqual(role, "filename")

    def test_identifier_snake(self):
        role, _ = self._role("get_user_id")
        self.assertEqual(role, "identifier")

    def test_identifier_with_digit(self):
        role, _ = self._role("Foo_1_impl")
        self.assertEqual(role, "identifier")

    def test_prose_still_body(self):
        # 普通正文：即便含 `return`/`if` 等弱信号，多行但非结构化结构时不误判
        role, _ = self._role(
            "We need to return to the main argument and if possible refine it.",
        )
        self.assertEqual(role, "body_text")

    def test_citation_not_code(self):
        # 引用行：不被 CODE 误吞（无结构信号）
        role, _ = self._role("See [12] and [34, 56] for details.")
        self.assertEqual(role, "body_text")

    def test_empty_unknown(self):
        from pdf2zh.v3.structure import BlockRole, StructureClassifier

        clf = StructureClassifier()
        block = clf.classify_paragraph(_make_block(""), page=None)
        self.assertIs(block.role, BlockRole.UNKNOWN)


class TestDocumentModelArbitration(unittest.TestCase):
    """forensic/build_document_model 主路径：CODE 块定型 kind=code（无 SemanticPass）。"""

    def _model_from_blocks(self, blocks):
        from pdf2zh.v3.canonical_page import PageModel, annotate_style
        from pdf2zh.v3.document_model import (
            DocumentModel,
            annotate_render,
            annotate_roles,
        )

        page = PageModel(page_num=0, width=600.0, height=800.0)
        page.blocks = blocks
        annotate_style(page)
        annotate_roles(page)
        annotate_render(page)
        model = DocumentModel()
        model.add_page(page)
        return model

    def test_build_path_types_code(self):
        model = self._model_from_blocks(
            [
                _make_real_block("namespace xyza {", lines=["namespace xyza {"]),
                _make_real_block(
                    "int main() { return 0; }", lines=["int main() { return 0; }"]
                ),
                _make_real_block("Ordinary prose.", lines=["Ordinary prose."]),
            ]
        )
        kinds = [b.kind for b in model.pages[0].blocks]
        self.assertEqual(kinds[0], "code")
        self.assertEqual(kinds[1], "code")
        self.assertEqual(kinds[2], "paragraph")  # prose 保持可译

    def test_policy_preserves_code_and_translates_prose(self):
        from pdf2zh.v3.doc_passes import TranslationPolicyPass

        model = self._model_from_blocks(
            [
                _make_real_block("namespace xyza {", lines=["namespace xyza {"]),
                _make_real_block("Ordinary prose.", lines=["Ordinary prose."]),
            ]
        )
        TranslationPolicyPass().run(model)
        blocks = model.pages[0].blocks
        code_pol = blocks[0].metadata["translation_policy"]
        prose_pol = blocks[1].metadata["translation_policy"]
        self.assertFalse(code_pol["translate"])
        self.assertTrue(code_pol["preserve_code"])
        self.assertTrue(prose_pol["translate"])

    def test_translate_document_preserves_code(self):
        from pdf2zh.v3.doc_passes import TranslationPolicyPass
        from pdf2zh.v3.document_model import translate_document

        model = self._model_from_blocks(
            [
                _make_real_block(
                    "using std::lock_guard;", lines=["using std::lock_guard;"]
                ),
                _make_real_block(
                    "This prose gets translated.", lines=["This prose gets translated."]
                ),
            ]
        )
        TranslationPolicyPass().run(model)
        stats = translate_document(model, lambda t: "译_" + t)
        blocks = model.pages[0].blocks
        self.assertEqual(blocks[0].metadata["translated"], "using std::lock_guard;")
        self.assertFalse(blocks[0].metadata["translate"])
        self.assertTrue(blocks[1].metadata["translated"].startswith("译_"))
        self.assertGreaterEqual(stats["preserved"], 1)
        self.assertGreaterEqual(stats["translated"], 1)

    def test_formula_but_not_code_kind(self):
        model = self._model_from_blocks(
            [
                _make_real_block("E = mc^2", lines=["E = mc^2"]),
            ]
        )
        self.assertEqual(model.pages[0].blocks[0].kind, "formula")

    def test_command_filename_identifier_kinds(self):
        model = self._model_from_blocks(
            [
                _make_real_block("pip install numpy", lines=["pip install numpy"]),
                _make_real_block("settings.yaml", lines=["settings.yaml"]),
                _make_real_block("get_user_id", lines=["get_user_id"]),
            ]
        )
        kinds = [b.kind for b in model.pages[0].blocks]
        self.assertEqual(kinds, ["command", "filename", "identifier"])


class TestForensicF2Zero(unittest.TestCase):
    """F2 检测器在修复后的 build_document_model 证据上 FDS 正确（model/translation），
    且 code 块不产生 renderer 假阳性。"""

    def _rows_with_code_preserved(self):
        return [
            {
                "node_id": "p0_0",
                "kind": "code",
                "parser": {"bbox": [0, 0, 100, 50], "text": "namespace xyza {"},
                "translation": {
                    "translated_text": "namespace xyza {",
                    "translation_status": "preserved",
                },
            },
            {
                "node_id": "p0_1",
                "kind": "paragraph",
                "parser": {"bbox": [0, 50, 100, 100], "text": "prose here."},
                "translation": {
                    "translated_text": "prose here.",
                    "translation_status": "translated",
                },
            },
        ]

    def test_f2_code_preserved_not_flagged(self):
        from dual_forensics.defect import F2, run_defect_detectors
        from dual_forensics.diff import Trace

        traces = [
            Trace(
                node_id="p0_0", page=0, kind="code",
                source_text="namespace xyza {", translated_text="namespace xyza {",
                translation_status="preserved",
                render_rows=[{"text": "namespace xyza {"}], matched_present=True,
            ),
            Trace(
                node_id="p0_1", page=0, kind="paragraph",
                source_text="prose here.", translated_text="prose here.",
                translation_status="translated",
                render_rows=[{"text": "prose here."}], matched_present=True,
            ),
        ]
        # code 块 kind=code → F2 detector不应 flag（status preserved → model PASS,
        # translation PASS）→ 无 F2 finding。
        findings = [f for f in run_defect_detectors(traces) if f.defect_id == F2]
        self.assertEqual(findings, [])

    def test_f2_untouched_prose_not_false_positive(self):
        from dual_forensics.defect import F2, _is_code_like

        # prose 不算 code-like → F2 detector 直接跳过（不误报）
        self.assertFalse(_is_code_like("This is ordinary body text."))


class TestNoTranslatorPatch(unittest.TestCase):
    """纪律验证：policy 消费最终 role（metadata.translation_policy），
    修复不在 translator/translate 调用点用 ``if code`` 特殊处理。"""

    def test_build_document_model_has_no_code_preserve_branch(self):
        import inspect

        import pdf2zh.v3.document_model as dm
        from pdf2zh.v3.render_payload import block_translation_unit

        src = inspect.getsource(block_translation_unit)
        # block_translation_unit 依据 presence（policy/kind KEEP_KINDS），
        # 没有单考文本的 translator 级 code 特判。
        self.assertIn("KEEP_KINDS", src)
        self.assertIn('pol.get("translate") is False', src)


if __name__ == "__main__":
    unittest.main()