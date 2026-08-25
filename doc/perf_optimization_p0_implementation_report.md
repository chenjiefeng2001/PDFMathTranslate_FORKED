# 基准测试 P0 优化落地实施报告

> **日期**：2026-08-25
> **依据**：`doc/perf/itbook-benchmark/report.md`（§6 测试过程中发现并修复的 Bug + §3 关键数字）
> **范围**：四个 P0 修复（magicpdf 页切片 / legacy 切片-回贴 / babeldoc 子进程隔离 / 启动预热）+ 批量翻译全链路 + 构建/安装并行化
> **验证**：全量回归 **2819 passed, 6 skipped**（tests/ 1246 + tests/v3/ 1573，含本报告新增 26 项性能修复测试）；前端 `tsc --noEmit` 通过

---

## 1. 执行摘要（TL;DR）

| # | 问题（基准实测） | 落地 | 开关 | 状态 |
|---|---|---|---|---|
| P0-1 | magicpdf 的 MinerU analyze **无视页选择**，730 页文档局部翻译也要付 ~40min 全文档解析（12.9GB RSS） | `magicpdf_adapter.py` 解析前按页选择切出临时子 PDF，解析后页号还原；临时文件必清理 | `PDF2ZH_NO_MAGICPDF_SLICE=1` 熔断 | ✅ |
| P0-2 | legacy 链路 `translate_stream(pages=…)` 切片翻译产物无法拼回原文档（下游拿不到完整 mono/dual） | `high_level.py` 新增切片→翻译→pymupdf 回贴闭环：mono 原位替换、dual 2N 交错、TOC 页号重映射、v3_output 键还原 | `PDF2ZH_NO_SLICE_SPLICE=1` 熔断 | ✅ |
| P0-3 | babeldoc 连续任务 RSS 2.77GB → 4.72GB（**+2GB 泄积**），长会话 OOM 风险 | 每任务一次性子进程执行（进程退出即归还全部原生内存）；stdout JSON 帧协议流式回传进度；取消=父进程看门狗 kill | `PDF2ZH_BABELDOC_SUBPROCESS=1` 启用 | ✅ |
| P0-4 | legacy 首任务冷加载 ~15s（ONNX 模型 + 远程字体）全部落在第一个用户请求内 | API 服务启动后台线程预热 doclayout 模型单例 + 中文字体 | `PDF2ZH_NO_WARMUP=1` 关闭 | ✅ |
| Bug-1 | `_execute_legacy` 未传 pages → REST/Dashboard 的 page_range 对 legacy 完全失效（每次全文档翻译） | `runtime_service.py` 新增 `_parse_page_range_to_indices()` 并传入 translate_stream | — | ✅ |
| Bug-2 | babeldoc 对 >30 页文档必崩：伪代码保护跳过时返回 None，next 内核 `'NoneType' has no attribute 'handle_document'` | None 时显式加载 BabelDOC 默认 DocLayoutModel 兜底 | — | ✅ |
| 增强 | REST/Dashboard 不支持多文件 | 后端批量执行器（有界并发、逐文件进度、失败续行、结果 ZIP）+ 前端批量上传 UI | `PDF2ZH_BATCH_CONCURRENCY`（默认 2，上限 4） | ✅ |

**设计总则**：全部优化默认保持原行为（子进程隔离与预热为默认开、切片类带熔断开关），任何失败路径都回退旧行为，绝不产出错误译文或崩溃。

---

## 2. P0-1 magicpdf 页切片解析

### 2.1 依据
基准 M_* 六次运行 analyze 阶段均 ~2500s：MinerU pipeline 无视页选择强制扫描全部 730 页（~3.1s/页）。局部翻译场景 99.98% 成本浪费在未选页解析上。

### 2.2 实现（`pdf2zh/magicpdf_adapter.py`）
- `_normalize_page_selection(pages, page_count)`：接受 `"1-3, 5"` / `[4, 2]` / `None`，归一化为去重升序 0 基索引；`"all"`/空/全选/全越界 → `[]`（不切片）。
- `_slice_pdf_for_pages(pdf_path, pages)`：pymupdf 选页另存临时 PDF，返回 `(slice_path, {切片局部页号 → 原文档页号})`。
- `MagicPdfAdapter.parse()` 调度：需要切片时对切片调用 `_parse_by_backend(backend, slice_path)`（切片内不再传 pages），返回结果经 `_remap_magicpdf_result_pages()` 把 `page_num` 与 `raw["page_no"]` 还原为原文档页号，finally 清理临时文件。

### 2.3 收益
页选择 p100 场景 analyze 从 ~2500s 降到 ~3s 量级（只解析 1 页），内存从 13GB 量级回落到单页水平。

---

## 3. P0-2 legacy 切片-回贴（translate_stream hook）

### 3.1 依据
P0-1 解决了解析侧，但 legacy 链路的 `translate_stream(stream, pages=[…])` 产物是「切片文档的译文」——直接返回会导致下游拿到只有 2 页的 mono/dual。需要完整的「切出 → 翻译 → 贴回」闭环。

