# Session 续接报告 — 7G 排版层收尾 + 7G-5 item 1 交付

日期：2026-08-30 · 分支：`main` · 涉及：`pdf2zh/semantic/layout/*`、`doc/*`

本报告记录一次跨 session 的工作续接：先收尾上个 session 遗留的
7G-2.1 / 7G-2.2 / 7G-4 成果（从未提交），再实现并交付报告
`corpus_layout_scan_7g_report.md` §15.5 定义的 7G-5 item 1
（recovery-side drawn-extent parity）。

---

## 1. 接续前状态调查

工作目录 `c:\Users\14977\source\repos\PDFMathTranslate_FORKED` 处于 7F→7G
布局排版里程碑中。上个 session 完成了 **7G-2.1 P0 修复、7G-2.2 packer 几何
对齐、7G-4 有序两阶段级联** 的实现与三份文档，但**全部未提交**（工作区
10 个修改文件 + 5 个未跟踪文件）。

关键状态确认：

- **586 个 layout/recovery/page-break 测试全绿**（本 session 重验）；
- 全量测试套件在工具 10 分钟超时下被截断于 63%，日志中的 ERROR
  （`_FakeStore`、onnxruntime CUDA 回退）均为测试预期噪音，非回归；
- ruff 报的 UP045 / F401 等为仓库既有状态（HEAD 版本同样存在）；
  pre-commit 实际使用 black + flake8，而非 ruff；
- `.pytest_cache/lastfailed` 引用的两个测试文件
  （`test_diag_recovery_exec.py`、旧版 `test_page_shift_7g4` 用例）已随
  迭代重命名/删除，缓存为陈旧状态，非真实失败。

## 2. 本 Session 完成的工作

### 2.1 收尾提交上个 session 的成果（3 个 commit）

| commit | 内容 |
|--------|------|
| `0f6b675` | **7G-2.1 P0 fixes + 7G-2.2 conservative occupied draw-extent**：8e 越出文档边界（`next_free_page` 增加 `max_page` 约束，`last_page_index(page_sizes)` 兜底，两个执行器以 `no_page`/unresolved 拒绝幻影页）+ packer word-level guards + `_glyph_excess` / `_entry_occupied_bottom_spill` / `_estimate_wrapped_lines` draw-extent 模型 |
| `14f7916` | **7G-4 ordered two-phase receiver-at-FINAL recovery cascade**：`page_shift.py` 的 Phase-1 intent（V1 级联）→ Phase-2 receiver-at-FINAL floor，`src_box`/X/字体/文本逐字不变 |
| `d081f45` | **docs**：`corpus_layout_scan_7g_report.md` §15、`corpus_v1_failures.md`、`freeze_investigation_report.md` |

### 2.2 实现并提交 7G-5 item 1 —— recovery-side drawn-extent parity（`d9d81a1`）

报告 §15.5 定义的下一步：让 recovery 的 SHIFT_DOWN 下限使用接收块的
**真实绘制字形顶部**（glyph top = `dst_top + excess`），而非纯 box top。

实现位于 `page_shift.py`：

- 新增 `_recovery_draw_extent_by_key(plan)`，从 settled plan 一次性计算两张
  纯读取 map：
  - **`excess_by_key`**（接收块顶部 glyph excess，仅 command blocks）：
    Phase-2 floor = `final_top + excess`，preserved 接收块同样适用；
  - **`spill_by_key`**（仅 command-less 块的 wrap 底部溢出）：cap 清真实
    绘制底部。command blocks 被排除 —— 7F-8b `_resolve_bbox` 已把它们的
    绘制底部折叠进 `resolved_bbox`，再减一次会双重计算。
- 严格保持 §13.3 的「不扩展 `resolved_bbox`」拒绝决策：只改移动量、不改
  移动集合，不改 X / src / preserved / `resolved_bbox` 本身。
- 新增回归语料 `tests/test_page_shift_7g5.py`（11 个测试），锁定：接收块
  glyph-top floor 生效、无 map 时与 7G-4 逐字节一致、command-less 自身
  spill 收紧 cap、preserved 同款 floor、真实级联在 FINAL glyph top 收敛、
  无双重计算、端到端 `resolve_page_shifts` 生效。

夹具适配：7F-8d / 7F-9.2 的 `_flow` 把 command 基线放在 box 顶部边缘
（`y = y1`），新语义正确读出 8 pt glyph 溢出，把它们的 *bbox 级联* 意图
变成 glyph-floor 场景。遵循 7G-2.1 先例（`test_layout_packer_7g2.py`
基线从 `top-5` 移到 `top-9`），两处夹具现把基线置于 `top-9`
（ascent 0.8·10 = 8 → 零 excess，良构块在 box 内），保持级联/预算语义；
glyph 溢出 P0 场景由 7G-5 语料专门持有。

## 3. 当前状态

- **586 个 layout/recovery/page-break 测试全绿，零回归**；7G-4/7G-5/
  7F-8d/7F-9.2 专项 51 个测试通过；
- 工作区干净，4 个新 commit 已落定在 `main`：

```
d9d81a1 feat(layout): 7G-5 item 1 recovery-side drawn-extent parity
d081f45 docs(eval): 7G corpus audit reports + freeze investigation
14f7916 feat(layout): 7G-4 ordered two-phase receiver-at-FINAL recovery cascade
0f6b675 feat(layout): 7G-2.1 P0 fixes + 7G-2.2 conservative occupied draw-extent
```

- 下一增量（报告 §16.5）：**7G-5 item 2**（packing word-boundary parity，
  包络剩余 wrap 类，门控用 §13.1 ledger 硬门）与 **item 3**（V2 全语料视觉
  验收）。

## 4. 已知限制

- 全量测试套件（~880 测试）受工具 10 分钟超时限制未能完整跑完；已通过
  layout 相关全套 586 个测试覆盖所有 7G 改动面，非 layout 层
  （services / parallel / translator）未受影响；
- 报告 §15.3 的 44 文档语料渲染门需要在能承载长任务的会话中重跑，以量化
  7G-5 对 `recovery_introduced` 的实际削减。
