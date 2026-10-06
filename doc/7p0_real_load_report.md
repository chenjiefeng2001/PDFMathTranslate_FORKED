# 7P0 — 真实翻译负载测量报告（mp2e 前 50 页）

跑的是当前 HEAD 的生产链路，不是回放、不是 mock。翻译用 OpenRouter 托管的
**NVIDIA Nemotron 3 Ultra（free）**，经 opencode 网关的 openrouter provider。

- 语料：`tests/file/The Art of Multiprocessor Programming, 2e.pdf`（562 页，MIT Press）
- 页范围：**1-50**（按要求只跑前 50 页，不跑全书）
- 引擎：`--parse-engine magicpdf`（MinerU，OCR off）
- 服务：`--service opencode` → `openrouter/nvidia/nemotron-3-ultra-550b-a55b:free`
- HEAD：`8db5928`（工作区干净）
- 复现：`python doc/7p0_real_load_run.py --out <dir> --pages 1-50 --thread 4`
- 度量：`python doc/7p0_real_load_analyze.py --run <dir>`
- 证据：`doc/7p0-load50/`（`run-config.json` / `run.log` / `analysis.json` /
  `render_plan.json` / `defect-evidence.json`）

---

## 0. 先说工具本身的问题：既有"合格"判定是空的

`doc/7n9-mp2e-fix3/audit/summary.json` 写着 `qualification: PASS`。同一份文件里：

```
pages: 562            page_grades: {A:0, B:0, C:0, D:0}     <- 562 页，0 页被评分
total_events: 11467   rule_results: 0                        <- 11467 事件，0 条规则
rules: []                                                  <- 规则列表是空的
defect-ledger.csv       只有表头
```

**没有一条规则被执行过**，那个 PASS 不代表任何质量结论。所以本报告不复用它的判定，
全部指标用 `doc/7p0_real_load_analyze.py` 从产物重新算。

这也是我做这次调查的第一个发现：**当前没有可信的自动合格闸门**，下面的两个缺陷
之所以能出货，正是因为没有东西会拦住它们。

---

## 1. 总览

| 指标 | 值 |
|---|---|
| 页 / 块 | 50 / 621（均 12.4 块/页，峰值 201） |
| 墙钟 | **5341s = 89 min**（107s/页） |
| 翻译产出 | 585 块有译文，覆盖率 94.2% |
| 真漏译 | **0** |
| 越界字形 | **0**（独立复算） |
| 叠印（overlap） | 渲染器自报 11 页；独立复算 **41 页 / 4170 处** |
| 内容截断 | **1 块丢 59%** |
| 瞬时失败 | 1 次（重试即成功） |

翻译质量这一层是干净的。两个真实缺陷都在**解析页归属**和**渲染排版**。

---

## 2. 缺陷 P1（严重）：跨 8 页的目录被压进 1 页

### 现象

源 PDF 的目录在 **第 7~14 页**（8 页，每页约 3600 字符）：

```
pdf page  7: chars=3153  markers=['Contents', 'Preface']
pdf page  8: chars=3620
pdf page  9: chars=3551  markers=['compareAndSet']        <- 源第 9 页
pdf page 11: chars=3763  markers=['BucketList','LockFreeHashSet']  <- 源第 11 页
...
pdf page 14: chars=3234
```

而 render_plan 的页分布是：

```
blocks per page (0-based), pages 0..16:
[5, 2, 1, 7, 18, 8, 201, 1, 1, 1, 1, 1, 1, 1, 7, 8, 2]
                      ^^^ 201      ^^^^^^^ 后面 7 页各只剩 1 块
```

**201 个块全落在 render 第 6 页**，第 7~13 页各只剩 1 块。

### 根因（在 MinerU 侧，不在我们）

MinerU 原始解析输出（`*_magicpdf.json`）第 7 页元素里只有 **2 个块**，但那两个块
的 JSON 同时包含 `compareAndSet`（源第 9 页的内容）和 `BucketList`（源第 11 页的
内容）。即**版面模型把 8 页目录合并成了 1 页**。

我们的 `document.json` 只是忠实展开：2 个原始块 → 201 个块，仍然全挂 page 6。
渲染器随后按 `src_box` 把它们画到同一页上。

### 后果（实测）

- **87 个块共享 35 个重复的 `src_box` 顶部 y**，最多 **5 个块 y 完全相同** ——
  也就是字面上叠在同一个位置。y 跨度 56~574pt，即它们来自 8 个不同源页的同一批
  行位置。
