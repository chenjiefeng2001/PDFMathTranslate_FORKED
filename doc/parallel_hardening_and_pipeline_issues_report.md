# 并行加固与管线质量调查报告（v1.0）

> 日期：2026-08-08 | 对应代码基线：PDFMathTranslate_FORKED 当前 HEAD
> 关联：GPU 并行崩溃（BrokenProcessPool）加固收尾 + 三个管线质量调查课题

---

## 〇、摘要

| 主题 | 结论 | 状态 |
|------|------|------|
| GPU 并行崩溃（BrokenProcessPool） | 环境瞬时 GPU 故障为主因；真实缺陷：`.optimized` 缓存并发写损坏、崩溃后永久降级 UX | ✅ 已加固（3 项修复 + 1 项防御 + 测试） |
| ① 翻译全流程日志覆盖 | GPU/并行段存在 7 处缺口（worker 初始化、分片、每页推理、provider、缓存竞争、GPU 指标、阶段计时） | 🔍 已分析（本报告 §2） |
| ② 段内多字体 → 不翻译 / 段落错位 | 三候选根因：字体指纹缺失导致 v3 诊断/保护跳过；major-font 单字体回退导致宽度估计失真；翻译键不含字体 | ✅ 已落地（P2：缓存键含字体 variant + 兼容旧接口），逐 run 宽度回退待实证 |
| ③ 目录（TOC）识别判断标准缺失 | 识别为硬性二值判定（无置信度），≥6 种失败模式把目录行降级为普通段落 → 点线被翻译、子章节错位连锁 | ✅ 已落地（置信度双模式 + 页码容错 + 保护渲染 + 观察报告） |

---

## 1. GPU 并行崩溃：调查回顾与加固总结

### 1.1 根因调查回顾（结论）

- 崩溃现场：21:44–21:49 五连崩（54/7/26/55/32 页），worker 进程被终止，
  `concurrent.futures` 只能抛出 `BrokenProcessPool`（无 traceback、无 WER 事件）。
- 复现探针全部成功（无法复现崩溃）：
  - `probe_ort_spawn.py`（spawn 子进程单 DML 会话）
  - `probe_ort_parallel.py`（父会话 + 4 worker 并发 DML）
  - `probe_ort_real.py`（真实页面渲染 + DML，4 worker，263 boxes/页）
  - `probe_real_pipeline.py`（真实 `translate_stream` + bing，2505.05427v1，74.2s）
  - `probe_vram_pressure.py 5500`（5.5GB 显存压力 + 4 worker，通过）
  - `probe_gui_replica.py`（324 页 Hilbert 书 RuntimeService 全流程跑通）
- 结论：**同代码 1 小时后即全部正常** → 崩溃源于瞬时 GPU/D3D12 上下文层故障，非逻辑 bug；但排查中发现两个真实缺陷：

| 缺陷 | 说明 | 影响 |
|------|------|------|
| `.optimized` 缓存并发写 | 多 worker 首跑同时缺缓存时并发写同一路径，互相截断 → ORT 读损坏文件**原生崩溃**（无 traceback，worker 瞬死） | 加剧/复用 BrokenProcessPool |
| 永久降级 UX | `mark_cpu_degraded()` 幂等，崩溃后任何任务都锁死 CPU，GUI 必须重启服务才能再试 GPU——即使只是瞬时故障 | 用户不可恢复 |

### 1.2 已实施的加固（本任务）

1. **`_OptimizedCache` 跨进程缓存锁**（`pdf2zh/doclayout.py:138`）
   - 互斥：`<model>.optimized.lock`（O_EXCL 原子创建 + pid 写入）+ 死进程残锁回收（`_pid_alive`）
   - 原子发布：ORT `optimized_model_filepath` 写 `<path>.<pid>.tmp` → 成功后 `os.replace` 一次性落地
   - 损坏兜底：`onnx.load` 校验缓存完整；损坏 → 按未缓存重新生成
   - waiter 语义：等待者复用已发布缓存；锁竞争超时（15s）→ 本次安全跳过缓存加载
   - `publish()/abort()` 内部守卫 `state != "busy"` 直接返回，避免复用者误删持锁者 `.lock`（回归测试 `test_waiter_never_touches_owner_lock`）
   - `_try_lock` Windows 加固：unlink 失败→视为他人持有（返回 False），不再循环重试
