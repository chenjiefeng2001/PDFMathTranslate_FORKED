# 7I-4-0 — Expanded Residual Corpus Scan（post-7I-3）

日期：2026-08-31 · 前置：7I-3 COMPLETE（stage-aware FDS attribution + font-aware CID
recovery）· 对象：5 书 Expanded Residual Corpus · 方法：in-pipeline provenance
（恒等翻译 + 生产 renderer + ID-direct diff + `run_defect_detectors`）+ **undefined
CID 分类**（`recover_unicode` 判定 recoverable/unrecoverable）。

工具：`doc/7i4/residual_corpus_scan.py`，输出 `doc/7i4-corpus-scan/{summary.json, report.md}`。

## 1. 语料与页集合

| 书 | 源文件 | 样本页 | 性质 |
|----|--------|--------|------|
| C book | `pdf2zh_files/Large-Scale C Volume I..._2c3bdba4.pdf` | 62 65 69 75 185 186 187 | code 密集（7H F2 最重的书） |
| AI for Games | `pdf2zh_files/AI for Games..._4ca3f7b5.pdf` | 0 10 20 30 40 | 正文+图注 |
| Game Physics | `pdf2zh_files/Game Physics David H. Eberly...z-lib.sk.pdf` | 0 15 30 45 | 数学/物理 |
| Networking | `pdf2zh_files/Networking and Online Games..._1eed56a6.pdf` | 0 12 24 36 48 | 正文 |
| Multiprocessor 2e | `tests/file/The Art of Multiprocessor Programming, 2e.pdf` | 0 5 8 12 20 40 80 120 200 300 400 500 550 | 7I-1 解除 model-hang 后新纳入 |

页集合沿用 7I-0 manifest（四书）+ 7I-1/3 的 Multiprocessor 13 页样本 → 同信号可比。

## 2. Defect & FDS 分布（5 书 / 33 页 / 489 块）

```text
                 pages  blocks  present  dangling  F1 F2 F3 F4 F5 F6 F8 F9 F10  preserved_v
C book             7      146      146      0       0  0  0  0  0  0  0  0   0        0
AI for Games       5       18       18      0       0  0  0  0  0  0  0  0   0        0
Game Physics       4       36       36      0       0  0  0  0  0  0  0  0   0        0
Networking         5       66       66      0       0  0  0  0  0  0  0  0   0        0
Multiprocessor 2e 13      223      223      0       0  0  0  1  0  0  0  0   0        0
──────────────────────────────────────────────────────────────────────────────────────
合计              33      489      489      0       0  0  0  1  0  0  0  0   0        0
```

- **全世界唯一 finding**：`F4 × 1`，`first_divergence = parser`（Multiprocessor p300）。
- FDS 直方图：`parser=1`，其余 stage 全 0。
- `present 489/489`、`dangling=0`、`stray=0`、`preserved_violation=0`。

## 3. CID artifacts（undefined CID 分类）

```text
                 undefined  recovered Unicode  preserved placeholder
C book               0              0                    0
AI for Games         0              0                    0
Game Physics         0              0                    0
Networking           0              0                    0
Multiprocessor 2e    3              1                    2
```

Multiprocessor p300 的三个 undefined CID（整 corpus 仅此 3 个）：

```text
recoverable : GLBJKM+MTMI    cid 3  →  'Θ'   （identity→CFF charset Theta1→AGL；已还原）
unrecoverable: GLBJJG+Times-Roman cid 129     （×2，列表项目符号；无可靠 code→glyph 证据）
preserved placeholder: 2（'(cid:129)' 显式留在 parser 文本 + FDS=parser）
```

**7I-4-0 要回答的问题** —— `cid:129`（bullet）是孤立的 source-PDF encoding anomaly，
还是复杂 PDF corpus 上更大的 parser-decoding 类别？

**答案：孤立的。** 5 书 / 33 页 / 489 块中，除 Multiprocessor p300 的 3 个 undefined
CID 外，**其余 32 页 / 486 块一个 CID artifact 都没有**。`(cid:129)` 是这一本书这一页
的字体编码缺口，不存在「更大的 parser-decoding 类别」需要新工具去解码。

## 4. CID recovery 的 corpus-scale 验证（比 F4 归零更重要的结论）

Corpus 层面实证了 7I-3 的安全边界：

```text
undefined CID
  ├─ 有可靠证据（1 个）→ 还原为唯一 Unicode（Θ）        no guess，有证据才恢复
  └─ 无可靠证据（2 个）→ 保留显式占位符 + parser anomaly  no guess，宁可留 artifact
```

**没有出现**「为了 defect count 归零而猜测 bullet」的 shortcut；也**没有新 corpus
evidence 表明需要 universal CID decoder**。7I-3 §8 的决策（扩大 AGL/字体格式支持应等
corpus 再给出证据）被本扫描支持：当前没有这样的证据。

## 5. 诚实边界（沿 7I-0 §5.3）

- F1/F3/F5/F6/F8 检测器在 7H/7I 各轮**仍未实现可靠信号** —— 本报告对这些类别的
  「0」是**未测量**，不是**已证实干净**。corpus 的「干净」目前只对 F2/F4/F9/F10
  （本轮有可靠检测器）成立。
- CID 统计来自样本页（33/上万页）；非样本页若存在同类编码缺口不在本测量内。

## 6. 7I-4 主题建议（由数据决定，非预设）

数据指向的中心事实：**生产 corpus 已无可复现的 fidelity 缺陷（除一个有意的、
归因正确、保留占位的 F4），且 CID recovery 安全边界已实证。** 与其「修最后一个
F4 / 万能 CID decoder」，data-driven 的下一个最有价值的缺口是：

> **补全 F1/F3/F5/F6/F8 检测器**，把 residual「未测量的 0」升级为「已证实干净」。
> 这是唯一让整张 defect 表从「假设干净」变成「证明干净」的剩余工程量；
> 也是 7I-0 §5.3 早就标注、7I-3 之后仍未覆盖的一格。

明确**不做**：

```text
F4 × 1（parser, insufficient evidence）
    → 继续保留（不强制恢复 bullet）
    → 不等同于管线的 fidelity defect
CID recovery
    → 维持现状，等 corpus 给出新证据再扩
```

若后续真的想给 `(cid:129)` 一个语义，应走「渲染层 glyph 身份确认（像素/OCR）」作为
独立机制，而不是扩展 AGL 推理 —— 但那需要新的证据/需求驱动。

## 7. 状态

7I-4-0 **✅ COMPLETE**（Expanded Residual Corpus Scan：0 缺陷 + 隔离 CID anomaly +
CID 恢复边界实证）。
7I-4 主题：待选 —— 推荐 A「补全 F1/F3/F5/F6/F8 检测器，升级 residual 为 proven clean」。