# 四大隐蔽失效点审计报告（"测试集全过，实际运行依旧失败"根因排查）

> 版本：v1.0 | 日期：2026-08-11
> 范围：`pdf2zh/` 主链路 + `v3/` side-channel 全链路静态审计 + 运行验证
> 结论：**四大失效点中两个 100% 成立、一个部分成立、一个当前不成立但隐患真实**。
> 主链路"假接线"为**结构性根因**：P5–P10 从未驱动过任何一像素渲染。

---

## 1. 执行摘要

"测试全过、实际 PDF 依旧碎裂"不是测试造假，而是**渲染主链路与 P5–P10 重建管线物理隔离**：

| # | 失效点 | 审计结论 | 严重度 | 本次处置 |
|---|--------|----------|--------|----------|
| 1 | 主链路"假接线"（side-channel 未覆盖渲染） | ✅ **100% 成立** | 🔴 致命 | 通道默认翻转 + 渲染数据源标注（`render_source`） |
| 2 | 真实 LLM 对 `<formula_x>` 锚点的破坏 | ✅ **100% 成立**（无容错 + 主链路未接线） | 🔴 高 | 宽松锚点提取 + 缺失回退 + QA 宽松匹配 |
| 3 | DocLayout 掩码二次打碎已重构段落 | ⚠️ **当前不成立，隐患真实** | 🟡 中 | `_block_split` IoU 阈值加固 |
| 4 | PyMuPDF 绘制层 Font Metrics 漂移 | ✅ **部分成立**（坐标转换正确，字体未注册导致丢字） | 🟡 中 | CJK 字体注册 + 基线越界防御 |

---

## 2. 失效点 1：主链路"假接线" —— 结构性根因（100% 成立）

### 2.1 现象
QA 报告显示 P5–P10 运行成功，但导出的 PDF 依然呈现旧版碎裂状态。

### 2.2 证据链（代码行级）

1. **`reconstruction_channel` 默认关闭且服务层未透传**
   - `pdf2zh/high_level.py:186`：`reconstruction_channel: bool = True`（**本次从 False 翻转**）
   - 全仓搜索 `reconstruction_channel`：仅 `high_level.py`（声明）、`converter.py:197-199`（容器初始化）、`v3/mainline_wiring.py:88`（开关检查）。**GUI/CLI/service/worker 均未显式传参** → 翻转前真实运行中该通道全程关闭，连 QA 数据都不产出。

2. **P5–P10 输出只进观测容器，无渲染消费者**
   - `pdf2zh/v3/mainline_wiring.py:88-89`：`run_reconstruction_channel(conv, ltpage)` 把结果写入 `conv.reconstruction_records[pageid]`。
   - `pdf2zh/high_level.py:416-421`：`translate_patch` 收尾仅回传 `v3_output["reconstruction"]` / `v3_output["reconstruction_qa"]`。
   - 全仓搜索 `v3_output["reconstruction"]` 的**读取方**：只出现在 `doc/` 报告与注释。**无任何代码消费它去改变渲染**。

3. **最终修改 PDF 的渲染器不消费 P5–P10 对象**
   - 主链路渲染 = `converter.py::receive_layout`（L223）逐 `LTChar` 流构建 `sstk/pstk` 段落 → `gen_op_txt`（L615）→ `ops` 字符串 → `obj_patch` → `page_patch`。
   - `_gate_records`（L1001）来自 legacy 段落几何，与 P5–P10 的 `LogicalParagraph`/`FormulaObject`/`TranslationUnit`/`SolvedUnit` **零交集**。
   - `DualPatcher.apply_to_pdf`（`pdf2zh/patch/dual_patcher.py`）只在 `qa_reconstruction_demo.py` 演示脚本被调用；`OverlayRenderer.render_hybrid` 同理。主链路 converter 双语文档渲染路径不调用任何二者。

### 2.3 根因
P5–P10 被设计为"双轨 side-channel"（`reconstruction_pipeline.py:10-12` 注释明确"所有失败只进 debug 日志，绝不干扰主链路渲染"）。该设计保证了安全，但也意味着**它从未被要求产出渲染**——QA 通过的只是"链路能算"的验证，不是"链路能改 PDF"的验证。

### 2.4 处置（本次）
- **通道默认翻转 `True`**：真实运行（含串/并行）中 P5–P10 链路激活，至少产出逐页重建 QA。
- **渲染数据源标注**（`mainline_wiring.py:470-477`）：每页记录追加 `render_source: "legacy"`、`render_consumer: "none"`、`channel_enabled: True` —— QA 报告直接暴露"已计算、未接线"真实状态，杜绝"QA 通过但 PDF 不变"的误判。

