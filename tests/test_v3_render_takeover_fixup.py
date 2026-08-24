"""RenderTakeover 渲染计划修正（fixup_render_plan）单元测试。

覆盖：
- preserve_float 保留块：dst_box 恒等于 src_box（code/formula/figure/table）；
- 溢出下移：翻译文本显著变长时在页面剩余空间内 shift_down；
- 空间不足：标记 overflowed 并原地保持；
- 正常块：保持 dst_box 不变（render_fixup=keep）。
"""

import unittest

from pdf2zh.v3.render_takeover import fixup_render_plan


def make_plan():
    return [
        {
            "block_id": "p0_0",
            "page": 0,
            "kind": "paragraph",
            "text": "short formula intro",
            "translated": "这是一个很长很长的中文翻译文本" * 8,
            "render_path": "translate_refit",
            "src_box": [72, 100, 222, 120],
            "dst_box": [72, 100, 222, 120],
            "font_size": 12,
        },
        {
            "block_id": "p0_1",
            "page": 0,
            "kind": "formula",
            "text": "x = a",
            "translated": "x = a",
            "render_path": "preserve_float",
            "src_box": [72, 130, 200, 150],
            "dst_box": [72, 130, 200, 150],
            "font_size": 12,
        },
        {
            "block_id": "p0_2",
            "page": 0,
            "kind": "code",
            "text": "def f(): pass",
            "translated": "def f(): pass",
            "render_path": "translate_refit",
            "src_box": [72, 160, 200, 175],
            "dst_box": [72, 160, 200, 175],
            "font_size": 10,
        },
        {
            "block_id": "p0_3",
            "page": 0,
            "kind": "paragraph",
            "text": "short",
            "translated": "short",
            "render_path": "translate_refit",
            "src_box": [72, 200, 300, 215],
            "dst_box": [72, 200, 300, 215],
            "font_size": 12,
        },
    ]


class TestFixupRenderPlan(unittest.TestCase):
    def test_preserve_float_stays_at_src(self):
        plan = make_plan()
        fixed, stats = fixup_render_plan(plan)
        formula = next(b for b in fixed if b["kind"] == "formula")
        self.assertEqual(formula["dst_box"], formula["src_box"])
        self.assertEqual(formula["render_fixup"], "preserve")
        self.assertEqual(stats["preserved"], 2)  # formula + code 保留块

    def test_code_kind_preserved_even_with_translate_path(self):
        # code 块即便 render_path 是 translate_refit，也应原样保留
        plan = make_plan()
        fixed, _ = fixup_render_plan(plan)
        code = next(b for b in fixed if b["kind"] == "code")
        self.assertEqual(code["dst_box"], code["src_box"])
        self.assertEqual(code["render_fixup"], "preserve")

    def test_overflow_shift_down(self):
        plan = make_plan()
        fixed, stats = fixup_render_plan(plan, page_height={0: 792.0})
        first = next(b for b in fixed if b["block_id"] == "p0_0")
        self.assertEqual(first["render_fixup"], "shift_down")
        self.assertGreater(first["dst_box"][1], first["src_box"][1])
        self.assertEqual(stats["shifted"], 1)

    def test_keep_when_fits(self):
        plan = make_plan()
        fixed, _ = fixup_render_plan(plan)
        last = next(b for b in fixed if b["block_id"] == "p0_3")
        self.assertEqual(last["render_fixup"], "keep")
        self.assertEqual(last["dst_box"], last["src_box"])

    def test_overflow_without_space_marks_overflowed(self):
        plan = make_plan()
        # 页面高度很小 → 没有剩余空间下移 → overflowed
        fixed, stats = fixup_render_plan(plan, page_height={0: 175.0})
        first = next(b for b in fixed if b["block_id"] == "p0_0")
        self.assertTrue(first["overflowed"])
        self.assertEqual(first["render_fixup"], "keep_overflow")
        self.assertEqual(stats["overflowed"], 1)

    def test_empty_plan(self):
        fixed, stats = fixup_render_plan([])
        self.assertEqual(fixed, [])
        self.assertEqual(stats["shifted"], 0)

    def test_none_plan(self):
        fixed, _ = fixup_render_plan(None)
        self.assertEqual(fixed, [])

    def test_no_mutation_of_input(self):
        plan = make_plan()
        snapshot = [dict(b) for b in plan]
        fixup_render_plan(plan)
        self.assertEqual(plan, snapshot)


if __name__ == "__main__":
    unittest.main()
