"""预检盲区回归测试：采样覆盖 + image_ratio 信号接线。

两个历史盲区：

1. **只采前 N 页**：``_extract_pdf_samples`` 曾以 ``pageno >= max_pages: break``
   只取前 3 页，于是「封面/目录有文本层、正文是扫描图」这类最常见的教科书
   版式永远检测不到 —— 恰恰是最该开 OCR 的一批。改为首尾各 N 页。
2. **image_ratio 信号从未接线**：它要求调用方传 ``blocks_by_page``，而所有
   调用点都传 ``None``，五个信号里死了一个。现改为无布局产物时用 pymupdf
   image bbox 现算。
"""

import pytest

from pdf2zh.scanned_detection import (
    DEFAULT_MAX_PAGES,
    _page_image_ratio,
    _sample_page_indices,
    preflight_scan_check,
)

pymupdf = pytest.importorskip("pymupdf")


# ── 采样页选取 ────────────────────────────────────────────────────────────────


def test_sample_pages_small_doc_is_all_pages():
    assert _sample_page_indices(4, 3) == [0, 1, 2, 3]


def test_sample_pages_exact_double():
    assert _sample_page_indices(6, 3) == [0, 1, 2, 3, 4, 5]


def test_sample_pages_head_and_tail():
    """大文档取首尾各 N 页，且去重有序。"""
    got = _sample_page_indices(100, 3)
    assert got == [0, 1, 2, 97, 98, 99]


def test_sample_pages_covers_tail_of_long_doc():
    """回归：正文靠后的长文档必须被采到（修复前只看前 3 页）。"""
    got = _sample_page_indices(700, DEFAULT_MAX_PAGES)
    assert 699 in got
    assert len(got) == 2 * DEFAULT_MAX_PAGES


def test_sample_pages_no_limit():
    assert _sample_page_indices(5, None) == [0, 1, 2, 3, 4]
    assert _sample_page_indices(0, 3) == []


# ── image_ratio 信号 ──────────────────────────────────────────────────────────


def _write_scanned_pdf(path, pages=2):
    """构造「整页一张图」的扫描件 PDF。"""
    doc = pymupdf.open()
    for _ in range(pages):
        page = doc.new_page(width=300, height=400)
        # 满页图片
        page.insert_image(pymupdf.Rect(0, 0, 300, 400), pixmap=_gray_pixmap())
    doc.save(str(path))
    doc.close()
    return str(path)


def _gray_pixmap():
    return pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 32, 32))


def _write_text_pdf(path, pages=2):
    doc = pymupdf.open()
    for _ in range(pages):
        page = doc.new_page()
        page.insert_text((72, 72), "This is a real extractable text layer page.")
    doc.save(str(path))
    doc.close()
    return str(path)


def test_image_ratio_high_for_full_page_scan(tmp_path):
    path = _write_scanned_pdf(tmp_path / "scan.pdf")
    ratio = _page_image_ratio(path, max_pages=2)
    assert ratio is not None
    assert ratio > 0.9, ratio


def test_image_ratio_low_for_text_page(tmp_path):
    path = _write_text_pdf(tmp_path / "text.pdf")
    ratio = _page_image_ratio(path, max_pages=2)
    # 纯文本页：要么没有图像（0.0），要么只有极小图像
    assert ratio is None or ratio < 0.2, ratio


def test_image_ratio_none_for_missing_file(tmp_path):
    assert _page_image_ratio(str(tmp_path / "nope.pdf")) is None


def test_preflight_emits_image_ratio_signal(tmp_path):
    """回归：image_ratio 必须真的出现在预检信号里（修复前缺席）。"""
    path = _write_scanned_pdf(tmp_path / "scan2.pdf")
    decision = preflight_scan_check(path, max_pages=2)
    names = {s.name for s in decision.signals}
    assert "image_ratio" in names, names


def test_preflight_emits_image_ratio_without_blocks(tmp_path):
    """即使没有 blocks_by_page，image_ratio 也必须被算出来。"""
    path = _write_scanned_pdf(tmp_path / "scan3.pdf")
    decision = preflight_scan_check(path, max_pages=2, blocks_by_page=None)
    sig = next(s for s in decision.signals if s.name == "image_ratio")
    assert sig.value > 0.9
    assert "页面图像 bbox" in sig.detail


def test_preflight_uses_supplied_blocks_when_present(tmp_path):
    """给了 blocks_by_page 就用布局产物，不重复探测。"""
    path = _write_text_pdf(tmp_path / "text2.pdf")
    decision = preflight_scan_check(
        path,
        max_pages=2,
        blocks_by_page=[[{"bbox": [0, 0, 300, 400], "type": "image"}]],
    )
    sig = next(s for s in decision.signals if s.name == "image_ratio")
    assert "布局块" in sig.detail