### 2.5 遗留：主链路真正接线的路线图（三阶段，见 §6）

---

## 3. 失效点 2：真实 LLM 对 `<formula_x>` 锚点的破坏（100% 成立）

### 3.1 现象
公式位置丢失、文本打印出 `<formula_0>` 原字样或翻译卡死。

### 3.2 证据链

1. **严格正则无容错**：`pdf2zh/formula/anchor.py` 原 `ANCHOR_RE = re.compile(r"<formula_(\d+)>")`。
   - 引入空格：`< formula_0 >`、`<formula 0>` → **不匹配**
   - 大小写篡改：`<FORMULA_0>` → **不匹配**
   - 合并/丢失：`<f_0><f_1>` 合并为 `<f_0>`、直接删除 → **几何丢失**

2. **`integrity_score` 只统计不修复**：原实现仅计算匹配率，缺失锚点无回退兜底。

3. **锚点机制主链路未接线**：全仓搜索 `AnchorProtector` / `anchors_in_text` 只出现在 `formula/__init__.py` 导出与测试；`converter.py`/`translator.py` 主链路公式处理仍走旧 `{v0}`/`{v1}` 占位（`converter.py:331-335` + `formula_font_investigation_report.md` §2.3.1）。即使 P5–P10 接进主链路，当前实现也会在真实 LLM 污染下失效。

### 3.3 处置（本次）
`pdf2zh/formula/anchor.py` 新增：
- `LOOSE_ANCHOR_RE`：容忍 `< formula_0 >` / `<formula 0>` / `<FORMULA_0>` / `<formula0>` / 大小写混合，并**不误匹配** `<f_0>`。
- `normalize_anchor_token`：任意变体 → 规范化 `<formula_N>`。
- `repair_anchors` / `AnchorProtector.repair`：**缺失回退** —— 规范译文中所有变体；期望锚点缺失时按顺序补到最后一个已识别锚点之后（保持公式相对顺序），一个都没识别到则补在译文末尾。**绝不丢弃公式几何**（Layout Solver 依赖锚点落位），完整性交给 QA `anchor_ok` 标记。
- `integrity_score(loose=True)`：QA 侧改用宽松匹配，真实污染变体不再误判锚点丢失。
- `DualPatcher.anchor_qa` 同步切换宽松匹配（`anchor_matcher: "loose"`）。

验证（实测）：
```
'< formula_0 >'  → ['<formula_0>']     '<FORMULA_0>' → ['<formula_0>']
'<formula 0>'    → ['<formula_0>']     '<formula0>'  → ['<formula_0>']
'<f_0>'          → []                  （明确拒绝，不误伤普通文本）
repair('the value is ', {f0,f1}) → 'the value is <formula_0> <formula_1>'
score(polluted)=0.5（宽松） vs 0.0（严格）
```

---

## 4. 失效点 3：DocLayout 掩码二次打碎段落（当前不成立，隐患真实）

### 4.1 现象
P5/P6 成功重构段落，但送入翻译前又被拆碎。

### 4.2 证据链
- **当前不成立**：`run_reconstruction_channel`（`mainline_wiring.py:464-468`）调 `pipe.run(ltpage, page_id=pageid)` **未传 blocks**（`blocks` 默认 `None`）→ `build_logical_paragraphs(blocks=None)` → `_block_split` 恒 False。重构层当前没有被 Layout Block 裁剪。
- **隐患真实**：`_block_split`（`geometry/paragraph.py` 原 L113-118）判定为"行与 Block bbox **任何二维重叠**即截断"。一旦未来把 DocLayout (YOLO) 检测框传入：
  - 检测框越界 1~3pt 是常态（YOLO bbox 回归误差）；
  - 一个轻微越界的检测框就会把**已重构好的段落**在渲染前二次切割 → 语义段落碎成多段送翻译。

### 4.3 处置（本次）
`geometry/paragraph.py` 加固：
- `ParagraphConfig.block_iou_threshold: float = 0.3`：Block 与行的**交叠面积占行面积比例 ≥ 阈值**才算障碍物。
- `_block_split(line, blocks, iou_threshold)`：计算二维交叠面积比。

验证（实测）：
```
edge block（交叠 1pt/12pt，ratio 0.083）→ split=False（不再打碎）
big block（覆盖整行，ratio 1.0）        → split=True（真实障碍仍截断）
段落聚合：传入 edge block，2 行仍聚为 1 段
```

---

## 5. 失效点 4：PyMuPDF 绘制层 Font Metrics 漂移（部分成立）

### 5.1 现象
坐标计算正确，但最终 PDF 文字与原公式重叠或基线不对齐。

