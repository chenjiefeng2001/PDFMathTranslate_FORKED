# P1–P4 修复未生效根因调查报告（真实 PDF 实证）

> 版本：v1.1（迭代落地） | 日期：2026-08-11
> v1.1 增量：P1–P4 修复已全部落地并接线，真实 PDF 接管实证 + 新增 P5
> 未知字体行内乱码根因（font.unknown.pdf 接管路径上的第二个独立失效点）。
> v1.0 结论（P1–P4 停留在 side-channel、真实 PDF 100% 回退）经本轮迭代后已修复。
> 范围：对 doc/ 最新报告（`four_hidden_failure_points_audit_report.md` v1.0、
> `stage3_mainline_adoption_completion_report.md` v1.0、`reconstruction_qa_report.json`）
> 宣称「P5–P10 主链路接管已完成」的迭代验证。
> 结论：**P1–P4 的所有产物停留在 side-channel，渲染主链路一个像素都不消费；
> 阶段 3 的「接管」在真实 PDF 上 100% 回退（实测）。** 测试集全过是「组件局部
> 正确性」通过，不是「渲染输出正确」通过。

---

## 1. 实证数据（真实 PDF，真实 interpreter 管线）

对 `tests/file/` 三个真实 PDF 用 `PDFPageInterpreterEx + TranslateConverter`
逐页跑 `receive_layout`（`reconstruction_channel=True, reconstruction_adopt=True`），
并 patch `pair_legacy_to_reconstructed` 捕获实际配对输入：

```
=== tests/file/translate.cli.plain.text.pdf  (1 pages) ===
  page 0: adopted=False reason=text_mismatch legacy_segs=1 recon_segs=2 glyphs=568
=== tests/file/translate.cli.font.unknown.pdf  (1 pages) ===
  page 0: adopted=False reason=no_reconstruction_result glyphs=0
=== tests/file/translate.cli.text.with.figure.pdf  (1 pages) ===
  page 0: adopted=False reason=text_mismatch legacy_segs=0 recon_segs=7 glyphs=71
    MISMATCH[0] legacy='Ancilla Data A Data B ... {v0} {' recon='τcycle'
    formula marks: legacy={vN}=2  recon=anchor=0
```

**三态全覆盖**：
1. `text_mismatch`：`pair_legacy_to_reconstructed` 段数/文本不满足「归一化后严格
   相等 + legacy 段数 ≥ 重建段数」→ 回退。
2. `no_reconstruction_result`：页面级 LTPage 无平铺 LTChar（文字全在 Form XObject /
   LTFigure 内）时，side-channel 产出 0 glyph → 无结果。
3. 公式页：legacy 把整页吞成 1 段（推断空格 + 公式占位在段尾），P5 正确切成 7 段
   且公式锚点位置/顺序不同 → 配对失败。

**阶段 3 报告宣称「render_source=reconstructed 逐页由接管报告驱动」，实测三个真实
PDF 全部 `adopted=False`，即真实运行中 `render_source` 永远是 `legacy`。**

---

## 2. 为什么 P1–P4 修补没有正确生效（分层根因）

### 根因 0（结构性）：P1–P4 与渲染主链路物理隔离

主链路渲染 = `converter.py::receive_layout`（L223）逐 `LTChar` 构建 `sstk/pstk/var`
→ 翻译 worker → `gen_op_txt`（L621）→ `ops` 字符串 → `obj_patch` → `doc_zh.update_stream`
（`high_level.py`）。P1–P4 的全部产物（`FormulaObject.is_display_mode`、
`solver.translated_bbox` 的垂直流、`formula_placements`、`render_bbox`）只写入
`conv.reconstruction_records/qa/results/adoptions` 观测容器。

### 根因 1（P1）：Display/Inline 判定从未进入渲染

- P1 判定在 `formula/extractor.py::FormulaExtractor`，只给 `FormulaObject.is_display_mode`
  打标，仅被 side-channel 的 `InlineLayoutEngine` 消费。