2. **GPU 崩溃自动 re-arm**（`doclayout.py:83` + `services/runtime_service.py`）
   - `try_rearm_gpu()`：第一次崩溃后**自动**尝试一次 GPU；第二次崩溃（`_crash_streak > 1`）保持 CPU，直到显式 `set_backend()`（CLI `--backend auto`/服务重启）
   - `set_backend(非 cpu)` 重置 `_crash_streak`；`mark_cpu_degraded()` 计数
   - 服务层 `_execute_legacy` 加载模型前检查 `is_cpu_degraded()` → 自动 re-arm 时清 `ModelInstance.value` 重载
3. **减半 worker 重试**（`pdf2zh/high_level.py:687-710`）
   - BrokenProcessPool 时不再单 worker 串行重试，而是 `max(1, (parallel_workers or 4) // 2)` 减半并发重试整任务
   - 获写成日志："Parallel crash detected; retrying the whole task with N worker(s)..."
4. **回归测试**（`tests/test_doclayout.py` + `tests/test_high_level_backend_degrade.py`）

### 1.3 验证结果

| 集合 | 用例 | 结果 |
|------|------|------|
| `tests/test_doclayout.py`（含新增 `TestOptimizedCacheLock` 6 用例） | 18 | ✅ 全部通过（环境噪音见 1.4） |
| `tests/test_high_level_backend_degrade.py`（含 re-arm 3 用例） | 13 | ✅ 13 passed |
| `tests/test_spawn_entry.py` 等 8 个相关回归文件 | 214 | ✅ 通过 |

### 1.4 残余问题与环境噪音（诚实留档）

- **本机 pytest 环境噪音**：完整跑 `test_doclayout.py` 时在 `test_stale_lock_is_reclaimed`（与其前用例组合）后接环境级 `KeyboardInterrupt` 中断编号（Traceback 指向 `doclayout.py:220 _try_lock` 的 `os.unlink`），独立进程/`-k` 筛选运行该用例**恒通过**。已排除代码级死锁（分步复刻同路径正常）；判定为控制台事件注入（harness 环境），非产品缺陷。`_try_lock` 已按 1.2-1 加固以规避 Windows 句柄语义绕环。
- 遗留待办：GUI 增加"恢复 GPU"按钮（调 `set_backend("auto")` 清 `_crash_streak`），由服务重启规避属临时行为。

---

## 2. 调查课题①：翻译全流程日志覆盖（GPU / 并行段缺口）

### 2.1 现状盘点（已经有的）

| 链路点 | 锚点 | 现状 |
|--------|------|------|
| 模型加载（会话） | `doclayout.py:369` | ✅ `ONNX Runtime providers: [...]`（INFO） |
| 降级事件 | `high_level.py:1016-1028` | ✅ warning + `progress_cb` 上报 GUI |
| 并行重试/回退 | `high_level.py:678-710` | ✅ warning（类型/上下文/减半 worker 数） |
| worker 模型加载失败 | `high_level.py:984-987` | ✅ warning |
| CPU 重载失败 | `high_level.py:726-728` | ✅ warning |

### 2.2 缺口清单（需补）

