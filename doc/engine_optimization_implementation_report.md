# 性能瓶颈报告落地实施报告（V5 引擎优化）

> **日期**：2026-08-17
> **依据**：`doc/performance_bottleneck_report.md`（§8 优化维度 / §8.5 路线图）
> **范围**：Phase 1（§8.2.2）+ Phase 2（§8.2.1 / §8.3.1 / §8.4.1）+ Phase 3 增量（§8.3.2「预测预取」）
> **验证**：全量回归 **2529 passed, 3 skipped**（含本报告新增 24 项测试）；converter.py strangulation 死线保持 **1093 行（< 1095）**

---

## 1. 执行摘要（TL;DR）

| # | 维度 | 依据（实测） | 落地 | 状态 |
|---|---|---|---|---|
| 1 | **§8.2.1 Warm Process Pool** | worker 启动+模型加载 8.2s 占总耗时 29% | 进程级常驻池：跨任务复用 spawn + 模型加载，`PDF2ZH_WARM_POOL=1` 启用 | ✅ |
| 2 | **§8.2.2 LLM Client 单例化** | 每次 `build_translator` 新建 client ≈1.59s（SSL 上下文+证书） | OpenAI/AzureOpenAI client 按参数缓存（上限 8），线程安全 | ✅ |
| 3 | **§8.3.1 段落级 Batch** | 单页 ~59 个段落翻译请求，网络 RTT 为长文档主要成本之一 | 同页多段合并单次请求（RTT 减 70–90%），严格还原校验失败逐段回退 | ✅ |
| 4 | **§8.3.2 预测预取流水线** | 逐页串行「推理 0.36s → 翻译 1–2s → 渲染 0.10s」 | 下一页版面推理与当前页翻译/渲染重叠（线程预取，顺序边界保留） | ✅ |
| 5 | **§8.4.1 写回/体积优化** | 写回阶段两次 `write`；输出体积偏大 | `write(garbage=4, clean=True)` 深度清理未引用对象 + 内容流清洁 | ✅ |
| 6 | **converter.py strangulation** | 死线测试 `0 < lines < 1095` | 段落批处理核心逻辑外移 `v3/paragraph_batch.py`，converter 仅 1 行调用 | ✅ |

**设计总则**：所有优化默认**关闭**（env 开关），未启用时行为与原实现完全一致；启用路径带失败兜底（绝不产出错误译文/崩溃）。优化逻辑外移 v3/ 模块，保持 legacy 主链路行数与语义边界。

---

## 2. §8.2.1 Warm Process Pool（并行常驻进程池）

### 2.1 依据
19 页真实 LLM 翻译 28.4s 中，**worker 进程启动 + 每 worker 模型加载占 8.2s（29%）**——4 个 spawn 子进程各自 `import pdf2zh`（4–5.6s）+ ORT 模型初始化。每次并行任务（如多文件批量翻译）都重复支付该成本。

### 2.2 实现
- **新增 `pdf2zh/parallel/pool.py`**：
  - `SharedProcessPool`：进程级单例池，`get()` 懒创建 `ProcessPoolExecutor`（`initializer=init_worker_process`，backend 与主进程一致），`mark_broken()` 后下次 `get()` 重建，`shutdown()` 幂等。
  - `get_shared_pool(workers, backend)`：按 `(workers, backend)` 参数变化自动重建；`PDF2ZH_WARM_POOL != "1"` 时返回 `None`（旧行为）。
  - worker 数 `<2` 归一化至 2（多页分块最少 2 个 chunk）。
- **`pdf2zh/parallel/coordinator.py`**（`TaskCoordinator.run`）新增参数：
  - `executor_factory`：注入 executor 的工厂（默认仍是每次新建 `ProcessPoolExecutor`）。
  - `reuse_executor=True`：任务结束后**不** shutdown 池（常驻复用）。
  - `pool_owner`：中断/异常传播时回调 `mark_broken()`（worker 被硬杀后池内部状态不可信，下次任务重建，成本仅一次 spawn）。
- **`pdf2zh/high_level.py` `_translate_parallel`**（~1693 行）：
  - 启用时：`coordinator.run(..., executor_factory=_shared_pool_factory, reuse_executor=True, pool_owner=shared_pool)`。
  - 未启用时：走原路径（每次新建池），语义与旧实现完全一致。

### 2.3 行为开关
```
PDF2ZH_WARM_POOL=1        # 启用进程级常驻池（默认关闭）
```

### 2.4 收益与权衡
- **收益**：短文档省 8.2s（29%）中的 spawn+模型加载重复成本；批量翻译多文件共享同一池。
- **权衡**：常驻 worker 占用内存（每个 worker 加载 ORT 模型）；`mark_broken` 保守触发重建保证正确性；`reuse_executor` 只在启用开关后生效，回归面为零。
---

