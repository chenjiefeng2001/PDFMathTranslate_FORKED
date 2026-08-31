# 7I-4-1 — Detector Coverage Completion（contract + F1/F3）

状态：**COMPLETE（7I-4 步骤一）**

上一篇 `doc/7i4_residual_corpus_assessment.md`（7I-4-0）找出的最后一个方法学缺口是：

> 成绩单里的「0」语义不统一 —— 有些是 detector 运行了、明确没发现；有些是
> detector 根本没覆盖、只是暂时没发现。

7I-4-1 冻结 detector contract（7I-4-1 第一步），并实现 **F1（wrong translation
area）** 与 **F3（abnormal font size）** 两个缺失的 per-node detector。按计划，
F5/F6（7I-4-2）与 F8（7I-4-3）**不在本节实现**，但它们现在会被显式标记为
`NOT_MEASURED`，而不是伪装成 `0`。

---

## 1. Detector Contract（已冻结）

每个 defect 的状态必须来自四值枚举，**永不缩写回二元**：

```text
PASS           = detector 运行，且 >=1 个节点证据充分、未发现缺陷
FAIL           = detector 运行，且发现 >=1 个缺陷
SKIP           = detector 运行，但该页没有任何节点带充分证据（未测量到任何东西）
NOT_MEASURED   = 该 defect 的 detector 尚未实现
```

核心不变量（写进代码 docstring 与单测）：

> **SKIP / NOT_MEASURED ≠ 0。** 一个没被测的页面绝不能报告成「干净的 0」。

契约实现在 `dual_forensics/defect.py`：

- 状态常量 `STATUS_PASS / FAIL / SKIP / NOT_MEASURED`；
- `Coverage` dataclass：`{defect_id, status, evaluated_nodes, findings, note}`，
  带 `to_dict()` 供报告序列化；
- `coverage_page(traces, dual_page)` → 每页全 F1..F10 的 Coverage；
- `aggregate_coverage(pages)` → corpus 级 `{defect_id: {status,
  pages_evaluated, pages_total, pass, fail, skip, not_measured, findings}}`。

每格验收格式就是这样：

```text
F1  PASS  33/33 pages evaluated
F2  PASS  33/33 pages evaluated
...
F5  NOT_MEASURED  0/33
```

---

## 2. 实现：F1 与 F3

`_NODE_DISPARITY` 表把每个实装 detector 绑定到一个 `can_evaluate` 谓词和一个
run 函数；detector 只有在证据充分（谓词为真）的节点上才算 `evaluated`。

### F1 — wrong translation area / placement

- 证据：layout 计划的 `dst_box` **与** 实际画出的 render box（`_node_render_box`
  取 union）**两者都具备**；
- 判定：IoU（两 box）`< 0.20` 即视为渲染到了计划外的区域 → `F1 FAIL @ layout`；
- 缺 dst_box 或缺 render box → `SKIP`（无法判断放置，缺失由 F8/F10 dangling 处理，
  不是 wrong-area）。

### F3 — abnormal font size

- 证据：layout 目标的 `layout_font_size` **与** 实际画出的 `font_size` 都存在；
- 判定：`drawn / target` 低于 `0.55` 或高于 `1.6` → 异常字号 → `F3 FAIL @ layout`；
- 缺目标字号或缺画出的字号 → `SKIP`。

两者都刻意保守、带 `confidence: "uncertain"`，绝不猜。

---

## 3. 证据链路修复（本节伴随发现）

运行扫描时发现 F1/F3 在真实 corpus 上**静默 SKIP**：布局证据里
`target_bbox`/`target_font_size` 恒为 `None`。根因在 `dual_forensics/snapshot.py`：

- `render_plan_from_model` 生成的 `block_id` 用 pdfminer 内部 `pageid`；
- 而 `block_evidence_per_page` 用调用方 remap 后的 `page_num` 去查 plan。

两个 id 对不上 → layout 证据 lookup 永远 miss → dst_box/font_size 拿不到。