### 3.2 实现（`pdf2zh/high_level.py`）
- `_normalize_slice_pages()` / `_slice_pdf_pages()`：与 magicpdf 侧同语义的字节级切片。
- `_splice_mono_pages(original, translated_slice, sel)`：mono 产物逐页原位替换选中页，未选中页保持原页副本，TOC 保留不动（页数不变）。
- `_interleave_dual_pages(original, slice_dual, sel)`：dual 产物按 `[原页副本, 译页]` 2N 交错重建，TOC 第 p 页（1 基）映射到 dual 第 2p 页。
- `_remap_slice_local_pages(v3_output, page_map)`：递归还原 `processor_reports` / `ir_snapshots` 等以局部页号为键的 dict。
- `translate_stream(..., _allow_slice_splice=True)`：`pages` 为有效子集且开关未熔断时走切片路径；`emit_ir=True` 或 `PDF2ZH_NO_SLICE_SPLICE=1` 时保持全文档路径（IR 快照必须覆盖全文，不可切片）。

### 3.3 收益
legacy 局部翻译从「固定开销 ≈90s 全文档解析+合并」变为只付选中页成本；REST `page_range` 语义真正生效（配合 Bug-1 修复）。

---

## 4. P0-3 babeldoc 子进程隔离（RSS 泄积治理）

### 4.1 依据
babeldoc 连续 6 次运行 RSS 从 2.77GB 涨到 4.72GB。泄积点在 next 内核原生层（IL 求解器/渲染），进程内无法回收；唯一可靠手段是进程边界。

### 4.2 实现
- **新增 `pdf2zh/babeldoc_next_worker.py`**：stdin 读 JSON payload → 调用进程内的 `run_babeldoc_next_translation`（`cancelled_check` 恒为 None——取消不可跨进程，由父进程看门狗实现）→ stdout 逐行输出 JSON 帧：
  - 进度帧 `{"progress": true, "stage": …, "pct": …, "detail": …}`（即时 flush，流式透传）
  - 终帧成功 `{"ok": true, "files": […]}` / 失败 `{"ok": false, "error": …, "error_type": …}`
  - 退出码约定：0 成功 / 1 一般错误 / 2 内核缺失（`BabeldocNextUnavailableError`）
- **`babeldoc_next_adapter.run_babeldoc_next_translation_subprocess()`**：spawn worker 子进程；读线程解析 stdout 帧并回调 `progress_cb`；`cancelled_check()` 触发时看门狗 `kill()` 整棵进程树并抛 `_BabeldocNextCancelledError`；错误类型按退出码+error_type 映射回原异常层级。worker 模块可经 `PDF2ZH_BABELDOC_WORKER_MODULE` 注入（测试用 stub）。
- **`runtime_service.py`**：`PDF2ZH_BABELDOC_SUBPROCESS=1` 时把 next 链路 runner 换成子进程版本，签名/契约不变；runner 缺失时告警并回退进程内。

### 4.3 权衡
每任务多一次 spawn + 内核 import（秒级）；换来的是 RSS 上界恒定（单任务峰值），长会话不再累积。默认关闭，桌面端安装包可按需启用。

---

## 5. P0-4 服务启动 layout 模型预热

### 5.1 实现（`pdf2zh/services/api.py`）
`create_api_app()` 启动 daemon 线程：
1. `ModelInstance.value is None` 时 `OnnxModel.load_available()`（主进程单例——既有 registry-prewarm 只覆盖并行池 worker 进程，主进程仍需单独预热，runtime_service 直传该单例）；
2. `download_remote_fonts("zh")` 拉取远程字体（失败仅 debug，不阻断）；
3. `PDF2ZH_NO_WARMUP=1` 时整段跳过（CI/离线环境）。所有异常吞掉记日志，预热失败绝不阻断服务启动。

### 5.2 收益
首个用户任务的 ~15s 冷加载（基准 L_block parsing=14.9s）移出用户感知路径。

---

## 6. Bug 修复（基准 §6 #1/#2）

### 6.1 legacy page_range 失效（Bug-1）
`runtime_service.py` 新增 `_parse_page_range_to_indices(page_range)`：`"1-3,5"` → `[0,1,2,4]`（0 基、去重、越界裁剪、非法输入返回 None）。`_execute_legacy` 构造 `translate_stream` 调用时传入 `pages=`。此前 REST/Dashboard 传什么都会全文档翻译。

### 6.2 babeldoc >30 页崩溃（Bug-2）
伪代码保护逻辑自动跳过时 layout model 为 None，next 内核不回退默认模型直接崩。`babeldoc_next_adapter.py` 在检测到 None 时显式 `DocLayoutModel.load_available()` 加载 BabelDOC 默认模型兜底。>30 页文档链路恢复可用。

---

## 7. 批量翻译全链路（增强）

