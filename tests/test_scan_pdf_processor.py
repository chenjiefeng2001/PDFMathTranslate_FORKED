"""
Tests for ScanPDFProcessor (pdf2zh 2.0 L1 - scanned PDF OCR pipeline).
"""
import numpy as np
import pytest

from pdf2zh.scan_pdf_processor import (
    LayoutRegion, ScanPDFProcessor, TextSegment, _smooth, _threshold,
)


@pytest.fixture
def processor():
    return ScanPDFProcessor(lang="zh-cn", dpi=72)


@pytest.fixture
def white_image():
    return np.ones((400, 300, 3), dtype=np.uint8) * 255


@pytest.fixture
def two_column_image():
    img = np.ones((400, 600, 3), dtype=np.uint8) * 255
    img[50:350, 20:270] = 0
    img[50:350, 330:580] = 0
    return img


class TestScanPDFProcessor:
    def test_init_defaults(self):
        p = ScanPDFProcessor()
        assert p.lang == "zh-cn"
        assert p.dpi == 300
        assert p.scale == 300 / 72

    def test_init_custom(self):
        p = ScanPDFProcessor(lang="ja", dpi=150, ocr_engine="paddle")
        assert p.lang == "ja"
        assert p.ocr_engine == "paddle"

    def test_extract_empty_image(self, processor, white_image):
        assert processor.extract_text_with_positions(white_image) == []

    def test_analyze_layout_white_image(self, processor, white_image):
        regions = processor.analyze_layout(white_image)
        assert len(regions) >= 1
        for r in regions:
            assert len(r.bbox) == 4

    def test_analyze_layout_two_column(self, processor, two_column_image):
        regions = processor.analyze_layout(two_column_image)
        text_regions = [r for r in regions if r.region_type == "text"]
        assert len(text_regions) >= 1

    def test_textsegment_dataclass(self):
        seg = TextSegment(text="hello", bbox=(10, 20, 100, 50), confidence=0.95, page_num=1)
        assert seg.text == "hello"
        assert seg.confidence == 0.95

    def test_textsegment_defaults(self):
        seg = TextSegment(text="test", bbox=(0, 0, 10, 10))
        assert seg.confidence == 1.0
        assert seg.font_size_est == 12.0
        assert seg.page_num == 0

    def test_layoutregion_defaults(self):
        region = LayoutRegion()
        assert region.region_type == "text"

    def test_sort_by_reading_order_empty(self, processor):
        assert processor._sort_by_reading_order([]) == []

    def test_sort_by_reading_order_top_to_bottom(self, processor):
        segs = [
            TextSegment(text="second", bbox=(0, 100, 100, 120)),
            TextSegment(text="first", bbox=(0, 10, 100, 30)),
        ]
        result = processor._sort_by_reading_order(segs)
        # PDF coords: larger y = higher on page = first in reading order
        assert result[0].text == "second"
        assert result[1].text == "first"

    def test_pdf_to_pixel_coords(self, processor):
        bbox = (10, 20, 100, 50)
        px = processor.pdf_to_pixel_coords(bbox, 400)
        assert len(px) == 4
        assert px[0] == 10
        assert px[1] == 400 - 50

    def test_ocr_region_returns_empty(self, processor, white_image):
        region = LayoutRegion(bbox=(0, 0, 100, 100), region_type="text")
        assert processor._ocr_region(white_image, region) == []


class TestUtilityFunctions:
    def test_smooth_window_1(self):
        arr = np.array([1.0, 2.0, 3.0])
        result = _smooth(arr, window=1)
        np.testing.assert_array_equal(result, arr)

    def test_threshold_binary(self):
        img = np.array([[100, 150, 200], [250, 50, 180]], dtype=np.uint8)
        result = _threshold(img, 128, 255, "binary")
        assert result[0, 0] == 0
        assert result[0, 2] == 255
