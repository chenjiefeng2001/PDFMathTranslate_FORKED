# 7N-FIX-2 — fixup shift_down 整体几何变换（MECH-2 坐标脱钩修复）

> **日期**：2026-09-02 · **基线**：v1.9.16 / `4eccfa6` + 工作区 · **性质**：生产修复
> （7N-8 闸门按 §10.3 分叉矩阵开启：**仅 FIX-2**；FIX-1 维持冻结）。
> 授权 contract：用户冻结的四不变量（Invariant 1–4，见 §2）。

---

## 0. 结论先行

`fixup_render_plan` 的 `shift_down` 分支原来是**只改 `dst_box`、不改
`render_payload.commands`** 的半边几何变换。对带已定版 flow commands 的块，
renderer 只读 command 坐标、白底矩形只读 `dst_box` —— 两者从此脱钩
（MECH-2）。本次修复让 shift_down 成为**整体几何变换**：

```text
shift_down = Δy
   ├── dst_box.y0 += Δy
   ├── dst_box.y1 += Δy
   └── render_payload.commands[*].y += Δy   ← 新增（deep-copy 后）
             ↓
   renderer 的白底矩形与文字重新共享同一几何
```

7N-REAL 分叉矩阵裁决（`doc/7n0_mp2e_forensic_report.md` §10.3）：

- MECH-1（CLIP line collapse）：真实运行 **0 clip** → FIX-1 闸门**不开**，冻结；
- MECH-2（fixup 坐标脱钩）：真实运行 **decoupled=3**（p5_1/p5_3/p5_7）
  → FIX-2 闸门**开启**，本轮落地。

---

## 1. 真实复现证据（修复前）

`doc/7n-real/align-magicpdf.json`（生产 CLI 实跑 MP2e 12 页采样）：

```text
p5_1  page 5  shift_down  dst_y1=443.85  first_cmd_y=426.0   decoupled=True
p5_3  page 5  shift_down  dst_y1=393.46  first_cmd_y=366.0   decoupled=True
p5_7  page 5  shift_down  dst_y1=261.12  first_cmd_y=219.0   decoupled=True
（另有 p5_5 / p550_1 两个 shift_down，均为 heading，无 commands —— 不涉脱钩）
```

锚定事实：每个块 `first_cmd_y == src_box.y1`（命令顶锚定在原块顶），fixup
平移 `dst_box` 后 `dst_y1 - first_cmd_y` = 全额 shift —— 覆盖矩形与文字
几何脱钩 17.85 / 27.46 / 42.12 pt。

## 2. 冻结 contract（四不变量）

| # | 不变量 | 内容 |
|---|---|---|
| 1 | command geometry 权威 | 带 `render_payload.commands` 的 flow：实际文字位置 = `commands[*].{x,y}`；`dst_box` 不能独立代表文字位置 |
| 2 | shift_down 是整体几何变换 | `dst_box.y0/y1 += Δy` 与 `commands[*].y += Δy` 同步；`commands[*].x` / `font_size` / `overflow` / 文本内容不变 |
| 3 | 锚定关系保持 | shift 前 `first_cmd_y == src_box.y1`；shift 后 `first_cmd_y == dst_box.y1` 继续成立（`mech2_decoupled == 0`） |
| 4 | deep-copy | `dict(item)` 是浅拷贝；改写嵌套 commands 前必须 `copy.deepcopy(item)`，绝不污染调用方原计划 |

补充裁决（用户确认）：

- `mech2_shifted_with_commands = 0` **不能**作为验收 —— 正确实现下它仍为 3；
  真正归零的是 `mech2_decoupled`；
- `commands_shifted` 审计字段第一版**不加**（不新增 production schema 字段）；
- 负面控制：**只有 `shift_down + flow commands` 才做 co-shift**；keep /
  preserve / keep_overflow / 无 commands（heading 等）分支零改动。

## 3. 实现（`pdf2zh/v3/render_takeover.py`）

1. 循环开头 `item = dict(item)` → 追加 `item = copy.deepcopy(item)`
   （Invariant 4；仅在该 item 进入非 preserve 分支后才发生，preserve 分支
   在深拷贝之前 continue，行为不变）；
2. `shift_down` 落位处新增 `_shift_payload_commands_y(item, shift)`
   （Invariant 1/2/3）；
3. `_shift_payload_commands_y` 覆盖 `render_payload.commands` 与宿主渲染器
   可能回退的旧字段 `list_items` / `toc_commands` 的 commands，**按 command
   dict 身份（`id`）去重** —— `render_plan_from_model` 里两者共享同一批
   command dict（列表是新 list、dict 是别名），列表身份去重（page_shift/
   packer 的既有写法）在本 plan 形态下会漏判导致双重平移；
