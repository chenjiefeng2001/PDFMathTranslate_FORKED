# 7N-FULL — MP2e 全书验证运行（FIX-2 / pfkstk 落地后）

> **日期**：2026-09-02 · **基线**：v1.9.16 / `4eccfa6` + 工作区（FIX-2 + V1.17-3 pfkstk 同步）
> **性质**：evidence-only 验证运行；0 代码修改。
> 产物：`doc/7n-full-mp2e/`（config / environment / run log / align / 双引擎输出）。
> 前置：`doc/7n_fix2_fixup_coshift_report.md`（FIX-2，12 页采样验收）；
> `doc/7n0_mp2e_forensic_report.md` §10（7N-REAL 分叉矩阵）。

## 0. 结论先行

两引擎全书（562 页）串行实跑，**双双 exit=0、零 ERROR**：

- **MECH-2 全书关闭**：37 个 shift_down + 已定版 commands 的块，
  `mech2_decoupled = 0`（FIX-2 不变量 3 全书成立）。
- **MECH-1 残量 = 1**（p442_4，2 行 → CLIP 1 行，5.0pt）——真实全书首个
  MECH-1 复现实例；FIX-1 闸门维持冻结（12 页采样 0 复现的裁决不变，
  本例记录为 evidence-only）。
- **pfkstk 修复在 legacy 全书路径经受住检验**：0 IndexError、0 页崩；
  legacy 全书 mono/dual 双产物完整。

## 1. 运行契约（7N-0 三件套）

| 项 | legacy | magicpdf |
|---|---|---|
| argv 快照 | `config-legacy.json` | `config-magicpdf.json` |
| pages | ALL | ALL |
| 环境 | `environment.txt` | 同一份（进程级 `PDF2ZH_AUTO_SWITCH_MAGICPDF=0`、`PDF2ZH_NO_PARALLEL=1`） |
| 引擎 | legacy 内核 | `--parse-engine magicpdf --magicpdf-ocr-mode off` |
| 并发 | `--no-parallel --thread 1` | 同 |
| 翻译服务 | google | google |
| 耗时 | ≈27 min（562 it） | ≈9 min（MinerU CUDA 解析 + RenderTakeover） |
| exit | 0 | 0 |
| 日志 | `run-legacy.log` / `console-legacy.log` | `run-magicpdf.log` / `console-magicpdf.log` |
| ERROR 计数 | 0 | 0 |

## 2. 输出完整性

- `output-legacy/`：`*-dual.pdf` + `*-mono.pdf`（legacy 渲染路径）。
- `output-magicpdf/magicpdf/`：`*_mono.pdf`（562 页）+ 4 类 JSON 转储
  （document / formula_channel / magicpdf / render_plan）。
  mono p4/p6 正文页 CJK 字符 457 / 821，译文真实落版。

## 3. 全书对齐审计（`align-magicpdf.json`）

| 指标 | 值 | 对照 12 页采样（FIX-2 前 → 后） |
|---|---|---|
| plan_entries | 4401 | — |
| recovery decisions | shrink 25 / clip 1 | 探针 clip 70 → 全书 1 |
| steps | WRAP→SHRINK 24 / SHRINK 1 / WRAP→SHRINK→CLIP 1 | 同上趋势 |
| MECH-1 CLIP blocks | **1**（p442_4） | 采样 0 → 全书 1 |
| fixup counts | preserve 2826 / keep 3689 / keep_overflow 22 / shift_down 153 | 采样 5 → 全书 153 |
| MECH-2 shifted-with-commands | **37** | 采样 3（预期非零） |
| **MECH-2 decoupled** | **0** ✅ | 3 → 0（采样），全书保持 0 |
| doc_blocks / with_translated | 见 align JSON | — |

MECH-2 detail 样例（锚定关系逐块核验）：`p3_4` dst_y1=399.84 vs first_cmd_y=399.80；
`p3_6` dst_y1=354.22 vs first_cmd_y=354.20 —— 0.02–0.04pt 为取整噪声，判据通过。

## 4. MECH-1 唯一残量：p442_4

```text
page 442  trace: WRAP(2行,7.65pt) → SHRINK(2行,5.0pt) → CLIP(1行,5.0pt)
final_font 5.0 / overflow True
```

符合 7N 报告 §8 定义的「真 unbreakable 残量」允许范围：FIX-1 闸门维持冻结，
本例仅入档。若后续开 FIX-1，应按该节方向（多行结构进入 CLIP、per-line clip）。

## 5. 遗留事项

1. **视觉确认**（承接 FIX-2 报告 §6.3，扩大到全书抽样）：dedica 页
   （p5_1/p5_3/p5_7 所在）与 p3 shift_down 块、p442 CLIP 块，肉眼复核
   mono PDF 无覆盖错位/文字重叠/截断。
2. **`commands_shifted` 审计字段**：维持用户裁决（第一版不加）。
3. **7n-real legacy 对照组空跑**（slice-splice 回退全文档路径）：独立工单，
   本轮未涉及。
4. 工作区 6 个修改文件 + 本轮证据目录待提交（用户此前未授权 commit）。

## 6. 产物清单

| 产物 | 说明 |
|---|---|
| `doc/7n-full-mp2e/` | 双引擎全书运行全套（config / env / log / 输出） |
| `doc/7n-full-mp2e/align-magicpdf.json` | 全书对齐审计（本报告 §3 数据源） |
| 本报告 | 全书验证结论 |