### 5.2 证据链
1. **y-up → y-down 转换数学正确**：`apply_to_pdf` 的 `py = page_h - baseline`（fitz `insert_text` 的 point 是基线 y-down），推导无误。
2. **真正的问题是字体未注册**：`fontname="china-s"` 是 CJK 字体名，但 PyMuPDF 内置 Base-14 字体只有 `helv/times/cour/symb/zapf`。未 `insert_font` 注册时 fitz 静默回退 helv → **中文无字形 → 丢字/退化**。demo/测试用 ASCII（"Let x be."）所以通过——这正是"测试集全过、实际中文 PDF 失败"的典型假阴性。
3. `OverlayRenderer`（`overlay_renderer.py:76-82`）用 `y0 + font_size*0.85` 经验基线近似，非真实 baseline，属历史遗留。

### 5.3 处置（本次）
`pdf2zh/patch/dual_patcher.py`：
- `_ensure_cjk_font(page, fontname)`：探测平台常见 CJK 字体路径（Windows msyh/simhei/simsun、macOS PingFang、Linux Noto/WQY）并 `insert_font` 注册；全部失败回退 `helv` + warning（**不抛异常**，side-channel 纪律）。
- `apply_to_pdf`：注册字体后落位；baseline 落位前防御性 clamp 到页内（防幽灵障碍物把基线推到页外，`bbox.y0 < 0` 类事故）。

验证（实测，Windows）：
```
resolved font: china-s（成功注册 msyh）
apply_to_pdf 写入 '中文译文 abc' → get_text() 原样可提取，无丢字
```

---
## 6. 遗留：主链路真正接线的三阶段路线图

本次已把失效点 2/3/4 修掉、失效点 1 暴露为可观测状态。**让 P5–P10 真正驱动渲染** 需分三阶段推进，避免一次重写 converter 主循环（1042 行，耦合 sstk/pstk/公式括号/碰撞/TOC/gate 记录）：

| 阶段 | 动作 | 验收 | 状态 |
|------|------|------|------|
| **1. 观测** | 通道默认开 + `render_source`/`render_consumer` 标注；真实 PDF dump 逐页确认 `render_source=legacy` | QA 报告明确显示未接线状态 | ✅ 已完成 |
| **2. 验证** | 在 `gen_op_txt`/`_gate_records` 生成处加断点/日志，确认渲染消费对象类型（LTTextLine/legacy 段落 → 原因 1 证实）；dump 一次真实 LLM 完整 Prompt/Raw Output 检查 `<formula_x>` 污染 | 锚点污染样本库 + 修复后还原率 100% | ✅ 已完成 |
| **3. 接管** | 复用既有 `adopt_geometry_cluster` 接管点（v1.7 已实现"GeometryEngine 段落原地接管 sstk/pstk，文本集完全一致才接管"），把 `LogicalParagraph` 经适配器产出 GeometryEngine 兼容段落 → 主链路渲染真正消费 P5–P10 输出；公式锚点段经旧 `{vN}` 机制逐字形还原 | 逐页 `render_source=reconstructed`；段落在渲染层不再被拆碎；公式位置零漂移 | ✅ **已完成**（`pdf2zh/v3/reconstruction_adapter.py` + `converter.py` 渲染前接管点 + `high_level`/`chunk`/`worker` 透传；详见 `stage3_mainline_adoption_completion_report.md`） |

**阶段 3 的关键约束**（防回归）：接管仅限"文本集完全一致"的段落（现有 `adopt_geometry_cluster` 契约）；公式占位/段落拆分差异一律回退 legacy，保证 mono 原文页零回归。

---

## 7. 验证记录

- `tests/test_reconstruction_p5_p10.py` + `test_p5p10_remaining.py` + `v3/test_v19_reconstruction_sidechannel.py`：**52 passed**（修复后无回归）
- 锚点宽松/回退实测：污染变体全识别、缺失回退保留几何、`<f_0>` 不误伤
- `_block_split` IoU 实测：轻微越界检测框不再打碎段落
- `apply_to_pdf` 实测：`china-s` 成功注册，中文可落位可提取
- 主链路通道实测：`receive_layout` 中 `reconstruction_channel=True` 产出 `render_source=legacy` 标注
- **阶段 3 接管实测**（`tests/v3/test_v21_mainline_reconstruction_adoption.py`，23 passed）：Level 1 接管替换几何保持 sstk、Level 2 合并压缩 `sstk/toc_track/pfkstk`、TOC/分歧/通道关闭回退、`render_source` 随接管报告更新
- **全量回归**：`tests/v3/` 1545 passed；`tests/`（不含 v3）738 passed, 1 skipped；并行 runtime/interrupt 69 passed


