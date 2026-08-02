"""Tests for CollisionResolver (Phase 2)."""
import unittest
from pdf2zh.collision_resolver import BoundingBox, CollisionResolver


class TestBoundingBox(unittest.TestCase):
    """Test BoundingBox geometry."""

    def test_width(self):
        bbox = BoundingBox(10, 20, 110, 220)
        self.assertEqual(bbox.width, 100)

    def test_height(self):
        bbox = BoundingBox(10, 20, 110, 220)
        self.assertEqual(bbox.height, 200)

    def test_overlap_true(self):
        a = BoundingBox(0, 0, 100, 100)
        b = BoundingBox(50, 50, 150, 150)
        self.assertTrue(a.overlaps(b))

    def test_overlap_false(self):
        a = BoundingBox(0, 0, 100, 100)
        b = BoundingBox(200, 200, 300, 300)
        self.assertFalse(a.overlaps(b))

    def test_overlap_touching(self):
        """Touching boxes without margin should not overlap."""
        a = BoundingBox(0, 0, 100, 100)
        b = BoundingBox(101, 0, 200, 100)
        self.assertTrue(a.overlaps(b, margin=2))
        self.assertFalse(a.overlaps(b, margin=0))

    def test_intersection_area_partial(self):
        a = BoundingBox(0, 0, 100, 100)
        b = BoundingBox(50, 50, 150, 150)
        area = a.intersection_area(b)
        self.assertAlmostEqual(area, 2500)  # 50 * 50

    def test_intersection_area_no_overlap(self):
        a = BoundingBox(0, 0, 100, 100)
        b = BoundingBox(200, 200, 300, 300)
        area = a.intersection_area(b)
        self.assertEqual(area, 0)

    def test_intersection_area_contained(self):
        a = BoundingBox(0, 0, 200, 200)
        b = BoundingBox(50, 50, 100, 100)
        area = a.intersection_area(b)
        self.assertAlmostEqual(area, 2500)


class TestCollisionResolver(unittest.TestCase):
    """Test collision resolution strategies."""

    def setUp(self):
        self.resolver = CollisionResolver(max_shrink=0.8)
        self.text_bbox = BoundingBox(100, 100, 300, 200)
        self.obstacle = BoundingBox(150, 50, 250, 250)  # Overlaps text

    def test_no_obstacles_returns_original(self):
        x, y, size = self.resolver.resolve(
            self.text_bbox, [], 12.0
        )
        self.assertEqual(x, 100)
        self.assertEqual(y, 100)
        self.assertEqual(size, 12.0)

    def test_non_overlapping_obstacle_returns_original(self):
        far_obstacle = BoundingBox(500, 500, 600, 600)
        x, y, size = self.resolver.resolve(
            self.text_bbox, [far_obstacle], 12.0
        )
        self.assertEqual(x, 100)
        self.assertEqual(y, 100)
        self.assertEqual(size, 12.0)

    def test_collision_resolves(self):
        """Collision should resolve without crashing."""
        x, y, size = self.resolver.resolve(
            self.text_bbox, [self.obstacle], 12.0
        )
        self.assertIsNotNone(x)
        self.assertIsNotNone(y)
        self.assertIsNotNone(size)

    def test_collision_shifts_vertically(self):
        """Collision resolver should attempt vertical shift first."""
        obstacle = BoundingBox(100, 102, 200, 104)  # Thin strip at top edge
        x, y, size = self.resolver.resolve(
            self.text_bbox, [obstacle], 12.0
        )
        # Should shift
        self.assertNotEqual(y, 100)

    def test_font_size_shrink_as_last_resort(self):
        """When vertical shift can't work, font size may shrink."""
        # Large obstacle that fully contains text area
        big_obstacle = BoundingBox(50, 50, 350, 250)
        x, y, size = self.resolver.resolve(
            self.text_bbox, [big_obstacle], 12.0
        )
        # Size may or may not shrink depending on strategy
        self.assertLessEqual(size, 12.0)

    def test_large_vertical_shift_for_line_expansion(self):
        """单行段落（lidx==0）膨胀侵占下方空间时，应获得大幅（多行）
        垂直偏移，而不是回退到缩小字号。"""
        text = BoundingBox(100, 300, 500, 340)      # 单行段落
        obstacle = BoundingBox(100, 280, 500, 330)  # 占据约 2.5 行高度
        new_y = self.resolver._try_vertical_shift(text, [obstacle], 10.0)
        self.assertIsNotNone(new_y)
        # 需要 >= 4 行偏移（40pt）才能完全避开障碍物
        self.assertGreaterEqual(new_y, text.y0 + 40.0)

    def test_resolve_avoids_lower_noncolliding_obstacle(self):
        """向下偏移时必须避开原本未重叠的下方元素（全量障碍物探测）。"""
        text = BoundingBox(100, 100, 300, 160)
        upper = BoundingBox(100, 150, 300, 155)    # 与 text 重叠
        lower = BoundingBox(100, 165, 300, 170)    # 位于 text 下方
        x, y, size = self.resolver.resolve(text, [upper, lower], 10.0)
        shifted = BoundingBox(x, y, x + text.width, y + text.height)
        self.assertFalse(shifted.overlaps(upper))
        self.assertFalse(shifted.overlaps(lower))
        self.assertNotEqual(y, text.y0)  # 必须发生移动


if __name__ == "__main__":
    unittest.main()
