# 7N-TRACE — Flight Recorder / Trace 体系构建报告

> MECH-4 的教训：`y=366.58` 这种裸数字，谁都说不清它是 box top、baseline
> 还是 bottom。本构建把「真实翻译 → plan → fixup → renderer → PDF」全链路
> 变成结构化、可关联、可重放的 trace（JSONL 流式），每个坐标都带语义声明，
> 规则引擎直接消费 trace 判定 —— **真实翻译本身就是测试数据源**，
> 不再需要「拿到一本问题书 → 猜问题 → 手写测试」。

## 1. 新增模块

| 文件 | 职责 |
|---|---|
| `pdf2zh/v3/flight_recorder.py` | `FlightRecorder`（JSONL 流式 sink，崩溃不毁 trace）、`TraceContext`（`trace_id = "<page>/<block_id>"` 贯穿全链）、`Coord`（value+space+origin+meaning 语义坐标）、`read_events` / `build_trace_index` |
| `pdf2zh/v3/trace_rules.py` | 不变量规则引擎：`Rule → Trace fields → predicate → severity → action`，规则即 FAIL 才输出 |
| `pdf2zh/v3/trace_audit.py` | `python -m pdf2zh.v3.trace_audit trace <events.jsonl> [--pdf] [--out]` → summary / pages / trace-index / defect-ledger.csv / qualification.md / crops/（Level-2 光栅证据只对 FAIL 块生成） |

## 2. 插点（生产运行时默认开启，`trace=None` 时全部零开销）

```
render_plan_from_model  → plan.flow / plan.block        （y_meaning=box_top）
fixup_render_plan       → plan.shift_down / keep / preserve / overflowed
                          （src/dst + Δy + first_cmd_y：平移后的命令锚）
render_plan_to_pdf      → render.flow / render.wrapped / render.block / render.erase
                          （v3 box_top → fitz baseline 因果链：actual/expected/delta）
```

- `plan.shift_down` 携带 **post-fixup** `first_cmd_y`（co-shift 后的命令 y），
  DECOUPLED 规则不再拿平移前的 plan.flow y 比对 dst_box.y1。
- `render.erase` 的 `erase_rect` 翻回 **v3 空间** 与 src/dst 同帧比较
  （初版把 fitz 矩形与 v3 盒比较 → 永远 `"other"`、ERASE_GEOMETRY 静默 no-op
  —— smoke 运行现场发现并修复；附带 `erase_rect_fitz` 保留渲染侧原始值）。

## 3. 运行时不变量（MECH-4 / FIX-2 / FIX-3 的机器版）

| 规则 | 语义 | 严重度 | 动作 |
|---|---|---|---|
| `FLOW_BASELINE_SEMANTICS` | render 命令 y 必须声明 `box_top`（回归成 baseline 立即 FAIL） | HIGH | FIX-3 |
| `FLOW_BASELINE_MISMATCH` | `actual_baseline == (page_h − y) + fs×0.85`（去掉 0.85 偏移立刻暴露） | HIGH | FIX-3 |
| `ERASE_GEOMETRY` | shift 块白矩形必须覆盖 src_box，不得覆盖 dst_box | HIGH | FIX-3 |
| `SHIFT_DIRECTION` | v3 y-up：shift_down 必须 −Δy（+Δy 把盒子移向页顶） | HIGH | FIX-3 |
| `LARGE_SHIFT` | \|Δy\| > 60pt | MEDIUM | investigate |
| `DECOUPLED` | shift 后 `first_cmd_y == dst_box.y1`（0.5pt 容差） | HIGH | FIX-2 |
| `CLIP_READABILITY` | recovery.decision == clip | MEDIUM | FIX-1 |
| `EMPTY_TRANSLATION` / `TOKEN_LEAK` / `ONE_LINE_COLLAPSE` / `RESIDUAL_OVERFLOW` / `BBOX_ANOMALY` | 翻译质量 / 布局观测 | 按级 | — |

## 4. CLI 用法

```bash
# 真实翻译（magicpdf_cli run 已内置 recorder）
python -m pdf2zh.magicpdf run book.pdf            # 产出 output/trace/*_events.jsonl
                                                   # 并在结束时自动跑 trace-audit

# 离线审计任意 trace（jq/grep/Python streaming 均可读 JSONL）
python -m pdf2zh.v3.trace_audit trace trace/events.jsonl \
    --pdf output.pdf --source book.pdf --out audit/

# 单块一键诊断：根因阶段 → 模块 → 不变量 → 修复 → 下游症状 → evidence
python -m pdf2zh.v3.trace_audit explain trace/events.jsonl 442/p442_4 [--pdf mono.pdf]

# 输出
# audit/summary.json  audit/pages.json  audit/trace-index.json
# audit/defect-ledger.csv  audit/qualification.md  audit/crops/
```

## 5. 562 页全书 smoke（`doc/7n9_trace_smoke.py`，纯几何复跑 + recorder）