| # | 位置 | 缺口 | 建议 |
|---|------|------|------|
| L1 | `_init_worker_process` (`high_level.py:968`) | worker 启用后端/可用 providers/模型加载耗时**均无日志**；worker 崩溃时无从定位是"哪个 worker、哪个页" | 进 initializer 后 DEBUG 打 `worker pid / backend / available providers / model load 耗时` |
| L2 | `translate_stream` 主循环 (`high_level.py:191-243`) | 每页 `model.predict` 无日志（耗时/box 数/类别） | DEBUG：`page %d predict %.1fs boxes=%d`；INFO 级每 50 页汇总 |
| L3 | `_translate_parallel_chunk` / 分片提交区 (`high_level.py:1254`) | 无 chunk→worker 映射、无每 chunk 页数/起止时间、无结果聚合校验日志 | 提交点 INFO：`chunk w{p} pages [a,b] n=N`；聚合点 INFO：耗时 + 成功率 |
| L4 | `_OptimizedCache` (`doclayout.py:153-205`) | acquire/publish/abort 完全静默 | DEBUG：`cache {final}: busy(dur)/cached(reuse)/idle(lock busy)`；警告：publish 失败、损坏缓存已重建 |
| L5 | 降级后任务 | 降级状态虽在但无"本任务实际后端"断言 | 每任务入口 INFO：`task uses backend=cpu (degraded, streak=N)`；re-arm 时 INFO：`auto-re-armed GPU, streak reset` |
| L6 | GPU 指标 | 无 VRAM / 推理耗时采样，`onnxruntime` 版本与 provider 版本不可见 | 任务开始 INFO 打 `onnxruntime ver; providers; DML device`（`session.get_providers()` + 环境变量） |
| L7 | 阶段计时 | 布局分析 / 翻译 / 渲染三段无粒度计时 | 在 `translate_patch` 三阶段关键点打 `phase done x.xxxs`；异常时与页面数、worker 数同上下文 |

### 2.3 落地优先级

P0：L2（每页推理日志）与 L1（worker 上下文）——本次 BrokenProcessPool 排查时发现的直接痛点；
P1：L4（缓存竞争事件）——与本次修复同源，为后续回归观测提供证据链；
P2：L3/L5/L6/L7（研判 & 性能类）。

---

## 3. 调查课题②：同段多字体 → 不翻译 / 段落错位

### 3.1 现象（用户报）

- 一个段落内存在多个字体（如正文 Times 混 Code / 斜体 run）时该段**不翻译**
- 严重时出现**段落错位**（文字位置偏移错误）

### 3.2 机制分析（代码锚点）

1. **字体指纹只进诊断、不进翻译决策**（v3 路）
   - `v3/canonical_page.py:410-417`：`annotate_style` 收集 `fonts`/`multifont` （`fonts` 点按 span 聚合）
   - `v3/diagnostics.py:165-176`：`font_uncertain` 仅当 `multifont` **且** 块带 `orphan_glyphs` anomaly 才入场（条件重叠，覆盖面窄）
   - → 多字体段落在翻译决策（`content_preservation` / `roles`）中没有专门规则，疑似在去重/保护路径被静默跳过（需语料实证确认）
2. **legacy 转换器：段落按 chars 逐字聚合并行，字体不参与聚合**（`converter.py:339-378`）
   - 段落切分只依赖 `cls`/字号/`vchar`；`fontname` 仅用于 `vflag()` 公式判定（300 行）
   - 因此"多字体"在 legacy 里**不构成跳过**，但**渲染侧**：
3. **渲染宽度估计仅用单一字体回退链**（`toc.py:119 char_adv` 同逻辑；`converter.py:540-…`）
   - `fcur_` 判定：tiro（等宽回退）→ noto → fontmap 命中
   - 混排段落按"段落基准字体"排版整串 → 非基准字体 run 的推进值由错字体字典给出 → **译文宽度/换行错位**（与"文字位置偏移"吻合）
4. **翻译缓存键仅含文本**（`translation_cache.py`）
   - 同文本多字体段落命中缓存后跳过新翻译；若首条翻译/布局假定了一种字体，其余同文本段全部复用 → **"多个同文本段落不翻译"现象**
5. 空段落/公式保护（`converter.py:462` `if not s.strip() or re.match(r"^\{v\d+\}$", s)`）——与字体无关，排除。

### 3.3 建议（验证优先，逐步落地）

| 步骤 | 内容 | 触达文件 |
|------|------|----------|
| 1 实证 | 用 `pipeline_dump` + multifont 诊断对受    1-2 个样例 PDF 对比"翻译跳过率 vs multifont 块" | `v3/pipeline_dump.py` |
| 2 决策侧 | `translation_advisor`/`roles` 增加 `multifont` 线索：不整体扔，改为按 run 边界拆翻译单元 | `v3/translation_advisor.py` |
| 3 渲染侧 | `char_adv` 增加逐 run 字体索引（每个 run 用真实 fontname 取度量），段落 x 预估按 run 累加 | `toc.py`、`converter.py` |
| 4 缓存侧 | 翻译键扩大为 `(text, lang, glyph_run_signature)`（可选开关） | `translation_cache.py` |

