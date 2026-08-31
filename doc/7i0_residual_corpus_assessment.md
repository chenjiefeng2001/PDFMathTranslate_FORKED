# 7I-0 — Residual Corpus Assessment（7H-after）

日期：2026-08-31 · 基线：`3b01ad3`（7H COMPLETE）· 方法：**in-pipeline provenance render**
（恒等翻译 + 生产 renderer `render_plan_to_pdf(provenance=True)` + ID-direct diff）

本报告回答一个问题，且只回答这一个：

> **在 7H 的 stage-faithful 修复落地之后，真实 Dual corpus 上"现在还剩什么"的客观
> 分布是什么样的？**

工具、方法、语义全部沿用 7H（`python -m dual_forensics`），唯一区别是**渲染证据来源**：
7H-1 读的是仓库内既有外部 BabelDOC Dual 的译文页（历史离线产物，含不可回溯的坏 float /
被中文化的 code）；这里用本管线对源页重渲染（恒等翻译 + `provenance=True`）取 render
证据，做 ID-direct 匹配——测的是**「本管线再造该源页时，缺陷是否还会出现」**。

---

## 1. 方法学边界（诚实声明，沿用 7H 报告）

- 本测量对**管线内渲染**给出可直接证实的结果（source_node_id → render_object_ref →
  present / absent / stray / moved），不再有 geometry-match 的 UNCERTAIN；
- 仓库内既有 **外部 BabelDOC Dual 不可被注入 provenance**，无法回溯修复其中历史
  坏 float（F9）与已被中文化的 code（F2）。in-pipeline 结果与历史 dual 信号**不可
  直接相减**——前者是「本管线再生成」，后者是「遗留产物快照」；
- 恒等翻译：本测量只证明语义/model 层定型正确（code/formula 决不会进 translator），
  并不证明真实翻译文本质量；后者需生产 translator 的长任务验证，超出 7I-0 范围。

## 2. 语料与页集合

| 书 | 源文件 | 本报告页集合 | 性质 |
|----|--------|--------------|------|
| C 书（Large-Scale C I） | `Large-Scale C Volume I..._2c3bdba4.pdf` | 62 65 69 75 185 186 187 | F2 最重（code 密集） |
| AI for Games | `AI for Games and Animation..._4ca3f7b5.pdf` | 0 10 20 30 40 | 正文+图注 |
| Game Physics | `Game Physics David H. Eberly...z-lib.sk.pdf` | 0 15 30 45 | 数学/物理 |
| Networking | `Networking and Online Games..._1eed56a6.pdf` | 0 12 24 36 48 | 正文 |

`Art of Multiprocessor Programming, 2e`：本地 corpus 无其 PDF（7H-1 已记录为 corpus
**qualification failure**，且 `build_document_model` 对其 infinite-hang）→ 无法进入
in-pipeline 测量，作为独立遗留项单人维护（见 §5）。

## 3. 7H-after residual defect distribution

每页跑 `capture_source_chain` + `aggregate_page_id_direct` + `run_defect_detectors` + MuPDF
`content_stream_anomaly`，输出 `doc/7i{0}-*/summary.json`。

### 3.1 全部四书汇总（in-pipeline，ID-direct）

```text
                pages  blocks  dangling  F2  F4  F9-in-pipeline  preserved-violation
C 书              7      150       0     0   0        0              0
AI for Games      5       18       0     0   0        0              0
Game Physics      4       36       0     0   0        0              0
Networking        5       66       0     0   0        0              0
──────────────────────────────────────────────────────────────────────
合计             21      270       0     0   0        0              0
```

- **FDS 直方图：全 stage 归零**（source/parser/model/translation/layout/render/pdf 均 0）。
- **dominant class：无**——in-pipeline 渲染不产生 F2/F9/F10（confirmed-absent）缺陷。
- **preserved-violation = 0**：每条 `code/formula/filename/identifier` 块翻译状态都是
  `preserved`，无一条落入 translator。

### 3.2 C 书代码页（7H-2C 的直接证据）

in-pipeline 证据下，代码页 185/186/187 的 kind 定型与翻译状态：

