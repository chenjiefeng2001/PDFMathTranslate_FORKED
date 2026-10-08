"""回归：任何 ``insert_text`` 调用点都不得再收到带 ``\\n`` 的文本。

根因见 ``magicpdf_renderer._layout`` 的注释：pymupdf 会把 ``\\n`` 当换行，在基线
下方额外画一行 —— 这是 mp2e p23 上 0.91pt 幽灵行的来源。所有落笔点都必须自己先
``splitlines()`` 再逐行绘制，而不是让 ``\\n`` 穿透到 ``insert_text``。
"""

import pymupdf
import pytest

from pdf2zh.overlay_renderer import OverlayRenderer, OverlaySegment
from pdf2zh.v3.pdf_renderer import V4PDFRenderer
from pdf2zh.v3.visual_tree import VisualTree


@pytest.fixture
def captured_inserts():
    captured = []
    original = pymupdf.Page.insert_text

    def spy(self, point=None, text="", fontsize=None, **kwargs):
        captured.append(text)
        return original(self, point=point, text=text, fontsize=fontsize, **kwargs)

    pymupdf.Page.insert_text = spy  # type: ignore[assignment]
    try:
        yield captured
    finally:
        pymupdf.Page.insert_text = original  # type: ignore[assignment]


def test_overlay_renderer_splits_newlines_before_insert(captured_inserts):
    seg = OverlaySegment(
        text="A\nB\n\nC", bbox=(50.0, 50.0, 400.0, 200.0), font_size=12.0
    )
    doc = pymupdf.Document()
    page = doc.new_page(width=612.0, height=792.0)

    OverlayRenderer().render_overlay(page, [seg])

    assert captured_inserts, "没有捕获到任何 insert_text 调用"
    assert all("\n" not in t for t in captured_inserts), captured_inserts
    # 3 个非空行，各画一条
    assert len(captured_inserts) == 3, captured_inserts


def test_overlay_renderer_hybrid_splits_newlines_before_insert(captured_inserts):
    seg = OverlaySegment(text="甲\n乙", bbox=(50.0, 50.0, 400.0, 200.0), font_size=12.0)
    doc = pymupdf.Document()
    page = doc.new_page(width=612.0, height=792.0)

    original = pymupdf.Document()
    original.new_page(width=612.0, height=792.0)
    original_bytes = original.tobytes()
    original.close()

    OverlayRenderer().render_hybrid(page, [seg], original_bytes)

    assert captured_inserts, "没有捕获到任何 insert_text 调用"
    assert all("\n" not in t for t in captured_inserts), captured_inserts
    assert len(captured_inserts) == 2, captured_inserts


def test_v4_pdf_renderer_splits_newlines_before_insert(captured_inserts):
    tree = VisualTree()
    tree.freeze_layout()
    tree._display_list = [
        {
            "page": 0,
            "bbox": (50.0, 50.0, 400.0, 200.0),
            "text": "第一行\n第二行\n\n第四行",
            "font_size": 12.0,
        }
    ]

    V4PDFRenderer().render(tree)

    assert captured_inserts, "V4 renderer 没有调用 insert_text"
    assert all("\n" not in t for t in captured_inserts), captured_inserts
    # 3 个非空行，各画一条
    assert len(captured_inserts) == 3, captured_inserts
