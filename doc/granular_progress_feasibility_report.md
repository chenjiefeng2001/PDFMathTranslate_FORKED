# 翻译细粒度进度统计可行性实现报告

> **结论摘要**：细粒度进度（BabelDOC 当前分析页、MinerU 各组件工作状态等）的观测数据**大部分已经存在，只是被丢弃了**。BabelDOC 的事件流自带 `stage_current/stage_total/stage_progress`（LayoutParser/DetectScannedFile 逐页推进，即"第几页/共几页"；ILTranslator 是段落计数），而我们的适配器只取 `overall_progress` 一个数。magic-pdf 的 `doc_analyze` 以 Batch 粒度输出 `Batch i/n: x pages/y pages` 日志；MinerU 2.x `Document.parse` 预留了 `callback` 参数。因此 P0 方案是**纯透传 + 展示**：扩展 `TaskProgressEvent` 携带结构化 `detail`，适配器转发完整事件，前端展示「Analyzing page 12/200」级细节——改动集中在 5 个文件、无协议破坏、向后兼容，预估 1–2 天工作量。

## 一、现状与差距

### 1.1 现有进度链路

```
引擎内部
  └─ babeldoc async_translate 事件流 / magicpdf 日志 / legacy chunk 上报
       └─ 适配器 progress_cb(stage, overall, stage_name)     ← 只剩 3 个标量
            └─ RuntimeService._emit_smooth(task_id, gui_stage, pct, msg)
                 └─ _emit_event → TaskProgressEvent{stage,progress,message,eta}
                      ├─ store.add_event（事件历史，SSE 重连回放）
                      ├─ store.update_task(stage/progress/message/current_file_name)
                      └─ _notify_event_listeners → SSE "progress" 帧
                           └─ 前端 ProgressPanel：进度条 + 阶段步骤条 + ETA + 日志面板
```

### 1.2 被丢弃的数据（证据）

| 数据源 | 已有的细节 | 丢弃点 |
|---|---|---|
| **BabelDOC 事件流** | `progress_update = {stage, stage_progress, stage_current, stage_total, overall_progress}`——`async_translate` docstring 明确定义（babeldoc 0.6.4 `format/pdf/high_level.py:299-327`）；LayoutParser/DetectScannedFile 每页 `progress.advance(1)`（`midend/layout_parser.py:123-126`、`detect_scanned_file.py:108-123`），ILTranslator 按段落计数（`il_translator.py:406-416`） | `babeldoc_next_adapter.py:548-559` 与 `babeldoc_adapter.py:398-409` 的 `_drive()` 只读 `event["overall_progress"]` |
| **BabelDOC 阶段权重表** | `TRANSLATE_STAGES` 13 个阶段名+权重（`high_level.py:60-74`）——可用于把阶段内百分比换算成精确总体进度 | 我们用自维护的 `_STAGE_WEIGHTS` 近似映射 |
| **magic-pdf (1.x)** | `doc_analyze` 内部逐 Batch 输出日志 `Batch {i}/{n}: {x} pages/{y} pages`（site-packages `magic_pdf/model/doc_analyze_by_custom_model.py:160-163`）；模型加载状态亦有 logger 输出 | 无捕获机制；任务期间用户只看到 "Processing..." |
| **MinerU (2.x)** | `Document.parse(pdf_path, dpi=200, language="ch", callback=None)` 预留 `callback` 形参（`pdf2zh/magicpdf_adapter.py:867`）——疑似页级回调，**本机未安装 mineru，签名语义需运行时验证** | 固定传 `None` |
| **legacy 并行管线** | chunk 级 `_emit_smooth` 已在用（该函数的设计初衷） | message 为自由文本，无结构化字段 |

### 1.3 用户可见的差距

- 进度条只有百分比与 GUI 五阶段（parsing/analyzing/translating/layouting/rendering），看不到"卡在第几页"；
- MinerU/magic-pdf 引擎整个解析期（分钟~十分钟级）几乎无反馈；
- 并发批处理只显示 `current_file_name` 单文件名，多文件并行时其余文件状态不可见。

## 二、数据源盘点（粒度矩阵）

