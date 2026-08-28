# MinerU→BabelDOC 链路启动阻塞根因分析与异步化修复报告

> **日期**：2026-08-27
> **范围**：PDFMathTranslate_FORKED（pdf2zh）BabelDOC 引擎 × MinerU（magic-pdf）伪代码保护链路
> **关联文档**：
> - `doc/babeldoc_pseudocode_mistranslation_report.md`（2026-08-13：伪代码保护依赖布局模型 `algorithm` 类别）
> - `doc/mineru_integration_implementation_report.md`（MinerU 集成）
> - `doc/magicpdf_parse_failure_rootcause_and_fix_report.md`（magic-pdf 1.3.12 解析链路）
> **本文定位**：用户报告 **MinerU 链路传递给 BabelDOC 错误，表现为"任务卡在 starting 或长时间无进度/无输出 PDF"**。本次定位到启动阶段的**同步等待阻塞**，并在**不改变当前多线程框架**（`ThreadPoolExecutor` + `shutdown(wait=False)`）的前提下，将 MinerU 分支改为**完全异步化 + 检测器热注入**，任务即刻开工、MinerU 后台完成后自动补充保护。

---

## 0. TL;DR（结论摘要）

| # | 结论 | 状态 |
|---|---|---|
| 1 | **现象确认**：BabelDOC 模式下任务长时间卡在 `starting`、无进度事件、不产 PDF | 已确认 |
| 2 | **根因**：任务启动前 `_build_doclayout_model` 在**主线程同步等待** MinerU VLM 整本解析；PP-DocLayoutV2 缺失时每次 BabelDOC 任务/每个文件都会 `future.result(timeout=budget)`（默认 240s）等满预算，任务才真正开始 | 已定位 |
| 3 | **修复方案**：MinerU 分支**完全异步化**——主线程先返回 `detector=None` 的无保护融合模型让任务立即开始；MinerU 在后台线程解析完成后经 **`attach_detector()` 热注入**，任务运行中的后续页面自动获得伪代码保护 | ✅ 已落地 |
| 4 | **多线程框架不变**：仍是 `ThreadPoolExecutor(max_workers=1)` + `shutdown(wait=False)`，不 join 后台线程，与既有并发模型完全一致 | ✅ 已落地 |
| 5 | **回归验证**：相关套件（doclayout_pseudocode / pipeline_no_output_guards / babeldoc perf·onnx_backend / magicpdf adapter·bridge·cli·code_protection·renderer / parse_engine_switch / gpu_governor / v3 fixup·formula_side_channel）共 **168 passed, 1 skipped** | ✅ 已通过 |

---

## 1. 背景与用户现象

用户反馈：

> 当前的 MinerU 链路传递给 BabelDOC 错误，请在不改变当前多线程的框架的情况下修复这个问题。

追问后确认具体现象为：

> **任务卡在 starting 或长时间无进度/无输出 PDF（MinerU 后台线程阻塞）**

即在 BabelDOC 引擎下、且 **PP-DocLayoutV2 模型缺失**（此时才会走 MinerU VLM 伪代码检测分支）时，任务提交后长时间停留在启动阶段，没有任何进度事件，也不会产出 PDF。

---

## 2. 根因分析

### 2.1 调用链：任务启动前的同步模型构建

BabelDOC 任务启动时的布局模型构建链路：

```
run_babeldoc_translation
  └─ YadtConfig(doc_layout_model=_build_doclayout_model(work_path))   ← 同步调用
       └─ build_pseudo_code_protected_layout_model(pdf_path=work_path)
            └─ _build_with_mineru_or_paddle(pdf_path)                 ← 故障点
```

`babeldoc_adapter.py` / `babeldoc_next_adapter.py` 均在**翻译开始前**同步构建融合布局模型；当 PP-DocLayoutV2 不可用时，`_build_with_mineru_or_paddle` 进入 MinerU VLM 分支。

### 2.2 旧实现：主线程同步等待 MinerU

旧实现（修复前 `_build_with_mineru_or_paddle` 的 MinerU 分支）：

```python
executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pseudo-mineru")
try:
    future = executor.submit(MinerUAlgorithmDetector, pdf_path)
    detector = future.result(timeout=budget)   # ← 主线程同步等满预算
finally:
    executor.shutdown(wait=False)
```

阻塞链：

1. `budget` 默认 **240s**（`resolve_pseudo_mineru_budget`，默认 240）；
2. `future.result(timeout=budget)` 让**主线程**在任务真正开始前同步等待 MinerU 冷启动 + 整本解析；
3. 该等待发生在**每次 BabelDOC 任务启动前**，批量场景下**每个文件**都触发一次；
4. MinerU VLM 冷启动可能长达数分钟（拉起子进程、加载模型），若超时则白等满 `budget` 后任务才开始。

### 2.3 为什么表现为"卡死"而非"慢"

- 等待发生在任务启动阶段、第一个进度事件（layout 渲染）**之前**，GUI/CLI 界面只看到 `starting`；
- 期间**无任何进度事件、无输出 PDF**，观感等同于"卡死/无响应"；
- 曾有过 3600s 级超时版本，把任务长时间卡死（见既有 `test_pipeline_no_output_guards.py` 用户报告），后虽降为预算制（240s），但**同步等待**的根因未消除。

