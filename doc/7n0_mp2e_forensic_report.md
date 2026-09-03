# 7N — 《The Art of Multiprocessor Programming, 2e》重复故障 forensic 报告

> **日期**：2026-09-02 · **版本**：v1.9.16 / `4eccfa6` · **性质**：evidence-only，
> **未修改任何生产代码**（7N 生产修改闸门未开启）。
> 产物：`doc/7n0_mp2e_baseline.py`（取证仪，black/flake8 clean）、
> `doc/7n0-mp2e/`（恒等基线）、`doc/7n0-mp2e-expand/`（扩展探针）。

## 0. 调查问题

> 7I-5C 的 SHRINK re-wrap 修复已落地、v1.9.16 已冻结，为什么这本书仍然复现
> 旧行为（文字截断 / 缩到 5pt / 版面漂移）？

结论先行：**修复没有被推翻，而是被绕开和被接棒**。按 7N 四分类归因：

- **分类 2（修复覆盖了，但该 case 走了另一条代码路径）= 成立（主因）**
- **分类 3（修复实际生效，但后续阶段再次制造同样的问题）= 成立（同页叠加）**

两个机制在同一本书、同一页面上**独立存在、同时生效**，都由 7N-3 的
per-block trace registry 逐块证实（`doc/7n0-mp2e*/summary.json` 的
`recovery_trace_histogram` 与 `bad_blocks`）。

---

## 1. 7N-0 Baseline

```text
Input      : tests/file/The Art of Multiprocessor Programming, 2e.pdf (562 pages)
Version    : v1.9.16 @ 4eccfa6（HEAD，tag 在 ff08303）
Concurrency: 1（in-process identity pipeline，无 job 交错）
OCR        : 未引入（恒等翻译，无解析引擎切换）
Sample     : 7I-4/7I-5 冻结 manifest —— [0,5,8,12,20,40,80,120,200,300,400,500,550]
Blocks     : 223
```

最小复现单元（7N-1）：

```text
Page 5, Block p5_1（正文 paragraph，源框高 ~12pt ≈ 1 行）
  修复前 (7I-4-4): F8 clip，WRAP→SHRINK→CLIP，final_font ≤ 7pt，line_count = 1
  恒等翻译   : FIX_HIT，WRAP→SHRINK（fit），无截断
  译文扩展探针: WRAP(28 lines) → SHRINK@5.0(10 lines, re-wrap 正确) → CLIP@5.0
              → 输出 1 行 —— 与修复前 F8 完全同表象
```

---

## 2. 7N-2 Pipeline checkpoint（逐块，两轮）

恒等翻译（223 块）：

| 阶段 | 状态 | 证据 |
|---|---|---|
| source / parser / model | 正常 | 223 块全部抽出，kind 分布与 7I-4-4 一致 |
| layout（adaptive 7I-5C） | **正常** | decisions={shrink:85}，steps={WRAP→SHRINK:45, SHRINK:40}，**0 clip**，trace 无 line collapse |
| fixup（RenderTakeover） | **异常** | shifted=140 / overflowed=16 —— 恒等翻译（长度比 1.00）下仍有 140 块被移位 |
| render / emit | 正常（机制 2 除外） | flow_layout_used=92，flow_legacy_fallback=0 |

扩展探针（223 块）：

| 阶段 | 状态 | 证据 |
|---|---|---|
| layout（adaptive 7I-5C） | 部分异常 | SHRINK re-wrap 本身正确（28→10 行），但 70 块跌入 Stage 3 CLIP |
| fixup | 异常 | overflowed=37 |
| emit | **异常** | 70 块按 CLIP 结果绘制，`flow_overflow=70` |

---

## 3. 7N-3：修复是否被调用？——逐块登记结果

分类定义：`FIX_HIT`=进入 adaptive 执行器；`SHIFT_DOWN`/`PRESERVE_REGION`=被
fixup 接管；`TOC_CHANNEL`=TOC 侧信道；`PRESERVE_OR_OTHER`=code/formula/heading 等。