```
[TRACE] pages: 562  plan entries: 6690
[TRACE] events: 11467  blocks: 4515
[TRACE] rule FAILs: none
[trace-audit] qualification=PASS  pages=562  rule_fails=0
```

- 事件直方图：`plan.keep 3805 / plan.preserve 2826 / plan.shift_down 59 /
  render.flow 2393 / render.wrapped 1067 / render.erase 1067 / render.block 248`。
- p442_4 完整因果链（trace 一键可答「为什么」）：

```
plan.shift_down  src=[118,277,142,286] dst=[118,266.29,142,275.29] Δy=−10.71
                 first_cmd_y=275.29 == dst.y1（DECOUPLED 成立）
render.flow      y=275.29 meaning=box_top  actual_baseline=393.96 == expected
                 erase_rect(v3)=[118,277,142,286] == src_box（FIX-3B 成立）
```

- 全书 1067 个 erase 事件语义全部 `src_box`；对「dst 擦除」的合成事件，
  `ERASE_GEOMETRY` 正确给出 `HIGH / FIX-3` —— 规则在真实数据上可命中。

## 6. first_divergence（根因定位）

每个 FAIL 块在审计时标注根因：pipeline 顺序（plan → fixup → layout →
render → erase → raster）中最靠前的 FAIL 阶段是 **first divergence**，
其后阶段的 FAIL 是同一根因的 **downstream symptom**（连锁症状，不重复计数）。

- `summary.json`：每条规则结果携带 `first_divergence` / `downstream`；
  新增 `first_divergence_by_stage`（按根因阶段的块数直方图）、
  `first_divergence_blocks`、`downstream_symptoms`。
- `defect-ledger.csv`：新增 `first_divergence` / `downstream` 列。
- `qualification.md`：每个 FAIL 块输出 pipeline 阶段树 —— 拿到一本陌生书
  的任意 `trace_id`，无需预先知道 bug，直接回答「问题第一次出现在哪一层」：

```
442/p442_4
 ├─ plan    PASS
 ├─ fixup   PASS
 ├─ layout  -
 ├─ render  FAIL  ← first divergence (FLOW_BASELINE_SEMANTICS)
 ├─ erase   -
 └─ raster  FAIL  ← downstream symptom (INK_OVERLAP)
```

标注必须在**完整结果集合并去重之后**统一完成（`trace_audit.write_outputs`）：
Level-2 光栅事实（`raster.ink`）第二批复跑规则，若在第一次 `run_rules`
内标注，`INK_OVERLAP` 会被误标成自己的根因。

## 7. 测试

- `tests/test_flight_recorder.py`（16 例）：recorder JSONL 往返 / 语义坐标 /
  index / 每一条 FIX-3+FIX-2 规则对「旧 bug 事件形状」必 FAIL、对「新形状」静默 /
  first_divergence 标注（render 根因 + raster 下游症状、plan 优先于 raster）/ trace-audit
  产物完整性（含阶段树）。
- `tests/test_mixed_pdf_golden_7f6d.py`：fixture 的 flow 盒与 list 盒原本重叠
  （fitz 102–152 ∩ 92–172），旧 renderer 的「上浮 1em」恰好躲开 marker 行；
  FIX-3A 正确基线暴露后按真实布局语义移出重叠区（布局层绝不产生重叠 dst_box）。
- 全套件 **4022 passed**。

## 8. 使用体验（对一本新书）

```bash
translate book.pdf          # 自带 trace + 自动 audit
→ audit/qualification.md    # 直接报告：QUALIFICATION FAIL / HIGH MECH-4 x N / → FIX-3
```

任何 block 的「输入 → 输出」因果链都在 `trace-index.json` 里：source bbox →
translation → plan dst_box（含 y 语义）→ renderer baseline（actual/expected）→
erase 几何 → raster 观测，异常块自动带 crops/ 光栅证据。

拿到 `trace_id` 后，`explain` 把整条因果链变成一段可直接交给开发者的诊断：

```
Trace:       442/p442_4
Status:      FAIL
First stage: render
Module:      pdf2zh/v3/magicpdf_renderer.py
Severity:    HIGH
Rules:       FLOW_BASELINE_SEMANTICS
Fix:         FIX-3
Downstream:  INK_OVERLAP (raster)

 ├─ plan    PASS
 ├─ fixup   PASS
 ├─ layout  -
 ├─ render  FAIL  ← first divergence (FLOW_BASELINE_SEMANTICS)
 ├─ erase   PASS
 └─ raster  FAIL  ← downstream symptom (INK_OVERLAP)

Plan:       dst_box.y1 = 275.29, meaning = box_top (v3 y-up)
Fixup:      delta_y = -10.71, first_cmd_y = 275.29
Renderer:   y_meaning = baseline  ← 与 plan 的 box_top 矛盾（根因现场）
Raster:     foreign_overlap_pct = 42.0
```

开发者不再需要打开 renderer 内部状态去猜 —— trace 已经把「哪里第一次坏、
违反哪个 invariant、哪个模块负责、后面有哪些连锁症状、该看哪份 evidence」
一次性讲清楚。