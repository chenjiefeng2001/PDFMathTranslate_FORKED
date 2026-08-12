# 阶段 3 完成报告：P5–P10 主链路接管接线（Stage 3 Mainline Adoption）

> 版本：v1.0 | 日期：2026-08-11
> 范围：四大隐蔽失效点审计报告 §6「主链路真正接线的三阶段路线图」阶段 3 —— 把
> P5–P10 重建管线的几何输出**真正接回 legacy 渲染主链路**。
> 结论：**阶段 3 已实施并验收**。接管采用「文本集完全一致才接管」契约，公式锚点
> 经旧 `{vN}` 机制逐字形还原，公式位置零漂移；任何分歧回退 legacy（零回归）。
> 渲染数据源标注（`render_source`）随接管结果逐页更新，失效点 1 闭环。

---

## 1. 背景

四大隐蔽失效点审计报告指出：

> P5–P10 被设计为"双轨 side-channel"，**从未被要求产出渲染** —— QA 通过的只是
> "链路能算"的验证，不是"链路能改 PDF"的验证。

阶段 1（观测）已完成：通道默认翻转 True + `render_source`/`render_consumer`
标注，真实 PDF dump 显示每页 `render_source=legacy`（未接线）。
阶段 2（验证）已完成：确认渲染消费对象是 legacy 段落。
**阶段 3（接管）** —— 复用既有接管点把 `LogicalParagraph`/`SolvedUnit` 几何
接回 `sstk/pstk` —— 本次实施完成。

---

## 2. 实现（文件级）

### 2.1 新增 `pdf2zh/v3/reconstruction_adapter.py`（阶段 3 接管适配器）

```
legacy sstk/pstk ──► 文本集一致性配对 ──► SolvedUnit.render_bbox 几何接管 ──►
    翻译 + gen_op_txt 渲染（legacy 引擎，公式走旧 {vN} 机制逐字形还原）
```

| 函数 | 职责 |
|------|------|
| `normalize_formula_tokens` | 比较键：legacy `{vN}` 与重建锚点 `<formula_N>` 折叠为 `{formula}`；`\n`/连续空白折叠为单空格 |
| `pair_legacy_to_reconstructed` | 贪心配对：单段一致 → Level 1；legacy i..k 拼接归一化后与重建段 j 一致 → Level 2 合并；无法配对 → None（回退） |
| `adopt_reconstruction_cluster` | 入口：通道/开关/结果检查 → 配对 → TOC 段拒绝 → 几何构造 → 原地压缩 `sstk/pstk/toc_track/pfkstk` → 接管报告 |
| `_apply_adoption` | 原地压缩；合并段 `toc_track`/`pfkstk` 同步合并 |
| `_adopted_from_solved` / `_adopted_from_paragraph` | SolvedUnit.render_bbox / LogicalParagraph.bbox → legacy 段落几何鸭子类型（y-up） |

### 2.2 修改 `pdf2zh/v3/mainline_wiring.py`

- `run_reconstruction_channel` 改为**幂等**：records 已存在（阶段 3 已在渲染前
  提前执行）时只标注渲染数据源，不重复计算。
- 新增 `_mark_reconstruction_render_source`：依据接管报告标注
  `render_source`（`reconstructed` / `legacy`）、`render_consumer`
  （`legacy_renderer` / `none`）、`adopt_level`、`merged_paragraphs`、
  `adopt_reason` —— **QA 报告直接暴露"已计算、已接线 / 已计算、未接线"真实状态**。

### 2.3 修改 `pdf2zh/converter.py`（`receive_layout` 渲染前接线点）

在 `geometry_cluster` 之后、`toc_split` 之前插入阶段 3：

```
reconstruction_channel on → run_reconstruction_channel(conv, ltpage)   # 提前重建（幂等）
                            adopt_reconstruction_cluster(...)          # 接管 sstk/pstk
```

- `reconstruction_channel off`：完全跳过，零开销。
- `adopt=False` 也记录报告（`reason="reconstruction_adopt_disabled"`），由
  adapter 内部处理，避免"报告缺失 → 无法观测真实状态"。
- 失败只进 debug 日志，绝不干扰主链路渲染（side-channel 纪律）。

### 2.4 修改 `pdf2zh/high_level.py`（参数 + 容器 + 并行透传）

- `translate_patch`：新增 `reconstruction_adopt: bool = True` 参数 + 容器
  `reconstruction_results={}` / `reconstruction_adoptions={}`。
- `translate_stream` / `_translate_parallel` / `_translate_parallel_chunk`：
  标量透传（`**dict(locals())` 链）。

### 2.5 修改 `pdf2zh/parallel/chunk.py` + `worker.py`

`ChunkTask` 新增 `reconstruction_channel` / `reconstruction_adopt` 字段；
worker 透传到 `translate_patch` —— **真实并行路径与串行路径行为一致**。