| 运行 | FIX_HIT | SHIFT_DOWN | PRESERVE_REGION | TOC_CHANNEL | PRESERVE_OR_OTHER | CLEAN |
|---|---:|---:|---:|---:|---:|---:|
| 恒等 | 14 | 67 | 8 | 67 | 64 | 3 |
| 扩展探针 | 2 | 63 | 27 | 67 | 64 | 0 |

关键事实：

1. **恒等翻译下修复自身完全健康**：85 块进入修复后的 SHRINK，`WRAP→SHRINK`
   全部 fit，0 clip，无 1-line collapse。7I-5C 的因果链没有被破坏。
2. **但每个页面平均只有不到 1/4 的 flow 块真正经过 adaptive**：大量块被
   `fixup_render_plan` 以 `shift_down` 接管（恒等下即有 67 块），走了**没有
   WRAP/SHRINK 语义的粗估路径**。
3. 扩展探针下，70 块（页均 6–8 块）跌入 `WRAP→SHRINK→CLIP`，final=5pt、
   输出 1 行 —— **与 7I-4-4 冻结的 F8 直方图（71 块，width 主导，
   line_count=1/71）逐项吻合**。这本书正是当初 F8 最重的书（41 块）。

---

## 4. 机制一（分类 3）：CLIP 终端阶段重新引入 line collapse

**代码链**：`adaptive.py` Stage 3 CLIP → `overflow.lay_out` CLIP 分支：

```python
# overflow.py（CLIP 分支，现有代码）
clipped, _ = clip_text(text, _m, width)
return _finish([clipped], [_m(clipped)], True, pol, fs)
```

`clip_text` 把**整段文本**按宽度截成一行 —— 正是 7I-5C 从 SHRINK 中移除的
"单行坍缩"，从**下一个 stage** 原样回归。逐块 trace 铁证（p5_1，扩展探针）：

```text
WRAP   : 28 lines @ 11.96pt   ← WRAP 正确发生
SHRINK : 10 lines @ 5.0pt     ← 7I-5C re-wrap 正确执行（28→10）
CLIP   : 1 line  @ 5.0pt      ← 上一阶段的成果被终端阶段丢弃
final  : 输出 1 行，overflow=True
```

**First Divergence = layout（adaptive Stage 3 CLIP，line-collapsing terminal）**。
这不是 7I-5C 修复失效，而是修复的 5D 文档中明确遗留的终端 CLIP 语义与
re-wrap 修复不兼容：SHRINK 交付了多行 fit-候选，CLIP 却不接收它。

> 注意：7I-5D 的结论"terminal CLIP correct, auditable, non-silent"在
> **单 token unbreakable** 场景成立；但**可 wrap 的多行内容**在 floor 处
> 跌入 CLIP 时，会把可读的多行布局坍缩成一行残句。7I-5D 的 corpus 没有覆盖
> "re-wrap 到 floor 仍高度溢出"的复合场景，这是检测盲区，不是行为正确。

触发条件：**译文长度增长**（真实翻译的普遍现象，恒等翻译掩盖了它）。
这正是"我明明修好了，为什么这本书还是老样子"的第一半答案。

---

## 5. 机制二（分类 2）：fixup_render_plan 绕过 adaptive 执行器

**代码链**：`magicpdf_cli.py`（生产 magicpdf 引擎主路径）→
`render_plan_from_model` → **`fixup_render_plan`** → `render_plan_to_pdf`。

`fixup_render_plan`（`render_takeover.py:148`）用自己的估算器决定块的位置：

```text
est_width  = 1.0em/全角 + 0.5em/半角      ← 不用字体度量
est_lines  = ceil(est_width / box_w)
est_height = est_lines × 1.4 × font_size
> box_h×1.25 → shift_down 或 keep_overflow
```

它**从不调用 `adaptive_layout` / `lay_out`**，与 7F 恢复体系没有契约关系。
证据：

- **恒等翻译下 140/223 块被 shift_down**（长度比 1.00 仍触发）——因为它用
  em 估算行数，而 v3 块的 src_box 常常只有一行高（PDF 行盒），任何多行段落
  都被估成"放不下"→ 下移。p5 全页 7/8 块被移位。