| 管线/引擎 | 可获得的最细粒度 | 获取方式 | 成本 |
|---|---|---|---|
| BabelDOC 布局分析 | **页级**（current/total） | 事件字段直读 | 零（已有） |
| BabelDOC 扫描检测 | 页级 | 同上 | 零 |
| BabelDOC 段落翻译 | 段落级 | 同上 | 零 |
| BabelDOC 排版/字体/PDF 保存 | 阶段级（无内部计数） | 事件 stage 切换感知 | 零 |
| magic-pdf 解析 | **Batch 级**（≈每 N 页一条） | 捕获 `magic_pdf` logger 行（正则解析）或包装 `may_batch_image_analyze`（monkey-patch 先例：`apply_babeldoc_list_split`） | 低 |
| magic-pdf 组件加载 | 组件级（MFD/MFR/OCR/Layout 模型加载日志关键词） | 同一 logger 捕获通道 | 低 |
| MinerU 2.x | 待验证：callback 可能提供页级；否则仅「解析中」+ 完成态 | `inspect.signature` 运行时探测 callback 形参并试调；不可用则降级 | 中 |
| legacy 管线 | chunk/页级（已有 message） | 结构化补字段 | 低 |

## 三、可行方案设计

### 3.1 数据模型（向后兼容的核心）

```python
# TaskProgressEvent 新增可选字段（runtime_service.py:335）
detail: Optional[Dict[str, Any]] = None
# 结构约定：
# {
#   "engine": "babeldoc" | "magicpdf" | "mineru" | "legacy",
#   "raw_stage": "Parse Page Layout",        # 引擎原始阶段名（原样透传）
#   "unit": "page" | "paragraph" | "batch",  # 计数单位
#   "current": 12, "total": 200,             # 阶段内计数
#   "component": "layout_model",             # 组件加载场景（magicpdf/mineru）
# }
```

- `to_dict()` 增加 `"detail"` 键——旧前端忽略未知字段，**协议向后兼容**；
- `TaskState` 增加持久化快照字段 `stage_detail: dict`（store.update_task 已按 `hasattr` 动态赋值，天然支持），供 `/api/tasks/{id}` 轮询与 SSE 重连后恢复最新细节。

### 3.2 后端改造点（5 处）

1. **两个 babeldoc 适配器 `_drive()`**：转发完整事件字段
   ```python
   detail = {"engine": "babeldoc", "raw_stage": stage_name,
             "current": event.get("stage_current"), "total": event.get("stage_total"),
             "unit": _guess_unit(stage_name)}   # layout/detect→page, ILTranslator→paragraph
   ```
   `_forward_progress` 签名扩为 `(stage, pct, msg, detail=None)`。
2. **`_emit_smooth`/`_emit_event`**：新增可选 `detail` 参数，写入 `TaskProgressEvent.detail` 与 `update_task(stage_detail=...)`；节流逻辑不变（detail 属于同一次发射，不单独产生事件）。
3. **magicpdf adapter**：新增 `_MagicPdfLogProbe`（logging.Handler 或 monkey-patch `may_batch_image_analyze`），正则 `Batch (\d+)/(\d+): (\d+) pages/(\d+) pages` → progress_cb(detail={engine:"magicpdf", unit:"batch"/"page", current, total})；组件加载日志关键词（Loading model 等）→ `component` 字段。
4. **MinerU 分支**：运行时探测 `Document.parse` 的 `callback` 形参——存在则传入计数回调；不存在/抛错则降级为「开始解析（N 页）」+ 完成事件（粗粒度仍优于现状的全黑盒）。
5. **并发批处理**：`_BatchContext.progress_map` 已有 per-file 百分比；detail 扩展为携带 `{file: {current,total}}` 子字典，或在事件 message 中聚合「a.pdf 45% · b.pdf 分析中 12/80 页」。

### 3.3 前端改造点（3 处）