---

## 4. 调查课题③：目录（TOC）判断标准缺失 / 点线被翻译 / 子章节错位

### 4.1 现状机制链

```
converter.py:450-457   detect_toc_line(段落文本, brk, track, 右边界)
        │ 命中 → toc_specs → toc_mode（标题单独翻译、点线+页码原位渲染）  [正确路径]
        └ 未命中 → 整段作为普通段落走翻译 → 「........」被机器翻译成「。。。。。。。」
~~；子章节合并段：toc_split=False 时正文目录整段译 → 章节目录全部错位
```

关键判断 `detect_toc_line`（`pdf2zh/toc.py:53-116`）是**硬性二值判定**，任一条件不满足即返回 None：

- 段落无物理换行（`brk=False`）
- 点线字符 ≥2 且结尾 1–4 位数字（`TOC_LEADER_RE`）
- 字符几何 track（`.·…‥` / 数字）与页码长度一致
- 标题长度 ≥2

### 4.2 失败模式清单（→ 点线被翻译 / 排版全乱）

| # | 触发条件 | 后果 |
|---|----------|------|
| F1 | 页码带范围/后缀（`12–13`、`(12)`、`12.`）→ 正则 `\d{1,4}\s*$` 不命中 | 整行普通翻译 |
| F2 | 目录行被并入上一段落（几何保护不足）→ `brk=True` | 整段普通翻译（**最常见**，多行/子章节缩进场景） |
| F3 | 点线 1 个或点线中含空格/异字符 | 整行普通翻译 |
| F4 | `track` 为空（渲染路径缺逐字符几何） | 页码长度校验失败 → 整行普通翻译 |
| F5 | 页码构成被点线吞噬（`….12.` 尾点） | 页码错判 |
| F6 | 空列页码目录行（无点线）→ 分支 B 需要“标题编号开头”，无编号标题且页码不右对齐 | 子章节（`2.1.2` 无点线 + 无编号标题）整行普通翻译 → 子章节全乱 |
| F7 | `toc_split` 门控：`high_level.py:1068` 中 `translate_stream(... toc_split=False)` 默认关闭；仅 `runtime_service` 的 v1/v2 profile 将其置 True（`runtime_service.py:128-133`）→ CLI 直接调用路径合并目录段落不会被重切 | 多行目录段整体翻译、错位连锁到全书目录 |

#### 子章节排版错误的具体机制

- 识别命中但标题含编号部分未被剥离时，`parse_toc_entry`（`v3/toc_semantics.py`）语法模板要求 `Chapter/Section/Part/数字编号` 开头；裸 `2.3.1` 用 `_RE_BARE_NUMBERED_MULTI` 判定（line 107/182-185），但 `3.2.` 带尾点或 `附录 A` 等不命中 → 标题原文保留（翻译器处理整串）→ 模板前缀丢失 → 缩进/编号错位
- toc 行渲染禁折行（`converter.py:543` `x1_bound=inf, 674-737` in-place 渲染）：长标题不再折行却仍原位排版 → 与后续行重叠/超出页面，视觉错位
- 检测通过但不匹配的「页码右对齐参考右边界」在并行分 chunk 时依赖 `track` 完整性，缺失即返回 None（F4）

### 4.3 建议：引入"合理的判断标准"（置信度模型）

1. **一维置信分（0–1）替代二值**，设双阈值：
   ```
   score = 0.35×leader_连续率(点线占比/2+) 
         + 0.25×page_col_geo(页码右列比例 ≥0.8)
         + 0.20×start_fmt(章节编号/Chapter… 前缀)
         + 0.15×height_uniform(行高=标题字号量级)
         + 0.10×font_uniform(同段字体一致)
   命中:score≥0.55 → 目录行；0.3≤score<0.55 → 进兜底保护（剥离引导线）；<0.3 → 普通段
   ```