修复：在**构建 plan 之前**先把 `model.pages[*].page_num` remap 成请求的 0-based
page 号，使 plan 的 `block_id` 与调用方一致。这是让 F1/F3 能真正评测的**前置证据
修复**，与 7I-4「detector-first，production-code-last」一致（只改了取证快照，未
动生产渲染管线）。

---

## 4. 验收：coverage 成绩单（5 书 / 33 页）

重跑 `doc/7i4/residual_corpus_scan.py`（原文案属 7I-4-0，新增 §4 coverage 表）。
每格 `状态 evaluated/total`：

| 书 | F1 | F2 | F3 | F4 | F5 | F6 | F8 | F9 | F10 |
|---|---|---|---|---|---|---|---|---|---|
| C book | PASS 7/7 | PASS 5/7 | PASS 7/7 | PASS 7/7 | NM | NM | NM | NM | NM |
| AI for Games | PASS 4/4 | SKIP 0/4 | PASS 4/4 | PASS 4/4 | NM | NM | NM | NM | NM |
| Game Physics | PASS 3/3 | SKIP 0/3 | PASS 3/3 | PASS 3/3 | NM | NM | NM | NM | NM |
| Networking | PASS 5/5 | PASS 1/5 | PASS 5/5 | PASS 5/5 | NM | NM | NM | NM | NM |
| Multiprocessor | PASS 12/12 | PASS 6/12 | PASS 12/12 | FAIL 12/12 | NM | NM | NM | NM | NM |

`NM` = `NOT_MEASURED`（0/页）。**关键点：F5/F6/F8 现在读作「未测量」，不是「0」。

### 4.1 Multif F4 FAIL 的诚实语义

`F4 FAIL 12/12` 的明细（summary.json）：`pass: 11, fail: 1`。那 1 个 fail 正是
Multiprocessor **p300 的 `(cid:129)` 源 PDF parser anomaly** —— 与 7I-3 决策一致，
**故意没有强行恢复**，作为 detector completion 的负例/控制样本：

> 一个 detector 必须能识别真实 source-PDF anomaly，同时把它正确归因到 **parser**，
> 而不是当作 renderer defect。

全 corpus 当前异常分布保持：`F2/F9/F10 = 0`，`F4 = 1 @ p300 @ parser`，
CID `undefined 3`（recover 1 = Θ，keep 2 = bullet）。

### 4.2 SKIP 也是信息

- **AI for Games / Game Physics F2 SKIP 0/**：这些样本页无 code-like block，
  F2 detector 无法评测 —— 这不是「没有误译代码」，而是「无代码可测」。
- **F2 在多数 book 只有部分页可测**（如 C book 5/7）：只有含代码块的页才算
  evaluated，语义正确。

---

## 5. 验证

- 新增 `tests/test_detector_coverage_7i4.py`（11 项）：契约（SKIP/NOT_MEASURED≠0）、
  空页不伪造 PASS、F1 的 PASS/FAIL/SKIP、F3 的 PASS/FAIL/SKIP、跨页 aggregate 只计
  evaluated、`Coverage.to_dict` 往返。
- 回归：`tests/test_dual_forensics_7h1.py` + `tests/test_cid_recovery.py` 全过；
  顶层 forensics/cid/coverage/architecture 300 项全过。
- black 干净（新改文件）、flake8 无新增告警（遗留 `__main__.py` black-dirty 为
  HEAD 既有状态，未扩增无关 churn）。

---

## 6. 状态与下一步

```text
7I-4 Detector Coverage
  ├─ 7I-4-1  contract + F1/F3     ✅ COMPLETE
  ├─ 7I-4-2  F5 / F6              ▶
  └─ 7I-4-3  F8                   ▶
最终：full corpus rerun + 终版成绩单（F1..F10 全部 evaluated 或显式 NOT_MEASURED）
```

完成 7I-4-2（F5 figure/table 脱离文本、F6 caption 位移）与 7I-4-3（F8 截断）后，
上述表格的 `NM` 列会被替换为真实的 PASS/FAIL，整张成绩单才达到
「**0 = 在定义好的检测能力范围内没有发现 bug**」的可信语义。