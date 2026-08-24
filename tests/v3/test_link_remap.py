"""V8.5 hyperlink re-anchoring tests.

Two layers:
  * Pure unit tests for ``pdf2zh.v3.link_remap`` rect math, paragraph
    matching, projection and multi-paragraph handling.
  * A fitz-backed integration test that reproduces the reported defect
    (link /Rect stuck at source layout while the translated span moved) and
    asserts the re-anchored rect overlaps the translated span with IoU >= 0.5.

Run with:
    python -m pytest tests/v3/test_link_remap.py -v
"""

import unittest

from pdf2zh.v3.link_remap import (
    compute_link_updates,
    match_link_to_paragraphs,
    normalize_rect,
    project_rect,
    records_to_boxes,
    rect_area,
    rect_center,
    rect_iou,
    remap_document_links,
)


class TestRectBasics(unittest.TestCase):
    def test_normalize_flip(self):
        # (x0, y0) may exceed (x1, y1) after a y-down frame; canonicalize.
        self.assertEqual(normalize_rect((72, 104, 260, 90)), (72, 90, 260, 104))

    def test_area_zero_for_degenerate(self):
        self.assertEqual(rect_area((0, 0, 0, 10)), 0.0)
        self.assertEqual(rect_area((1, 2, 5, 2)), 0.0)

    def test_center(self):
        self.assertEqual(rect_center((72, 90, 260, 104)), (166.0, 97.0))

    def test_iou(self):
        a = (0, 0, 100, 100)
        b = (50, 0, 150, 100)
        self.assertAlmostEqual(rect_iou(a, b), 1 / 3)
        self.assertEqual(rect_iou(a, (200, 200, 300, 300)), 0.0)


class TestParagraphMatching(unittest.TestCase):
    def setUp(self):
        # Source paragraphs on a page (PDF user space, y-up).
        self.boxes = [(72.0, 90.0, 260.0, 104.0), (72.0, 48.0, 320.0, 63.0)]

    def test_center_containment(self):
        # Link targeting the paragraph whose bbox contains the link center.
        idx = match_link_to_paragraphs((100.0, 93.0, 120.0, 100.0), self.boxes)
        self.assertEqual(idx, 0)

    def test_max_overlap_fallback(self):
        # Link wider than the paragraph but overlapping most with para 0.
        idx = match_link_to_paragraphs((70.0, 92.0, 265.0, 103.0), self.boxes)
        self.assertEqual(idx, 0)

    def test_no_match(self):
        self.assertIsNone(
            match_link_to_paragraphs((0.0, 300.0, 30.0, 310.0), self.boxes)
        )
        self.assertIsNone(match_link_to_paragraphs((0.0, 0.0, 0.0, 0.0), []))


class TestProjection(unittest.TestCase):
    def test_translate_then_scale(self):
        src = (72.0, 90.0, 260.0, 104.0)
        dst = (72.0, 48.0, 320.0, 63.0)
        # Full-paragraph link: spans the whole source paragraph.
        linked = project_rect(src, src, dst)
        self.assertAlmostEqual(linked[0], 72.0, places=4)
        self.assertAlmostEqual(linked[2], 320.0, places=4)
        # 整段链接，src->dst 的 y 是 dst 段底 48 与顶 63（高度标度几乎不变）。
        self.assertAlmostEqual(linked[1], 48.0, places=4)
        self.assertAlmostEqual(linked[3], 63.0, places=4)

    def test_degenerate_src_degrades_to_translation(self):
        # src height collapses to 0 -> fall back gracefully without explosion.
        src = (72.0, 90.0, 260.0, 90.0)
        dst = (72.0, 48.0, 320.0, 63.0)
        out = project_rect((100.0, 90.0, 120.0, 90.0), src, dst)
        # Vertical edge stays degenerate, horizontal maps proportionally.
        self.assertTrue(all(x == x for x in out))  # no NaN

    def test_orientation_preserved(self):
        # y-down source conventions still project consistently.
        src = (72.0, 90.0, 260.0, 104.0)
        dst = (72.0, 48.0, 320.0, 63.0)
        out = project_rect((100.0, 93.0, 200.0, 100.0), src, dst)
        self.assertLess(out[1], out[3])  # preserving y-up output ordering


class TestRecordsBoxes(unittest.TestCase):
    def test_extended_schema(self):
        records = [
            {
                "x": 72.0,
                "y": 63.0,
                "width": 188.0,
                "height": 15.0,
                "src_box": (72.0, 90.0, 260.0, 104.0),
                "dst_box": (72.0, 48.0, 320.0, 63.0),
            }
        ]
        src_boxes, dst_boxes = records_to_boxes(records)
        self.assertEqual(src_boxes[0], (72.0, 90.0, 260.0, 104.0))
        self.assertEqual(dst_boxes[0], (72.0, 48.0, 320.0, 63.0))

    def test_legacy_schema_identity(self):
        records = [{"x": 72.0, "y": 63.0, "width": 188.0, "height": 15.0}]
        src_boxes, dst_boxes = records_to_boxes(records)
        # Legacy path: src == dst -> projection is a no-op identity.
        self.assertEqual(src_boxes, dst_boxes)


