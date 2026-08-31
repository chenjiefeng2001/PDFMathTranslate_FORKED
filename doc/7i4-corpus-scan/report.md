# 7I-4-0 — Expanded Residual Corpus Scan（post-7I-3）

日期：2026-08-31 · 方法：in-pipeline provenance（恒等翻译 + 生产 renderer + ID-direct diff + run_defect_detectors）+ font-aware CID recovery 分类（7I-3B）。

## 1. Defect & FDS 分布（按书）

| 书 | 块 | present | dangling | stray | F1 | F2 | F3 | F4 | F5 | F6 | F8 | F9 | F10 | preserved_v |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| C book | 146 | 146 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| AI for Games | 18 | 18 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| Game Physics | 36 | 36 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| Networking | 66 | 66 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| Multiprocessor 2e | 223 | 223 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 | 0 | 0 | 0 |

## 2. FDS 直方图

- **C book**: 全部 0
- **AI for Games**: 全部 0
- **Game Physics**: 全部 0
- **Networking**: 全部 0
- **Multiprocessor 2e**: parser=1

## 3. CID artifacts（undefined CID 分类）

| 书 | undefined | recovered Unicode | preserved placeholder |
|---|---|---|---|
| C book | 0 | 0 | 0 |
| AI for Games | 0 | 0 | 0 |
| Game Physics | 0 | 0 | 0 |
| Networking | 0 | 0 | 0 |
| Multiprocessor 2e | 3 | 1 | 2 |

### C book

### AI for Games

### Game Physics

### Networking

### Multiprocessor 2e
- recoverable 例子：`GLBJKM+MTMI 3 → 'Θ'`
- unrecoverable 例子：`GLBJJG+Times-Roman 129`; `GLBJJG+Times-Roman 129`

## 4. Detector Coverage（7I-4 contract）

每格：`状态 已评测页面/总页面`（pass/fail/skip/not_measured）。SKIP/NOT_MEASURED **不等于 0**——表示该 defect 未被能力覆盖。

| 书 | F1 | F2 | F3 | F4 | F5 | F6 | F8 | F9 | F10 |
|---|---|---|---|---|---|---|---|---|---|
| C book | PASS 7/7 | PASS 5/7 | PASS 7/7 | PASS 7/7 | SKIP 0/7 | PASS 4/7 | PASS 7/7 | PASS 7/7 | PASS 7/7 |
| AI for Games | PASS 4/4 | SKIP 0/4 | PASS 4/4 | PASS 4/4 | SKIP 0/4 | PASS 1/4 | PASS 4/4 | PASS 4/4 | PASS 4/4 |
| Game Physics | PASS 3/3 | SKIP 0/3 | PASS 3/3 | PASS 3/3 | SKIP 0/3 | SKIP 0/3 | PASS 3/3 | PASS 3/3 | PASS 3/3 |
| Networking | PASS 5/5 | PASS 1/5 | PASS 5/5 | PASS 5/5 | SKIP 0/5 | PASS 1/5 | PASS 5/5 | PASS 5/5 | PASS 5/5 |
| Multiprocessor 2e | PASS 12/12 | PASS 6/12 | PASS 12/12 | FAIL 12/12 | SKIP 0/12 | PASS 4/12 | PASS 12/12 | PASS 12/12 | PASS 12/12 |
