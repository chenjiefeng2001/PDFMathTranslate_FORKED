# Commit 7E-Audit — 全链路架构审计

> **目标**：把整条链路锁成四个单向阶段，并证明它们真的贯穿执行链：

```text
PDF → Extraction → Semantic Detection/Parsing → Semantic Model
   → TranslationUnit → Translation → RenderPayload → Layout Primitive
   → LayoutResult → Renderer → PDF
```

每层只做自己的事情：

| 层 | 只回答 | 不做 |
|---|---|---|
| Semantic | "这是什么？" | 不接触翻译 / 布局决策 / 绘制 |
| Translation | "译文应该是什么？" | 不改几何、不判 fit |
| Layout | "放哪里 / 怎么 fit？" | 不做语义检测、不重推几何 |
| Renderer | "怎么画？" | 不做检测、不重排、不改语义几何 |

7E-Audit **不改任何探测器 / 解析器 / semantic 模型 / ONNX / 翻译算法 /
renderer 实现 / Layout 原语 / overflow 策略** —— 只新增架构测试锁定既有
边界，并生成此报告。

---

## 1. 新增审计测试

| 文件 | 覆盖 |
|---|---|
| `tests/test_architecture_7e.py` | 依赖方向 / Geometry Ownership / Legacy 回退 / 单向性 / Translation→Layout 隔离 / Overflow 归属 / Golden corpus |
| `tests/test_translation_boundaries.py` | Code / List / TOC / Style 的 translator 输入边界 + 恶意 translator |

合计 **28 个审计用例**，与既有 7A/7B/7C/7E-1/2/3 架构测试一起构成完整
的架构契约。

---

## 2. 依赖方向检查（Architecture / Dependency Direction）

**锁定内容：** `looks_like_*` / `detect_code` / `detect_list` /
`detect_toc` / `parse_list` / `parse_toc` 不得出现在：

- `pdf2zh/semantic/layout/`（纯几何载体，无任何语义检测）；
- `pdf2zh/semantic/renderer/` 的 **draw-only 类**（`ListRenderer` /
  `TocRenderer`）；
- `pdf2zh/v3/magicpdf_renderer.py`（绘制端）。

整个 renderer 模块连 `looks_like` 都不出现。`build_page_*_plan` 组合链
（detect → parse → render）是**编排职责**，检测/解析合法地位于其中（与
7A / 7E-3 约定一致）。

```
0 forbidden dependency  ✅
```

---

## 3. Geometry Ownership（AST 精确扫描）

| 几何 | Owner |
|---|---|
| `marker_x` / `content_x` / `continuation_x` / `title_x` / `page_x` / `bbox` / `level` | **Semantic**（原始几何） |
| available / wrapped width、resulting bbox | **Layout** |
| PDF coordinate transform（y-flip） | **Renderer** |

**AST 精确到算术项**：`_ast_binops` 只把 BinOp 操作数命中 `level` /
`index` 的表达式判为违规，**放行** `font_size * 1.4`、`index * line_step`、
`text * 0.6` 等合法标量运算 —— 不误杀正常代码。

扫描范围：`layout/*.py`、`renderer/*.py` 的 draw-only 类、
`magicpdf_renderer.py`。全部 **0 命中**：

```
0 geometry reconstruction from level/index  ✅
```

---

## 4. Translation Boundary（translator 输入边界）

| 内容 | 输入 | translator_calls | 违反即失败 |
|---|---|---|---|
| Code | `def hello(): print(...)` | `[]` | code 块任何进 translator |
| List | `1. Introduction` / `2. Background` | `["Introduction","Background"]` | `"1. Introduction"` 进 translator |
| TOC | `Introduction ........ 42` | `["Introduction"]` | `"Introduction ........ 42"` 进 translator |
| Style | `This is very important.`（bold） | 收到**保护结构**的文本 `<b0>very important</b0>` | bold/italic 边界破坏 |

```
Code      0 次调用        ✅
List      marker 0 次调用  ✅
TOC       page 0 次调用    ✅
Style     bold/italic 边界保留 ✅
```

---

## 5. Legacy Path Audit（render_payload.kind 优先）

`render_plan_from_model → render_payload → magicpdf_renderer`：

- `render_payload.kind`（list / toc / flow）**始终优先**分派；
- legacy 字段（`list_items` / `toc_commands`）只在 **payload.commands 为空**
  时才显式回退 —— 紧跟在 kind 分支内，且 `if not list_cmds:` /
  `if not toc_cmds:` 是**显式**回退守卫，绝不抢占新 payload 主路径；
- flow 侧信道失败（`layout_ok=False`）→ 可观测 legacy 回退
  （`stats["flow_legacy_fallback"]`），永不静默。

```
Legacy fallback isolation  ✅
```

---

## 6. 单向性 / 隔离