class TestComputeUpdates(unittest.TestCase):
    def test_basic_mapping(self):
        # 近整段链接：投影后应与 dst 段落 bbox 高度重合（IoU >= 0.5）。
        links = [{"id": "L0", "from": (75.0, 91.0, 255.0, 103.0), "uri": "http://x"}]
        src = [(72.0, 90.0, 260.0, 104.0)]
        dst = [(72.0, 48.0, 320.0, 63.0)]
        updates = compute_link_updates(links, src, dst)
        self.assertEqual(len(updates), 1)
        _, new_rect = updates[0]
        self.assertGreaterEqual(rect_iou(new_rect, dst[0]), 0.5)

    def test_unmatched_link_untouched(self):
        links = [{"id": "L0", "from": (0.0, 500.0, 50.0, 520.0)}]
        updates = compute_link_updates(
            links, [(72.0, 90.0, 260.0, 104.0)], [(72.0, 48.0, 320.0, 63.0)]
        )
        self.assertEqual(updates, [])

    def test_mismatched_boxes_len(self):
        links = [{"from": (100.0, 93.0, 200.0, 100.0)}]
        self.assertEqual(compute_link_updates(links, [(1, 1, 2, 2)], []), [])


class TestFitzIntegration(unittest.TestCase):
    """Repro the reported defect end-to-end via the fitz entry point."""

    def _seed_doc(self):
        import fitz

        doc = fitz.open()
        page = doc.new_page(width=612, height=792)
        # "源" 布局的段落文字（作为参考，仅用于锚定预期 dst span）。
        page.insert_text((72, 104), "Source paragraph text")  # y-up baseline
        page.insert_link(
            {
                "kind": fitz.LINK_URI,
                "from": fitz.Rect(72, 90, 260, 104),
                "uri": "http://example.com",
            }
        )
        # 注解需要写入/回读才在 get_links() 可见（与生产路径一致：源 PDF 已含注解）。
        return fitz.open(stream=doc.tobytes(), filetype="pdf")

    def test_relink_rect_tracks_translated_span(self):
        import fitz

        doc = self._seed_doc()
        # 译后段落实际渲染在 (72, 48..63)，而 link /Rect 仍停留在源布局 (72, 90..104)。
        records = {
            0: [
                {
                    "x": 72.0,
                    "y": 63.0,
                    "width": 188.0,
                    "height": 15.0,
                    "src_box": (72.0, 90.0, 260.0, 104.0),
                    "dst_box": (72.0, 48.0, 320.0, 63.0),
                }
            ]
        }
        stats = remap_document_links(doc, records, page_offset=0)
        self.assertEqual(stats["relinked"], 1)

        # Re-open (fitz exposes links only after re-open/save round-trip).
        page = doc[0]
        links = page.get_links()
        self.assertEqual(len(links), 1)
        new_rect = tuple(float(v) for v in links[0]["from"])
        # 验收口径：修正后的 rect 与译后 span bbox IoU >= 0.5
        self.assertGreaterEqual(rect_iou(new_rect, (72.0, 48.0, 320.0, 63.0)), 0.5)

    def test_no_records_is_noop(self):
        doc = self._seed_doc()
        stats = remap_document_links(doc, {}, page_offset=0)
        self.assertEqual(stats["pages"], 0)
        self.assertEqual(stats["relinked"], 0)

    def test_out_of_range_page_skipped(self):
        doc = self._seed_doc()
        stats = remap_document_links(
            doc, {5: [{"src_box": (0, 0, 10, 10), "dst_box": (0, 0, 10, 10)}]}
        )
        self.assertEqual(stats["relinked"], 0)

    def test_page_shift_applied(self):
        import fitz

        doc = self._seed_doc()
        records = {
            0: [
                {
                    "x": 72.0,
                    "y": 63.0,
                    "width": 188.0,
                    "height": 15.0,
                    "src_box": (68.0, 86.0, 256.0, 100.0),
                    "dst_box": (68.0, 44.0, 316.0, 59.0),
                }
            ]
        }
        shifts = {0: (4.0, 4.0)}  # 微信等页 cropbox 偏移
        stats = remap_document_links(doc, records, page_offset=0, page_shifts=shifts)
        self.assertEqual(stats["relinked"], 1)


if __name__ == "__main__":
    unittest.main()
