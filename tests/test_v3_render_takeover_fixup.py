"""RenderTakeover 渲染计划修正（fixup_render_plan）单元测试。

覆盖：
- preserve_float 保留块：dst_box 恒等于 src_box（code/formula/figure/table）；
- 溢出下移：翻译文本显著变长时在页面剩余空间内 shift_down；
- 空间不足：标记 overflowed 并原地保持；
- 正常块：保持 dst_box 不变（render_fixup=keep）；
- 7N-FIX-2：shift_down 必须是**整体几何变换** —— dst_box 与已定版
  render_payload.commands[*].y 同步平移（deep-copy，绝不污染输入计划）；
  keep/preserve/无 commands 块零改动（MECH-2 坐标脱钩回归闸门）。
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


def _flow_entry(block_id, page, n_cmds=1):
    """一个带已定版 flow commands 的条目（命令 y 与 src_box 顶锚定）。

    锚定关系来自真实 dump（p5_1：first_cmd_y == src_box[3]，v3 y-up）。
    盒宽取 300pt：est_lines≈5 → shift_down 可落进页面剩余空间（窄盒会
    因估算行数过大而跌入 keep_overflow，那是另一条被负面控制的分支）。
    """
    src = [418.0, 412.0, 718.0, 426.0]
    cmds = [
        {
            "kind": "flow-text",
            "text": f"line {i}",
            "x": 418.0,
            "y": src[3] - i * 17.0,  # wrapped lines step downward (y-up)
            "width": 30.0,
            "line": i,
            "is_last": i == n_cmds - 1,
            "overflow": False,
            "font_size": 12.8,
        }
        for i in range(n_cmds)
    ]
    return {
        "block_id": block_id,
        "page": page,
        "kind": "paragraph",
        "text": "src text",
        "translated": "这是一个很长很长的中文翻译文本" * 8,
        "render_path": "translate_refit",
        "src_box": list(src),
        "dst_box": list(src),
        "font_size": 12.75,
        # 7N-FIX-3（amount）：layout_ok=True 且无 overflow 的已定版布局会被
        # fixup 直接 keep（不再重估）。要命中 shift_down 路径，载荷必须是
        # 真正溢出的块 —— 与 p442_4（CLIP）同形：layout_ok=False + overflow=True。
        "render_payload": {
            "kind": "flow",
            "commands": cmds,
            "overflow": True,
            "layout_ok": False,
        },
    }


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
        # 7N-FIX-3：v3 y-up 中「页面下方」是 −Δy —— 盒子必须**下移**（y 减小）。
        # 把 p0_0 抬到页面上部，给向下位移留出空间。
        for e in plan:
            if e["block_id"] == "p0_0":
                e["src_box"] = [72, 500, 222, 520]
                e["dst_box"] = [72, 500, 222, 520]
        fixed, stats = fixup_render_plan(plan, page_height={0: 792.0})
        first = next(b for b in fixed if b["block_id"] == "p0_0")
        self.assertEqual(first["render_fixup"], "shift_down")
        self.assertLess(first["dst_box"][1], first["src_box"][1])
        self.assertLess(first["dst_box"][3], first["src_box"][3])
        self.assertEqual(stats["shifted"], 1)

    def test_keep_when_fits(self):
        plan = make_plan()
        fixed, _ = fixup_render_plan(plan)
        last = next(b for b in fixed if b["block_id"] == "p0_3")
        self.assertEqual(last["render_fixup"], "keep")
        self.assertEqual(last["dst_box"], last["src_box"])

    def test_overflow_without_space_marks_overflowed(self):
        plan = make_plan()
        # 7N-FIX-3：p0_0 盒底 y0=100，估算下移量 ≈165pt > 距页底（v3 y=0）
        # 的空间 → 没有剩余空间向下 → overflowed。
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


# ---------------------------------------------------------------------------
# 7N-FIX-2 — shift_down 是整体几何变换（MECH-2 坐标脱钩回归闸门）
# ---------------------------------------------------------------------------


class TestShiftDownCommandCoShift(unittest.TestCase):
    """7N-FIX-2 contract（真实复现锚点：MP2e p5_1 / p5_3 / p5_7）。

    不变量（用户冻结的 FIX-2 contract）：
    1. settled flow 的 command geometry 是权威 —— 实际文字位置 =
       commands[*].{x, y}；
    2. shift_down = Δy 时：dst_box.y0/y1 与 commands[*].y 同步 Δy
       （7N-FIX-3 方向修正：v3 y-up 向下位移为 **−Δy**）；commands[*].x /
       font_size / overflow / 文本不变；
    3. 锚定关系保持：shift 前 first_cmd_y == src_box.y1（真实 dump 锚定），
       shift 后 first_cmd_y == dst_box.y1 继续成立（decoupled == 0）；
    4. deep-copy：fixup 绝不改写调用方传入的原计划（含嵌套 commands）。
    负面控制：keep / preserve / keep_overflow / 无 commands（heading 等）
    永不做 command co-shift —— 只有 shift_down + flow commands 才平移。
    """

    def _fixed(self, extra_entries=None, page_height=792.0):
        plan = [_flow_entry("p5_1", 5), _flow_entry("p5_7", 5, n_cmds=3)]
        plan.extend(extra_entries or [])
        fixed, stats = fixup_render_plan(plan, page_height={5: page_height})
        return plan, fixed, stats

    def test_shift_down_moves_commands_in_lockstep(self):
        plan, fixed, _ = self._fixed()
        entry = next(b for b in fixed if b["block_id"] == "p5_1")
        self.assertEqual(entry["render_fixup"], "shift_down")
        delta = entry["dst_box"][3] - entry["src_box"][3]
        # 7N-FIX-3：v3 y-up 向下位移 → Δy 为负
        self.assertLess(delta, 0.0)
        for cmd, orig in zip(
            entry["render_payload"]["commands"],
            plan[0]["render_payload"]["commands"],
        ):
            self.assertAlmostEqual(cmd["y"], orig["y"] + delta, places=2)

    def test_anchor_relationship_restored_after_shift(self):
        """Invariant 3：shift 后 first_cmd_y == dst_box.y1（decoupled 判据）。"""
        _, fixed, _ = self._fixed()
        for entry in fixed:
            if entry["render_fixup"] != "shift_down":
                continue
            cmds = entry["render_payload"]["commands"]
            self.assertTrue(cmds)
            self.assertAlmostEqual(
                cmds[0]["y"],
                entry["dst_box"][3],
                delta=0.51,
                msg=f"{entry['block_id']}: first_cmd_y 与 dst_box.y1 脱钩",
            )

    def test_command_x_font_and_text_untouched(self):
        plan, fixed, _ = self._fixed()
        entry = next(b for b in fixed if b["block_id"] == "p5_1")
        for cmd, orig in zip(
            entry["render_payload"]["commands"],
            plan[0]["render_payload"]["commands"],
        ):
            self.assertEqual(cmd["x"], orig["x"])
            self.assertEqual(cmd["text"], orig["text"])
            self.assertEqual(cmd.get("font_size"), orig.get("font_size"))
            self.assertEqual(cmd.get("overflow"), orig.get("overflow"))

    def test_shifted_commands_are_copies_not_aliases(self):
        """Invariant 4：fixup 输出不得与输入计划共享可变 command dict。"""
        plan, fixed, _ = self._fixed()
        src_entry = next(b for b in plan if b["block_id"] == "p5_1")
        out_entry = next(b for b in fixed if b["block_id"] == "p5_1")
        src_cmds = src_entry["render_payload"]["commands"]
        out_cmds = out_entry["render_payload"]["commands"]
        self.assertIsNot(src_cmds, out_cmds)
        for a, b in zip(src_cmds, out_cmds):
            self.assertIsNot(a, b)

    def test_input_plan_not_mutated_including_nested_commands(self):
        plan, fixed, _ = self._fixed()
        self.assertEqual(fixed, fixed)  # sanity
        for entry in plan:
            for cmd in entry["render_payload"]["commands"]:
                # 输入计划的 commands y 必须仍是原始锚定值（p5_1: 426.0）
                self.assertAlmostEqual(
                    cmd["y"],
                    426.0 - cmd["line"] * 17.0,
                    places=2,
                    msg="fixup 污染了输入计划的嵌套 commands",
                )

    def test_keep_and_preserve_get_no_command_coshift(self):
        """负面控制：只有 shift_down + flow commands 才做 co-shift。"""
        keeper = _flow_entry("p40_0", 40, n_cmds=2)
        keeper["translated"] = keeper["text"]  # 不变长 → keep
        formula = {
            "block_id": "p40_1",
            "page": 40,
            "kind": "formula",
            "text": "x = a",
            "translated": "x = a",
            "render_path": "preserve_float",
            "src_box": [72, 130, 200, 150],
            "dst_box": [72, 130, 200, 150],
            "font_size": 12,
            "render_payload": {
                "kind": "preserve",
                "commands": [{"kind": "flow-text", "text": "x = a", "x": 72, "y": 150}],
            },
        }
        plan, fixed, _ = self._fixed(extra_entries=[keeper, formula])
        # formula：preserve —— dst 恒等于 src，命令 y 不动
        fx = next(b for b in fixed if b["block_id"] == "p40_1")
        self.assertEqual(fx["render_fixup"], "preserve")
        self.assertEqual(fx["dst_box"], fx["src_box"])
        self.assertEqual(
            fx["render_payload"]["commands"][0]["y"],
            150.0,
            msg="preserve 块的 commands 被意外平移",
        )
        # keep 块（若仍是 keep）—— dst_box 与命令都不得被改动
        kp = next(b for b in fixed if b["block_id"] == "p40_0")
        if kp["render_fixup"] == "keep":
            self.assertEqual(kp["dst_box"], kp["src_box"])
            for cmd, orig in zip(
                kp["render_payload"]["commands"],
                next(b for b in plan if b["block_id"] == "p40_0")["render_payload"][
                    "commands"
                ],
            ):
                self.assertEqual(cmd["y"], orig["y"])

    def test_shift_down_without_commands_leaves_payload_alone(self):
        """heading（无 commands）走 shift_down：dst_box 平移，无 payload 可动。"""
        heading = {
            "block_id": "p5_5",
            "page": 5,
            "kind": "heading",
            "text": "PREFACE",
            "translated": "这是一个很长的标题文本内容用于触发溢出",
            "render_path": "translate_refit",
            "src_box": [423.0, 279.0, 457.0, 293.0],
            "dst_box": [423.0, 279.0, 457.0, 293.0],
            "font_size": 16.15,
        }
        plan, fixed, _ = self._fixed(extra_entries=[heading])
        fx = next(b for b in fixed if b["block_id"] == "p5_5")
        self.assertEqual(fx["render_fixup"], "shift_down")
        delta = fx["dst_box"][3] - fx["src_box"][3]
        # 7N-FIX-3：v3 y-up 向下位移 → Δy 为负
        self.assertLess(delta, 0.0)
        self.assertNotIn("render_payload", fx)

    def test_keep_overflow_marks_overflowed_without_coshift(self):
        """keep_overflow（空间不足）：无平移，任何 commands 保持原 y。"""
        entry = _flow_entry("p9_9", 9)
        # 7N-FIX-3：盒子贴近页底（y0=40 < 估算下移量）→ 向下无空间 → overflowed
        entry["src_box"] = [418.0, 40.0, 718.0, 54.0]
        entry["dst_box"] = [418.0, 40.0, 718.0, 54.0]
        entry["render_payload"]["commands"][0]["y"] = 54.0
        plan = [entry]
        fixed, stats = fixup_render_plan(plan, page_height={9: 350.0})
        fx = next(b for b in fixed if b["block_id"] == "p9_9")
        self.assertEqual(fx["render_fixup"], "keep_overflow")
        self.assertTrue(fx["overflowed"])
        self.assertEqual(stats["overflowed"], 1)
        self.assertEqual(
            fx["render_payload"]["commands"][0]["y"],
            entry["render_payload"]["commands"][0]["y"],
        )

    def test_real_shape_p5_1_reproduction(self):
        """真实复现形状：p5_1（src_top=426, cmd_y=426, 12.75pt）。

        FIX-2 前：shift 后 dst_top=443.85 而 cmd_y 停在 426 → decoupled。
        FIX-2 后：cmd_y 必须与 dst_top 同步（decoupled == 0）。盒宽与真实
        dump 一致用窄盒，但把探针文本换成短句使 shift 可落进页面空间
        （真实 p5_1 的 shift 量 17.85pt 远小于页高，脱钩判据与文本长度无关）。
        """
        entry = _flow_entry("p5_1", 5)
        # 窄盒（box_w=32）→ 估算 2 行 → 命中 shift_down（7N-FIX-3 行高模型：
        # 宽盒下 1 行 + 最后行 1em 会判 keep，这里必须窄到真正换行）。
        entry["src_box"] = [418.0, 412.0, 450.0, 426.0]
        entry["dst_box"] = [418.0, 412.0, 450.0, 426.0]
        entry["translated"] = "致我的父母"
        entry["render_payload"]["commands"][0]["y"] = 426.0
        fixed, _ = fixup_render_plan([entry], page_height={5: 792.0})
        fx = fixed[0]
        self.assertEqual(fx["render_fixup"], "shift_down")
        self.assertAlmostEqual(
            fx["render_payload"]["commands"][0]["y"],
            fx["dst_box"][3],
            delta=0.51,
        )


if __name__ == "__main__":
    unittest.main()
