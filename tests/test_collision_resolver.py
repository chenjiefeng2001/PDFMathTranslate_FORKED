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
        垂直偏移，而不是回退到缩小字号。新版语义：优先向下（正文推进方向）。"""
        text = BoundingBox(100, 300, 500, 340)      # 单行段落
        obstacle = BoundingBox(100, 280, 500, 330)  # 占据约 2.5 行高度
        new_y = self.resolver._try_vertical_shift(text, [obstacle], 10.0)
        self.assertIsNotNone(new_y)
        # 下移 >= 4 行偏移（40pt）才能完全避开障碍物（y 减小 = 向下）
        self.assertLessEqual(new_y, text.y0 - 40.0)

    def test_vertical_shift_prefers_downward(self):
        """优先向下避让：上、下两个方向都可行时，必须选择向下（正文推进方向）。"""
        text = BoundingBox(100, 100, 300, 160)
        # 上方障碍物迫使段落必须让出重叠区；上下都有空档
        upper = BoundingBox(100, 145, 300, 165)  # 与 text 重叠
        new_y = self.resolver._try_vertical_shift(text, [upper], 10.0)
        self.assertIsNotNone(new_y)
        self.assertLess(new_y, text.y0)  # 向下（y 减小）

    def test_vertical_shift_pushes_below_stacked_obstacles(self):
        """多障碍物层叠时，下推需逐层让开所有障碍物（贪心精确下推）。"""
        text = BoundingBox(100, 300, 300, 360)  # 高 60
        a = BoundingBox(100, 280, 300, 330)    # 第一层
        b = BoundingBox(100, 240, 300, 290)    # 第二层（与 a 紧邻）
        new_y = self.resolver._try_vertical_shift(text, [a, b], 12.0)
        self.assertIsNotNone(new_y)
        # 需越过 a 与 b 两层：b 底部 y0 = 240
        shifted = BoundingBox(100, new_y, 300, new_y + 60)
        self.assertFalse(shifted.overlaps(a))
        self.assertFalse(shifted.overlaps(b))
        self.assertLess(new_y, 240)

    def test_page_rect_clamps_downward_shift(self):
        """页面底部钳制：下推结果不得低于页面底边（y=0）。"""
        page_rect = BoundingBox(0, 0, 500, 700)
        text = BoundingBox(100, 30, 300, 60)      # 接近页面底部
        obstacle = BoundingBox(100, 20, 300, 45)  # 与 text 重叠
        new_y = self.resolver._try_vertical_shift(text, [obstacle], 10.0, page_rect)
        self.assertIsNotNone(new_y)
        self.assertGreaterEqual(new_y, page_rect.y0)
        # 若钳制后仍重叠，则下移路径不可行，应尝试向上
        if new_y == page_rect.y0:
            shifted = BoundingBox(100, new_y, 300, new_y + 30)
            self.assertTrue(shifted.overlaps(obstacle))

    def test_push_down_keeps_font_margin_at_bottom(self):
        """P2: 底部钳制保留一个字号空间（page.y0 + font_size）。
        下推计算值低于 page.y0 + font_size 时应被钳制到字号空间上沿，
        避免字形 descent 把 bbox 顶出页面底边（bbox.y1 < 0）。"""
        page_rect = BoundingBox(0, 0, 500, 700)
        text = BoundingBox(100, 30, 300, 50)
        # obstacle 顶部较低，使下推目标 new_y = 36-20-2*2-10*0.3-0.01 = 8.99 < 10，
        # 且下移后 bbox(10..30) 与 obstacle(34.5..36) 间隔 > 2*margin → 可一次到位
        obstacle = BoundingBox(100, 34.5, 300, 36)
        new_y = self.resolver._try_vertical_shift(text, [obstacle], 10.0, page_rect)
        self.assertIsNotNone(new_y)
        self.assertGreaterEqual(new_y, page_rect.y0 + 10.0 - 1e-6)
        self.assertLess(new_y, text.y0)  # 发生了下移

    def test_push_up_keeps_font_margin_at_top(self):
        """P2: 顶部钳制保留一个字号空间（page.y1 - height - font_size），
        避免字形 ascent 把 bbox 顶出页面顶部（修复后 pymupdf bbox.y0 < 0 不再出现）。"""
        page_rect = BoundingBox(0, 0, 500, 700)
        text = BoundingBox(100, 600, 300, 640)
        down_ob = BoundingBox(100, 0, 300, 610)  # 占满下方 → 下推不可行
        up_ob = BoundingBox(100, 635, 300, 645)  # 与 text 顶部重叠 → 触发上推
        new_y = self.resolver._try_vertical_shift(text, [down_ob, up_ob], 10.0, page_rect)
        self.assertIsNotNone(new_y)
        self.assertLessEqual(new_y, page_rect.y1 - text.height - 10.0 + 1e-6)
        self.assertGreater(new_y, text.y0)  # 发生了上移

    def test_resolve_return_strategy_vertical(self):
        """return_strategy=True 时返回 4 元组，垂直避让应标记为 vertical。"""
        obstacle = BoundingBox(100, 102, 200, 104)
        x, y, size, strategy = self.resolver.resolve(
            self.text_bbox, [obstacle], 12.0, return_strategy=True
        )
        self.assertEqual(strategy, "vertical")
        self.assertNotEqual(y, 100)

    def test_resolve_return_strategy_clear(self):
        """无碰撞时返回 strategy='clear'，位置不变。"""
        x, y, size, strategy = self.resolver.resolve(
            self.text_bbox, [BoundingBox(500, 500, 600, 600)], 12.0, return_strategy=True
        )
        self.assertEqual(strategy, "clear")
        self.assertEqual(y, 100)

    def test_resolve_no_obstacles_strategy_noop(self):
        """空障碍物返回 strategy='noop'。"""
        x, y, size, strategy = self.resolver.resolve(
            self.text_bbox, [], 12.0, return_strategy=True
        )
        self.assertEqual(strategy, "noop")

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
