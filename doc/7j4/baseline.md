# 7J-4A — Frozen Acceptance Baseline（release hardening 入口）

冻结于提交 **`c887884`**（7J-3D qualification 收口，工作树 clean 时捕获）。

## 冻结矩阵（5 书 / 33 页，`doc/7i4-corpus-baseline/`）

| defect | PASS | FAIL | SKIP | NOT_MEASURED | 语义 |
| --- | --- | --- | --- | --- | --- |
| F1 | 31 | 0 | 0 | 0 | 已测干净 |
| F2 | 12 | 0 | 19 | 0 | 无代码块页 SKIP |
| F3 | 31 | 0 | 0 | 0 | 已测干净 |
| **F4** | 30 | **1** | 0 | 0 | **唯一 measured residual**（p300 @ parser，故意保留） |
| F5 | 0 | 0 | **31** | 0 | representation gap（物理层有 drawings/images，model 无 float 语义块） |
| F6 | 10 | 0 | 21 | 0 | 无 caption 证据 SKIP |
| F7 | 0 | 0 | 0 | **31** | real-translation harness 未解锁 |
| F8 | **31** | 0 | 0 | 0 | 7I-5C re-WRAP 修复后 0 residual |
| F9 | **31** | 0 | 0 | 0 | 7J-3A text-layer integrity 扩展后 31/31 |
| F10 | **31** | 0 | 0 | 0 | provenance 接线后 31/31 |

**residual histogram**: 总 residual = **1**（F4 × 1，FDS = **parser**）。

## 冻结不变量（release gate 断言对象，`doc/7j4/release_gate.py`）

1. `total_residual == 1` 且 `by_defect == {"F4": 1}` 且 `by_first_divergence == {"parser": 1}`
2. F4 必须 FAIL 且必须存在于 p300 @ parser —— **preserved negative control 缺失本身即 gate 失败**
3. F5 SKIP 31 / F7 NOT_MEASURED 31 —— 边界永远不得被涂成 PASS
4. F8/F9/F10 PASS 31 —— 无 migration
5. 历史 F9 artifact（AI mono p3 nul=60 / p157 nul=1）必须仍被 detector 捕获
6. corpus baseline 复跑必须与已提交基线**逐字节一致**（determinism）

## 语义

> 本基线的含义是：**当前 pinned stack（`doc/7j4/dependencies.md`）上，可测范围内
> 唯一 measured residual 是 F4×1 @ parser；F5/F7 未覆盖的原因已明确记录。**
> 它不是"没有 bug"，而是"在定义好的检测能力范围内没有发现 bug"。
> 任何依赖升级（尤其 pymupdf/babeldoc）或 renderer 变更后，必须重跑
> `doc/7j4/release_gate.py` 才能重新主张此基线。

## 验收基线链

```
00b001e  7J-3C  Case B first-divergence（不可复现）
c887884  7J-3D  dual-subclass qualification（all checks pass）← 本基线锚点
```