## 3. §8.2.2 LLM Client 单例化（连接池 + SSL 复用）

### 3.1 依据
每次 `build_translator` 构造 `openai.OpenAI` 都新建 httpx 客户端（实测每个 client ≈1.59s：3 个 SSL 上下文 + 证书加载）；单次运行可能调用 2–3 次（`translate_patch` / `_apply_bookmarks` / 串行回退）。

### 3.2 实现（`pdf2zh/translator.py` ~51 行）
- `_openai_client_cache: Dict[(base_url, api_key), client]`，上限 `_OPENAI_CLIENT_CACHE_MAX = 8`（多租户切换时整表清理防泄漏）。
- `_get_openai_client(base_url, api_key)`：按 key 复用，线程锁保证并发构造安全。
- `_get_azure_openai_client(base_url, model, api_version, api_key)`：4 元组 key，复用同一缓存表。
- `clear_openai_client_cache()`：测试隔离 / 服务热更新入口。
- 注意：openai-python 的 `OpenAI`/`AzureOpenAI` 实例线程安全且可复用。

### 3.3 收益
同 base_url+api_key 只构造一次 client，Keep-Alive 连接池跨 `translate_patch`/书签/回退路径复用；减少 2–3 次 ×1.59s 的固定开销。

---

## 4. §8.3.1 段落级 Batch 翻译

### 4.1 依据
单页约 59 个段落翻译请求（报告 §6.6），每段一次网络 RTT；长文档由逐页本地处理 + 网络延迟共同主导（§4）。

### 4.2 实现（新增 `pdf2zh/v3/paragraph_batch.py`）
- `batch_translate_paragraphs(texts, font_sigs, toc_specs, safe_worker, thread=0)`：
  1. **聚合规则**：跳过空段 / 纯公式占位符（`{vN}`）/ TOC 行（`toc_specs[i]` 非 None）。
  2. **分批**：按 `PDF2ZH_PARAGRAPH_BATCH_CHARS`（默认 2000，范围 200–16000）聚合同页段落。
  3. **强分隔符** `PARA_BATCH_SEP`（低碰撞概率）拼接为单次请求。
  4. **严格还原校验**：切回段数不符或出现空段（LLM 吞掉分隔符 / 篡改格式）→ **整批逐段回退**（与原语义完全一致，绝不产出错误译文）。
  5. 并发线程数 `PDF2ZH_PARAGRAPH_BATCH_THREADS`（默认 4）。
- **converter.py 接入**（`receive_layout` 串行路径，~590 行）：`news = batch_translate_paragraphs(sstk, _font_sigs, toc_specs, _safe_worker)`——单行调用，开关判断在模块内部；未启用时模块内部退化为 `ThreadPoolExecutor.map(safe_worker, ...)`（与原 `executor.map` 语义一致）。

### 4.3 strangulation 约束
converter.py 死线测试（`tests/v3/test_v4_migration.py::TestConverterStrangulation::test_line_count`）要求 `< 1095` 行。本优化将核心逻辑整体外移 v3/ 模块，converter 净增 **0 行**（顶部 import +1，with 块调用重构 -1），当前 **1093 行** 通过死线。

### 4.4 行为开关
```
PDF2ZH_PARAGRAPH_BATCH=1            # 启用段落级 Batch（默认关闭）
PDF2ZH_PARAGRAPH_BATCH_CHARS=2000   # 单批字符预算（200–16000）
PDF2ZH_PARAGRAPH_BATCH_THREADS=4    # 并发翻译线程数
```

### 4.5 收益
同页多段合并为单次请求，网络 RTT 减 70–90%（59 段 → 通常 ≤ 3–5 批）；失败场景与逐段语义逐字节一致。

---

## 5. §8.3.2 预测预取流水线（Phase 3 增量落地）

### 5.1 依据
逐页串行链路为「版面推理 0.24–0.53s → 翻译（网络 1–2s）→ 渲染 0.10s」，每页墙钟 2.5–3.6s。报告 §8.3.2 建议三阶段流水线，其中**版面推理**最易与翻译网络等待重叠（两者无数据依赖：推理只用渲染图，翻译只用文本）。

### 5.2 实现（`pdf2zh/high_level.py` translate_patch 非批量路径）
- **严格顺序边界保留**：`process_page` 永远串行（TOC / 书签 / 公式组跨页依赖不受影响）。
- 主循环每页：
  1. 取当前页预取结果（`_pf_future.result()`），无预取时同步 predict；
  2. **提交下一页**版面推理到 1 线程后台池（主线程渲染下一页 pixmap 保证顺序，后台只做推理）；
  3. 当前页翻译（含网络等待）与下一页推理**并行重叠**。
