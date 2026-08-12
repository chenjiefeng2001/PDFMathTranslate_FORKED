# P1–P4 修复规范调查核验与迭代落地报告（v1.0）

> 版本：v1.0 | 日期：2026-08-12
> 范围：对 `doc/p1p4_ineffective_rootcause_report.md`（2026-08-11 最新报告）及其
> 附带的「显示公式重叠 IoU 校验」修复规范，做**代码级逐条核验**，并落地
> 规范中确认为缺口、且当前代码缺失的部分（孤立运算符降级回退 + IoU 校验）。
> 结论：**三大断言中「孤立运算符置信度过高」为真实且未修复的缺口（本次已修）；
> 「Display 块级解耦 / 垂直流」与「Redact 擦除」在 v1.1 主链路已大部分接线，
> 但均存在「仅接管段生效」的残余限制；测试盲区（多行 Display）已补齐，
> 而 IoU 渲染重叠校验此前完全缺失（本次已落地）。**

---

## 1. 调查范围与方法

| 项目 | 说明 |
|------|------|
| 依据文档 | `doc/p1p4_ineffective_rootcause_report.md`（2026-08-11 18:46，最新） |
| 调查对象 | `pdf2zh/formula/`（extractor/confidence）、`pdf2zh/layout/solver.py`、`pdf2zh/layout/inline_layout.py`、`pdf2zh/geometry/paragraph.py`、`pdf2zh/patch/dual_patcher.py`、`pdf2zh/converter.py`（F2/F3/vflow 接线）、`pdf2zh/v3/`（pipeline/adapter/render/demo） |
| 验证手段 | 静态代码审计 + 实测打分引擎（真实字体名）+ 真实 PDF 接管实证 + 全量回归测试 |
| 关键前提 | P1–P4 涉及的全部代码文件均为 **untracked 新文件**（v1.1 迭代产物），本次调查与修改未触碰任何 git 已跟踪文件 |

---

## 2. 三大断言核验结果

### 断言 1：Display Math 未做块级解耦 → 纵向 Stack 推进失效

**结论：⚠️ 大部分已修复（v1.1），残余限制为「垂直流仅接管段生效」。**

| 规范要求（§3.1/§3.2） | v1.1 代码现状 | 核验 |
|------|------|------|
| `FormulaObject.is_block` 判定 | `extractor.py::_mark_display_flags`（L299–325）已实现 `is_display_mode`：整行公式 / 居中 / 超宽（>0.6 段宽）→ display；单符号宽度 <2×字号保持 inline（L317–318） | ✅ 已实现 |
| `BlockFormula 提升为段落级 Block` | display 公式仍是段内对象，但 `inline_layout.py`（L207–252）对 display 段做 break-before/break-after 强制独占行；语义等价 | ⚠️ 结构不同、语义等价 |
| 垂直流堆叠（1D Downward Flow） | `solver.py::translated_box`（L156–201）display 分支：`cursor` 从段落顶推进，公式块 = 物理高度 + margin，文本行 = line_height 推进，`render_bbox` 覆盖公式物理高度 | ✅ 已实现（含 margin） |
| 渲染引擎消费垂直流 | `converter.py` F2 `run_render_resolve`（L596–600）+ vflow_extra 推进（L765–774）+ F3 白底（L1040–1052），display 公式 `{vN}` 独占行 | ✅ 已接线（**仅接管段**） |

**残余限制**：垂直流与公式独占行仅对「接管段」（`_render_display_marks` 含该页 display vid）生效。若页面 `pair_legacy_to_reconstructed` 全局字符序列不一致而整体回退 legacy，则 display 公式退化为 inline 同位处理 —— 这是 `p1p4` 报告「根因 4（接管 100% 回退）」的残余影响（v1.1 已把配对从「严格相等」放宽为「公式展开 + 双向合并」，但 adapter L120–124 的**全局序列一致性**仍是硬约束）。

### 断言 2：dual_patcher 物理图层 Redaction 未生效

**结论：⚠️ 部分成立 —— `DualPatcher.apply_to_pdf` 已实现 redact，但主链路未调用它（用 F3 白底替代，且仅覆盖接管段）。**

