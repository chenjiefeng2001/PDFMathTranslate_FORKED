# 7N-9 — FIX-3 渲染锚 + 白矩形几何 + shift 方向修正（全书几何-only 复跑资格报告）

> **日期**：2026-09-02 · 基线：7N-8（`doc/7n8-mp2e/`，FIX-2 后全绿）
> **性质**：Stage-4 renderer 修复 + plan 层 shift 方向/幅度修正；翻译文本不变
> （FIX-3 是纯几何修复），因此用 7N-8 的 dump 反推 pre-fixup 计划 → 重跑
> FIXED fixup → 全书重渲染 mono → 全书重审计，无需翻译 API。
> **产物**：`doc/7n9-mp2e-fix3/`（plan + mono + `7n-postfix-audit.json/md`）、
> `doc/7n9_fix3_reread_verify.py`（可复现脚本）。

---

## 0. 结论先行

| 门 | 7N-8 基线 | 7N-9（FIX-3 后） |
|---|---|---|
| 页面分级 | 456 A / 105 B / 0 C / 1 D | **506 A / 55 B / 0 C / 1 D** |
| flow 基线 = box 顶沿 | 2507 / 2507（渲染错） | **0**（墨水实测落在盒内） |
| 译文上浮 ≈ font_size | p442_2 7.6 / p442_4 5.0 / p442_7 7.7 | **0**（全部归零） |
| shifted 白矩形误抹相邻行 | 28/30（MECH-4） | **0**（erase 只盖 src_box） |
| 8B 视觉 missing / overlap | 0 / 1（p442_4 43.6%） | **0 / 0** |
| FIX-2 decoupled / double-shift / alias | 0 / 0 / 0 | **0 / 0 / 0**（不变量保持） |
| MECH-3 真实碰撞 | 0*（方向错误的伪 0） | 1（p233_4，见 §5） |
| 残余 defect | p442_4 三层（上浮+误抹+CLIP） | p442_4 仅剩 CLIP 截断（FIX-1） |

\* 7N-8 的 MECH-3=0 是 shift 方向错误（+Δy 把盒子移向页顶、落在非 preserve
文本行上）造成的伪 0 —— 实际存在 28/30 例误抹无辜英文行（MECH-4 附录 A）。

---

## 1. 修复内容（FIX-3A / FIX-3B / 方向 / 幅度）

### FIX-3A — renderer 基线锚（`magicpdf_renderer.py`）

flow 命令的 `y` 是 v3 y-up 的 **box 顶沿锚**（`first_cmd_y == dst_box.y1`，
全书 2507/2507），renderer 原先只做 `page_h − y` 翻转就当作基线绘制 →
墨水整体上浮 ≈1em。修复：基线 = `(page_h − y) + draw_fs × 0.85`
（与 `_insert_text_wrapped` 的锚定一致），多行命令相对步进不变。

### FIX-3B — 白矩形几何解耦（`magicpdf_renderer.py`）

白矩形只覆盖**源文本几何（src_box）**，与译文落点（dst_box / commands）
解耦。shift 块不再用位移后的 dst_box 擦除 → 不再误抹相邻行
（p442_4 的 "such" 恢复可见）。

### 方向修正 — shift_down 从 +Δy 改为 −Δy（`render_takeover.py`）

v3 是 y-up，页面下方是 **−Δy**。原实现 `+shift` 把盒子移向页顶，
全书 153/153 shift 块 dst 都落在源盒**上方**（p442_4 dst 直接压住 "such" 行、
p3_4 压住 "Maurice Herlihy" 行）。修复后盒子真正下移，`decoupled=0`
不变量保持（commands 与 dst_box 同步 −Δy）。边界守卫改为盒底不越
页面下边缘（v3 y=0）。

### 幅度修正 — 尊重已定版布局（`render_takeover.py`）

两处让 fixup 的溢出估计与 adaptive_layout 的**已定版裁决**对齐：

1. **layout_ok=True 且无 overflow** 的块直接 keep（不再用名义字号重估）——
   修掉 SHRINK 已解决的误移（p3_4 尼尔·沙维特 13.01pt 单行、p263_1 章号
   57.8pt → 此前被误移 22.8 / 95.2pt，产生 6 个 large_shift 页）。
2. **行高模型**：只有换行行消耗完整 line_h，最后一行只需 1em
   （`(est_lines−1)×line_h + font_size`）——修掉 box_h < 1.4×fs 的
   heading/单行盒永远"溢出"（p26_6 11pt 盒等 47 例压住下一行）。