---

## 3. 接管契约（防回归关键约束）

1. **文本集完全一致才接管**：legacy 段文本（含 `{vN}`）与重建段语义文本
   （含 `<formula_N>`）经归一化后一致，才能接管。任何分歧回退 legacy。
2. **`sstk` 文本保持 legacy**：只替换 `pstk` 几何。公式占位符 `{vN}` 原样保留
   → 公式渲染走旧 `{vN}` 逐字形还原机制，**公式位置零漂移**。
3. **几何来自 `SolvedUnit.render_bbox`**：P9 LayoutSolver 三阶段坐标
   （source→translated→render）已做页面边界防御夹紧，直接替换段落容器。
4. **Level 2 合并接管（修复多字体段落语义割裂）**：重建段 j = legacy 段 i..k
   拼接（归一化后）→ 合并为一个渲染段落，`sstk/toc_track/pfkstk` 同步压缩
   —— LLM 获得完整自然段上下文。
5. **TOC 页永不接管**：`toc_track` 非空的段保持 legacy（目录行逐字符几何保护
   不能被段落合并破坏）。
6. **`var/varl/varf/vlen` 保持不动**：公式 `{vN}` 占位符索引全局不变。

---

## 4. 四大失效点闭环状态

| # | 失效点 | 状态 | 本阶段贡献 |
|---|--------|------|-----------|
| 1 | 主链路"假接线" | ✅ **闭环** | `render_source=reconstructed` 逐页由接管报告驱动；接管直接改写 `pstk` 几何 → 渲染主链路消费 P5–P10 输出 |
| 2 | LLM 锚点破坏 | ✅ 已修（阶段 2） | 宽松提取 + 缺失回退（`formula/anchor.py`） |
| 3 | Layout 掩码二次切块 | ✅ 已防护 | `_block_split` IoU 0.3 阈值；`run_reconstruction_channel` 不传 blocks → 段落聚合纯几何启发式 |
| 4 | PyMuPDF 绘制基线漂移 | ✅ 已规避 | 接管保留 legacy 渲染引擎 + legacy sstk 文本 → 基线由 legacy 引擎负责，`SolvedUnit.render_bbox` 仅作容器几何；CJK 字体注册已修复（阶段 2） |

---

## 5. 验证

### 5.1 新增 `tests/v3/test_v21_mainline_reconstruction_adoption.py`（23 项）

- 归一化：`{vN}`/`<formula_N>`/混合/空白折叠/None。
- 配对：Level 1 / Level 2 合并 / 公式占位符匹配 / 分歧拒绝 / 空输入。
- 接管：Level 1 保持 sstk 替换几何（`brk` 保留）/ Level 2 压缩 `sstk/toc_track/pfkstk`
  / TOC 回退 / 文本分歧回退 / 通道关闭 / adopt 关闭 / 无结果回退。
- render_source 标注：接管 → `reconstructed`/`legacy_renderer`；未接管 →
  `legacy`/`none` + `adopt_reason`。
- e2e：`receive_layout` 真实页面产出接管报告 + records。

### 5.2 全量回归

```
python -m pytest tests/v3/ -q          → 1545 passed
python -m pytest tests/ -q --ignore=tests/v3 → 738 passed, 1 skipped
python -m pytest tests/test_parallel_runtime.py tests/test_parallel_interrupt.py → 69 passed
```

无回归。`converter.py` 行数回落到 1048（< 1050 死线）。

### 5.3 验收指标（§9.1 / §9.2 顺带快照）

接管后每页 QA 快照仍产出（`reconstruction_qa[pageid]`）：文本
`font_switch_ratio`、公式 `drift_dx/dy`、锚点 `anchor_score` —— 与既有
`DualPatcher.synthesize` 报告一致。

---

## 6. 剩余限制与下一步

| 项 | 说明 |
|----|------|
| 1 | 接管几何来自 `SolvedUnit.render_bbox`，但**渲染仍走 legacy `gen_op_txt` 引擎**（`render_consumer=legacy_renderer`）。这是有意设计：legacy 引擎已处理字体度量/基线/公式括号，阶段 3 只替换容器几何，改动面最小 |
| 2 | `var/varl/varf/vlen` 未随 Level 2 合并段调整（占位符索引全局不变），多段合并后公式仍逐字形还原于合并容器内 |
| 3 | 若未来接管渲染引擎（render_takeover 路径），需在 `gen_op_txt` 侧做 `insert_textbox` 的 Ascent 补偿（当前走 legacy 路径无此问题） |
| 4 | Level 2 合并的段落宽度取 SolvedUnit 容器宽度；极端长公式段 + 译文超长时多行自适应仍受 legacy 换行引擎约束 |