- **几何矛盾（MECH2）**：flow 块的已定版 commands 锚定在**原块顶**
  （`p5_1` 首行 y=485.0 = 原 box y1=496.93 的顶部锚点；fixup 却把
  dst_box 整体下移 16.7pt）。renderer（`_render_flow_commands`）**只读
  command 的 x/y，不读 dst_box**，所以 fixup 的"下移"对这些块根本不生效
  —— 148 块 fixup 决策中，67 块 shift_down 与实际绘制坐标**脱钩**，
  其余 preserve/keep_overflow 的白底矩形（`block_rect` 来自 dst_box）
  与文本错位，这正是"文字重叠 / 位置错误"类表象的直接来源。
- fixup 的白底覆盖矩形按**移位后**的 dst_box 画，文本却按**未移位**的
  command 坐标画 —— 两者错位 = 覆盖原文不全/盖掉邻块 = 用户可见的"旧问题"。

**First Divergence = layout 之后、emit 之前的 fixup 阶段（alternate path）**。
它不推翻修复，而是让同页面上的另一批块从未享受修复，并制造独立的几何错位。

---

## 6. 因果链（7N-7 定案）

```text
Input PDF (MP 2e)
   ↓
IR / Paragraph / Layout#1（adaptive 7I-5C）   —— 恒等下健康（0 clip）
   ↓
[译文长度增长 —— 真实翻译必然，恒等探针掩盖]
   ↓
MECH-1: SHRINK re-wrap 正确 → 多行 fit 候选
        → Stage 3 CLIP 用单行 clip_text 覆写 → 1 行 @5pt   ← First Divergence ①（layout）
   ↓
MECH-2: fixup_render_plan 用 em 估算第二次改写 dst_box（shift 16–21pt）
        → 与已定版 commands 的绘制坐标脱钩 / 白底矩形错位   ← First Divergence ②（fixup，alternate path）
   ↓
Emitter（_render_flow_commands 忠实绘制 command、忽略 dst_box）
   ↓
输出 PDF：截断/极小字号/重叠/漂移 —— 与修复前 F8 同表象
```

两个 First Divergence 均满足 7N-8 闸门四条件：**可重复（两次运行直方图稳定）、
明确责任代码、现有 contract 无法覆盖（5D 只保 unbreakable 单 token；
fixup 与 7F 无契约）、表象与用户报告一致**。

---

## 7. 7N-5 / 7N-6 附记

- **7N-5（并发对照）**：本轮为 in-process 恒等管线，单任务即复现两机制；
  并发不是本 defect 主因（`code=2`/Windows temp 清理竞争按原方案仍是
  独立故障，未纳入本轮因果链）。
- **7N-6（temp lifecycle）**：未触发，维持原判——与页面质量分开验证。

---

## 8. 7N-8 建议（下一步，不在本轮执行）

优先级按对用户表象的贡献排序：

1. **FIX-1（对应 MECH-1）**：Stage 3 CLIP 之前，SHRINK 交付的多行 fit 候选
   若仍然溢出，应**保留多行结构**进入 CLIP（clip 每行而非整段单行），
   或对 re-wrap 后的多行结果做 per-line clip；`overflow=True` 语义不变。
   触点：`adaptive.py` Stage 3 / `overflow.py` CLIP 分支。
2. **FIX-2（对应 MECH-2）**：`fixup_render_plan` 对携带已定版 flow commands
   的块**跳过 shift/keep_overflow**（或把 shift 同步写回 command 坐标），
   让 7F 恢复体系成为 flow 块唯一的版面权威；白底矩形与绘制坐标必须同源。
   触点：`render_takeover.py`、必要时 `magicpdf_renderer.py` 的 rect 来源。
3. 验证闸门：扩展探针直方图应从 `WRAP→SHRINK→CLIP:70` 降至仅真正
   unbreakable 的残量；恒等基线 `shift_down` 应归零；既有 4/4 contract
   tests + 7F-7 golden baseline 不得回退。

---

## 9. 产物清单

| 产物 | 说明 |
|---|---|
| `doc/7n0_mp2e_baseline.py` | 取证仪（checkpoint 表 + 7N-3 分类 + `--expand` 探针） |
| `doc/7n0-mp2e/summary.json` `checkpoints.md` | 恒等基线（223 块逐块） |
| `doc/7n0-mp2e-expand/summary.json` `checkpoints.md` | 扩展探针（复现旧行为） |
| 本报告 | 因果链 + First Divergence 定案 |

