"""Headless tests for the Structure Engine（阶段三：块角色分类 + IR 升级）。

覆盖：标题（字号比/节编号）、题注、目录行、页码、页眉页脚、
脚注、公式、正文默认、特征向量计算、DocumentIR 升级与序列化快照。
"""

import pytest

from pdf2zh.v3.geometry import Line, PageGeometry, Paragraph, Word, Char
from pdf2zh.v3.structure import (
    BlockFeatures,
    BlockRole,
    StructureClassifier,
    compute_features,
    to_document_ir,
)
from pdf2zh.v3.document_ir import SemanticRole
from pdf2zh.v3.migration_diff import snapshot_ir


def _para(text, y0, size=10.0, x0=72.0, x1=400.0, font="Helvetica", weight=None):
    ch = Char(
        text=text, x0=x0, y0=y0, x1=x1, y1=y0 + size, size=size, font=weight or font
    )
    return Paragraph([Line([Word([ch])])])


def _page(paras):
    p = PageGeometry(page_num=0, paragraphs=list(paras))
    return p


class TestFeatureVector:
    def test_digit_ratio(self):
        f = compute_features(_para("Figure 42", 100), page=None)
        assert f.digit_ratio == pytest.approx(2 / 9)

    def test_leader_ratio_toc_line(self):
        f = compute_features(_para("1. Intro .......... 3", 100))
        assert f.leader_ratio > 0.05

    def test_weight_from_font_name(self):
        f = compute_features(_para("Bold heading", 100, font="Noto-Bold"))
        assert f.weight_est > 0.8

    def test_numbering_detected(self):
        f = compute_features(_para("3.1 Methods", 100))
        assert f.numbering is True

    def test_serialization_round_trip(self):
        f = compute_features(_para("Hello 123", 100))
        d = f.to_dict()
        assert d["digit_ratio"] == pytest.approx(3 / 9, abs=0.001)


class TestClassifier:
    def test_heading_by_font_ratio(self):
        body = 10.0
        para = _para("Chapter 3: Results", 700, size=18.0)
        page = _page(
            [_para("body one", 660, size=10.0), _para("body two", 640, size=10.0), para]
        )
        block = StructureClassifier(body_font_size=body).classify_paragraph(
            para, page=page, body_font_size=body
        )
        assert block.role is BlockRole.HEADING
        assert block.confidence >= 0.7

    def test_heading_by_numbering(self):
        para = _para("3.1 Methods", 700, size=11.0)
        block = StructureClassifier().classify_paragraph(para)
        assert block.role is BlockRole.HEADING

    def test_caption_detected(self):
        para = _para("Figure 1: Overview of the system.", 300)
        block = StructureClassifier().classify_paragraph(para)
        assert block.role is BlockRole.CAPTION

    def test_table_caption_detected(self):
        para = _para("Table 2: Ablation results.", 300)
        block = StructureClassifier().classify_paragraph(para)
        assert block.role is BlockRole.CAPTION

    def test_toc_entry_detected(self):
        para = _para("1. Introduction .......... 3", 200)
        block = StructureClassifier().classify_paragraph(para)
        assert block.role is BlockRole.TOC_ENTRY

    def test_leader_regex_linear_non_backtracking(self):
        r"""7I-1 regression: long dot-leader TOC text without a trailing digit must
        terminate linearly. Previously ``(?:[.·…‥][\s.·…‥]*){2,}\s*\d{1,4}\s*$"
        had nested-greedy quantifiers -> catastrophic backtracking (an
exponential-time artificial string would hang build_document_model).
        """
        import time

        from pdf2zh.v3.structure import _RE_LEADER

        # failure case (dot run followed by non-digit page number) -> must be fast
        text = "Acknowledgments " + "." * 300 + " xix\nSuggestedways " + "." * 300 + "end"
        t0 = time.perf_counter()
        m = _RE_LEADER.search(text)
        assert m is None
        assert time.perf_counter() - t0 < 1.0  # linear, bounds catastrophic re-growth

        # match case still classifies as TOC entry
        ok = _RE_LEADER.search("1. Introduction .......... 3")
        assert ok is not None

    def test_page_number_detected(self):
        para = _para("42", 30)
        page = _page([para, _para("body", 300), _para("body2", 280)])
        block = StructureClassifier().classify_paragraph(para, page=page)
        assert block.role is BlockRole.PAGE_NUMBER
        assert block.confidence >= 0.9

    def test_roman_page_number(self):
        para = _para("xii", 30)
        block = StructureClassifier().classify_paragraph(para)
        assert block.role is BlockRole.PAGE_NUMBER

    def test_header_at_top(self):
        para = _para("Annual Report 2026", 760)
        page = _page(
            [para, _para("body", 600), _para("body2", 580), _para("body3", 560)]
        )
        block = StructureClassifier().classify_paragraph(para, page=page)
        assert block.role is BlockRole.HEADER

    def test_footer_at_bottom(self):
        para = _para("Page footer text", 40)
        page = _page(
            [_para("body", 600), _para("body2", 580), _para("body3", 560), para]
        )
        block = StructureClassifier().classify_paragraph(para, page=page)
        assert block.role is BlockRole.FOOTER

    def test_footnote_mark_small_font(self):
        para = _para("1 See the appendix.", 60, size=8.0)
        page = _page(
            [_para("body", 500, size=10.0), _para("body2", 480, size=10.0), para]
        )
        block = StructureClassifier(body_font_size=10.0).classify_paragraph(
            para, page=page, body_font_size=10.0
        )
        assert block.role is BlockRole.FOOTNOTE

    def test_formula_symbol_line(self):
        para = _para("E = mc^2", 200, size=12.0)
        block = StructureClassifier().classify_paragraph(para)
        assert block.role is BlockRole.FORMULA

    def test_body_default(self):
        para = _para("This is a normal body paragraph with some words.", 500)
        block = StructureClassifier().classify_paragraph(para)
        assert block.role is BlockRole.BODY_TEXT

    def test_body_font_median_estimate(self):
        paras = [
            _para("title", 700, size=18.0),
            _para("a", 600, size=10.0),
            _para("b", 580, size=10.0),
            _para("c", 560, size=10.0),
        ]
        clf = StructureClassifier()
        assert clf.estimate_body_font_size([_page(paras)]) == pytest.approx(10.0)

    def test_empty_text_unknown(self):
        para = _para("", 100)
        block = StructureClassifier().classify_paragraph(para)
        assert block.role is BlockRole.UNKNOWN


