# 7I-4-4 — Full Corpus Baseline（F1–F10 全落地后）

日期：2026-08-31 · 方法：in-pipeline provenance（恒等翻译 + 生产 renderer + ID-direct diff）+ four-state detector coverage。**本阶段只测量、不修 F8** —— 目标是冻结可审计 baseline。

## 1. 全局 Coverage Matrix（页面计数，5 书合并）

| defect | PASS | FAIL | SKIP | NOT_MEASURED |
|---|---|---|---|---|
| F1 | 31 | 0 | 0 | 0 |
| F2 | 12 | 0 | 19 | 0 |
| F3 | 31 | 0 | 0 | 0 |
| F4 | 30 | 1 | 0 | 0 |
| F5 | 0 | 0 | 31 | 0 |
| F6 | 10 | 0 | 21 | 0 |
| F7 | 0 | 0 | 0 | 31 |
| F8 | 31 | 0 | 0 | 0 |
| F9 | 0 | 0 | 0 | 31 |
| F10 | 0 | 0 | 0 | 31 |

> 原则：`SKIP`/`NOT_MEASURED` ≠ `0`（F5=SKIP 是 representation gap，不是干净）。

## 2. F1–F10 Residual Histogram（first_divergence）

- 总 residual: **1**
- **F4** = 1  (FDS: parser=1)

## 3. Per-book 分布

| 书 | blocks | F1 | F2 | F3 | F4 | F5 | F6 | F7 | F8 | F9 | F10 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| C book | 146 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| AI for Games | 18 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| Game Physics | 36 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| Networking | 66 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| Multiprocessor 2e | 223 | 0 | 0 | 0 | 1 | 0 | 0 | 0 | 0 | 0 | 0 |

## 4. F8 深度分布（bug 类判别）

- 总 clip 块数（含重复页）：0
- by kind: {}
- by recovery reason: {}
- by steps: {}
- final_font_size 分布(<=7pt): 0 / 0
- by line_count: {}
- translated/source 长度比均值: n/a

## 5. F5 observability gap 普遍度

- 无 model float 块的页 / 有 model float 的页: 31 / 0
- 这些无 model float 页的物理层对象（抽样）：drawings=142, images=10
→ F5 的 representation gap **普遍**：即便物理层有 drawings，document model 也无 figure/table/image 语义块，F5 只能 SKIP。

## 6. p300 control（多 defect 独立）

p300: blocks=13, model_float=0
上表 F4=F@parser / F8=F@layout / F6=P / F10=P —— 一个 source/parser anomaly 不污染其它 detector。