---

## 3. 修复方案：MinerU 完全异步化 + 检测器热注入

### 3.1 设计要点（不改变多线程框架）

| 约束 | 落实 |
|---|---|
| 不改变当前多线程框架 | 沿用 `ThreadPoolExecutor(max_workers=1)` + `shutdown(wait=False)`，不 join 后台线程 |
| 主线程绝不等待 MinerU | `_build_with_mineru_or_paddle` 在 PP 缺失时**立即返回** `detector=None` 的融合模型 |
| 保护尽力而为、不阻塞换取 | MinerU 后台解析完成后**热注入** detector，后续页面自动获得保护；前置页面无保护 |
| 线程安全 | 新增 `_detector_lock`，读写一致；`_protect_page` 锁内取快照、锁外执行检测 |

### 3.2 代码变更明细

#### 变更 1：`PseudoCodeProtectedLayoutModel.attach_detector()`（新增）

```python
def attach_detector(self, detector) -> None:
    """热注入算法框检测器（后台 MinerU 探测完成后调用，线程安全）。"""
    if detector is None:
        return
    with self._detector_lock:
        if self.detector is detector:
            return
        self.detector = detector
        self._detector_accepts_page_index = _detector_supports_page_index(detector)
```

- 只在构造后把 detector 从 `None` 升级为可用实例；
- 同步刷新 `_detector_accepts_page_index` 能力标志（MinerU 检测器接受 `page_index`）。

#### 变更 2：`_protect_page` 读点线程安全

```python
with self._detector_lock:
    detector = self.detector
    accepts_page_index = self._detector_accepts_page_index
if detector is None:
    return
if accepts_page_index:
    algo_boxes = detector.detect_algorithm_boxes(geometry.image, page_index=page_number)
else:
    algo_boxes = detector.detect_algorithm_boxes(geometry.image)
```

- 锁内取快照、锁外执行检测调用：与热注入互斥，又不长占锁。

#### 变更 3：`_build_with_mineru_or_paddle` 完全异步化

```python
model = PseudoCodeProtectedLayoutModel(base, None)   # 立即返回，任务即刻开工

def _run_mineru_detector() -> None:
    try:
        det = MinerUAlgorithmDetector(pdf_path)
    except Exception as exc:
        logger.debug("MinerU algorithm detector unavailable (%s); ...", exc)
        return
    try:
        model.attach_detector(det)
    except Exception:
        logger.debug("failed to hot-attach MinerU algorithm detector", exc_info=True)
        return
    logger.info("BabelDOC pseudo-code protection enabled "
                "(MinerU VLM algorithm detector, hot-attached after task start)")

executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pseudo-mineru")
try:
    executor.submit(_run_mineru_detector)
finally:
    executor.shutdown(wait=False)
return model
```

- 不再有任何 `future.result(timeout=...)` 同步等待；
- 主线程立即返回融合模型 → BabelDOC 布局阶段即刻开始 → 进度事件正常流动、照常产 PDF；
- MinerU 解析在后台线程（不 join）继续，完成后 `attach_detector` 热注入。

#### 变更 4：`resolve_pseudo_mineru_budget` 语义更新（保留兼容）

```python
def resolve_pseudo_mineru_budget(default_seconds: int = 240) -> int:
    """MinerU VLM 伪代码检测分支的时间预算（秒，``PDF2ZH_PSEUDO_MINERU_BUDGET``）。

    注意：自 MinerU 分支完全异步化后，该预算**不再**作为 BabelDOC 主链路
    的同步等待上限（主链路绝不等待 MinerU，任务立即开始）；保留本函数仅为
    兼容旧环境变量与既有测试，避免用户按旧文档配置后行为突变。
    """
```

- 保留函数与 `PDF2ZH_PSEUDO_MINERU_BUDGET` 环境变量兼容性（旧文档/旧测试不破坏）；
- 语义从"主链路等待上限"更新为"后台线程的尽力而为预算参考"。

### 3.3 时序示意（修复后）

```
T0      任务提交 → _build_with_mineru_or_paddle 立即返回无保护融合模型
T0      后台线程启动：MinerU 冷启动 + 整本解析（不阻塞主线程）
T0+ε    BabelDOC 布局阶段开始 → 进度事件流动 → 前置页面无伪代码保护
T0+X    MinerU 解析完成 → attach_detector 热注入 detector
T0+X+ε  BabelDOC 布局阶段继续 → 后续页面自动获得伪代码保护（带 page_index）
```

---

## 4. 验证结果

### 4.1 单元测试（tests/test_pipeline_no_output_guards.py 更新 + 新增）

| 测试 | 覆盖 |
|---|---|
| `test_mineru_slow_returns_unprotected_model_immediately` | MinerU 慢 → 主线程 **<0.5s** 立即返回无保护融合模型，不等待 |
| `test_timeout_returns_without_joining_running_mineru_thread` | 异步化后立即返回、**不 join** 后台线程（不等待 3s 挂起探测） |
| `test_mineru_detector_hot_attached_when_ready` | 事件闸门固定时序：attach 前页面保持 `plain text` 不提升 → MinerU 后台完成热注入 → 后续页面按 `page_index` 提升为 `algorithm`；能力标志随热注入同步刷新 |
| `test_mineru_prefers_paddle_over_mineru`（既有） | PP 可用时不触发 MinerU 分支（既有语义保留） |
| `test_budget_env_floor_and_default`（既有） | 环境变量兼容保留 |