---

## 10. 7N-REAL — production-like reproduction baseline（FIX 前置闸门）

> 状态：**待执行**（手动）。0 代码修改；`doc/7n_real_mp2e.py` 只包装生产 CLI
> 与审计产物。定案规则：**probe 中成立的机制，真实翻译必须出现对应表象，
> 否则不得据 probe 开 FIX 闸门。**

### 10.1 采集命令（手动执行）

```bash
# ① magicpdf 引擎（RenderTakeover 主路径，MECH-1/MECH-2 都应命中）
#    注意：生产 CLI 的 --pages 是 **1-based**（内部转 0-based），而 align
#    审计的 --pages 与 7N manifest 一致是 0-based —— 两者都按各自约定填。
#    6,9,13,21,41,81,121,201,301,401,501,551 (1-based) ≙ 7N 的采样页。
python doc/7n_real_mp2e.py run --engine magicpdf --out doc/7n-real \
    --pages 6,9,13,21,41,81,121,201,301,401,501,551

# ② legacy 引擎对照组（可选；用于排除引擎差异）
python doc/7n_real_mp2e.py run --engine legacy --out doc/7n-real \
    --pages 6,9,13,21,41,81,121,201,301,401,501,551

# ③ 对齐审计（只读产物；run 完成后执行；此处 0-based，同 7N manifest）
python doc/7n_real_mp2e.py align --engine magicpdf --out doc/7n-real \
    --pages 5,8,12,20,40,80,120,200,300,400,500,550
```

说明：run 会把 `PDF2ZH_AUTO_SWITCH_MAGICPDF=0`、`PDF2ZH_NO_PARALLEL=1`
写入**本次进程**环境（不动全局），并把 argv / env / 完整日志存档到
`doc/7n-real/{environment.txt, config-magicpdf.json, run-magicpdf.log}`。
产物在 `doc/7n-real/output-magicpdf/`（dual/mono + `magicpdf/*_render_plan.json`
等 4 类 JSON 转储）。

### 10.2 对齐审计要回答的问题（acceptance）

| # | 问题 | 数据源 | 判定 |
|---|---|---|---|
| 1 | 真实译文的 layout 决策直方图是什么？ | `align-*.json` `recovery_decisions/steps` | 出现 `clip` → MECH-1 命中 |
| 2 | CLIP 块的 trace 是否 `WRAP→SHRINK(多行)→CLIP(1行)`？ | `mech1_detail[].trace` | line collapse 复现 |
| 3 | fixup 在真实译文下 shift 了多少块？与 7N probe 的 140/223 是否同量级？ | `fixup_counts` | MECH-2 路径在跑 |
| 4 | shift_down 且带 settled commands 的块中，坐标脱钩多少？ | `mech2_decoupled` | 几何错位实锤 |
| 5 | 异常页/block 与 7N checkpoint 表（p5_1、p8_1…）是否重合？ | `mech*_detail[].block_id` | First Divergence 与用户表象对齐 |
| 6 | （人工）打开 mono/dual PDF，肉眼确认截断/重叠/漂移页 | `*_mono.pdf` / `*_dual.pdf` | 表象清单 |

### 10.3 分叉矩阵（7N-8 最终裁决表）

```text
真实复现 MECH-1（trace clip + 1 行输出）        → FIX-1 进生产
真实复现 MECH-2（decoupled > 0 或表象重叠/漂移） → FIX-2 进生产
真实未复现（align 全绿 + 肉眼无异常）           → 冻结 probe 结论，
                                                  重查用户配置差异（引擎/OCR/并发）
```

### 10.4 产物清单（7N-REAL 追加）

| 产物 | 说明 |
|---|---|
| `doc/7n_real_mp2e.py` | run（生产 CLI 包装）+ align（只读审计） |
| `doc/7n-real/environment.txt` `config-*.json` `run-*.log` | 7N-0 基线契约三件套 |
| `doc/7n-real/output-*/` | dual / mono PDF + 4 类 JSON 转储 |
| `doc/7n-real/align-*.json` | MECH-1/MECH-2 逐块对齐审计 |
