# 7I-5A — Clip Causality Forensics（71 个 F8/clip residual 全量取证）

状态：**COMPLETE（7I-5 阶段 A —— 先取证，不修）**

数据与脚本：`doc/7i5-causality/{summary.json, report.md}`（收集器
`doc/7i5/clip_causality.py`）。纯取证，**未改任何生产代码**（只读
`render_plan_from_model` 产出的 `render_payload`；F8 仍按 7I-4-4 baseline 保留）。

---

## 1. CLIP 的真正决策点（代码定位）

CLIP 在 `pdf2zh/semantic/layout/adaptive.py` 的 `adaptive_layout` **Stage 3** 触发：

```text
Stage 0  lay_out(WRAP)      → width/height 超 → overflow
Stage 1  WRAP               → 重新换行
Stage 2  SHRINK             → shrink_to_fit(budget + min_font(默认 5.0))
Stage 3  CLIP  ← 若仍 overflow     ← 在这里出现 F8
```

**关键实现细节（7I-5A 找到的决策点）**：`adaptive_layout` Stage 2 的 SHRINK 调用
`lay_out(policy=OverflowPolicy.SHRINK)`，而后者内部只调一次
`shrink_to_fit(text, ..., width, fs, min_font_size)`——**把整段 text 当【单行】来
缩字**（见 `overflow.py` SHRINK 分支 → `_finish([text], [w], ...)`），
**完全没有保留/write-back WRAP 阶段已经排好的多行结果**。

---

## 2. 71 个 case 的因果聚类

```
总 CLIP          = 71         （全 paragraph）
by steps         = WRAP->SHRINK->CLIP: 43 | SHRINK->CLIP: 28
by reason        = width: 68 | height: 3

WRAP 产生 >1 行的块          = 43 / 71
其中 SHRINK 又折叠回 1 行     = 43 / 43        ⬅ 100%
其中 SHRINK 触底(≤5pt)     = 31 / 43
未经历 WRAP（直接 SHRINK->CLIP）= 28 / 71
源块高 < 20pt（极小源框）      = 47 / 71
CLIP 时 width-ratio ≤ 1.0    = 70 / 71        ⬅ 文本本已适配宽度
```

> 注意：`width_ratio` 是 CLIP **之后**的 `line_widths`（已截断），所以接近 1.0
> 属自然现象；真正证据在 **per-stage trace**。

### 2.1 代表 case（trace 最直接）

**C book p62_9**（width，font 10→5.0，len 653，src_h 94）：
```text
WRAP   : line_count=14, font=10.0, overflow=True   ← 已换成 14 行，fit width
SHRINK : line_count=1 , font=5.0 , overflow=True   ← 把 14 行重新当【1 行】缩到 5pt，仍超宽
CLIP   : line_count=1 , font=5.0 , overflow=True   ← 截断成 1 行
```

**C book p69_2**（width，font 8→5.0，len 38，src_h=18，avail_w=66.1）：同样
WRAP 后 SHRINK collapse 成 1 行 → 5pt → CLIP。

---

## 3. 根因判定（7I-5A 结论）

> **CLIP 不是「文字量远超盒子可容纳」的必然结果；是 recovery 的 SHRINK 阶段
> 不当执行的结果。**

实证链：

1. WRAP 已把多行文本排好（43/71 明确 WRAP 成 >1 行，且 70/71 width_ratio ≤ 1.0）；
2. 但 Stage 2 的 `shrink_to_fit` 用**未 wrap 的整段文本**做单行缩字 —— 它
   **丢弃了 WRAP 的行结构**，把多行塌缩成 1 行；
3. 整段 653 字单行即使缩到 5pt floor 也远超 box width → `overflow` 恒 True；
4. 于是进入 Stage 3 CLIP，把「本可以多行放下」的文本截断成 1 行。

**这不是 renderer/emitter 的错，也不是「内容真的放不下」**。这是
`lay_out` 的 SHRINK 策略（单行 `shrink_to_fit`）与 `adaptive_layout` 的 WRAP 阶段
（多行 wrap）**之间的一致性问题**：SHRINK 不消费 WRAP 的结果、重新对整段做
单行布局。

### ② 是否存在可行的非-CLIP 方案？（对照你问的 ②）

**有**——SHRINK 应当在 **已 WRAP 的多行基础上**缩字 / 允许更多行 / 或缩字后
**重跑 wrap**，而不是把整段当单行。对 43/71（本可换行的段落）这是显然可行的
离开 CLIP 的路径；对 28/71（未 wrap 直接 SHRINK→CLIP，多为 `_is_unbreakable`
超宽 token 或单行超宽）才是「缩到 floor 仍放不下」的真不可避免场景。

> 因此：**7I-5 不应写成「禁止 CLIP」**（那会掩盖真的放不下的 28 例）。而应定义为
> **修复 SHRINK 与 WRAP 的一致性 + 给不可避免 overflow 显式 terminal policy**。

---

## 4. 已达标项（7I-5A 契约）

- [x] 71 个 F8 全量因果记录（node_id/kind/src+dst bbox/avail w+h/font 轨迹/
      steps/reason/decision/overflow/layout_ok/trace）→ `summary.json`；
- [x] 定位 CLIP 真正决策点（`adaptive_layout` Stage3，因 SHRINK 单行缩字丢弃
      WRAP 结果）；
- [x] 聚类（WRAP→SHRINK→CLIP 43 / SHRINK→CLIP 28；height 3；tiny box 47）；
- [x] 回答①：CLIP 是「recovery 穷尽后 fallback」的**当前实现**，但并非
      **admissible 唯一选择**——对 43/71 有可行替代（保留/重 wrap）；
- [x] 回答②：存在非-CLIP 方案（SHRINK 尊重 WRAP），有依据改 recovery policy。

---

## 5. 下一阶段

```text
7I-5A  Clip Causality          ✅ COMPLETE（决策点、根因、可行替代都已定位）
7I-5B  Policy Contract         ▶ 定义：
                              ├─ CLIP 何时允许（仅当 WRAP+SHRINK 都穷尽且不可缩）
                              ├─ SHRINK 必须在已 WRAP 多行上执行（或缩后重 wrap）
                              ├─ min_font / admissible layout 边界
                              └─ 不可避免 overflow 的 terminal policy（明确，非静默）
7I-5C  Minimal Fix + corpus rerun（F8↓，F4 不变，F10/F6/F1/F2/F3 不 cascade）
```

7I-5C 的验收不变：**F4 保持 1@p300@parser，F8→F10 不得发生 defect migration**。