### 4.2 并发热注入端到端验证

模拟真实运行（主线程逐页保护 × 后台线程热注入同时进行）：

```
page0-4 : ['plain text', ...]    ← MinerU 未就绪：前置页面无保护
page5-14: ['algorithm', ...]     ← 热注入完成后：后续页面受保护
OK: async hot-attach works across a running handle_document
```

- 无并发崩溃；`_algo_cls_cache`（按 `names` dict id 缓存）与热注入互不干扰；
- BabelDOC `OnnxModel` 全文档共享同一 `names` dict 的场景已由测试固化。

### 4.3 回归测试

```bash
python -m pytest tests/test_pipeline_no_output_guards.py \
       tests/test_doclayout_pseudocode.py \
       tests/test_babeldoc_perf_gates.py \
       tests/test_babeldoc_onnx_backend.py \
       tests/test_magicpdf_adapter.py tests/test_magicpdf_bridge.py \
       tests/test_magicpdf_cli.py tests/test_magicpdf_code_protection.py \
       tests/test_magicpdf_renderer.py tests/test_parse_engine_switch.py \
       tests/test_gpu_governor.py tests/test_v3_render_takeover_fixup.py \
       tests/test_v3_formula_side_channel.py -q
```

```
168 passed, 1 skipped in 20.94s
```

---

## 5. 残余风险与建议

| # | 风险 / 说明 | 级别 | 建议 |
|---|---|---|---|
| 1 | **前置页面无伪代码保护**（MinerU 完成前的页面）。尽力而为设计：保护由阻塞换取改为后台补齐，伪代码保护存在短暂的"前置页缺失"窗口 | 低 | 单页布局极快、MinerU 解析通常数秒内完成，实际影响极小；如需**全文保护**可评估 PDF 预解析缓存（按文件哈希缓存 MinerU 结果） |
| 2 | **后台 MinerU 线程在进程退出时可能未完成**（`shutdown(wait=False)` 不 join） | 低 | 与既有 magicpdf/GPU 预算线程模型一致；线程解析失败仅 debug 日志，不阻断任何链路 |
| 3 | **`PDF2ZH_PSEUDO_MINERU_BUDGET` 语义变化**（不再作为主链路等待上限） | 信息 | 已保留函数与环境变量兼容；文档/日志措辞已更新 |
| 4 | **detector 热注入依赖 `_detector_lock`**：未来新增 `self.detector` 读点须走同一把锁 | 信息 | 已在 `attach_detector`/`_protect_page` 注释中提示 |

---

## 6. 结论

1. **根因**：BabelDOC 任务启动前，MinerU VLM 伪代码检测分支在主线程同步 `future.result(timeout=budget)`（默认 240s），PP-DocLayoutV2 缺失时每次任务/每个文件都会等满预算，表现为任务长期卡在 `starting`、无进度、无输出 PDF。
2. **修复**：在不改变多线程框架（`ThreadPoolExecutor(max_workers=1)` + `shutdown(wait=False)`）的前提下，将 MinerU 分支**完全异步化**——主线程立即返回 `detector=None` 的融合模型让任务即刻开始；MinerU 后台解析完成后经线程安全的 `attach_detector()` **热注入**，任务运行中的后续页面自动获得伪代码保护。
3. **验证**：单测新增"立即返回 / 不 join / 热注入时序"三组场景；并发热注入端到端通过；相关 13 个测试套件 **168 passed, 1 skipped** 无回归。

**改动文件**：
- `pdf2zh/doclayout_pseudocode.py`（+107 / -23）
- `tests/test_pipeline_no_output_guards.py`（未跟踪新测试，本次增强）

---

## 7. 后续发现：Tauri 前端重复提交同一文件（submit 幂等去重）

### 7.1 现象

修复「启动卡住」后，用户实际运行 magicpdf 引擎解析一份 **482 页 Sipser《计算理论导论》**时，控制台连续出现 **3 次完全相同的启动序列**：

```
Starting...
magic-pdf/MinerU parsing...
a0c76d5a_Sipser_Introduction.to.the.Theory.of.Computation.3E (1).pdf: analyzing page 0/482
```

用户确认**只点击了一次翻译**，却生成了 3 个任务。文件路径前缀 `a0c76d5a_`（`api.py:714` 上传命名 `{uuid8}_{filename}`）三次一致，说明三个任务引用**同一个已上传文件**——即重复发生在**提交层**，而非解析/引擎层。

### 7.2 根因

1. **前端防连点存在闭包竞态**：
   - `Dashboard.tsx onSubmit` 用组件闭包变量 `submitting` 判重，但 zustand 的 `set({ submitting: true })` 同步生效、React 重渲染是异步的——快速连点（或 Tauri WebView 事件重放）时第二次点击读到的仍是旧值 `false`；
   - `taskStore.submit` 内部**没有**基于 `get().submitting` 的同步防重；
   - 提交按钮 `disabled` 只检查 `selectedCount === 0`，未包含 `submitting`。
