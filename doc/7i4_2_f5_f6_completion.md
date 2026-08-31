# 7I-4-2 — F5 / F6 检测器（figure/table 脱离 · caption 位移）

状态：**COMPLETE（7I-4 步骤二）**

按 7I-4-1 冻结的 four-state contract（PASS/FAIL/SKIP/NOT_MEASURED，且
**SKIP/NOT_MEASURED ≠ 0**）实现 **F5（figure/table detached from text）** 与
**F6（caption displaced）**。严格遵循「先 evidence inventory，再写 detector」，
且**未改任何 production pipeline**——只新增 forensic detector 逻辑。

---

## 1. Evidence Inventory（先做这一步）

F5/F6 都依赖「跨块几何对比」（一个块相对页面其他文本的位置），因此实现为
**page-level detector**，而非逐节点。

| F5 expected | F6 expected |
|---|---|
| figure/table/image 块 + 其 dst_box | caption 块 + 其 dst_box |
| + 同页至少一个文本块（paragraph/heading/formula） | + 该 caption 实际画出的 box |
| ——「脱离文本」必须能测出 gap | ——「位移」= 画出位置偏离计划位置 |

### 物理层 / 模型层观察（对 5 书 sample 页实际取证）

| 书 | 是否有 figure/table/image 块 | 是否截图/矢量图形 | caption 块数 | `caption_of` 关系 |
|---|---|---|---|---|
| C book | **0** | p185 drawings=9 | 4 | 0 |
| AI for Games | **0** | — | 0 | 0 |
| Game Physics | **0** | p15 images=0 | 0 | 0 |
| Networking | **0** | — | 2 | 0 |
| Multiprocessor | **0** | p300 drawings=4 | 4 | 0 |

**关键发现**：整个 corpus 的 document model **一个 `figure`/`table`/`image` 块
都没有**，`caption_of` 关系恒为 0。这些书里的图是矢量绘图 / 位图，经 LTChar
流解析后不产生 figure/table 模型块；连 caption 的宿主（figure/table kind）都不
存在。

### 这带来的诚实结论

- **F5 = 无法评测**：没有 figure/table 块，就没有「figure 脱离文本」可测。
  正确结果不是 `0`，而是 **SKIP**（→ 报告里 F5 全 corpus 显示 `SKIP 0/N`）。
  这是**当前 forensic 层建模的 observability gap**，是**数据**，不是缺陷被修掉。
  若未来需要 F5 真正可评，下一步应扩充取证快照以识别 figure/table 区域
  （vector drawing / image bbox），**而不是为了把表填成 0 去改生产管线**。
- **F6 = 可评测但弱化信号**：caption 块存在（可直接测其自身位移），但 captions↔
  figure 的 semantic relation 缺失（`caption_of`=0）。因此 F6 检测「caption 画出
  位置偏离其计划 dst_box」，这是当前证据能支撑的**最严格信号**。

---

## 2. 实现（`dual_forensics/defect.py`）

新增两个 page-level detector（跨块对比，因此不能放进逐节点的 `_NODE_DISPARITY`）：

### `_detect_f5_detached_page`
- 收集 kind ∈ {figure, table, image} 的块 + 同页文本块 box；
- 对每个 float 块：算出 `_box_distance(dst_box, 最近文本块)`，若
  `gap > 4 * 块高` → F5 FAIL @ layout（`confidence: uncertain`）；
- float 块没有被画（无 render box）→ 不算 F5（那是 F10/F8 的 dangling）；
- 页面无 float 块 → **SKIP**（返回 `(findings, 0)`，绝不视为 0）。

### `_detect_f6_caption_displaced_page`
- 收集 kind == "caption" 的块且同时有 dst_box + render box；
- `iou(render_box, dst_box) < 0.30` → F6 FAIL @ layout；
- 页面无 caption 块 → **SKIP**。

两者都并入 `run_defect_detectors`（FAIL 流向后兼容）与 `coverage_page`（四态账目）。

---

## 3. 四项核心单测（`tests/test_detector_coverage_7i4.py`，共 19 项）

- F5：float 邻近文本 → PASS；float 远离文本（gap>4×高）→ FAIL；无 float → SKIP；
  float 未被画 → SKIP。
- F6：caption 在计划位置 → PASS；caption 位移 → FAIL；无 caption → SKIP。
- **Detector independence（关键）**：给一页同时放 caption 邻近 + figure 邻近 +
  **parser-originated `(cid:129)` anomaly（复刻 p300）**，断言：
  - `F5=PASS`、`F6=PASS`（无 cascade / false positive），
  - `F4=FAIL`（仍独立触发）。
- 契约回归：F5/F6 在无证据页是 **SKIP**（不再是 NOT_MEASURED），F7/F8/F9/F10
  仍显式 **NOT_MEASURED**。

---

## 4. 验收：corpus 成绩单（5 书 / 33 页）

| 书 | F5 | F6 | F4（对照）|
|---|---|---|---|
| C book | **SKIP 0/7** | PASS 4/7 | PASS 7/7 |
| AI for Games | **SKIP 0/4** | PASS 1/4 | PASS 4/4 |
| Game Physics | **SKIP 0/3** | SKIP 0/3 | PASS 3/3 |
| Networking | **SKIP 0/5** | PASS 1/5 | PASS 5/5 |
| Multiprocessor | **SKIP 0/12** | PASS 4/12 | FAIL 12/12（pass11/fail1）|

每格含义（aggregate 四个计数）：

- **F5 全 corpus SKIP**：这不是「干净」，而是**能力不足**——pdfminer 流不产出
  figure/table 模型块，F5 无对象可测。报告里每条 `defect_id/F5` 的
  `pages_evaluated=0`。
- **F6 在含 caption 页 PASS**（C / AI / Net / MP 各 1–4 页；Game Physics 无 caption
  故 SKIP）。**含 p300 的那页 F6 也是 PASS** —— 即使该页有 F4 parser anomaly，
  F6 也未被 cascade。（F4 明细 pass=11/fail=1，与 7I-3 决策完全一致。）

corpus 总体缺陷分布不变：`F2/F9/F10=0`，`F4=1 @ p300 @ parser`，CID undefined 3
（recover 1 / keep 2）。

---

## 5. 结论与下一步

7I-4-2 expactly 走向了你预告的方向：

> **F5 显示大量 SKIP——那本身就是数据**：说明当前 forensic 层缺 figure/table
> 建模，下一步该补的是取证可观测性，而不是 PDF pipeline。

这也验证了 detector independence：F5/F6 在同页跑过 F4 anomaly 时**没有**产生
级联误报。

```text
7I-4-0  Expanded Corpus         ✅
7I-4-1  Contract + F1/F3        ✅
7I-4-2  F5/F6                   ✅ COMPLETE
7I-4-3  F8（text truncated）     ▶ NEXT
7I-4-4  Full corpus audit
```

下一步 **7I-4-3（F8 text truncated）**：证据契约需 `source/translated_text`
长度 vs 渲染文本长度 / 画框与文本 bbox 覆盖率，并从「SFKIP 还是 measured」给出
诚实的四态账目。