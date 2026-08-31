# Session 续接报告 — 7H 差分诊断闭环（7H-1 / 7H-2A / 7H-2B / 7H-2C 全部完成）

日期：2026-08-30 · 分支：`main` · 工作区：7H 成果已实现但**全部未提交**

主交付报告：`doc/7h1_dual_forensics_report.md`（284 行，完整方案对照 +
FDS 报告 + 诚实验证）。本续接报告按跨 session 惯例（对照
`doc/session_resume_7g5_report.md`）记录**工作区状态调查、已交付内容、
下一步手把**，供后续 session 无需重读全部代码即可接续。

---

## 1. 接续前/当前状态调查

上个 session 完成的是 **7G 排版层**（报告 `session_resume_7g5_report.md`）。
本 session 进入 **7H 校验诊断层**：不修 bug、先让系统对「一个已生成的
Dual PDF，视觉缺陷第一次出现在哪一层」（First Divergence Stage，**FDS**）
给出可运行的工程答案。

本 session 产出**全部在未提交工作区**（分支已与 `origin/main` 分叉，
31 ahead / 4 behind，工作区 2 个修改文件 + `dual_forensics/` 等未跟踪）:

```text
 M pdf2zh/pdfinterp.py                     # 7H-2B：cm 矩阵/页面偏移改走 pdf_num()
 M pdf2zh/v3/magicpdf_renderer.py          # 7H-2A：renderer provenance 采集
?? dual_forensics/                         # 7H-1：诊断工具链（顶层包，8 模块）
?? pdf2zh/pdfnum.py                        # 7H-2B：PDF numeric token contract
?? tests/test_dual_forensics_7h1.py        # 10 forensic tests
?? tests/test_pdfnum_contract.py           #  9 contract tests
?? tests/test_pdfnum_emitter.py            #  3 emitter tests
?? doc/7h1_dual_forensics_report.md        # 主交付报告
```

本 session 全量相关测试 **48 passed, 0 failed**（forensic 10 + pdfnum
contract 9 + pdfnum emitter 3 + babeldoc formula protect + v13 doc_passes），
零回归。改动严格遵守「**不改 recovery/packing/不新增 layout rule**」——
只新增诊断 instrument 与两个狭义 emitter 浮点修复。

## 2. 本 Session 完成的工作

### 2.1 7H-1 —— Dual Fidelity Forensics（差分诊断闭环）

新增顶层包 `dual_forensics/`（纯读、可复现、不需要 translator/ONNX/
renderer）：`provenance`（阶段注册表）+ `snapshot`（抓 parser/model/
translation/layout 四份证据）+ `pdf_inspector`（对象层读取 + MuPDF
emitter 语法错误探测）+ `diff`（几何匹配 → Trace）+ `defect`（F1–F10
taxonomy + FDS）+ `report` + `__main__` CLI。

真实 corpus（仓库 `pdf2zh_files/` 的真实 Dual 对）FDS 报告实质结论：

- **F2 code 误译 → First Divergence = model**（C 书 14 条）：源 C++ 代码块被
  逐行中文化，错在 semantic 层把 code/formula 判成可翻译块，**不是 layout**。
- **F9 → First Divergence = render**（10 条）：内容流含合法科学计数法 float
  `-9.000000001435637e-05`，MuPDF 解析 `cm` 报 `unknown keyword`（tokenizer
  在 `e` 处分字），文本层看不出、只有对象层可见。
- **F10 → UNCERTAIN**（92 条）：双栏重排几何失配，按诚实原则不把 match-gap
  当已证实缺失。
- AI 书干净页零 F2/F9 → 工具能区分「真缺陷页 vs 干净页」。

### 2.2 7H-2A —— Renderer Provenance（F10 盲区闭合）

`render_plan_to_pdf(..., provenance=True)`（缺省 `False`，stats 不含该键，
24 个 renderer 测试零影响）把每个绘制块记录
`{source_node_id, render_object_ref, page, object_type, final_bbox_v3,
font_size, text}`。`aggregate_page_id_direct` 用 id 直连查表替代几何匹配：
present / **absent（已证实缺失，不再 UNCERTAIN）** / stray。对管线内自渲染
产物生效；仓库内外部 BabelDOC Dual 无法追溯注入，仍走几何匹配 + UNCERTAIN。