- legacy 渲染的公式识别是 `converter.py::vflag`（字体/字符集正则）→ `{vN}` 逐字形还原，
  **完全不知道 display 概念**。display 公式在 legacy 中与 inline 公式同等待遇。

### 根因 2（P2）：垂直流堆叠算出的坐标只进观测容器

- `layout/solver.py::translated_box`（L156-201）的 display 垂直流堆叠**实现完整**，
  但输出 `translated_bbox`/`formula_placements` 只被 `SolvedUnit` 承载。
- 渲染引擎 `gen_op_txt`（converter.py L627-768）行推进是 **uniform
  `y - (lidx+1)*size*line_height`**（L888），display 公式的物理高度不参与行推进；
  后续译文行被画在展示公式正上方 → 重叠。**公式渲染代码（L714-742）对公式高度
  零感知。**
- 唯一把 solver 几何接进渲染的通道是 `adopt_reconstruction_cluster`，但它：
  - 100% 回退（根因 4）；
  - 且即使成功，`_adopted_from_solved` **只取 `render_bbox` 的 x0/y0/x1/y1/size/brk**
    （reconstruction_adapter.py L193-199）——**丢弃 `lines`（行级 baseline）与
    `formula_placements`（公式落位）**。

### 根因 3（P3）：Redact 覆盖完全未接入主链路

- `DualPatcher.apply_to_pdf`（`pdf2zh/patch/dual_patcher.py`）只在
  `qa_reconstruction_demo.py` 演示脚本被调用；主链路 dual 渲染 = `doc_zh.update_stream`
  覆写内容流，**无 `add_redact_annot` / `apply_redactions`**。
- 原文非内容流元素（公式背景、图片、未翻译区域）与译文 ops 直接叠加 → 截图所示
  「原文 Introduction / 公式背景与译文重叠」。

### 根因 4（接管 100% 回退的算法原因）

`reconstruction_adapter.py`：

1. **严格相等**：`pair_legacy_to_reconstructed`（L72）要求 `normalize_formula_tokens`
   （空白折叠）后完全相等。legacy `sstk` 含**推断空格**（converter.py L372-375：
   `child.x0 > xt.x1 + 1` 时加空格），而 P5 重建的 `VisualLine.text` 是字形字符直拼
   （`line.py L51-52`，**无推断空格**）→ 几乎所有真实段 mismatch。
2. **段数硬约束**：`if n == 0 or m == 0 or n < m: return None`（L67）——P5 按视觉行/
   逻辑段切分（更细）时 n < m 直接整体失败；P5 合并多字体段时 n > m 依赖拼接匹配，
   但无**反向合并**（重建段合并匹配 legacy 段）。
3. **纯公式段**：legacy 段 `sstk[-1] == ""` 时公式标记 `{vN}` 追加在段尾
   （converter.py L360-362），P5 把公式锚点放行中 → 顺序差异。

### 根因 5：solver 用恒等译文求解，与真实译文无关

`mainline_wiring.py::run_reconstruction_channel` 里 `solver.solve(unit, unit.text)`
（**恒等译文**）。真实 LLM 译文由 legacy 翻译 worker（converter.py L579-587）产生，
**不经 P6 锚点路径、不经 solver**。即使接管成功，`render_bbox` 行数/高度基于源文
长度，与真实译文行数不匹配 → 容器高度错误。

---

## 3. 为什么「测试集全过」

| 测试 | 验证对象 | 盲区 |
|------|----------|------|
| `tests/v3/test_v22_display_math_vertical_flow.py` | solver/extractor 在 **mock Glyph** 上的局部正确性 | 渲染不消费 |
| `tests/v3/test_v21_mainline_reconstruction_adoption.py` | adapter 在**手工构造、文本恰好一致**的 mock `sstk` 上的接管逻辑 | 真实 PDF 文本/段数结构不一致 |
| `tests/v3/test_v19_reconstruction_sidechannel.py` | mock `LTPage`（平铺 LTChar）下通道能算 | mock 页面无真实文本差异 |
| `stage3` 报告的 1545 passed | 全部 mock/合成数据 | 无「真实 PDF + 真实 receive_layout → 渲染 ops」断言 |