- `references` 块的章节号横跨 **13 个章节**（3,4,5,8 … 15,16,17,20），
  而它们应该在连续 8 个目录页上。
- 有一个块的 `text` 本身就是**多个源页熔接**的：
  `"Preface .xv\nAcknowledgmentsxix\nSuggested ways to teach...\nCHAPTER 1 Introduction .1\n1.1 Shared objects and synchronization3\n1.2 A fable6"`
- 该页成为全篇最差页：渲染器自报 **569 处叠印**，我独立复算 **708 处**。
- 目录条目在成品里查不到：`compareAndSet`、`BucketList`、`操作119`、`类.318`
  在 PDF 第 7 页上**原文和译文都不存在**（既被擦掉也没画出来，或被压在同位置
  互相覆盖）。

### 影响面

凡是**多页目录**的书都会中招 —— 教材、技术书、论文合集几乎都是。这不是 mp2e 特有。
commit `3d40300`/`63a5555` 处理的正是目录渲染（"contents page splitting"），但那次
只改了绘制侧，没解决"多个源页的条目被指派到同一个 render 页"这个更上游的问题。

---

## 3. 缺陷 R2（中）：字号触底后静默截断译文

渲染器日志：

```
line too wide for the page even at the size floor; 145 of 244 characters dropped (wrapped):
'互斥可能是多处理器编程中最常见的协调形式。本章介绍通过读写共享内存工作的经典互斥算法。…'
```

独立复核块 `p38_2`（abstract，render 第 38 页 = PDF 第 39 页）：

| 项 | 值 |
|---|---|
| 译文长度 | 244 字 |
| 实际落到页面 | **99 字（41%）** |
| 丢失 | **145 字（59%）** |
| 尾部 6-gram 命中 | **0 / 7** |
| `src_box` 高 | 141pt |
| 字号 | 7.65pt（已到下限） |

也就是说这段落的后半段**根本不在成品里**。它有告警（不是完全静默），但产物照样
交付 —— 一段摘要丢掉 59% 内容，对用户是实质损失。

伴随信号：**168 个块分布在 38 页上字号 ≤ 7.7pt**，即触底不是孤例。CJK 比英文宽，
`translate_refit` 不断缩字号，缩到下限仍放不下就截断。当前策略是"宁可丢内容也不
溢出"，方向反了。

---

## 4. 吞吐：`--thread 4` 完全没生效

### 实测

- 串行服务延迟（常驻 `opencode serve`，5 段不同文本）：**均值 9.7s**（7.7 / 8.9 /
  9.5 / 10.1 / 12.2）
- 本次运行：585 块，翻译窗口 ~5280s → **9.0s/块**
- 比值 9.0 / 9.7 = **有效并发 1.07×**，也就是基本串行

若 4 线程真的生效，同样 585 块应在 ~24 分钟内完成，而不是 89 分钟。

### 根因（已核实）

`pdf2zh/v3/document_model.py` 的 `translate_document()` 是**纯串行 for 循环**：

```python
for page in model.pages:
    for i, block in enumerate(page.blocks):
        unit = block_translation_unit(block, translate_fn, model=model)
```

`pdf2zh/magicpdf_cli.py:1147` 直接调用它。线程池在
`pdf2zh/v3/paragraph_batch.py`（`ThreadPoolExecutor(max_workers=workers)`），
而它只被 **legacy `converter.py`** 引用 —— magicpdf 路径完全不经过。

所以 `--thread` 对 magicpdf 引擎是个**无效参数**。这是白捡的 3~4 倍吞吐。

### 另外两点负载事实

- **常驻服务是必需的**：CLI 冷启动实测 **16~21s/次**，常驻 `opencode serve` 后
  9.7s/次。`OpenCodeTranslator` 的 `OPENCODE_SERVER_URL` 正是为此存在。
- **`:free` 变体可用但要限速**：585 次调用只出现 **1 次**瞬时失败（重试成功），
  说明 OpenRouter free 层在这个量级下够用；但它有 20 req/min 量级上限，
  开 4 线程后需要观察 429。

---

## 5. 翻译质量：这一层是干净的

| 指标 | 值 | 说明 |
|---|---|---|
| 有译文的块 | 585 / 621 | 其余 36 块**原文也为空**（图/占位块） |
| 原文非空却空译文 | **0** | |
| 身份翻译（译文==原文） | 26 | 全部**本就不该译**：formula 16 / code 6 / references 2 / list 1 / heading 1 |
| 可疑漏译 | **0** | |
| 输出/原文字符比 | 0.396 | CJK 压缩比，正常 |

