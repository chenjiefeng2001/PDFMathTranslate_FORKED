# 7K-2 — Release Readiness Statement

**Status: COMPLETE · prepared for release on the pinned stack**
(reproduce: `python doc/7j4/release_gate.py --smoke`)

## 1. 发布状态

当前 pinned stack（babeldoc 0.6.4 / pymupdf 1.28.2 / Python 3.13）上，
7I–7K 全部 forensic milestone 收口。唯一 measured residual 是 **F4 × 1**
（Multiprocessor p300，FDS=parser，source-PDF encoding anomaly，故意保留为
negative control）。F5 / F7 / Annotation 是三类**已明确记录、有解锁条件**
的边界，不是 defect。

## 2. Release gates — 状态

| gate | 内容 | 状态 |
|------|------|------|
| Latch tests | 7I-7 / 7J-3A / 7I-4 / 7I-6A-B / 7I-3 / 7I-5B / 7I-6C / 7J-3C（95 项） | 绿 |
| Corpus baseline | residual=1, F4=1@parser, F5 SKIP 31, F7 NM 31, F8/F9/F10 PASS 31 | 绿 |
| Historical capture | Case A p3 NUL=60 / Case B p157 NUL=1 仍被 F9 sensor 捕获 | 绿 |
| Fresh smoke | 四特殊字符保留 · NUL=0 · 无 token 泄漏 · cjk_delta=0 | 绿 |
| Determinism | corpus baseline 复跑与已提交 summary **逐字节一致** | 绿 |

（最终 gate report：`doc/7j4/gate_report.json` — 随本次 rerun 刷新）

## 3. Known limitations（release notes 素材）

* **Annotation 不保留**（7K-1）：源 PDF 高亮/批注/链接在翻译输出中不存在。
  若产品 contract 要求保留，需先定义 translation-aware annotation
  relocation；当前**明确 unsupported**，建议 release note 声明。
* **F4 × 1**（7I-2/7I-3）：Multiprocessor p300 headings 文本层含 `(cid:129)`
  占位符而非 `•`；source-originated，显式可见而非错误字符，风险 LOW。
* **F5 SKIP / F7 NOT_MEASURED**：测量边界（figure 语义块、real-translation
  三元组），不是质量 PASS；四态 contract 防止误读。
* **历史 F9 artifact**（7J-3）：当前栈不可复现，detector 作长期回归防护；
  **任何依赖升级后必须重跑 release gate**。

## 4. 不引入项（有意保持）

URL 专用断词、更低 font floor、renderer 特判、OCR 模型更换、MuPDF 升级、
为 F5/F7/Annotation 强行造 detector —— 均无证据支持，保持冻结。

## 5. 发布检查单

```text
1. python doc/7j4/release_gate.py --smoke        # 全绿
2. git status clean                               # 无未提交产物
3. 更新 release notes（含 section 3 的限制声明）
4. 记录 pinned 版本（doc/7j4/dependencies.md）与 gate report 一起归档
```