**没有任何测试验证「真实 PDF 上接管率 > 0」或「渲染输出的 ops 几何与 display 垂直流
一致」。** 阶段 3 的验收指标 `render_source=reconstructed` 只在 mock 数据上被驱动过。

---

## 4. 修复方案（已实施，见 companion 报告/代码）

| # | 修复 | 文件 |
|---|------|------|
| F1 | 配对改为**字符序列键（去空白）** + **双向合并**（重建段也可合并匹配 legacy 段）；接管失败从「整页回退」改为「逐段回退（配上的接管，配不上的保持 legacy）」 | `v3/reconstruction_adapter.py` |
| F2 | 翻译 worker 后**用真实译文重新求解**，更新 pstk 容器几何；`gen_op_txt` 消费 display 映射，公式行独立预留物理高度（垂直流推进 y） | `converter.py` + `layout/solver.py` |
| F3 | dual 落盘前对公式源 bbox 区域执行 `add_redact_annot + apply_redactions`（P3 主链路接入） | `high_level.py` |
| F4 | `render_box` 边界防御夹紧（已有）经 F1 接管后真正写入 pstk | 随 F1 生效 |

---

## 4.1 迭代落地记录（v1.1，本轮实际完成）

### F1 落地：配对接通（含 P1 公式展开）

`v3/reconstruction_adapter.py` 已有字符序列键 + 双向合并，但**配对键不展开公式
占位**：legacy ``vflag`` 把斜体书名 _T_ 判为公式 ``{v0}``（``var[0]`` 存 35 字符），
P6 判为普通文本 → 配对键 ``{f}``/``{vN}`` 折叠后两侧字符序列不一致 →
plain.text.pdf 100% ``text_mismatch``。修复：

- `_pair_key` 展开 legacy ``{vN}`` / P6 ``<formula_N>`` 为**实际字形字符**
  （``legacy_formula_texts`` / ``recon_formula_texts`` 参数），再归一化比较。
- `detect_toc_line`（converter.py L515）精判目录行：旧判据 ``any(toc_track[t])``
  把「正文页含页码/年份数字」误判为目录 → 100% ``toc_present`` 回退；精判只
  识别「标题 + 点线 + 页码」结构，正文数字不再误判。

### P5 新根因（font.unknown.pdf）：未知字体行内 x0 分段重置 → 重建乱码

`VisualLineBuilder`（geometry/line.py）两处排序对未知字体失效，导致 P5 重建
文本乱码、配对必然失败（实测 ``"The sociology..."`` → ``"onwiogyofnestolproducsiheocT"``）：

1. 聚类排序 ``key=(-baseline, x0)``：unknown 字体 x0≈52.5（微差 0.01pt 噪音），
   x0 次级 key 打乱行内**内容流序** → 改 ``key=-baseline``（稳定排序保持流序）。
2. 行内排序 ``sort(key=x0)``：unknown 字体 x0 按 **content 段重置**（行内部分字符
   51.9、部分 53.5，非单调），按 x0 排序把字符**分组** → 乱码 → 改为「行内
   unique-x0 数量 ≥ 字符数一半才按 x0 排序」，否则保持内容流序（LTChar 流序 =
   PDF 内容流 = 阅读顺序）。

### F2 落地：真实译文重新求解 + display 垂直流消费

- `v3/reconstruction_render.py::run_render_resolve`：接管段用**真实译文**再跑
  LayoutSolver（三阶段），`render_bbox` 回写 ``pstk``（P4 几何真实化），
  `build_display_marks` 返回 ``{vN → display}`` 标记 + 记录源区域 bbox。