---

## 2. 全书复跑（几何-only，562 页）

`doc/7n9_fix3_reread_verify.py`：undo 旧 +Δy shift（153 块）→ 重跑 FIXED
fixup → 重渲染 mono → 重审计。

- fixup 决策：preserve 2826 / **shift 59** / overflow 0（7N-8：shift 153 /
  overflow 22 —— 方向与幅度修正后 94 个误移块归位，其中 22 个旧
  keep_overflow 中部分可下移、153 个旧误移中 59 个真实溢出保留）
- shift 方向：**0 个非负 Δy**；decoupled：**0**
- mono 渲染：562 页 / 4515 块 / 138690 字形

## 3. 关键页面核验（span + 像素双证据）

### p442_4（三层缺陷全部解决，仅剩 CLIP）

| 检查 | 7N-8 | 7N-9 |
|---|---|---|
| 译文 ink band | 361.6–367.6（悬在白矩形外，顶进 formula 行，overlap 43.6%） | **390.7–396.7**（下方空白区，无任何交叠） |
| "such" 行暗像素（144dpi） | 121（被白矩形抹掉 ~45%） | **222**（完整保留） |
| "tions." 行暗像素 | 159（原文未擦除，原文译文共存） | **0**（src 白矩形正确覆盖） |
| 译文 band 暗像素 | 0 | **159**（译文正常着墨） |

### p3_4（方向修正）

"尼尔·沙维特" 从 [252.2, 267.8]（浮在 "Maurice Herlihy" 上）→
**[286.0, 301.7]**（正好替换 "Nir Shavit" [291.0, 306.9]），版权页 4 位作者
译文全部原位替换，无交叠。

### p442_2 / p442_7（基线修正）

p442_2 译文从 [335.4, 344.5]（夹在两行英文之间）→ **[341.9, 351.0]**
（落在自身盒 [343, 352] 内，上浮 7.6pt 消失）；p442_7 首行同样归位。

## 4. 全书机器审计（8A/8B/8D/8E 重跑）

- 页面分级：**506 A / 55 B / 0 C / 1 D**（C 页 6 → 0，large_shift 全部消除；
  D 仅 p442）
- flags：`{clip: 1, residual_overflow: 1}`（仅 p442_4 的 CLIP 截断 —— FIX-1）
- 8B 视觉：checked=1（p442_4），**missing=0，overlap=0**
- 8D FIX-2：decoupled=0 / double_shift=0 / x_changed=0 / font_changed=0 /
  alias_mismatch=0
- 8E MECH-3：plan_hits 30，ink_verified 6，**real_collisions 1**（见 §5）

> 注：`doc/7n8_mp2e_audit.py` 的 8B/8E 期望落点公式已同步 FIX-3 语义
> （`expect_y = h − cmd_y + 0.85×fs`），否则视觉检查会按旧基线语义误报 missing。

## 5. 残余项（冻结 / 移交给 FIX-1）

1. **p442_4 CLIP**（D 页唯一 flag）：`系统蒸发散。` 被 CLIP 为 `系统蒸发…`
   5pt 单行，`散。` 不可读。几何已干净（见 §3），截断属 Stage-3 layout
   loss → **FIX-1**（per-line clip / 高度协商 / 空间仲裁）。
2. **MECH-3 真实碰撞 1 例：p233_4**（running header "PRAGMA 9.8.1" →
   "编译指示 9.8.1" 落在下方 formula 盒，ink 重叠 65%）：heading 无 settled
   payload，走文本估算，窄盒过估 → shift 落点与 preserve 块交叠。属
   MECH-3 落点仲裁范畴（7N-8 已冻结为观测项），随 FIX-1 排版迭代处理。
3. 另有 ~20 例窄盒索引条目/header（p549_2 "A"→"一个" 等）译文换行压线：
   索引列宽按拉丁字符设计，CJK 译文天然放不下 —— 翻译质量 + 布局协商，
   FIX-1。

## 6. 产物与复现

| 产物 | 说明 |
|---|---|
| `doc/7n9-mp2e-fix3/` | 复跑全套（plan / mono / audit json+md / ledger） |
| `doc/7n9_fix3_reread_verify.py` | undo→refixup→render 复现脚本 |
| `tests/test_magicpdf_render_fix3.py` | FIX-3 回归（基线落点 + erase 几何，像素断言） |
| `tests/test_v3_render_takeover_fixup.py` | FIX-2 契约测试更新（−Δy 方向） |
| 单元测试 | **1758 passed**（v3 全套 + 相关回归） |