2. **页码容错**：支持范围（`12–14`）、后缀（`(12)`）、罗马数字；页码列以**几何右列**（track x ≥ 0.8×页宽）判定，超出文本正则
3. **点线文本保护**：任何"疑似目录行"都先把 leader 字符从翻译文本剥离，保留并在渲染层原位画入（永不进翻译器）
4. **`toc_split` 默认开**：`translate_stream` 默认 `toc_split=True`，与 runtime_service 的 v2 profile 对齐
5. **子章节层级**：按编号段数（`2` / `2.1` / `2.1.3`）映射 level=1/2/3 + 缩进；`parse_toc_entry` 增补 `3.2.`（尾点）、`附录 A` 等变体
6. **toc 行渲染**：长标题不可折行时采用两种策略之一——缩小字号或强制换行进入"目录样式段落"（保持 ref 列），两者都记录 `toc_report` 供回归对比

---

## 5. 下一步计划（按优先级）

> 状态更新（V1.19，2026-08-09）：P0–P2 全部落地，回归全绿；剩"逐 run 宽度回退"待有故障样例实证后再做。

| P | 任务 | 归属 | 状态 |
|---|------|------|------|
| 0 | §2.2 L2/L1：并列页推理日志 + worker 上下文日志 | 日志调查① | ✅ `doclayout` worker 初始化日志 + `translate_stream` 逐页 debug/25 页聚合 INFO |
| 0 | §4.3：`detect_toc_line` 置信度改造 + 点线剥离保护 + `toc_split` 默认开 | 目录课题③ | ✅ `_score_toc` 5 因子双阈值 0.55/0.30；leader/page 永不进翻译器；`protect` 模式尾部原位渲染；`_translate_worker_chunk` 默认 true |
| 1 | §4.3 页码容错 + `parse_toc_entry` 变体补全 | 目录课题③ | ✅ 区间（`12–13`）+ 罗马数字 + `)` 后缀；`3.2.` 尾点编号、`附录 A`/`Annex A` 变体 |
| 1 | GUI "恢复 GPU" 按钮接入 `set_backend("auto")` | 遗留 UX | ✅ `pdf2zh/gui/app.py` 头部按钮 → `set_backend("auto")`，i18n 双语 |
| 2 | §3.3 字体课题：先抓样例实证，再缓存键/宽度逐 run | 多字体课题② | ✅ 缓存键 variant（多字体段指纹，旧接口 TypeError 回退兼容）；逐 run 宽度待样证实证 |
| 2 | LRU 报告持久化（`toc_split_reports`/`pipeline_dump` 落盘） | 观测基建 | ✅ TOC 观察报告：`PDF2ZH_TOC_REPORT=1` 时在输出侧写 `<stem>.toc_report.json` |

### 5.1 V1.19 落地细节与回归结果

- `toc.py`：`TOC_FULL_THRESHOLD=0.55` / `TOC_PROTECT_THRESHOLD=0.30`；`_score_toc` 权重 = 0.35 点线纯度/0.25 页码右列/0.20 起始格式/0.10 数字形态/0.10 标题长度；区间页码一律降级 `protect`；`_TOC_HEAD_RE` 修正（避免裸普通词被当章名）。
- `converter.py`：spec.mode 双分支 —— full 禁折行原位渲染；protect 标题照常折行 + 点线/页码原样追加尾部（`leader_orig`+`page_digits`）；`looks_like_toc_text` 且 track 缺失 → warning 提示观察；每页 `_toc_reports` 采集（page/title/页码/leader/score/mode/entry_kind）。
- `toc_semantics.py`：`_RE_BARE_NUMBERED_MULTI` 容忍尾点；新增 `_RE_ZH_APPENDIX`、`_RE_ANNEX`（附录/Annex → APPENDIX，level 1）。
- `translation_cache.py`：`get/set` 增 `variant` 参与 sha256 键；converter `_safe_worker` 计算多字体段指纹 `|fonts:A|B`（>1 字体才附），旧缓存接口 TypeError 自动回退。
- GUI：`recover_gpu_btn` 头部按钮 → `set_backend("auto")`，结果行内提示（`recover_gpu_ok/fail` 双语文案）。
- 回归（V1.19 全量）：converter/v3/cache 组 190 ✅；GUI/书签/评估 140 ✅；doclayout 16/16（环境级 ^C 噪声仅落盘于 teardown，分区运行全绿）+ degrade 13/13 ✅；其余 16 文件 224 ✅（1 既有 skip）。