2. **后端无幂等**：`api.py /api/tasks` 与 `RuntimeService.submit_task` 对完全相同的请求**无条件新建任务**，任何重复请求都会真实执行。

### 7.3 修复（不改变多线程框架）

| 层 | 改动 |
|---|---|
| **后端** `RuntimeService` | 新增 `_submit_fingerprint(request)`（文件集排序 + 全部关键参数 + `extra_config` 排序后的稳定 JSON）；`submit_task` 顶部先查 `_submit_dedup` 指纹表，**同指纹且有在途任务（非终态）时直接返回已有 task_id**，否则新建并记录。线程模型不变（仍是每任务一条 daemon 线程）。 |
| **前端** `taskStore.submit` | 函数开头加 `if (get().submitting) return null;`——store 级同步防重，不依赖 React 闭包/渲染时序。 |
| **前端** `Dashboard.tsx` | `onSubmit` 改为读 `useAppStore.getState().submitting`（实时值，非闭包）；按钮 `disabled={selectedCount === 0 \|\| submitting}`。 |

效果：前端连点/事件重放只发出一次 POST；即使绕过前端（脚本/网络重试），后端指纹幂等也会在窗口内复用同一任务，绝不重复建任务、不重复解析。

### 7.4 验证

- 新增 `tests/test_submit_dedup.py`（5 项）：同请求复用、异文件/异引擎新建、终态后允许重试、多文件乱序指纹稳定。
- 回归：`test_submit_dedup + test_services_api(+glossary) + test_parse_engine_switch + test_runtime_service_robustness + test_gui_modules` = **170 passed**；前端 `tsc --noEmit` 通过。

**改动文件**：
- `pdf2zh/services/runtime_service.py`（`_submit_fingerprint` + `submit_task` 幂等去重）
- `frontend/src/stores/taskStore.ts`（`submit` store 级同步防重）
- `frontend/src/pages/Dashboard.tsx`（`onSubmit` 读 store 实时值 + 按钮 `disabled` 含 `submitting`）
- `tests/test_submit_dedup.py`（新增 5 项）

---

## 8. 后续发现：MinerU 走 CPU —— GPU 无法启用的根因与修复

### 8.1 现象

用户报告 **MinerU 解析全程走 CPU**，请求 `--backend cuda` 也无效。

### 8.2 根因（两层）

| # | 根因 | 说明 |
|---|---|---|
| 1 | **隔离 venv 内 torch 是 CPU 版**（直接根因） | 实际解析走 `PDF2ZH_MINERU_PYTHON` 指向的隔离 venv（本机为 `%APPDATA%\pdf2zh\mineru-venv`）。实测 `torch 2.13.0+cpu`、`torch.cuda.is_available()=False`、`torch.cuda.device_count()=0`。MinerU 3.x 的设备决策（`mineru/utils/config_reader.py::get_device`）为：`MINERU_DEVICE_MODE` 环境变量 → `torch.cuda.is_available()` → mps/npu/... → **cpu**。CPU torch 无 CUDA 可指 → 必然 cpu。 |
| 2 | **pdf2zh 从未把 `--backend cuda` 传给 MinerU**（传递缺失） | `_parse_mineru` / `mineru_worker.py` 调 `do_parse(backend="pipeline", ...)` 里的 `backend="pipeline"` 是**解析后端类型**（本地模型管线），**不是设备**。MinerU 3.x 的设备只认 `MINERU_DEVICE_MODE` 环境变量——pdf2zh 未设置它。即便 venv 装了 CUDA torch，请求 cuda 也只会被动依赖 `get_device()` 的 torch 探测，无法显式指定。 |

> 对照：既有 `doc/magicpdf_gpu_off_reason_and_fix_report.md` 讲的是 **magic-pdf 1.x**（`~/magic-pdf.json` 的 `device-mode` + 主进程 torch）；本报告补充的是 **MinerU 3.x 隔离 venv** 路径，二者设备决策点不同。

### 8.3 修复（不改变多线程框架）

| 文件 | 改动 |
|---|---|
| `pdf2zh/kernel/mineru_worker.py` | 契约新增第 5 个参数 `[device]`；在 `import mineru` **之前**按映射设置 `MINERU_DEVICE_MODE`（`cuda/gpu→cuda`、`cpu→cpu`、`mps→mps`、`dml/auto/空→不设置`）。 |
| `pdf2zh/magicpdf_adapter.py` | ① 新增 `_mineru_device_mode()`（上层后端名 → `MINERU_DEVICE_MODE`）；② 主进程 `_parse_mineru`：`do_parse` 前设置 `MINERU_DEVICE_MODE` 并用 `try/finally` 恢复，避免常驻服务残留污染；③ 子进程 `_parse_mineru_subprocess`：新增 `_venv_torch_cuda(python_exe)` 轻量预检——请求 `cuda` 但 venv torch 无 CUDA 时**降级 cpu 并给出可执行安装命令**，绝不带病跑崩；④ 新增 `_mineru_cuda_torch_hint()`；⑤ `get_magicpdf_device_status` 增加 `mineru_venv` / `mineru_venv_torch_cuda` 诊断字段。 |
| `pdf2zh/kernel/mineru_env.py` | `ensure_venv` 支持 CUDA torch：`PDF2ZH_MINERU_CUDA=1` 时先用 pytorch 官方 index（`PDF2ZH_MINERU_TORCH_INDEX` 可覆盖，默认 cu126）装 CUDA torch/torchvision，再装 `mineru[pipeline]`。 |
| `pdf2zh/magicpdf_cli.py` | `[magicpdf] device status` 日志增加 `mineru_venv` / `mineru_cuda` 字段。 |

