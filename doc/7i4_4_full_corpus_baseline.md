# 7I-4-4 — Full Corpus Baseline（F1–F10 全落地后）

状态：**COMPLETE（7I-4 步骤四 / 终版 baseline 冻结）**

按你的定义，这轮**不是证明系统干净**，而是**建立完整检测覆盖之后的真实 residual
baseline**。因此本阶段**不修任何 F8**——先冻结可审计版本，下一阶段完全由这张表
的数据决定该进 `7I-5 Layout Clip Fidelity` 还是 `forensic observability /
model capability`。

数据与脚本：`doc/7i4-corpus-baseline/{summary.json, report.md}`
（收集器：`doc/7i4/full_corpus_baseline.py`；与 7I-4-0/4-1 的 scan 并行，未覆盖
旧证据）。corpus 采样页沿用 7I-4-0 manifest（5 书 / 33 页 / 489 块）。

---

## 1. Coverage Matrix（页面计数，5 书合并，四态）

| defect | PASS | FAIL | SKIP | NOT_MEASURED |
|---|---|---|---|---|
| F1 | 31 | 0 | 0 | 0 |
| F2 | 12 | 0 | 19 | 0 |
| F3 | 31 | 0 | 0 | 0 |
| F4 | 30 | **1** | 0 | 0 |
| F5 | 0 | 0 | **31** | 0 |
| F6 | 10 | 0 | 21 | 0 |
| F7 | 0 | 0 | 0 | **31** |
| F8 | 12 | **19** | 0 | 0 |
| F9 | 0 | 0 | 0 | **31** |
| F10 | 0 | 0 | 0 | **31** |

> 原则已冻结：`SKIP` / `NOT_MEASURED` ≠ `0`。
> **F5 = SKIP（31/31）** 是 representation gap，不是干净；**F7/F9/F10 =
> NOT_MEASURED** 是「尚未实现 detector」，也不是 0。这两格都不准被读成「没有 bug」。

---

## 2. F1–F10 Residual Histogram（first_divergence）

```
总 residual = 72

F4 = 1   (FDS: parser)      ← p300 源 PDF cid artifact，7I-3 决策保留
F8 = 71  (FDS: layout)      ← 全新揭出的真实截断类 defect
F1/F2/F3/F5/F6/F9/F10 = 0 findings
```

**这是 7I-4 最重要的产出**：相比 7I-4-0 的「F1–F10 ≈ 0」（那是 observability
blind spot），现在完整检测覆盖下：

> **F8 = 71 是真实的 residual class**，不是 renderer 把文字弄没了，而是
> **layout flow 已明确决定把仍然溢出的内容剪切掉**（`recovery.decision="clip"`）。

---

## 3. Per-book 分布

| 书 | blocks | F1 | F2 | F3 | F4 | F8 |
|---|---|---|---|---|---|---|
| C book | 146 | 0 | 0 | 0 | 0 | **13** |
| AI for Games | 18 | 0 | 0 | 0 | 0 | 0 |
| Game Physics | 36 | 0 | 0 | 0 | 0 | **6** |
| Networking | 66 | 0 | 0 | 0 | 0 | **11** |
| Multiprocessor 2e | 223 | 0 | 0 | 0 | 1 | **41** |

F8 不是某一本书的特例：**C/GP/Net/MP 四本都有**，仅 AI（5 页采样）无。

---

## 4. 排障三问（数据回答，不猜）

### A. F8 是系统性问题吗？→ 是

4/5 本书、19/33 页出现 F8 clip；总 71 块。这不是孤立 sample 缺陷，进入下一工程
milestone（`7I-5 Layout Overflow / Clip Fidelity`）是数据支持的。

### B. F8 集中于哪种 block / 哪一步？→ 布局放不下的段落（width 主导）

```
by kind    : paragraph = 71  (100%)
by reason  : width = 68, height = 3
by steps   : WRAP→SHRINK→CLIP = 43, SHRINK→CLIP = 28
final_font : <=7pt 的 46 / 71（64% 被缩到很小仍放不下）
by line    : line_count = 1 的 71 / 71
translated/source 长度比均值 = 1.00（identity——不是译文膨胀）
```

- **不是 translation expansion**：`长度比 = 1.00`（恒等翻译，文本长度没变长）；
- **是 layout constraint**：`width=68`，即源段落本身太宽、放不下目标块宽，
  `SHRINK→CLIP`（缩到 ≤7pt 仍溢出才剪）；
- **有 recovery policy 参与**：三步链 `WRAP→SHRINK→CLIP`，说明已试过换行、缩字，
  最后仍决定 clip。

→ 指向 `7I-5`：为什么 `SHRINK` 缩到 5–7pt 仍允许 clip 输出。这是 layout/recovery
政策问题，**不应继续改 renderer / PDF emitter**。

### C. F5 observability gap 普遍吗？→ 普遍

```
无 model float 块的页 / 有 model float 的页 = 31 / 0
这些无 model float 页的物理层对象（抽样）：drawings=142, images=10
```

**全部 31 采样页物理层都有 drawings/≥个别 images，但 document model 0 个
figure/table/image 语义块**。F5 的 representation gap 是系统性的，不是抽样巧合。

结论（照你的建议）：**如果当前不需要 F5 去区分真实 figure，就不必仅为 detector
coverage 立即重构 document model。** 但值得把它与 F8 的取证化一起汇总成一个独立
的 observability / model-capability milestone 提案，等 corpus 或产品需要时再做。

---

## 5. p300 control（多 defect 独立，继续保留）

```
p300:
  F4  = FAIL @ parser   （仅 p300_8，cid artifact）
  F8  = FAIL @ layout   （p300_8..12，各自 layout clip verdict）
  F6  = PASS            （caption 置于计划位置）
  F10 = PASS            （无 dangling block）
```

证明：一个 source/parser anomaly（F4）能与其页面上的 layout clip（F8）各自独立
触发，**不被 cascade 成多个同源 defect**；detector 已能并行描述不同阶段的独立
问题。

---

## 6. 结论：下一阶段由数据决定

```text
7I-4-0  Expanded Corpus         ✅
7I-4-1  Contract + F1/F3        ✅
7I-4-2  F5/F6                   ✅  (F5 = observability gap, F6 PASS where captions)
7I-4-3  F8                      ✅  (揭出真 residual class: F8 @ layout)
7I-4-4  Full Corpus Baseline    ✅  终版成绩单：F8=71, F4=1

NEXT（二选一，由数据选）:
  7I-5  Layout Overflow / Clip Fidelity   ← 强支持（F8=71, width/CLIP 主导）
  或
  forensic observability / model capability  ← F5 gap 普遍 + F7/F9/F10 取证化
```

**推荐**：先做 `7I-5 Layout Overflow / Clip Fidelity`——F8 是唯一成规模的真实
defect class（71/72），方向明确（layout/recovery policy，非 renderer）。F5 的
observability gap 与 F7/F9/F10 的 NOT_MEASURED 横跨 foreground/background，可作
为 7I-5 之后或并行的第二个工作流，但不应抢先重构 document model。

---

## 7. 验证

- baseline 收集器 `doc/7i4/full_corpus_baseline.py` 运行稳定（两次 rerun 结果
  一致：总 residual 72，F8=71/F4=1）；black 干净、flake8 无新增告警。
- 底层的 detector 全部经 `tests/test_detector_coverage_7i4.py`（25 项 +
  previous 们）与 forensics/cid/coverage top-level 314 项验证。
- 数据文件：`doc/7i4-corpus-baseline/{summary.json, report.md}`。