1. `types.ts`：`ProgressEventPayload` 与 TaskState 增加 `detail?` / `stage_detail?` 字段；
2. `taskStore`：SSE progress 帧写入 state.stage_detail；
3. `ProgressPanel`：进度条下方渲染一行细节文本，i18n 模板：
   - `{{stage}} · page {{current}}/{{total}}`（BabelDOC）
   - `解析批次 {{current}}/{{total}} · {{component}}`（magic-pdf/mineru）
   - 并发批处理：文件列表子进度行。
   阶段步骤条 tooltip 显示 raw_stage 原文。

### 3.4 不建议的做法

- ❌ 给每个页级变化都发独立 SSE 事件——BabelDOC 0.2s report_interval × 200 页会洪泛；必须沿用 `_emit_smooth` 节流，detail 仅随已发射事件搭车；
- ❌ 用正则解析 BabelDOC stdout——事件流已是结构化数据，无需绕道；
- ❌ 前端轮询替代 SSE——现有 push 通道完备。

## 四、分期实施计划

| 期 | 内容 | 改动文件 | 工作量 | 收益 |
|---|---|---|---|---|
| **P0** | detail 字段全链贯通（事件模型→适配器透传→store 快照→SSE→前端展示）；BabelDOC 页级/段落级细节上线 | runtime_service.py、2×babeldoc_adapter、types.ts、taskStore、ProgressPanel、i18n json | 1–2 天 | BabelDOC 全阶段可见"当前第几页"，覆盖最痛的大文档场景 |
| **P1** | magic-pdf Batch/组件捕获 + MinerU callback 探测降级 | magicpdf_adapter.py、(可选) mineru 分支 | 1 天 | MinerU/magic-pdf 解析期不再黑盒 |
| **P2** | 并发批处理多文件子进度聚合；legacy chunk 结构化；阶段步骤条对齐 TRANSLATE_STAGES 原始权重 | runtime_service、frontend | 1–2 天 | 多文件任务全景 |

## 五、风险与兼容性

| 风险 | 缓解 |
|---|---|
| babeldoc 版本升级改事件字段 | 全部 `event.get()` 兜底读取；缺字段时 detail=None（现状行为） |
| detail 撑大事件历史内存 | detail 为小 dict；`_events` 已有 per-task 上限管理（沿用） |
| 旧前端收到新字段 | JSON 忽略未知键，零影响 |
| MinerU callback 语义不明 | 运行时探测 + try/except 包裹，任何异常退回粗粒度，绝不阻断解析 |
| magic-pdf 日志格式跨版本漂移 | 正则不匹配时静默放弃（保持现状），monkey-patch 路径做主选 |
| 测试噪音增多 | detail 断言放进现有 runtime_service 测试风格，fake ctx/executor 模式可复用 |

## 六、验证方案

1. 单元：fake babeldoc 事件流（带 stage_current/total）→ 断言 TaskProgressEvent.detail 与 store 快照；
2. 集成：真实 10 页 PDF 走 babeldoc 模式 → `/api/tasks/{id}/events` 抽样断言出现 `{"raw_stage":"Parse Page Layout","current">0,"total":10}`；
3. 前端：ProgressPanel 渲染快照测试 + 手工验收大文档滚动更新；
4. 回归：现有 test_gui_modules/test_runtime_service_robustness 全套重跑。

## 七、关键代码索引

| 主题 | 位置 |
|---|---|
| 事件被丢弃点（next/legacy） | `pdf2zh/babeldoc_next_adapter.py:528-560`、`pdf2zh/babeldoc_adapter.py:379-410` |
| BabelDOC 事件 schema/阶段表 | babeldoc 0.6.4 `format/pdf/high_level.py:299-378`、`:60-74` |
| 页级 advance 调用方 | `document_il/midend/layout_parser.py:119-133`、`detect_scanned_file.py:84-123`、`il_translator.py:406-481` |
| magic-pdf Batch 日志 | site-packages `magic_pdf/model/doc_analyze_by_custom_model.py:150-164` |
| MinerU callback 形参 | `pdf2zh/magicpdf_adapter.py:851-890` |
| 事件模型/SSE/前端 | `pdf2zh/services/runtime_service.py:335-360,2404-2530`、`pdf2zh/services/api.py:622-672`、`frontend/src/api/types.ts:71-97`、`frontend/src/pages/ProgressPanel.tsx` |