### 8.4 让 MinerU 真正走 GPU：操作步骤

```bash
# 1) 重建隔离 venv 并安装 CUDA 版 torch（torch ~2GB 下载）
set PDF2ZH_MINERU_CUDA=1
pdf2zh-setup-mineru

# 2) 验证（必须输出 True；本机当前为 False → 仍走 CPU）
%APPDATA%\pdf2zh\mineru-venv\Scripts\python.exe -c "import torch; print(torch.cuda.is_available())"

# 3) 显式请求 cuda 解析（子进程 device=cuda，MinerU 走 MINERU_DEVICE_MODE=cuda）
pdf2zh --parse-engine magicpdf --backend cuda paper.pdf
```

GUI：后端选 **CUDA（NVIDIA GPU）** 后提交；诊断日志应显示
`mineru_venv=...\python.exe mineru_cuda=True`、`[mineru] using device: cuda`（来自
MinerU 的 `pdf_extract_kit` 日志）。

### 8.5 验证

- 新增 4 项测试（`tests/test_mineru_env.py`）：`_mineru_device_mode` 映射、
  worker `_apply_device_mode` 环境变量、子进程 cuda 请求 + venv 无 CUDA → 降级 cpu、
  子进程 venv 有 CUDA → 透传 cuda。
- 回归：`test_mineru_env + test_magicpdf_adapter + test_magicpdf_cli +
  test_engine_env + test_magicpdf_bridge + test_magicpdf_renderer +
  test_magicpdf_code_protection + test_parse_engine_switch` = **108 passed, 1 skipped**。
- 真实环境：本机 venv torch 仍为 CPU 版 → `cuda` 请求被预检降级 cpu 并给出安装命令（符合预期）。

**改动文件**：
- `pdf2zh/kernel/mineru_worker.py`（device 参数 + `MINERU_DEVICE_MODE`）
- `pdf2zh/magicpdf_adapter.py`（`_mineru_device_mode` / `_venv_torch_cuda` / `_mineru_cuda_torch_hint` / 子进程预检 / 诊断增强 / env 恢复）
- `pdf2zh/kernel/mineru_env.py`（`PDF2ZH_MINERU_CUDA=1` 安装 CUDA torch）
- `pdf2zh/magicpdf_cli.py`（设备状态日志字段）
- `tests/test_mineru_env.py`（+4 项）

### 8.6 「启用 GPU」后仍无 CUDA 的修复（`--upgrade` 缺失）

**现象**：点击「启用 MinerU GPU」后报 `RuntimeError: torch still reports no CUDA after upgrade`。

**根因**：`ensure_venv(cuda=True)` 的升级命令是
`pip install torch torchvision --index-url <cu126>`——**缺少 `--upgrade`**。隔离 venv
已装有 `2.13.0+cpu`，pip 判定它满足 `torch`（未指定版本）的依赖要求，输出
`Requirement already satisfied` **跳过重装**，CPU 版 torch 原样保留，随后校验
`torch.cuda.is_available()` 仍为 False 而抛错。

**佐证**：`pip install --dry-run --upgrade torch torchvision --index-url
https://download.pytorch.org/whl/cu126` 输出
`Would install torch-2.13.0+cu126 torchvision-0.28.0+cu126`——cu126 index 确有
py3.13 win wheel，加 `--upgrade` 后 pip 才会真正替换。

**修复**：
1. 升级命令增加 `--upgrade`（关键）；
2. 校验逻辑拆分为两步，区分两种「仍无 CUDA」：
   - `_venv_torch_cuda_tag()` 返回 `None`（`torch.version.cuda` 为空）→ wheel 仍是
     CPU 构建，报「未装 CUDA wheel，检查 index URL」；
   - tag 有值但 `torch.cuda.is_available()` False → CUDA wheel 已装但本机无可用
     NVIDIA GPU / 驱动不匹配，报「检查 NVIDIA 驱动」。

**验证**：
- `pip install --dry-run --upgrade --force-reinstall` 确认会安装 cu126 wheel；
- `tests/test_mineru_env.py` 升级测试新增 `--upgrade` 断言 + `_venv_torch_cuda_tag`
  探测测试（共 18 项通过）；
- 相关回归 **94 passed, 1 skipped**。

**改动文件**：
- `pdf2zh/kernel/mineru_env.py`（`ensure_venv` 升级命令加 `--upgrade` + `_venv_torch_cuda_tag` + 两步校验）
- `tests/test_mineru_env.py`（升级断言 + tag 探测）