class TestDocumentIR:
    def test_to_document_ir_hierarchy(self):
        paras = [
            _para("Chapter 1", 700, size=16.0),
            _para("Body text one.", 660, size=10.0),
            _para("Figure 1: Caption.", 620, size=10.0),
        ]
        page = _page(paras)
        ir = to_document_ir([page], title="doc")
        assert ir.node_count == 1 + len(paras)  # 1 page section + 3 blocks
        page_node = ir.get_node("page_0")
        assert page_node is not None
        assert len(page_node.children) == 3

    def test_to_document_ir_roles(self):
        paras = [
            _para("Chapter 1", 700, size=16.0),
            _para("Body text one.", 660, size=10.0),
            _para("Figure 1: Caption.", 620, size=10.0),
        ]
        page = _page(paras)
        ir = to_document_ir([page], title="doc")
        by_text = {n.text: n for n in ir.nodes()}
        assert by_text["Chapter 1"].semantic is SemanticRole.HEADING
        assert by_text["Body text one."].semantic is SemanticRole.BODY_TEXT
        assert by_text["Figure 1: Caption."].semantic is SemanticRole.CAPTION

    def test_ir_json_round_trip_and_snapshot(self):
        paras = [
            _para("Chapter 1", 700, size=16.0),
            _para("Body text one.", 660, size=10.0),
            _para("Table 1: Data.", 620, size=10.0),
        ]
        ir = to_document_ir([_page(paras)], title="doc")
        restored = type(ir).from_json(ir.to_json())
        assert restored.node_count == ir.node_count
        snap = snapshot_ir(ir, title="doc")
        assert snap["schema"] == "pdf2zh.v3.ir-snapshot"
        assert len(snap["headings"]) == 1
        assert len(snap["captions"]) == 1
        assert len(snap["paragraphs"]) == 1

    def test_ir_reading_order_preserved(self):
        paras = [
            _para("Body one.", 660, size=10.0),
            _para("Body two.", 640, size=10.0),
        ]
        ir = to_document_ir([_page(paras)], title="doc")
        page = ir.get_node("page_0")
        texts = [ir.get_node(c).text for c in page.children]
        assert texts == ["Body one.", "Body two."]