### 2.3 7H-2B —— Numeric Emitter（F9 根因）

先用 MuPDF probe 决定 contract（推翻了 `%.9g` 假设——`-9e-05` 仍报
`unknown keyword '-9e'`）→ 契约定为 **无指数、有界精度（≈9 位）、去尾零
小数的定点 PDF numeric token**：

- `pdf2zh/pdfnum.py`：`pdf_num(v)`，NaN/±Inf/`-0.0`/非数值一律 `"0"`，
  `never_emits_exponent` 不变量；
- `pdfinterp.py` 两处写坏 float 的点（Form XObject `cm`、页面偏移 `cm`）
  改走 `pdf_num`；
- Acceptance：contract token 全部 MuPDF 无警告，parse-back 相对误差 < 1e-7，
  六元组 round-trip 保真，24 renderer + 17 xobject + 96 converter/layout
  全绿 0 回归；B-5 回跑 in-pipeline 渲染 C 书 185/186 → **F9-in-pipeline 0**。

## 3. 下一步（7H-2C —— Semantic Translation Policy，已实现并完成）

报告 §8.1/§8.2 记录 7H-2C 已落地（`COMPLETE`）：F2 根因是 semantic 角色
仲裁（fds=model），已用**类别系统**（非 C 书特判）闭合：

```text
StructureClassifier._arbitrate_preserve_role → kind → translation_policy → Translator
```

- `structure.py`：`_arbitrate_preserve_role` 在 formula 判定**之前**，以可靠证据
  优先定型 `CODE / COMMAND / FILENAME / IDENTIFIER`，确认后普通角色规则不得覆盖；
- `doc_passes`/`document_model`/`render_payload` 的 `KEEP_KINDS` 全部纳入
  `command / filename / identifier`，`annotate_roles`/`annotate_render` 桥接同步；
  translator 调用点零特判（`TestNoTranslatorPatch` 纪律测试锁定）；
- forensic 证据：code 块 kind=code → F2 检测器不再 flag（model/translation PASS）。

修复后回跑 C 书 forensic，期望 `F2: model` 显著归零（复现/lock 用
`python -m dual_forensics` 恒等翻译重渲染源页）。

### 7H-2C 交付时本 session 的收尾

接手 session 已把 7H-2C 从 READY 推至 COMPLETE：修正测试桩 `_Para.line_count`
与 str 行包装（真实 `Paragraph`/`BlockModel` 接口对齐）、补 `test_empty_unknown`
的 import，并修 `FILENAME` 对**无扩展名多段路径**（`/usr/local/bin/python3`）
的漏判——之前会被 formula 吞掉。`tests/v3/test_7h2c_semantic_policy.py` **22 通过**；
直接受影响套件（structure/v12/v13/content_preservation/magicpdf_renderer/
babeldoc_formula_protect/final_layout_contract/architecture_7e + forensic/pdfnum
+ v13_doc_passes）**全绿 0 回归**。

## 4. 已知限制

- 7H 成果**未提交**（全部 7H-1/2A/2B/2C + 本 session 的 7H-2C 收尾都在未跟踪/修改工作区）；
  建议 `git add dual_forensics/ pdf2zh/pdfnum.py pdf2zh/pdfinterp.py
  pdf2zh/v3/magicpdf_renderer.py pdf2zh/v3/structure.py pdf2zh/v3/doc_passes.py
  pdf2zh/v3/document_model.py pdf2zh/v3/render_payload.py tests/test_*_7h1.py
  tests/test_pdfnum_*.py tests/v3/test_7h2c_semantic_policy.py
  doc/7h1_dual_forensics_report.md doc/session_resume_7h_forensics_report.md` 落一个 commit。
- 全量套件（~1800）未在本 session 完整跑（聚焦本改动面 48 测试验证）；
  如需全量回归需长任务会话。
- 既有外部 BabelDOC Dual 无法注入 provenance（7H-2A/2B 同一边界），
  ID-direct 模式需源页重渲染（恒等翻译，可复现）。