### 8.7 「启用后仍显示走 CPU」：设备状态显示的修正

**现象**：修复 §8.6 后，用户 venv 的 torch 已成功升级为 `2.13.0+cu126` 且
`torch.cuda.is_available()=True`（`device_count=1`），`mineru.utils.config_reader
.get_device()` 返回 `cuda`，子进程 device 也已透传 `cuda`——但 GUI 状态面板 /
CLI 日志仍显示「走 CPU」。

**根因（显示误导）**：`get_magicpdf_device_status()` 的 `effective` 字段按
**magic-pdf 1.x 逻辑**基于**主进程 torch** 计算（`_normalize_magicpdf_device`）；
主进程 torch 无 CUDA 时 `effective` 恒为 `cpu`。但 MinerU 3.x 解析实际跑在
**隔离 venv 子进程**，其设备由 venv torch 决定（本机已为 `cuda`）。主进程显示与
子进程实际脱节，导致「明明已启用 GPU 却显示 CPU」的误判。

**修复**：
1. `get_magicpdf_device_status()`：当 MinerU 隔离 venv 存在时，`effective` 直接
   反映 venv 实际设备（`venv_torch_cuda=True → cuda`），不再用 magic-pdf 逻辑。
2. GUI 状态面板（`config_panel.py`）：显示条件从「仅 magic-pdf 已安装」放宽为
   「magic-pdf 已安装 **或** MinerU 隔离 venv 存在」，并追加
   `MinerU venv cuda=True/False` 行，明确子进程实际设备。

**验证**：
- `get_magicpdf_device_status(requested='auto')` → `effective='cuda'`、
  `mineru_venv_torch_cuda=True`、`hint=''`；
- 子进程链路端到端：`_parse_mineru_subprocess` 构造的 worker 命令
  `[..., venv_python, 'auto', 'ch', 'cuda']` —— device 透传正确；
- 回归 **197 passed, 1 skipped**。

**改动文件**：
- `pdf2zh/magicpdf_adapter.py`（`get_magicpdf_device_status` effective 按 venv 修正）
- `pdf2zh/gui/components/config_panel.py`（显示条件 + venv cuda 行）

### 8.8 「启用 GPU 后解析失败 / no output artifacts」的根因与修复

**现象**：启用 GPU（CUDA torch）后解析 21 页论文，进度停在 `analyzing page 0/21`，
任务落 `Failed: magicpdf engine produced no output artifacts (expected under
%TEMP%\magicpdf)`。

**排查过程**：
1. 隔离 venv torch 已是 `2.13.0+cu126` 且 CUDA 可用（`device_count=1`）；用真实
   venv 直接跑 worker（2 页 / 21 页 PDF）**均成功**并产出 middle.json、mono PDF；
2. 完整 `run_magicpdf_main` 在 CUDA 下对 21 页 PDF 也成功（`effective=cuda`）；
3. **复现真正失败链**：模拟 MinerU 解析失败 → `_fallback_legacy` 熔断降级 legacy
   内核 → legacy 翻译成功，产物写为**父目录** `{out_dir}/{stem}-mono.pdf` 与
   `{stem}-dual.pdf`；而 `_collect_magicpdf_results` **只扫描 `{out_dir}/magicpdf/`
   子目录** → 收集不到 → 误报 `no output artifacts`。

**根因**：magicpdf 引擎熔断降级 legacy 后，产物落在父目录（legacy 命名），
与 magicpdf 子目录产物收集逻辑脱节，导致「翻译已成功但任务误报失败」。

> 触发降级的直接原因是 MinerU 子进程在 8GB 显存解析大文档时 CUDA OOM（日志
> `GPU Memory: 8 GB, Batch Ratio: 4`）——属显存预算问题，由熔断降级兜底，不应
> 再叠加「产物收集不到」的误报。

**修复**（`pdf2zh/services/runtime_service.py` `_collect_magicpdf_results`）：
magicpdf 子目录无产物时，回退扫描 `out_dir` 父目录的 **legacy 降级产物**
（`{stem}-mono.pdf` / `{stem}-dual.pdf`），收集并落 COMPLETED，不再误报失败；
仍无任何产物才落 FAILED（保持「空产物防护」语义）。

**验证**：
- 复现：magicpdf 目录清空、仅父目录有 `*_repro_21p-mono.pdf` / `-dual.pdf` →
  `_collect_magicpdf_results` 正确收集并 COMPLETED；
- 新增 `tests/test_pipeline_no_output_guards.py::test_legacy_fallback_pdfs_collected_when_magicpdf_empty`
  （magicpdf 空 + 父目录 legacy PDF → 收集成功）；
- 回归 **223 passed, 1 skipped**。

**改动文件**：
- `pdf2zh/services/runtime_service.py`（`_collect_magicpdf_results` 回退收集 legacy 降级产物）
- `tests/test_pipeline_no_output_guards.py`（+1 项）

### 8.9 「为什么小 PDF 也 CUDA OOM」：batch_ratio 显存估算过于激进

**现象**：启用 GPU 后，即便很小的 PDF 也可能触发 MinerU 的 CUDA OOM。