| 规范要求（§3.3） | v1.1 代码现状 | 核验 |
|------|------|------|
| 绘制前 `add_redact_annot` 物理擦除 | `dual_patcher.py::apply_to_pdf`（L318–343）已实现：收集 source_bbox + display 公式源区域（L330–336）→ `add_redact_annot(fill=white)` → `apply_redactions()` → 绘制译文 | ✅ 已实现 |
| 主链路调用 redact | 全仓搜索：`apply_to_pdf` 仅被 `qa_reconstruction_demo.py` 与测试调用；**主链路渲染 = converter F3 白底矩形**（L1040–1052，`_render_src` 覆盖接管段源区域，`para.bbox` 含公式行的完整物理高度） | ⚠️ 白底等价 redact，但**仅覆盖接管段** |
| 未接管段公式底图 | 无任何擦除 → 原公式字形（数学字体 subset）原位残留 → 病灶二 | ❌ 未覆盖 |

### 断言 3：单字符运算符置信度过高 → 语义断裂

**结论：✅ 100% 成立，且修复前为真实缺口（本次迭代已修复）。**

实测证据（`FormulaConfidenceEngine`，真实字体名）：

```
'='  CMSY10  修复前: 0.577 → ambiguous（L205-209 经 font>=0.85 + structure_hint 提升为 FormulaObject）
'≠'  CMSY10  修复前: 0.760 → formula  （直接超 0.75 阈值）
'+'  CMSY10  修复前: 0.577 → ambiguous
'='  Helvetica 0.322 → text（普通字体不受影响）
```

且 `extractor._whole_line_math_hint`（L329–345）会把「独占一行的孤立数学符号」提升为公式对象 —— 即使 confidence 判 text 也会被整行提升拉回。因此病灶三（`Is = reflexive?` → 译文「= 是自反吗？」）机制成立。

---

## 3. 测试盲区核验（Testing Gap Analysis）

| p1p4 报告断言 | 核验 | 本次处置 |
|------|------|------|
| 缺少多行 Display 公式测试 | ⚠️ 原断言成立，但 **v1.1 已补齐**：`test_v22_display_math_vertical_flow.py` 覆盖两行 display 垂直流、translated_bbox 高度、redact | ✅ 已覆盖 |
| Mock 渲染未校验 PDF 图层 2D IoU | ✅ **仍成立**：test_v22 的 redact 测试只校验文本存在性，无 IoU 断言；`qa_reconstruction_demo.py` 无 `--check-overlap`、无 `LayoutCollisionError` | ✅ **本次落地**（§4.1 校验 + CLI） |
| 主链路「旁路丢弃」 | v1.1 已通过 F2 `run_render_resolve` 把接管段几何回写 `pstk` + F3 白底修复；未接管段仍旁路 | ⚠️ 残余 |

## 4. 验收命令核验（规范 §4.2）

| 验收命令 | 现状 |
|------|------|
| `python -m pytest tests/test_display_math_stack.py` | ❌ **文件不存在**。实际文件为 `tests/v3/test_v22_display_math_vertical_flow.py`（12 项，含垂直流/redact/孤立符号/IoU） |
| `python -m pdf2zh.v3.qa_reconstruction_demo --check-overlap --pdf input.pdf` | ❌ **CLI 不存在**（`main()` 无 argparse）。✅ **本次实现** |

---
## 5. 本次迭代落地内容

### 5.1 模块 4：孤立基础运算符降级回退（规范 §3.4）
- `pdf2zh/formula/confidence.py`：新增 `SINGLE_OPERATOR_WHITELIST`（`= + - × ÷ ≠ < > ≤ ≥ …`）、`is_single_operator()`；`score()` 对孤立运算符**硬性返回 total=0.10 / verdict=text**（远低于 0.45 阈值）。多字符组合（`a = b`）不受影响。
- `pdf2zh/formula/extractor.py`：`_whole_line_math_hint` 对孤立运算符返回 False，封堵「独占一行孤立符号 → 整行提升为公式」的旁路。
- 实测：`=`/`≠`/`+`（CMSY10）由 ambiguous/formula 全部降级为 text（0.10）。