第一轮我曾把 `empty_translation = 36` 报成漏译，那是量法错 —— 没要求原文非空。
修正后为 **0**。同理 26 个身份翻译若不区分类型，会被误判成 26 处漏译。

译文质量抽样（Nemotron 3 Ultra）：

```
"The Art of Multiprocessor Programming examines the fundamental concurrency issues…"
  -> 《多处理器编程的艺术》探讨了将多个处理器组合成单一系统时出现的基本并发问题。
"Linearizability is the correctness condition that each operation appears to take effect instantaneously…"
  -> 线性一致性是一种正确性条件，即每个操作看起来都在其调用和响应之间瞬间生效。
```

术语准确（互斥锁 / 无锁自由性 / 面包店算法 / 线性一致性 / 阿姆达尔定律都对）。

---

## 6. 叠印指标：渲染器自报与独立复算不一致

| 来源 | 叠印页数 | 总处数 | 最差页 |
|---|---|---|---|
| 渲染器日志 | 11 | — | 569 |
| 我的独立复算（30% 面积重叠阈值） | **41** | **4170** | 708 |

两者都把 render 第 6 页（= PDF 第 7 页，即 P1 那个目录折叠页）列为最差，且数量级
一致（569 vs 708）。差异来自阈值与统计口径（我按 span 两两配对、要求交集 > 较小
span 面积的 30%）。

**41 vs 11 这个差距本身是个问题**：渲染器自报的"叠印页 11"会让人以为只有 11 页
受影响，实际 41 页。它目前只是 warning、不影响退出码，也不计分（见第 0 节）。

我确认过我的检测不是把"目录叠画在保留原文上"这种设计行为算进去 —— 渲染器自己把
它描述为"叠影/重影"，是缺陷。

---

## 7. 建议的优先级

| 级别 | 问题 | 依据 | 备注 |
|---|---|---|---|
| **P0** | 多页目录被合并到单个 render 页 | 8 页目录 → 201 块 / 1 页；87 块 y 碰撞 | 根因在 MinerU，但**我们必须兜住**：可以在解析后按 `src_box` 覆盖率或块数异常（如单页 >100 块）触发重解析/降级到 legacy |
| **P0** | 没有可信的合格闸门 | 既有审计 0 条规则 | 上面两个缺陷都能出货，就是因为这个 |
| **P1** | 字号触底静默截断 | p38_2 丢 59% | 应改为硬失败或溢出到后续空白，而不是丢内容 |
| **P1** | `--thread` 对 magicpdf 无效 | 有效并发 1.07× | 改 `translate_document` 走 `paragraph_batch` 的线程池，约 3~4 倍 |
| **P2** | 叠印自报口径偏窄 | 11 页 vs 实测 41 页 | 统一口径，否则告警会误导 |
| **P2** | 168 块触字号下限 | 跨 38 页 | 触底即该换策略（缩排/两栏/保留原文），不该继续缩 |

---

## 8. 本次调查中被推翻的三个中间结论

留档以免后续重复踩：

1. **"91.6% 的译文没进 PDF"** —— 量法错。要求"连续 8 字命中"，但 PDF 提取顺序
   会把原文插进译文中间、换行也会切断连续片段。CJK 提取本身完全正常（PDF 第 7 页
   855 个汉字）。改为 4-gram 多数命中 + 页码 0 基→1 基修正后，真实缺失率 **5.5%**，
   且集中在 P1 的目录折叠页。
2. **"36 块空译文 / 26 块漏译"** —— 量法错。没要求原文非空、没区分"本就不该译"。
   修正后 **0 和 0**。
3. **"叠印 4170 处"** —— 数字本身可信（与渲染器独立指向同一页），但一度被我误当作
   渲染器漏报；实际是两者口径不同，**方向一致**。

---

## 9. 尚未验证 / 遗留

- **只跑了前 50 页**，P1 之外的单页块数分布（峰值 18）看起来正常，但 51~562 页
  未测，不能外推"全书只有这一处折叠"。
- **未做对照实验**。本报告没有"把最近 6 个 fix 回退后再跑一遍"的 before/after，
  所以**不能回答"最近的修复到底带来了多少改善"**。要做需要 git worktree + 第二次
  89 分钟运行。
- **并发修复后的实际加速未测**，3~4 倍是从有效并发 1.07× 反推的推算值，不是实测。
- 未验证 `:free` 变体在更高并发下的 429 行为。
- 渲染器 `fixup(shift=5/overflow=0)` 中的 `shift_down` 5 处未单独核查是否正确。