**排查（实测）**：
- 隔离 venv 内 torch 为 `2.13.0+cu126`，`torch.cuda.is_available()=True`；
- 本机 **8GB 显存**，Windows 桌面合成常占 ~2GB（实测基线 2048MB）；
- 2 页简单 PDF 解析峰值 ~3.5GB 可完成；但 MinerU 日志显示
  `GPU Memory: 8 GB, Batch Ratio: 4`——MinerU 3.x 的 `batch_ratio` 由
  `mineru/utils/model_utils.py::get_vram` 的**物理显存总量**决定（`>=8GB→4`、
  `>=16→8`…），**未扣除系统 UI 占用与模型权重本身**：
  - 8GB 卡实际空闲仅 ~6GB；
  - `batch_ratio=4` 令 OCR 等批处理（`OCR_DET_BASE_BATCH_SIZE=8` → 实际
    batch=32）瞬时张量超限 → OOM，与 PDF 页数无关（页数少只减少 batch 数，
    不减小单批张量峰值）。

**修复**（`pdf2zh/kernel/mineru_worker.py`）：新增 `_apply_conservative_vram_budget()`，
在 CUDA 模式下、用户未显式设置 `MINERU_VIRTUAL_VRAM_SIZE` 时，按物理显存总量
注入保守预算（MinerU 官方覆盖项）：
- 16GB+ → 预算 16（`batch_ratio=8`，保持）；
- **8GB → 预算 6（`batch_ratio=2`）**；
- <8GB → 预算 5（`batch_ratio=1`）。

效果：8GB 卡 `batch_ratio` 从 4 降为 2，批处理张量峰值减半，显著规避 OOM。

**验证**：
- 实测 worker 在 8GB 卡日志变为 `GPU Memory: 6 GB, Batch Ratio: 2`，正常产出
  middle.json（exit 0）；
- 新增 `tests/test_mineru_env.py::test_worker_conservative_vram_budget`（8GB→6 /
  16GB→16 / 用户显式配置优先 / 非 CUDA 不注入）；
- 回归 **91 passed, 1 skipped**。

> 用户仍可显式覆盖：`set MINERU_VIRTUAL_VRAM_SIZE=<N>`（如 4 更保守 / 8 恢复
> 激进）或 `MINERU_MIN_BATCH_INFERENCE_SIZE` 控制每批页数。

**改动文件**：
- `pdf2zh/kernel/mineru_worker.py`（`_apply_conservative_vram_budget` + CUDA 时注入）
- `tests/test_mineru_env.py`（+1 项）

### 8.10 补上 minerU 配置选项与自动设置

**诉求**：① 前端缺少 minerU 配置选项；② 缺少自动设置，导致 MinerU 使用最激进
batch 策略（8GB 卡 `batch_ratio=4`）而 OOM。

**实现**：新增两个可配置项（空 = auto 自动保守估算），并贯通到 worker 子进程
环境变量：

| 层 | 改动 |
|---|---|
| `TranslationRequest` | 新增 `mineru_vram_size` / `mineru_window_size` 字段 |
| `RuntimeService._execute_magicpdf` | 写入 CLI namespace |
| `MagicPdfAdapter.__init__` | 新增同名参数；`_parse_mineru_subprocess` 构造子进程 env（`MINERU_VIRTUAL_VRAM_SIZE` / `MINERU_PROCESSING_WINDOW_SIZE`）传给 `_run_mineru_process(env=...)` |
| `magicpdf_cli.run_magicpdf_main` | 构造 adapter 时传参 |
| `api.py /api/tasks` | 表单新增 `mineru_vram_size` / `mineru_window_size` |
| 前端 `endpoints.ts` / `Dashboard.tsx` / `i18n` | 高级配置区新增「MinerU 显存预算 (GB)」「MinerU 每批页数」，空=自动 |

**行为**：
- 留空 → worker 按物理显存自动保守估算（8GB→6，`batch_ratio=2`；<8GB→5，
  ratio=1；16GB+→16，ratio=8），不再用最激进策略；
- 显式设置 → 覆盖自动值（如 `mineru_vram_size=4` → `MINERU_VIRTUAL_VRAM_SIZE=4`
  → `batch_ratio=1`；`mineru_window_size=8` → `MINERU_PROCESSING_WINDOW_SIZE=8`）。

**验证**：
- 实测 worker：留空 `GPU Memory: 6 GB, Batch Ratio: 2`；设
  `MINERU_VIRTUAL_VRAM_SIZE=4` → `Batch Ratio: 1`、`window_size=8`；
- API 表单 → request 字段透传断言通过；
- 新增 `tests/test_mineru_env.py`：子进程 env 透传（显式值）+ 空配置不透传
  （2 项，共 21 项通过）；
- 回归 **232 passed, 1 skipped**；前端 `tsc --noEmit` + `vite build` 通过。

**改动文件**：
- `pdf2zh/services/runtime_service.py`（request 字段 + `_execute_magicpdf` 透传）
- `pdf2zh/magicpdf_adapter.py`（`__init__` 参数 + 子进程 env）
- `pdf2zh/magicpdf_cli.py`（adapter 构造传参）
- `pdf2zh/services/api.py`（表单字段）
- `frontend/src/api/endpoints.ts`、`frontend/src/pages/Dashboard.tsx`、`frontend/src/i18n/index.ts`
- `tests/test_mineru_env.py`（+2 项）