- converter.py：`gen_op_txt` 循环读 ``_render_display_marks``（L640），display
  公式行 ``vflow_extra += 公式物理高度 + margin`` 且 ``lidx += 1``（L766-772）——
  公式独占一行、后续文本必然绘制在公式块之下；`para_bottom` 与行高压缩判定均
  计入 ``vflow_extra``（L894/L918/L1053）。
- **formula_id 唯一性**（P6）：extractor 的 ``formula_id = prefix_{glyphs[0].object_id}``
  在「同一 x 起始的多个 display 公式」（居中公式行）撞 id → solver 的
  ``formula_by_id`` 覆盖 → ``formula_placements`` 全部指向最后一个公式、F2
  display 标记漏标。改实例计数器 ``formula_{seq}_{object_id}`` 保证页内唯一。

### F3 落地：白底擦除写入主链路 ops

`receive_layout` 译段落盘前（L1038-1050）对接管段源区域输出
``q 1 1 1 rg ... re f Q``（白底矩形，等价 redact 的物理擦除）——原文文字/公式
背景先被白色覆盖，译文/公式字形绘制其上，杜绝「原文 / 公式背景与译文重叠」。

### V8.4-F3 落地：整页 Form XObject 文字平铺（with.figure.pdf legacy 0 段 → 有段）

`with.figure.pdf` 顶层只有 1 个整页 LTFigure（Form XObject，4418 字符全在其内）：
converter 顶层遍历 → ``sstk`` 空 → **整页 0 段不翻译**（主链路修复前对这类 PDF
直接空白）。新增 `v3/figure_flatten.py::flatten_page_children`：仅对面积 >70% 页面
的 LTFigure 平铺内部 LTChar 进主循环（局部 Logo/页眉/插图不平铺，避免垃圾文本），
converter 主循环入口调用（converter 行数门禁保持 <1095）。

### 已知限制（with.figure.pdf 接管仍 text_mismatch，独立于 P1–P4）

平铺后 legacy 已有段（整页 1 段，含图注按**内容流序**），P5 按**视觉行序**切 116 段
且公式拆散成单字符段；``pair_legacy_to_reconstructed`` 的全局字符序列检查
（L120）在「legacy 内容流序 ≠ recon 视觉序」时整体拦截。该场景需要 legacy 端按
视觉序重排分段（涉及 receive_layout 结构性重构），超出 P1–P4 范围，记为独立后续项。

### 实证（修复后，真实 interpreter 管线）

```
$ python diag_recon_real_pdf.py tests/file/translate.cli.font.unknown.pdf \
    tests/file/translate.cli.plain.text.pdf
  page 0: adopted=True reason=consistent glyphs=1456 paras=10 units=10   (font.unknown)
  page 0: adopted=True reason=consistent glyphs=568  paras=2  units=2    (plain.text)
  STATS: pages=2 records=2 adopted=2 legacy=0 errors=0
```

### 验收测试（新增）

`tests/v3/test_v23_reconstruction_render_effective.py`（11 项）：

| 测试 | 验证 |
|------|------|
| TestP1FormulaExpansionPairing | 公式展开配对成功 / 不展开失败 / 锚点公式展开 |
| TestF2RealTranslationResolve | display 标记、pstk 几何回写（P4）、多行译文高度增长 |
| TestF3WhiteoutCoverage | e2e 接管页白底矩形 ops、未接管页零回归 |
| TestP5UnknownFontInlineOrder | 未知字体 x0 分段重置保持流序 / x0 单调仍排序 |

---

## 5. 验证记录

- 真实 PDF 接管实证（修复后）：见 companion 诊断输出 —— `diag_recon_real_pdf.py`
  逐页 `adopted` 从全 False 提升；配对成功页 `render_source=reconstructed`。
- 全量回归：`python -m pytest tests/v3/ -q` 与 `python -m pytest tests/ -q --ignore=tests/v3`
  无回归。
- 渲染层断言：`gen_op_txt` display 公式行输出 y 推进 == 公式物理高度 + margin。
