# Commit 7F — Adaptive Layout / Overflow Recovery（7F-1 ~ 7F-3 策略层）

> 7F 不负责判断"这是什么"，也不改变 semantic geometry。它只回答：

> **译文放不下、且不能破坏原始锚点的时候，怎么恢复版面？**

```text
SemanticNode
    ↓ Translation
Layout Primitive
    ↓ lay_out()
LayoutResult (overflow)
    ↓ classify_reason        ← 7F-1
OverflowReason
    ↓ decide_recovery        ← 7F-3 policy
RecoveryDecision / OverflowDiagnosis
    ↓
Renderer（只执行，draw-only）
```

本 commit 落地 **7F-1（OverflowReason + RecoveryDecision）、7F-2（LayoutBudget）、
7F-3 的决策层（WRAP → SHRINK → CLIP 阶梯 + each-kind 豁免）** —— 只建立
"恢复策略"，**还没有让 renderer 自动缩字号**。这符合 7F 的节奏：
**先诊断，后决策，最后才执行**。

---

## 1. 新增：`pdf2zh/semantic/layout/recovery.py`

### 词汇（7F-1）

- **`OverflowReason`**（为什么溢出）
  - `WIDTH` — 可换行但超出可用宽度；
  - `HEIGHT` — 宽度够，但行数超出可用高度；
  - `UNBREAKABLE_TOKEN` — 单个超长 token，无法换行；
  - `FIXED_COLUMN_COLLISION` — 撞上不可移动列（TOC page_x）；
  - `PRESERVED_REGION` — 保留区（code）本身溢出。
- **`RecoveryDecision`**（做什么）
  - `NO_ACTION` / `WRAP` / `SHRINK` / `CLIP` / `PRESERVE_OVERFLOW`。
- **`LayoutBudget`**（7F-2，按 primitive kind 可调）
  - `max_extra_lines` / `max_height_expansion` / `min_font_size` /
    `max_font_reduction` / `allow_wrap` / `allow_shrink` / `allow_clip`。
  - `budget_for_kind(kind)` 给默认预算：flow/continuation 激进
    （wrap→shrink→clip）、anchor 设 `max_extra_lines=2`、column/preserved
    全禁（永不移动 / 永不缩）。
- **`OverflowDiagnosis`**（JSON-safe 记录）
  - `reason` / `decision` / `primitive_kind` / `measure_ratio` /
    `effective_font_size` / `extra_lines` / `message`。
  - message 形如 `width overflow -> shrink 10.5->9.7`，而不是只有
    `overflow=True` —— 日志可追踪每一档决策。

### 三个入口

- `classify_reason(result, avail_width, avail_height)` — 由已定版
  LayoutResult 推断为什么溢出（含 CJK 感知的 UNBREAKABLE 判定）。
- `decide_recovery(kind, reason, budget, target)` — 选出恢复动作。`target`
  ≈ `"marker"` 时该 anchor 永不 wrap/shrink。
- `diagnose_overflow(...)` — 单入口：`result → (reason, decision)`。

---

## 2. 决策阶梯（7F-3 policy）

```text
WRAP  →  SHRINK  →  CLIP / PRESERVE_OVERFLOW
```

按 primitive kind 的豁免表（与方案一致）：

| Primitive | 阶梯 | 约束 |
|---|---|---|
| FlowText | WRAP → SHRINK → CLIP | 可积极恢复 |
| List content | WRAP → SHRINK → CLIP | marker（anchor+target=marker）→ PRESERVE_OVERFLOW |
| List marker | PRESERVE_OVERFLOW | 永不 wrap / shrink |
| TOC title | WRAP → SHRINK → PRESERVE_OVERFLOW | 从不 CLIP（不截断） |
| TOC page column | PRESERVE_OVERFLOW | `page_x` 永不移动 |
| Code (PreservedRegion) | PRESERVE | 永不 wrap / shrink / clip |

关键保证：
- `UNBREAKABLE_TOKEN` **跳过 WRAP**，直接 SHRINK/CLIP（避免无限循环）。
- SHRINK 复用 7C 既有 `shrink_to_fit()`（7F 不重新发明缩字机制）；本层只做
  **决策**，真正的执行仍由 layout 侧信道经 `lay_out` 完成（7F-3 之后接入）。
