# magicpdf 切换路径 —— 全维度落地确认报告

> 依据 `doc/babeldoc_to_magicpdf_feasibility_report.md` 与
> `doc/mineru_integration_implementation_report.md` 的待办清单，本轮完成
> **可落地维度的全部实现**，并经全量回归验证。仅剩「需真实引擎与 20+ 样例」
> 的排版视觉评测（Step 3.2）留待有引擎环境执行。

## 1. 维度清单与状态

| # | 维度（报告出处） | 状态 | 落地位置 |
|---|---|---|---|
| 1 | 解析层抽象与可切换后端（可行性报告第 4 条路径 ②，MinerU 报告 Step 2.x） | ✅ 已完成（前迭代） | `engine_env.py`、`magicpdf_adapter.py`、`magicpdf_bridge.py`、`--parse-engine` 四路路由 |
| 2 | 引擎缺失熔断降级（Step 3.3） | ✅ 已完成（前迭代） | `magicpdf_cli._fallback_legacy` |
| 3 | Step 1.2 伪代码/`code` 保护 | ✅ **确认已落地** | `translation_policy_for._KEEP_KINDS`（含 `code`，`preserve_code` 标志）+ bridge `pseudocode_protected` + `detect_code_block` 重定型 |
| 4 | Step 1.3 公式 LaTeX / OCR 侧信道 | ✅ **公式侧信道已落地**（OCR 已透传） | `formula_side_channel.py`（收集/回填/落盘）、`--magicpdf-ocr` |
| 5 | Step 3.2 排版对比评测 | ⏳ 需真实引擎 + 20+ 样例视觉对比 | dumps + fixup 渲染计划 + 译后 mono PDF 已齐备 |
| 6 | **渲染接管（RenderTakeover）输出 PDF** | ✅ **本轮完成** | `v3/magicpdf_renderer.py` + CLI 接线（见 §2） |
| 7 | GUI / Service 接入（Step 3.4） | ✅ 已完成（前迭代） | `TranslationRequest`、`_execute_magicpdf`、GUI 下拉 |
| 8 | pdfminer-six 版本冲突（§12.4） | ✅ 结论：暂不宽松化（保留锁） | 可行性报告 §12.4 |

## 2. 本轮新增：渲染接管落地（维度 6）

### 2.1 新模块 `pdf2zh/v3/magicpdf_renderer.py`

`render_plan_to_pdf(plan, page_sizes=None, output_path=None)` —— 把
`render_plan_from_model` 产出、经 `render_takeover.fixup_render_plan` 修正
（shift 下移 / overflow 标记）的渲染计划渲染为 PDF：

- **坐标翻转**：v3 规范树（左下原点、y 向上）→ PDF（左上原点、y 向下）；
- **逐块换行插入**：按词累积 + `pymupdf.get_text_length` 度量（CJK 字体按
  1em/字符估算），行高 `font_size × 1.4`，超出 dst_box 下边界即停止
  （溢出不裁剪，评测用途）；
- **CJK 内置字体**：含中文译文时用 `china-ss`（pymupdf 内置，无需外部字体）；
- **健壮性**：空 plan 输出 1 个空页（保证可打开）、缺 `dst_box` 回退
  `src_box`、非法/零字号回退默认；
- **纯数据进出**：输入 render_plan + page_sizes，输出 PDF bytes + 统计，
  不触碰 legacy converter / BabelDOC 渲染路径。

### 2.2 CLI 接线

- `pdf2zh/pdf2zh.py`：新增 `--magicpdf-render`（`argparse.BooleanOptionalAction`，
  **默认开启**，`--no-magicpdf-render` 关闭）；
- `pdf2zh/magicpdf_cli.py`：`run_magicpdf_main` 在 `_write_dumps` 后按
  `page_sizes = {p.page_num: [p.width, p.height]}` 渲染，产物
  `{output}/magicpdf/{stem}_mono.pdf`；渲染失败仅告警，JSON 转储不受影响；
- Service 路径无需改动：`_execute_magicpdf` 经 `parse_args` 补齐
  `magicpdf_render` 默认值。

### 2.3 使用方式

```bash
# 默认：解析→翻译→fixup→渲染译后 mono PDF（+ JSON 转储）
pdf2zh --parse-engine magicpdf input.pdf -o out/

# 仅转储 JSON（不渲染 PDF）
pdf2zh --parse-engine magicpdf --no-magicpdf-render input.pdf -o out/
```

## 3. 测试验证

- 新增 `tests/test_magicpdf_renderer.py`（8 项）：文本层/页数/坐标翻转、
  多页尺寸、空 plan、缺框兜底、落盘、CLI 集成（默认产出 mono PDF、
  `--no-magicpdf-render` 不产出）。
- magicpdf 相关套件全绿：`test_magicpdf_renderer + test_magicpdf_cli +
  test_parse_engine_switch + test_magicpdf_adapter + test_magicpdf_bridge +
  test_magicpdf_code_protection + test_v3_render_takeover_fixup +
  test_v3_formula_side_channel` = **68 passed, 1 skipped**。
- 全量回归：**2540 collected（2537 passed + 3 skipped，0 失败）**
  （`pytest tests --ignore=pdf2zh/kernel/PDFMathTranslate-next.git`）。

## 4. 变更文件清单

| 文件 | 变更 |
|---|---|
| `pdf2zh/v3/magicpdf_renderer.py` | **新增**：`render_plan_to_pdf`（渲染接管核心） |
| `pdf2zh/magicpdf_cli.py` | 渲染接线 + 模块 docstring 更新 |
| `pdf2zh/pdf2zh.py` | 新增 `--magicpdf-render` 参数 |
| `tests/test_magicpdf_renderer.py` | **新增**：8 项测试 |
| `doc/mineru_integration_implementation_report.md` | §2 交付物、§4 测试统计、§5 TL;DR、§6 Roadmap 更新 |
| `doc/babeldoc_to_magicpdf_feasibility_report.md` | §12.3 状态更新 |

## 5. 遗留（需真实引擎/外部条件）

1. **Step 3.2 排版对比评测**：需安装 magic-pdf/MinerU + 20+ 真实 PDF，
   OmniDocBench 类视觉对比，评估接管比例；
2. **渲染精化**：CJK 字体嵌入、公式/表格图形化、translate_refit 流式重排
   —— 在 20+ 样例评测反馈后迭代；
3. **MinerU VLM 伪代码置信度**：接入真实布局模型置信度覆盖规则检测
   （当前规则检测 + kind 保护已生效）。