```text
page 185: code=5  formula=10  filename=1  identifier=1  paragraph=6   → 全部 code preserved
page 186: code=5  formula=7   filename=2  identifier=1  paragraph=5   → 全部 code preserved
page 187: code=9  formula=8   identifier=1               paragraph=3   → 全部 code preserved
```

对照 7H-before（历史报告 `doc/7h1_dual_forensics_report.md`）：C 书代码块被逐行中文化
`F2:14 @ first_divergence=model`。7H-after 在 model 层把这些块定型为 `code`，KEEP_KINDS
使它们 `preserved`，不再进 translator——**FDS = model 的 F2 在管线上已不可复现**。

## 4. 7H-before → 7H-after 对比（同类信号，非直接相减）

| 信号 | 7H-before（外部 dual 快照） | 7H-after（in-pipeline） | 说明 |
|------|--------------------|-----------------|------|
| F2 code 误译 | 14（C 书，FDS=model） | **0** | 7H-2C semantic 角色仲裁 |
| F9 MuPDF emitter 坏 float | 10（外部 dual 页流） | **0（in-pipeline）** | 7H-2B `pdf_num` 定点契约 |
| F10 draw-lost UNCERTAIN | 92（geometry match-gap） | **0 confirmed / 0 absent** | 7H-2A ID-direct provenance |
| AI 书干净页 | F10:2（match-gap 噪声） | **0** | 干净页保持干净 |

关键：**7H 修复没有「只是测试变绿」——真实 corpus 上缺陷页现在归零。**

## 5. 剩余项（真正残留的东西）与 7I 主题建议

7H-after residual 分布显示：**本管线 stage-faithful 修复已使真实 corpus 干净**。余下的
不是「还有哪个 fidelity 缺陷」，而是三类工程边界：

1. **遗留既有 Dual 不可回溯**：仓库里 BabelDOC 生成的历史 dual 仍含坏 float/中文化 code，
   只能重跑本管线再生成新 dual，无法就地修复。→ 若目标是"存量文件修复"，需要重渲染基础设施，
   不属于 affinity/layout/fidelity 本身。
2. **`Art of Multiprocessor Programming` 的 model-build infinite-hang**（独立系统级阻塞、
   7G 遗留）：不能仅凭 7H 关闭其 qualification failure；是 7G model-build termination
   范畴，需 7G 侧 terminate/robustness 工作修复后重跑本 corpus 复测。
3. **F1/F3/F5/F6/F8 检测器仍为占位**：本轮检测器只保证 F2/F4/F9/F10 有可靠信号，
   其余类别 taxonomy 覆盖不足 → residual 分布对这几类的"0"是**未测量**而非**已证实干净**。

### 7I 候选主题（按证据优先级，数据而非预设）

```text
A. 7I-corpus：给"已有既有 dual 的用户"提供 in-pipeline 存量重渲染路径，
   让 7H 关闭的 FDS 落地到产出而不是仅供诊断报告。
   ← 直接消费 7I-0 的"clean in-pipeline"结论，把 clean 变成交付。

B. 7G-model-terminate：修 build_document_model 对复杂 PDF（Multiprocessor 书）的
   infinite-hang，解除唯一剩余 corpus qualification blocker。
   ← 7H 之外唯一明确的系统性阻塞；由 7I-0 §5.2 支撑，而非新臆测的 layout defect。

C. 7I-detectors：补全 F1/F3/F5/F6/F8 检测器，把 residual 的"未测量 0"升级为
   "已证实干净"。
   ← 巩固诊断完整性，data-driven 收尾 7H 语义。
```

### 结论

> **7H-after：in-pipeline provenance 测量下，真实 corpus（C/AI/GP/Networking）不再产生
> F2/F9/F10 fidelity 缺陷（21 页 / 270 块 / 0 findings / 0 preserved-violation）。残余
> 问题不在 fidelity 层，而是既有 dual 存量重渲染（A）、Multiprocessor 书 model-build
> infinite-hang（B）、以及 F1/F3/F5/F6/F8 检测器占位（C）。7I 主题应由这三项数据决定，
> 而非预设继续加 layout rule。**