- 有 `min_font_size` / `allow_shrink=False` / `allow_clip=False` 时绝不无限
  缩下去，宁可显式 `overflow`（7F-5 的可接受阈值落地点）。

---

## 3. 架构保证（`tests/test_layout_recovery_architecture.py`）

recovery.py 是**纯策略层**，与 7E-Audit 同标准：

```
recovery.py
    ❌ detector / parser            （looks_like 无、detect_* 无、parse_* 无）
    ❌ renderer 耦合                （renderer / magicpdf / insert_text / draw 无）
    ❌ translator                   （translate / translator 无）
    ❌ level * const / index *      （level 与 index 名都不出现）
    ❌ 直接执行 fit                 （不调 wrap_lines / shrink_to_fit / clip_text，
                                    只返回 RecoveryDecision）
imports 只来自 layout 栈（primitives / overflow / wrap / tokenize）
```

---

## 4. 测试新增

| 文件 | 说明 |
|---|---|
| `tests/test_layout_recovery.py`（**18**） | classify 五类 reason、WRAP→SHRINK→CLIP 阶梯、budget 门控、code/column/marker 豁免、TOC title 不 CLIP、diagnose 的 JSON 记录、与真实 `lay_out` 引擎集成 |
| `tests/test_layout_recovery_architecture.py`（**11**） | 纯策略层架构锁（无检测 / 无渲染 / 无翻译 / 无 level 几何 / 纯决策） |

`diagnose_overflow` 集成验证示例：
- code 太宽 → `PreservedRegion` → `PRESERVE` → `PRESERVE_OVERFLOW`；
- flow 长句 → `FlowText` → wrap，`extra_lines>0`；
- TOC title 超宽 → WRAP（可换行优先）→ 关闭 wrap 才 SHRINK，绝不 CLIP；
- page_x 列 → `FixedColumn` → 永远 `PRESERVE_OVERFLOW`，与 title 通道独立。

配合 7E-Audit，恶意翻译场景（`""` / `"TRANSLATED"` / `text*100`/
`"1. 2. 3. "+text`）在 recovery 决策层同样保证：
Semantic structure / Geometry anchors 不变，只允许 line_count / font_size /
overflow 变化。

---

## 5. 验收

- [x] `tests/test_layout_recovery.py`（**18**）+ `test_layout_recovery_architecture.py`
      （**11**）通过 —— 策略层全部绿。
- [x] 7E-Audit（`test_architecture_7e.py` + `test_translation_boundaries.py`）未受影响，
      全绿。
- [x] layout 相关回归 `-k "layout or recovery or overflow or architecture_7e or
      translation_boundaries"` **450 passed**。
- [x] 新文件 ruff 零告警；`recovery.py` 用 `X | None` 风格、`__all__` 排序规范。
- [x] **没有调用 renderer / 没有自动缩字号** —— 7F 仍处于「先建立策略」阶段。

## 6. 下一步（7F-4 ~ 7F-7，每个 commit 独立可回滚）

- **7F-4** Flow / List 集成：`render_flow`/`render_item` 先 `diagnose_overflow`，
  再决定 wrap/shrink/overflow，把决策落到 LayoutResult 上；
- **7F-5** TOC adaptive：title 长 → leader 缩 → title wrap/shrink → 显式
  overflow，`page_x` 保持；
- **7F-6** 邻居 / 高度保护：只消费上游
  `previous_block_bbox / next_block_bbox / page_bbox`，计算 `available_height`
  （7F 不重新做 semantic detection；第一版不做跨页重排）；
- **7F-7** 7D evaluator + golden corpus adaptive 指标
  （`overflow_recovery_rate / shrink_count / clip_count / mean/max_font_reduction /
  extra_line_count`）。

在那之后才是 7G Benchmark Harness（Model A/B → pdf2zh → 7D Eval →
Objective Report）——届时能在「OCR / semantic / translation / layout / renderer」
五个维度上做真正的模型竞争，而不是凭观感选 ONNX。
```