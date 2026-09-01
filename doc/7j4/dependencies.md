# 7J-4D — Dependency / Runtime Freeze

7J-3 证明了一个核心事实：**版本变化本身会改变缺陷是否出现**（六月 artifact
的 NUL/GBK-mojibake 在当前 pinned stack 上不可复现，根因是 pymupdf 的
subset/ToUnicode 行为在版本间变化）。因此 release 主张必须绑定**确切版本**，
而不是"这个 bug 已经永久修好"。

## 冻结版本（2026-08-31 取证时刻）

| 组件 | 版本 | 来源 |
| --- | --- | --- |
| pdf2zh | 1.9.15 | `pyproject.toml` version |
| Python | 3.13.1 (CPython, Windows 64-bit) | `requires-python = ">=3.11,<3.14"` |
| BabelDOC | 0.6.4 | `pyproject.toml: babeldoc>=0.6.4` |
| PyMuPDF / MuPDF | 1.28.2 | `pyproject.toml: pymupdf>=1.26.7`（依赖覆写） |
| pdfminer.six | 20250506 | `pyproject.toml: pdfminer-six>=20250416,<20250507` |
| Pillow | 11.3.0 | — |
| doclayout_yolo | 0.0.2b1 | 生产 `_build_doclayout_model`（OCR/layout 路径） |
| ultralytics | 8.4.120 | doclayout 推理 |
| magic-pdf | 1.3.12 | MinerU 引擎路径（`vendor/MinerU`） |
| torch / onnxruntime-gpu | 2.13.0+cu126 / 1.29.0 | OCR/MinerU 推理 |
| transformers | 4.57.6 | — |

> 离线取证/qualification（7J-3B/C/D）均以 `doc_layout_model=None` 运行，
> 即**不经过** doclayout_yolo/ultralytics；但生产适配器会构建真实 layout
> model（`pdf2zh/babeldoc_adapter.py::_build_doclayout_model`），因此 OCR
> 依赖版本同样列入冻结。

## Pin 语义

- `>=` 区间内的**已实测版本**才是主张基线的版本；上游发新小版本后，必须先
  重跑 `doc/7j4/release_gate.py`（含 `--smoke`）才能维持"当前 pinned stack
  上不可复现"的结论。
- 已知历史版本行为差异（教训）：
  - **pymupdf <1.28.x 的 `subset_fonts`** 曾产生 GID 空间 `/ToUnicode` 与
    content-stream CID 错位 → 文本层 NUL/GBK-mojibake（Case A，六月 artifact）；
  - 该差异是**检测基线的一部分**：`doc/7i4-corpus-baseline/` 的 determinism
    与 7J-3A detector 对历史 artifact 的捕获（nul=60/1 → FAIL）共同证明
    "版本回退会被 detector 揭出"。

## 状态定义（本项目对"修复"的措辞）

> **"当前 pinned stack 上不可复现 + regression guarded"** —— 不是"永久修复"。
> 措辞规则：
> - 可以说：`babeldoc 0.6.4 + pymupdf 1.28.2 上 F9 两个 subclass 均不可复现，
>   由 7J-3A detector 长期守护`；
> - 不可以说：`F9 已经修好`（没有修，也没有被证明在所有版本上不存在）。

## 变更流程

任何以下变更都必须重跑 release gate 并记录结果：
1. `babeldoc` 版本变化（占位符/typeetting/parser 行为）；
2. `pymupdf` 版本变化（subset/ToUnicode/font 行为 —— Case A 的历史根因）；
3. `pdfminer.six` 版本变化（解析路径）；
4. Python 主/次版本变化；
5. `doclayout_yolo` / OCR 路径变化（若 release 依赖 OCR）；
6. 任何 `pdf2zh/` 生产代码变更。

Gate 失败（residual ≠ 1 / 矩阵漂移 / 历史捕获失效 / determinism 破坏）即
阻止 release，不靠人工判断"看起来没问题"。