- **`test_renderer_cannot_change_semantic_geometry`**：Semantic 节点
  （`marker_x=40/content_x=60/content_width=300/y=700/level=0`）经
  Renderer 完成后**一字未变** —— Renderer 只消费几何，不 mutate。
- **`test_translation_does_not_mutate_geometry_anchors`**：恶意 translator
  返回 `"TRANSLATED-内容-"+s*100`，源锚点 `marker_x/content_x/content_width/y`
  全部不变；只允许 text width / line count / overflow 变化 —— 这验证
  **translation changes content, NOT geometry**。

```
Renderer draw-only / no mutation  ✅
Translation does not mutate anchors  ✅
All fit decisions happen before renderer  ✅
```

---

## 7. Overflow Contract Ownership

| 内容 | 原语 | 策略 | 锁定 |
|---|---|---|---|
| Code | `PreservedRegion` | `PRESERVE` | 绝无 WRAP/SHRINK/CLIP，单行原样 |
| List content | `FlowText` | `WRAP` | 可换行 |
| List marker / TOC title | `FixedAnchor` | `SHRINK`（机制就绪，不自动应用） | 不静默收缩 |
| TOC page_x | `FixedColumn` | `PRESERVE` | 绝不被 title 变宽拉动 |
| Flow | `FlowText` | `WRAP` | 正文重排 |

`PRESERVE / WRAP / SHRINK / CLIP` 归属表由
`policy_for()` 与 `lay_out()` 单一引擎锁定。

```
Overflow policy ownership  ✅
```

---

## 8. 恶意输入测试（malicious translator）

| 场景 | 结果 |
|---|---|
| translator 返回 `"1. "+s*100`（吞 marker / 插假编号）| List structure 不破，marker 原样 `1.`/`2.` |
| translator 返回 `""` | List 不崩，marker 仍在 |
| translator 把 TOC title 拉长 100 倍 | page_x 页码列不动（FixedColumn PRESERVE），`page_number` 不被改写 |
| translator 吞掉全部 style markers | 优雅回退到纯文本，译文/原文不丢、绝不让整段失败 |
| 垃圾翻译 → 真实 PDF | marker 与译文字节级解耦（7E-2 铁律复验） |

这证明「marker 与 translation channel 解耦」是**架构级**的，不是测试巧合。

```
Code / List / TOC / Style 恶意输入结构保持  ✅
```

---

## 9. Golden Corpus（7D 全链路回归）

对 corpus `code / list / nested list / toc / toc_multiline / toc_no_leader /
style / cjk` 分别跑 `source → evaluate → compare baseline`，identity 副本
必须 zero regression，且关键指标全 1.0：

```text
code_preserved_bbox = 1.0
list_content_x_accuracy / list_continuation_x_accuracy = 1.0
list_nested_geometry_accuracy = 1.0
toc_title_x_accuracy / toc_page_x_accuracy = 1.0
toc_leader_integrity = 1.0
toc_continuation_x_accuracy = 1.0
outline_destination_accuracy = 1.0
bold_accuracy / italic_accuracy = 1.0
overflow_count = 0
text_exactness = 1.0
```

任一指标下降 → 测试失败。**0 regression**。

```
Golden corpus : PASS  ✅
```

---

## 10. 验收结论

```text
Architecture
  0 forbidden dependency               ✅
  0 semantic inference in renderer     ✅
  0 geometry reconstruction from level/index  ✅

Translation
  code  = 0 calls   ✅
  list marker = 0   ✅
  toc page = 0      ✅

Geometry
  source anchors preserved    ✅
  translation does not mutate anchors  ✅

Layout
  all fit decisions happen before renderer  ✅

Renderer
  draw-only / no mutation     ✅

Regression
  7D golden corpus = no regression  ✅

Tests
  new audit suite = 28 passed  ✅
  full suite      = green（2537 passed, 1 skipped；无侵入）
  converter.py    = 1094（未增）  ✅
```

**7E-Audit 通过。**

---

## 11. 本 Commit 未修改

```text
❌ 不改 detector / parser / semantic model / ONNX
❌ 不改 translation algorithm
❌ 不改 TOC / List / Code renderer 实现
❌ 不改 Outline
❌ 不加新 Layout primitive / overflow strategy
```

仅新增两个审计测试文件 + 本报告。若未来 Audit 发现 bug，只修违反架构
边界的部分，不趁机重构。

---

## 12. 下一步（建议）

7E-Audit 通过后，推荐进入：

# **7F Adaptive Layout / Overflow Recovery**（按优先级）：

```text
WRAP → GEOMETRY PRESERVE → SHRINK → CLIP
```

对「原文短、翻译长」场景，不做 `font_size *= 0.7`，而是综合：
available width / available height / original font size / minimum readable
size / line count / neighbor geometry 做恢复。

7F 之后才是最初关心的 ONNX 模型替换与基准选择（7G Golden Corpus +
7D Eval Model A/B → Objective Score → 最终选型）。
```