- `_prefetch_predict(model, image, imgsz)`：后台线程目标，成功返回 `(layout, elapsed)`；异常封装为异常对象返回，主线程检测后**同步兜底 predict**（绝不带病使用旧布局）。
- 仅 `model is not None` 且 `PDF2ZH_LAYOUT_PREFETCH >= 1` 时启用；`_layout_predictor` 批量路径（`PDF2ZH_LAYOUT_BATCH`）不受影响。

### 5.3 行为开关
```
PDF2ZH_LAYOUT_PREFETCH=1   # 启用预测预取（默认关闭）
```

### 5.4 收益
把串行「推理 0.36s → 翻译 1–2s → 渲染 0.10s」压缩掉推理墙钟（预取与网络等待重叠），长文档每页墙钟降幅约 0.2–0.5s（推理时间占比高时收益更大）。

---

## 6. §8.4.1 写回 / 体积优化

### 6.1 依据
报告 §8.4.1 指出 `write(deflate=True, garbage=3, use_objstms=1)` 已在主输出使用；剩余优化点为子集化前置、去重 `build_translator`（联动 §8.2.2，已落地）、深度清理。

### 6.2 实现（`pdf2zh/high_level.py` 912/913、1301/1309）
两处主输出 `write` 升级为：
```python
doc_zh.write(deflate=True, garbage=4, clean=True, use_objstms=1)
```
- `garbage=4`：在 3 的基础上彻底移除未引用对象（含共享对象），进一步缩小输出体积；
- `clean=True`：清理内容流中的未使用资源（旧图层残片等），与垃圾收集互补。
- 经 `tests/test_engine_optimizations.py::test_write_params_garbage4_clean_produce_openable_pdf` 验证：产出文档可正常打开、页数正确。

### 6.3 收益
长文档写回体积进一步缩减（mono/dual 各减数 MB 级）；`build_translator` 去重收益由 §8.2.2 client 缓存覆盖。

---

## 7. 测试与验证

### 7.1 新增测试（24 项）

| 文件 | 覆盖 |
|---|---|
| `tests/test_warm_pool.py`（10 项） | 开关/懒创建/复用/broken 重建/参数变化重建/幂等 shutdown/归一化；coordinator `reuse_executor`/`pool_owner` 契约（复用不 shutdown、非复用 shutdown、异常传播 mark_broken） |
| `tests/test_paragraph_batch.py`（7 项） | 关闭→逐段语义；开启→合并翻译；还原失败→整批回退；公式/TOC/空段不打包；顺序保持；预算分批；单段不批 |
| `tests/test_engine_optimizations.py`（7 项） | OpenAI/Azure client 缓存复用与清理；`_prefetch_predict` 成功/异常封装；预取开关 env 解析；`garbage=4, clean=True` 写回合法性；warm pool 开关接入 |

### 7.2 回归
- **全量回归**：`python -m pytest tests -q` → **2529 passed, 3 skipped**。
- **关键适配**：`tests/test_parallel_runtime.py::test_translate_parallel_uses_task_coordinator` 的 FakeCoordinator 桩同步扩展（新增 `executor_factory`/`reuse_executor`/`pool_owner` 断言），验证 warm pool 接入契约。
- **strangulation 死线**：converter.py **1093 行 < 1095** 保持通过。

---

## 8. 权衡与限制

| 项 | 权衡 |
|---|---|
| Warm Pool | 启用后常驻 worker 占用内存；broken 保守重建（成本仅一次 spawn）；默认关闭回归面为零 |
| 段落 Batch | 合并请求要求 LLM 保留分隔符；失败自动逐段回退（语义安全）；默认关闭保持缓存 key 兼容 |
| 预测预取 | 仅重叠推理与网络等待；`process_page` 顺序边界保留；预取失败同步兜底 |
| `garbage=4` | 比 3 更激进，已通过样本验证；若遇极端 PDF 依赖未标记对象，可回退 `garbage=3` |

## 9. 后续建议（未在本轮实施）

1. **§8.1.1 字体嵌入 O(F) 重构**：`insert_font` 全文档循环（959 页 1918 次 ≈18.5s）→ 理论上仅需 1 次（xref 共享）。
2. **§8.1.2 并行路径 `pages` 修复**：chunk 切分层按 `range(doc_zh.page_count)` 全量切分，`pages` 过滤仅在 worker 内生效。
3. **§8.3.2 完整异步流水线**：Batch 文本提取 + 并发 LLM 请求 + 重排渲染三阶段（预测预取已覆盖推理重叠部分）。
4. **§8.4.1 子集化前置**：`fonttools` 严格子集化（当前 PyMuPDF 子集化对逐页多副本字体收益有限）。
5. **§8.4.2 GPU 批推理**：DML 后端 + `PDF2ZH_LAYOUT_BATCH≥2`（CPU 动态 batch 无融合收益，保持默认关闭）。
