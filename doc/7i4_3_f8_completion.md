# 7I-4-3 — F8（text truncated）检测器

状态：**COMPLETE（7I-4 步骤三）**

严格沿用 7I-4-1 的 discipline：**Phase A 先 evidence inventory，再写 detector**，
并明确 F8 与 F10/F5 的 exclusion boundary。全程**未改 production pipeline**——
F8 的证据是生产 flow layout 已经产出的 verdict，我们只是把它镜像进 forensic
snapshot（observability，不是 semantics）。

---

## Phase A — F8 Evidence Inventory

### F8 要检测的对象是什么？

**text truncated** = 一个**存在（present）**的块，它的文本内容被排版**剪切（clip）**
了。注意：不是「整块消失」（那是 F10），也不是「图/表脱离文本」（那是 F5）。

### 它存在于哪个 stage？

证据链已经完整存在于 **production flow layout**（7F-7）的 verdict 里：

```text
flow layout  →  render_payload: { overflow, layout_ok, recovery: {reason, decision, steps} }
```

- `overflow=True`：该块放不下 → 需要 recovery；
- `recovery.decision == "clip"`：recovery 决定剪切 → **文本被截断**；
- `recovery.steps`：如 `['SHRINK','CLIP']` / `['WRAP','SHRINK','CLIP']`，
  附 `final_font_size`（可低到 5.0 下限）。

### 现有 snapshot 能看到吗？（关键 gap）

**不能直接看到**。`block_evidence_per_page` 的 layout 证据原本只带
`target_bbox / target_font_size / render_path`，**没有** production 的
`overflow / layout_ok / recovery`。（生产渲染器在 provenance 模式下不实际物理
剪切——它按命令画满——所以「画出的文本长度 == 原文长度」，不能用 render 长度差
来事后测截断；**权威信号就是 layout verdict 本身**。）

→ 这是**表示层 gap**，按 7I-4 纪律修复在 forensic 侧：

- `dual_forensics/snapshot.py`：layout 证据新增镜像
  `overflow / layout_ok / recovery`（读 plan 的 `render_payload`，纯只读）；
- `dual_forensics/diff.py`：`Trace` 新增 `layout_overflow / layout_recovery /
  layout_ok` 三个字段，build_traces 与 aggregate_page_id_direct 都填充。

### Exclusion boundary（避免 cascade）

| defect | 信号 | 本 detector 明确排除 |
|---|---|---|
| **F8** | 块存在，但 layout 要 clip 其文本 | 只对「有画出的 render 证据 + layout clip verdict」的块判 |
| F10 | 整块消失 / 漂移（dangling） | F8 不因「没画出来」而触发 |
| F5 | figure/table 脱离文本（位置关系） | F8 只关心文本剪切，不管位置 |

---

## Phase B — 实现（`dual_forensics/defect.py`）

新增 node-level detector `_detect_f8_text_truncated`：

- `can_evaluate`：`layout_overflow is not None`（有**已定版**的 flow verdict）；
- `FAIL` 条件：`overflow is True` **且** `recovery.decision == "clip"`；
- `FDS`：`layout`（剪切决定由 layout stage 做出）；
- `confidence: "uncertain"`（剪切可能落在空白尾，但具内容行被剪即算截断）；
- 块未被画（render_rows 空）→ 不算 F8（交给 F10/F8 dangling）。

挂进 `_NODE_DETECTORS` + `_NODE_DISPARITY` + `run_defect_detectors`，并被
`coverage_page` 纳入四态账目。

---

## Phase C — 验收：corpus 成绩单（5 书 / 33 页）

`pass/fail/skip/not_measured` 四计数，每格 `状态 evaluated/total`：

| 书 | F8 | F4（对照）|
|---|---|---|
| C book | **FAIL** 7/7 | PASS 7/7 |
| AI for Games | PASS 4/4 | PASS 4/4 |
| Game Physics | **FAIL** 3/3 | PASS 3/3 |
| Networking | **FAIL** 5/5 | PASS 5/5 |
| Multiprocessor | **FAIL** 12/12（pass5/fail7）| FAIL 12/12（pass11/fail1）|

### 这是 Δ：F8 从 NOT_MEASURED 变成 MEASURED，且**真的发现了**

7I-4-0 的成绩单 `F8 = 0` 是不真实的——它是「没测」。7I-4-3 上线后，F8 在
C/GP/Net/MP 上按**生产 layout 自己的 clip verdict** 报出真值：

```
MP   F8 findings 41, 全 @ layout, 例:
      p20_3 ['WRAP','SHRINK','CLIP'] final_font_size=5.0
      p80_1 ['WRAP','SHRINK','CLIP'] final_font_size=5.0
```

这些块确实放到 5pt 仍放不下、layout 决定剪切 —— 一个 7I-4-0「全 0」掩盖掉的
**真实截断信号**。这正是 detector coverage 的价值：**0 ≠ 没 bug，0 = 在能力范围
内没看到**；现在能力扩大了，看到了真 bug。

### F8 与 F4/F6/F10 independence（p300 验收）

```
p300:
  F4  = FAIL（仅 p300_8，cid artifact @ parser）
  F8  = FAIL（p300_8..12，各自 layout clip verdict @ layout）
  F6  = PASS（caption 置于计划位置，无位移）
  F10 = PASS（无 dangling 块）
```

p300 单测（`test_p300_f8_independent_of_f4`）锁定：
同一页上，F4 artifact 的块若没被 clip，**不会**被误判成 F8；真被 clip 的块才进
F8；F8 只报自己该报的 `clipped` 块——**一个 source/parser anomaly 不污染其它
detector**。

### F8 与 F5/F10 边界（单测锁定）

- `test_f8_not_triggered_by_missing_object`：整块未画出 → F8 不触发（归 F10）。
- `test_f8_pass_when_overflow_without_clip`：overflow 但 recovery 是 next_page
  （非 clip）→ 不算 F8 截断。
- `test_f8_skip_when_no_layout_verdict`：没经过 flow layout（无 verdict）→ SKIP。

---

## 验证

- `tests/test_detector_coverage_7i4.py` 扩到 **25 项**，全过（F8 的
  PASS/FAIL/SKIP、clip-vs-next_page、F8≠F10、p300 F8/F4 独立）。
- forensics/cid/coverage **50 项**、顶层相关 **314 项**全过。
- black 干净、flake8 无新增告警。

---

## 当前 milestones

```text
7I-4-0  Expanded Corpus         ✅
7I-4-1  Contract + F1/F3        ✅
7I-4-2  F5/F6                   ✅  (F5 = observability gap, F6 PASS where captions)
7I-4-3  F8                      ✅  (真发现 41 MP clip / C GP Net clip；独立于 F4)
7I-4-4  Full corpus audit       ▶ NEXT
```

### 已暴露的两个 representation gap（供 7I-4-4 / 独立 observability milestone 汇总）

1. **F5**：document model 不产出 figure/table/image 块 → F5 永远 SKIP（能力不足，
   非干净）。需补 figure region 建模（vector drawing / image bbox）。
2. **F8**（本次刚补上）：production flow layout 的 `overflow/recovery` 原不在
   forensic snapshot，现已镜像进 Trace，**F8 从不可测变为可测且真发现**。

下一步 **7I-4-4 full corpus rerun**：在全部 detector（F1–F10）落地后的终版成绩单，
把所有 `evaluated / SKIP / NOT_MEASURED` 四态完整呈现。