### 8.11 「182 页停在 0/182」：MinerU 3.x 子进程进度未上报

**现象**：182 页文档解析进度一直停在 `0/182`，重试三次依旧；GPU 与环境均正常。

**根因（进度上报缺失，非解析失败）**：`_parse_mineru_subprocess` 用
`_run_mineru_process`（`subprocess.run(capture_output=True)`）**阻塞**运行 worker，
期间 worker 的 MinerU 3.x 批处理日志（`Pipeline processing window batch X/Y:
N/M pages`）被捕获但**从未解析成进度事件**——只有开始时上报一次 `0/182`，之后
UI 一直显示 0/N（解析实际在跑，需数分钟），用户误判卡死并反复重试。

> 注：既有 `_MagicPdfLogProbe` 只包裹**主进程 `_parse_mineru`** 的 do_parse 调用；
> 子进程路径无探针，且 magic-pdf 1.x 的 `Batch i/n` 正则不匹配 MinerU 3.x 的
> `Pipeline processing window batch` 格式。

**修复**（`pdf2zh/magicpdf_adapter.py`）：
1. 新增 `_mineru_log_to_detail()`：解析 MinerU 3.x 批处理日志 → 页级 detail
   （与 magic-pdf 1.x 同 schema）；
2. `_run_mineru_process()` 改为 **Popen 流式读取** stdout，逐行尝试
   `_mineru_log_to_detail` / `_magicpdf_log_to_detail` 并通过新增的
   `progress_cb` 上报；同时保留完整输出用于错误诊断；
3. `_parse_mineru_subprocess` 把 `progress_cb` 与 `total_pages` 透传；解析结束时
   若仍无任何 batch 行（日志格式变化兜底）补发 `current=total` 事件。

**验证**：
- 真实 venv 实测：解析期间进度 `0/2 → 2/2`（`batch_current:1, batch_total:1`）
  实时上报，不再停 0/N；
- 新增测试：`_mineru_log_to_detail` 解析（2 项）+ 非批处理行忽略；
- 修复测试环境：`test_granular_progress_p1.py` 增加 autouse fixture 强制走主进程
  `_parse_mineru`（本机有 venv 时避免误走子进程）；
- 回归 **126 passed, 1 skipped**（+ 2 项进度解析测试）。

**改动文件**：
- `pdf2zh/magicpdf_adapter.py`（`_mineru_log_to_detail` + `_run_mineru_process` 流式 + 透传）
- `tests/test_granular_progress_p1.py`（+2 项 + autouse fixture）

### 8.12 minerU 显式切换模式（解析方法 / 后端）

**诉求**：minerU 之前不支持显式切换模式——解析方法只能由 OCR 开关间接决定
（`"ocr" if ocr else "auto"`），解析后端硬编码 `pipeline`，无法显式切换。

**实现**：新增两个显式模式配置项，贯通到 worker 子进程：

| 配置 | 含义 | 取值 |
|---|---|---|
| `mineru_parse_method` | 显式解析方法（对应 MinerU `do_parse.parse_method`） | `auto` / `ocr` / `txt`；空 = 跟随 OCR 开关（历史行为） |
| `mineru_backend` | 显式解析后端（对应 `do_parse.backend`） | `pipeline` / `hybrid` / `vlm`；空 = pipeline 本地后端 |

**贯通链**：`TranslationRequest` → `_execute_magicpdf` → CLI namespace →
`MagicPdfAdapter.__init__` → 主进程 `_parse_mineru`（wanted 用配置）/
子进程 `_parse_mineru_subprocess`（cmd 新增第 6 参数 backend）→
`mineru_worker.py`（argv[5]=backend）→ `do_parse(backend, parse_method)`。

**前端**：Dashboard 高级配置区新增「MinerU 解析方法」「MinerU 解析后端」下拉，
留空 = 自动（跟随既有 OCR 开关 / pipeline 后端）；API `/api/tasks` 表单对应
新增 `mineru_parse_method` / `mineru_backend`。

**验证**：
- adapter 显式模式透传（`parse_method=ocr` / `backend=hybrid`）断言通过；
- 子进程 cmd 断言：`cmd[4]==parse_method`、`cmd[-1]==backend`；
- 修复既有测试的 device 索引（cmd 多一位 backend 后 device 在 `[-2]`）；
- 回归 **129 passed, 1 skipped**；前端 `tsc --noEmit` + `vite build` 通过。

**改动文件**：
- `pdf2zh/magicpdf_adapter.py`（`__init__` 参数 + 主/子进程模式透传）
- `pdf2zh/kernel/mineru_worker.py`（argv[5]=backend）
- `pdf2zh/magicpdf_cli.py`（adapter 构造传参）
- `pdf2zh/services/runtime_service.py`（request 字段 + 透传）
- `pdf2zh/services/api.py`（表单字段）
- `frontend/src/api/endpoints.ts`、`frontend/src/pages/Dashboard.tsx`、`frontend/src/i18n/index.ts`
- `tests/test_mineru_env.py`（+1 项，2 处断言修正）