4. 只改 `y`，`x / width / font_size / text / overflow` 一律不动；
   平移符号与 `dst_box` 相同（v3 y-up，`+Δ`）。

## 4. 回归语料（`tests/test_v3_render_takeover_fixup.py`）

新增 `TestShiftDownCommandCoShift`（8 项），真实复现形状锚定（p5_1：
src_top=426、cmd_y=426、12.75pt）：

| 测试 | 锁定 |
|---|---|
| `test_shift_down_moves_commands_in_lockstep` | commands y 与 dst_box 同步 `+Δ`（Invariant 2） |
| `test_anchor_relationship_restored_after_shift` | shift 后 `first_cmd_y == dst_box.y1`（Invariant 3，decoupled 判据） |
| `test_command_x_font_and_text_untouched` | x / font / text / overflow 不动 |
| `test_shifted_commands_are_copies_not_aliases` | 输出 commands 与输入计划不共享 dict（Invariant 4） |
| `test_input_plan_not_mutated_including_nested_commands` | 输入计划嵌套 commands 逐字节保持 |
| `test_keep_and_preserve_get_no_command_coshift` | 负面控制：keep/preserve 无 co-shift |
| `test_shift_down_without_commands_leaves_payload_alone` | heading（无 commands）只平移 dst_box |
| `test_keep_overflow_marks_overflowed_without_coshift` | keep_overflow 无平移 |
| `test_real_shape_p5_1_reproduction` | 真实 p5_1 形状：修复前 red（426≠443.85）修复后 green |

红→绿证据：contract 测试先于 patch 落盘，4 项按预期失败
（`cmd_y=426.0 != dst_top 443.85/519.1` —— 即 MECH-2 脱钩本身），patch 后
17/17 通过。

## 5. 验收表（7N-REAL 同书同页复跑）

`doc/7n-real-fix2/`（同一 MP2e、同一 12 页采样、同一 7N-REAL harness）：

| 指标 | FIX-2 前 | FIX-2 后 |
|---|---:|---:|
| `mech2_decoupled` | **3** | **0** ✅ |
| `first_cmd_y == dst_box.y1`（p5_1/p5_3/p5_7） | ❌ | ✅（443.9/393.5/261.1 vs 443.85/393.46/261.12） |
| `mech2_shifted_with_commands` | 3 | 3（预期非零） |
| `mech1_clip_blocks` | 0 | 0（FIX-1 冻结未动） |
| fixup keep / shift_down / preserve | 153/5/33 | 153/5/33（决策面零漂移） |
| 块/字形/翻译计数 | 191/352/156 | 191/352/156 |

绘制坐标同源验证（mono PDF page 0 ≙ v3 page 5，H=665，pymupdf 逐词提取）：
`cover_top = H - dst_y1` 与 `text_anchor = H - cmd_y` 三块全部 `gap=0.00` ——
白底覆盖与文字共享同一几何；修复前 cover 锚在 221.1 而文字停在 239
（脱钩 17.9pt），修复后两者同为 221.15/271.54/403.88。

测试面：`test_v3_render_takeover_fixup` 17 passed；fixup 消费链与几何层
（renderer/adaptive/global-recovery/packer/page-shift/diagnostics/golden/
architecture/toc/dual-forensics）合计 **266 passed, 0 failed**；black/flake8
clean（两处 F401 为 HEAD 既有）。

## 6. 残留与后续

1. **FIX-1 维持冻结**：真实运行 MECH-1（CLIP line collapse）0 复现，
   按 §10.3 不得据 probe 开闸；probe 结论保持 evidence-only。
2. **Legacy 对照组空跑**：`run-legacy.log` 显示 slice-splice 失败后回退
   全文档路径，`output-legacy/` 无产物 —— 属 7N-5 已记录的独立故障
   （`code=2`/temp 竞争族），不在本因果链，待独立工单。
3. **视觉确认**：`doc/7n-real-fix2/output-magicpdf/.../..._mono.pdf` 第 1 页
   （ dedica 页）待人工肉眼复核 p5_1/p5_3/p5_7 不再覆盖错位/文字重叠。
4. **审计字段**：若后续需要「command 是否被 fixup 平移过」的可观测性，
   再按用户裁决加 `commands_shifted` 字段（第一版明确不加）。

## 7. 产物清单

| 产物 | 说明 |
|---|---|
| `pdf2zh/v3/render_takeover.py` | FIX-2 patch（deepcopy + lockstep co-shift） |
| `tests/test_v3_render_takeover_fixup.py` | +8 项 7N-FIX-2 contract 回归（含负面控制） |
| `doc/7n-real-fix2/` | 修复后 7N-REAL 复跑全套（environment/config/log/4 JSON 转储/align） |
| `doc/7n-real-fix2/align-magicpdf.json` | `mech2_decoupled: 0` 验收记录 |
| 本报告 | contract / patch / 验收 / 残留 |