### 7.1 后端
- `POST /api/tasks` 接受可重复 `files` multipart 部件（兼容旧单 `file` 与 `source_path`，混合提交时本地路径追加队尾）；统一进入 `TranslationRequest.files`。
- `runtime_service._execute_batch()`：按数量自动路由单文件/批量执行器。`PDF2ZH_BATCH_CONCURRENCY>1` 且文件数 >1 时走 `_execute_batch_concurrent()` 有界并发（默认 2、上限 4，线性进度模型 Σ(每文件百分比)/total）；否则串行（复用 stage 权重聚合器）。
- 失败续行：单文件异常不中断批次，计入 `file_failures: [{file, error}]` 并继续下一文件；任务态新增 `file_failures` 字段透出。
- `_ensure_result_zip()`：保证 `result_zip` 恒指向真实存在的 ZIP（即使引擎自带产物不是 ZIP 也补打包），新端点 `GET /api/tasks/{id}/result-zip` 提供「全部下载」。

### 7.2 前端（React SPA）
- `endpoints.ts`：`submitTask` 改发重复 `files` 部件；新增 `resultZipUrl()`。
- `types.ts`：`TaskState.file_failures` 可选字段。
- `Dashboard.tsx`：Upload.Dragger 开启 `multiple`，提交全部选中文件；按钮显示批量计数；结果区新增 ZIP 下载按钮与逐文件失败明细 Alert。
- `i18n/index.ts`：`batch_count` / `download_all_zip` / `batch_failed_files` 三条中英文案。

---

## 8. 构建/安装链路并行化（顺带落地）

| 文件 | 改动 |
|---|---|
| `script/setup.bat` | 瘦身为入口，实际逻辑外移 `script/setup-assets.ps1`（python embed 与 get-pip 并行下载，setuptools+pdf2zh 单次 pip 安装避免 site-packages 竞争） |
| `.github/workflows/exe-build.yml` | Python/PyStand 下载解压 `ForEach-Object -Parallel -ThrottleLimit 2` 有界并发（各任务写独立路径无竞争）；确定性 Wait-Job 式汇合，失败聚合快速失败 |
| 同上（清理阶段） | 离线资源包/babeldoc 缓存两类不相交目标并行删除 |
| `.github/workflows/fork-build.yml` / `release.yml` | 同模式并行化改造 |
| `frontend/src-tauri/windows/installer-hooks.nsh`（新增） | NSIS 安装钩子：装/卸前 taskkill 应用与 sidecar 进程树消除「文件占用」竞争；卸载用单个 robocopy `/MT:16` 多线程清除 sidecar 资源树（robocopy 0-7 视为成功，≥8 回退顺序删除） |
| `frontend/src-tauri/tauri.conf.json` | 注册 `installerHooks` |

---

## 9. 测试与验证

### 9.1 新增测试（`tests/test_perf_optimizations.py`，26 项，全部离线）
| 组 | 覆盖 |
|---|---|
| `TestSliceSpliceHelpers` | 归一化/切片/mono 回贴+dual 交错/TOC 重映射/v3_output 页号还原 |
| `TestTranslateStreamSliceSplice` | 端到端切片递归拦截验证、熔断开关、emit_ir 强制全文档 |
| `TestMagicPdfPageSlice` | 页选择归一化、切片生成与清理、noop 用例、熔断、parse 调度+页号还原 |
| `TestBabeldocNextWorker` | worker 协议五用例（成功帧/进度流式/退出码 2/退出码 1/坏 payload） |
| `TestBabeldocSubprocessRunner` | stub worker 端到端：进度回调、两种错误映射、取消 kill |
| `TestLayoutModelPrewarm` | 预热加载模型+字体、kill switch 生效 |

配套 `tests/stub_babeldoc_worker.py`（协议桩）与 `tests/test_services_api.py` 批量端点用例（149 行增量）。

### 9.2 回归结果
```
tests/（非 v3）        1246 passed, 6 skipped   (157s)
tests/v3/             1554 passed, 19 deselected*  (69s)
其中 phase1 单独       19 passed               (83s)
frontend tsc --noEmit 通过
```
\* deselect 仅为本轮分批执行的去重，全集无跳过。

### 9.3 已知未修项（记录在案，同基准报告）
- babeldoc 渲染合并占单页运行 ~50%（~29s 固定）：需上游 il_creater/写出改造，本轮未动。
- MinerU analyze 的 OCR/layout 推理热点：属上游 pipeline 内部，页切片已绕开其大头。

---

## 10. 环境开关一览

| 变量 | 默认 | 作用 |
|---|---|---|
| `PDF2ZH_NO_MAGICPDF_SLICE` | 关 | 熔断 magicpdf 页切片（回到全文档 analyze） |
| `PDF2ZH_NO_SLICE_SPLICE` | 关 | 熔断 legacy 切片-回贴（回到全文档路径） |
| `PDF2ZH_BABELDOC_SUBPROCESS` | 关 | babeldoc 任务改跑一次性子进程（RSS 泄积治理） |
| `PDF2ZH_BABELDOC_WORKER_MODULE` | 内置 | 子进程 worker 模块注入点（测试） |
| `PDF2ZH_NO_WARMUP` | 关 | 关闭服务启动预热（CI/离线） |
| `PDF2ZH_BATCH_CONCURRENCY` | 2（≤4） | 批量任务并发文件数 |