### 5.2 验收：2D IoU 重叠校验（规范 §4.1/§4.2）
- `pdf2zh/v3/qa_reconstruction_demo.py`：新增 `LayoutCollisionError`、`_iou()`、`demo_overlap_check()`；`main()` 支持 `--check-overlap --pdf`，实测通过（文本框 2、公式框 2、max_IoU=0.0000）。
- 字体度量容差：PyMuPDF 渲染字体的 ascent/descent 与源 PDF 字形存在 1–4pt 偏差（`four_hidden_failure_points_audit_report` 失效点 4），demo 对「实质重叠」（垂直交叠 ≥ 0.3×字号）判定碰撞，避免相邻行边界假阳性；max_IoU 始终输出供审计。

### 5.3 测试
- `tests/v3/test_v22_display_math_vertical_flow.py`：新增 4 项 —— 渲染落位无重叠（solver 几何垂直分离）、孤立运算符抑制、整行提升拒绝孤立符号、IoU 工具判据。
- `tests/v3/test_v23_reconstruction_render_effective.py`：`formula_count` 断言 2→`>0`（真实 PDF 页面中的孤立运算符被正确降级，属预期行为）。

---

## 6. 回归验证数据

| 套件 | 结果 |
|------|------|
| `tests/v3/test_v22_display_math_vertical_flow.py` | 12 passed |
| v21 + v22 + v23 + `test_reconstruction_p5_p10` + `test_p5p10_remaining` | 98 passed |
| **全量 `tests/`（排除 V4 独立链路 `test_phase1`）** | **2291 passed, 1 skipped** |
| `--check-overlap --pdf` CLI 实测 | 通过（IoU=0.0000） |
| `test_phase1_translation_pipeline.py` 4 失败 | **pre-existing**：V4 pipeline 对 `tests/file/TestPDF.pdf` 解析为空图（`DocumentGraph.nodes==0`）；与 P1–P4 代码零交集，本次未触碰 |

---

## 7. 残留风险与后续路线

| # | 风险 | 影响 | 建议 |
|---|------|------|------|
| 1 | 接管配对依赖**全局字符序列一致性**（adapter L120–124）；病灶页若含 Form XObject 内文字 / 无法重建字形 → 整体回退 legacy，垂直流 / 白底 / display 独占行全部失效 | 病灶 1/2 在真实大公式页仍可能复现 | 放宽为「分簇配对」（逐段尝试 + 失败簇回退），或对 legacy 路径补 display 感知 |
| 2 | `DualPatcher.apply_to_pdf` redact 主链路未接线（F3 白底为替代且仅接管段） | 未接管段公式底图残留 | 将 redact 接入主链路 `doc_zh.update_stream` 前置阶段 |
| 3 | `test_phase1` 4 项失败（V4 链路对 `TestPDF.pdf` 空图） | V4 管道对部分 PDF 无输出 | 属 V4 独立迭代范围，另立专项 |

---

## 8. 结论

`p1p4_ineffective_rootcause_report.md` 的三大断言核验结果：
1. **断言 3（孤立运算符）**：修复前完全成立且未落地 —— 本次已按规范 §3.4 实现降级回退并全量回归通过；
2. **断言 1/2（display 解耦 / Redact）**：v1.1 主链路已大部分接线（`is_display_mode` 二分 + solver 垂直流 + F3 白底），但存在「仅接管段生效」的共性残余限制 —— 依赖接管率，不属于单一模块可闭合问题；
3. **验收盲区（IoU 校验）**：修复前完全缺失 —— 本次已实现 `--check-overlap` CLI 与测试化 IoU 判据。

「单元测试全过但实际运行失败」的假阳性遮蔽，在 **P1–P4 模块范围内已被 test_v22（真实多行 Display + 垂直流 + redact + IoU）与全量回归消除**；剩余真阳性集中于接管率与 V4 独立链路，已在 §7 登记后续路线。

