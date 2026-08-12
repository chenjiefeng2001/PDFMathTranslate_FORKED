# Ctrl+C × Worker 初始化竞态修复报告（V3-3 迭代）

> 范围：GUI（`python -m pdf2zh.gui.app`）下 Ctrl+C 触发 `WorkerBootstrapError` →
> 整文档串行兜底 的真实场景根因与三层修复
> 日期：2026-08-10 ｜ 关联：`parallel_runtime_v3_iteration_report.md`（P3/P4）

---

## 1. 现场日志与问题定义

用户在 GUI 下启动翻译（218 页），文档已 `prewarmed`，随后按 Ctrl+C，日志序列：

```
Keyboard interruption in main thread... closing server.
Exception in initializer:  ... KeyboardInterrupt      # ×5（worker 死于 onnx 加载中途）
Parallel worker pool crashed (A process ...); chunk N queued for serial fallback   # ×5
Parallel engine degraded cleanly (WorkerBootstrapError: ...); full serial fallback  # ← 218 页全量串行
  9%|█████████████████▉  ...
```

**矛盾点**：V3 报告（§5.4/§5.5）已声明“Ctrl+C 绝不进入串行兜底”，为何又全量串行？

### 根因（两条独立链路叠加）

1. **主线程的 KeyboardInterrupt 永远到不了 coordinator**。
   `RuntimeService.submit_task` 把翻译任务放在 daemon 线程（`_execute_task`）；
   gradio 在主线程 `block_thread()` 捕获 KeyboardInterrupt 后**只关闭服务器**，
   不会向翻译线程传播任何中断。因此 `coordinator.run()` 的
   `except KeyboardInterrupt: raise` 短路在 GUI 场景是**死代码**。

2. **Windows 控制台 CTRL_C_EVENT 广播杀死 worker**。控制台 Ctrl+C 会广播给
   同控制台的所有进程；正在 `init_worker_process` 里加载 ONNX 模型的 worker
   收到 `KeyboardInterrupt` 直接死在 initializer → `ProcessPoolExecutor` 报
   `BrokenProcessPool` → coordinator 按“启动即崩”语义抛 `WorkerBootstrapError`
   → `high_level.py` 兜底**整文档串行重跑** —— **Ctrl+C 反而触发了最长执行路径**。

---

## 2. 修复设计（三层，互为兜底）

| 层 | 机制 | 作用 | 文件 |
| :--- | :--- | :--- | :--- |
| ① primary | worker initializer 内 `signal(SIGINT, SIG_IGN)` | worker 不再死于控制台 Ctrl+C，池不会被 Ctrl+C 打崩 | `parallel/worker.py` |
| ② 状态桥 | 主进程安装 SIGINT handler：记旗标后仍抛 `KeyboardInterrupt` | 保持 gradio/CLI 原关闭语义，同时把中断变为**任意线程可读**的进程级状态 | 新增 `parallel/interrupt.py` |
| ③ belt-and-suspenders | coordinator 在提交前 / 等待轮询(0.5s) / 池崩 三处检查旗标 → `KeyboardInterrupt` 短路 | 覆盖“Ctrl+C 恰与 worker 崩溃同刻”竞态；绝不把中断误判为 bootstrap 失败 | `parallel/coordinator.py` |

### 关键时序（修复后，复现用户场景）

```
Ctrl+C
 ├─ 主线程（gradio）: handler 置旗标 → 抛 KeyboardInterrupt → gradio “closing server.”
 ├─ worker 进程:      SIG_IGN → 继续加载模型/跑 chunk，不崩
 └─ 翻译线程:         coordinator wait 轮询 ≤0.5s 读到旗标
                      → KeyboardInterrupt → shutdown(wait=False, cancel_futures=True)
                      → 向上传播（high_level 两级 except KeyboardInterrupt: raise）
                      → 绝不进入串行兜底
```

### 语义保证

- **未按 Ctrl+C**：旗标 False，coordinator 行为与 V3 完全一致（池崩仍
  `WorkerBootstrapError` → 整体串行；chunk 失败仍增量补跑）。
- **按了 Ctrl+C**：任意时刻（提交前 / chunk 运行中 / worker 崩溃中）一律
  `KeyboardInterrupt` 短路，不进入任何串行兜底。
- **worker SIGINT 忽略不影响主进程**：handler 只装在 worker initializer
  内；主线程仍按默认语义收 KeyboardInterrupt。

---

## 3. 代码改动清单

| 文件 | 改动 |
| :--- | :--- |
| `pdf2zh/parallel/interrupt.py`（新增） | 进程级中断旗标：`install_interrupt_guard()` / `mark_interrupted()` / `reset_interrupt_flag()` / `is_interrupted()`；handler 置位后继续抛 `KeyboardInterrupt` |
| `pdf2zh/parallel/worker.py` | `init_worker_process` 第一行调用新增 `_ignore_ctrl_c_in_worker()`（模型加载**之前**执行，覆盖最危险的加载窗口） |
| `pdf2zh/parallel/coordinator.py` | `_submit` 提交前检查；wait 循环改 `timeout=0.5` 轮询 + 循环顶检查；`pool_broken` 分支先判中断 |
| `pdf2zh/gui/app.py` | `main()` 启动即 `install_interrupt_guard()` |
| `pdf2zh/pdf2zh.py` | CLI `main()` 同样安装 guard（chunk 运行期轮询感知，更快停止） |
| `tests/test_parallel_interrupt.py`（新增） | 13 例：旗标语义 / handler 行为 / worker SIG_IGN / 提交前短路 / 运行中轮询短路 / 池崩+中断短路 / 未中断语义不变 |

---

## 4. 验证

### 4.1 单测

```
tests/test_parallel_runtime.py + tests/test_parallel_interrupt.py : 49 passed
tests/test_gui_entry.py + test_spawn_entry.py + test_high_level_backend_degrade.py : 32 passed

---

## 7. V3-5 增补（worker 立即终止，2026-08-10 21:46 复测）

### 7.1 复测确认 V3-4 生效 + 暴露最后的残留

用户在 21:46 复测日志显示 V3-4 修复**完全生效**：

```
[task=task_709bd578fa86] interrupted by Ctrl+C; task cancelled
PS C:\...\PDFMathTranslate_FORKED>   ← 干净退出，无任何 traceback
```

不再有 `Exception in thread Thread-5`、不再有 `Exception ignored on threading
shutdown`、任务正确落 CANCELLED、进程正常回到 shell。

**唯一残留**：Ctrl+C 后 4 条 worker 进程的 tqdm 进度条继续刷完
`54/54 [02:15~02:19]`（约 2 分 19 秒）—— 这正是 §6.4 记录的遗留项：
`shutdown(cancel_futures=True)` 只能取消未开始的 future，正在运行的 chunk 会让
worker 继续跑完才退出。

### 7.2 修复：中断路径硬杀 worker

`coordinator.run()` 的 `finally` 现在先判断中断（**旗标置位**或**当前传播异常为
KeyboardInterrupt** 任一），命中则调用新增 `TaskCoordinator._force_terminate_workers`：
直接对 `executor._processes` 里所有存活 `multiprocessing.Process` 调 `terminate()`
（Windows `TerminateProcess` / POSIX SIGTERM），随后才 `shutdown(wait=False,
cancel_futures=True)`。正常完成 / 未中断池崩（串行兜底）路径不 terminate。

关键语义：中断后**绝不**进入串行兜底（与 V3-3 原则一致），terminate 只是回收
worker，不重试任何 chunk。

### 7.3 验证

```
tests/test_parallel_interrupt.py        : 22 passed
  （新增 TestForceTerminateWorkers：假 executor 跳过 / 只杀存活进程 /
    旗标短路路径调用 / 直接 KeyboardInterrupt 也调用 / 正常完成与池崩不调用）
tests/test_parallel_runtime.py + test_runtime_service_robustness.py +
  test_high_level_backend_degrade.py + test_gui_modules.py   : 201 passed 总计

script/_smoke_ctrl_c.py（真实 spawn）：
  [2]  coordinator aborted via KeyboardInterrupt in 0.52s (chunk sleeps 3s)
  [2b] workers force-terminated: alive_after=0 (0 tracked)
  [5]  force-terminate running workers: n=2 alive_after=0
  [smoke] all checks passed
```

`[5]` 直接验证：真实池提交 2 个 3s 慢任务，1s 后 `_force_terminate_workers`
杀掉全部 2 个运行中的 worker（`alive_after=0`）；coordinator 路径的 terminate
由日志 `Ctrl+C: force-terminated 2 parallel worker process(es)` 确认。

build 副本：`parallel/coordinator.py` 已同步至两处 build 目录，哈希一致。

### 7.4 效果

GUI 下 Ctrl+C 现在：任务立即落 CANCELLED + 主进程干净退出 + **worker 进程
0.5s 内被硬杀**，不再有 2 分 19 秒的残留进度条/后台计算。tqdm 刷屏问题彻底消除。


---

## 6. V3-4 增补（GUI 二次复现日志，2026-08-10）

### 6.1 新日志暴露的两个缺口

在 V3-3 落地后，用户再次在 GUI（`python -m pdf2zh.gui.app`）按 Ctrl+C，日志显示：

```
Keyboard interruption in main thread... closing server.
Exception in thread Thread-5 (_execute_task):
  ... KeyboardInterrupt: Parallel engine aborted: Ctrl+C received while chunks in flight
  ...(gradio block_thread 的 time.sleep 处 handler 再抛 KeyboardInterrupt)...
  ...(server.close → thread.join 又被 KeyboardInterrupt 打断)...
Exception ignored on threading shutdown:
  ... concurrent.futures.process._python_exit → t.join() 被打断
```

**根因 A —— KeyboardInterrupt 逃逸到翻译线程顶层**：coordinator 的短路
KeyboardInterrupt 被 `translate_stream`（`except KeyboardInterrupt: raise`）正确重抛，
但 `runtime_service._execute_legacy` / `_execute_task` 的 `except Exception` **不捕获
BaseException**，KeyboardInterrupt 一路冒到 `Thread-5` 顶层 → `threading.excepthook`
打印未处理线程异常，任务永远停在 TRANSLATING（无终态）。

**根因 B —— 信号 handler 每次 Ctrl+C 都抛 KeyboardInterrupt**：第一次信号触发
gradio 关闭后，关闭流程中的 `server.close()`、`thread.join(timeout=5)` 以及解释器
atexit 清理（`concurrent.futures.process._python_exit` 的 `t.join()`）在**任意位置**
被后续 Ctrl+C 再次打断 → 嵌套 traceback + `Exception ignored on threading shutdown`。

### 6.2 修复（三层）

| 层 | 改动 | 文件 |
| :--- | :--- | :--- |
| ① 状态落终态 | `_execute_task` 新增 `except KeyboardInterrupt`（置于 `except Exception` 之前）→ 按“用户取消”落 **CANCELLED** 终态并广播事件，绝不打印线程级未处理异常、不误判 FAILED | `services/runtime_service.py` |
| ② 语义传播 | `_execute_legacy` 新增 `except KeyboardInterrupt: raise`（显式不当作翻译失败） | `services/runtime_service.py` |
| ③ handler 只抛一次 | `_on_sigint`：仅第一次抛 KeyboardInterrupt（触发 gradio/CLI 关闭）；`sys.is_finalizing()`（atexit/threading shutdown 期）或已抛过一次后只置旗标不抛 → 关闭流程与进程退出不再被打断 | `parallel/interrupt.py` |

`reset_interrupt_flag()` 同时复位“只抛一次”守卫（测试隔离）。

### 6.3 验证

```
tests/test_parallel_interrupt.py + test_runtime_service_robustness.py  : 28 passed
  （新增：handler 只抛一次 / finalizing 不抛 / reset 重新武装 / _execute_task
    KeyboardInterrupt→CANCELLED / 普通异常仍 FAILED / batch 中断整批 CANCELLED）
tests/test_parallel_runtime.py + test_gui_modules.py +
  test_high_level_backend_degrade.py                              : 183 passed
script/_smoke_ctrl_c.py（真实 spawn）：
  [1] spawn worker SIGINT==SIG_IGN: True
  [2] coordinator aborted via KeyboardInterrupt in 0.53s (chunk sleeps 3s)
  [3] handler raises-once: first=True second=False
  [4] _execute_task KeyboardInterrupt -> CANCELLED: True (status=cancelled)
```

build 副本：`parallel/interrupt.py`、`services/runtime_service.py` 已同步至两处
build 目录，哈希一致。

### 6.4 遗留

- 日志末尾残留的 `54/54` tqdm 进度条来自 worker 进程 stderr 继承（Ctrl+C 后运行中的
  chunk 不被 `cancel_futures` 打断，跑完当前 chunk 才退出）；修复后主进程关闭/退出
  更干净，残留窗口显著缩短。如需立即终止可给 coordinator 的 `shutdown` 加进程级
  terminate，属后续可选优化。

```

### 4.2 真实 spawn 冒烟（`script/_smoke_ctrl_c.py`）

```
[1] spawn worker SIGINT==SIG_IGN: True
[2] coordinator aborted via KeyboardInterrupt in 0.53s (chunk sleeps 3s)
```

真实 `ProcessPoolExecutor(spawn)`：worker 免疫确认；运行中置旗标后 coordinator
0.53s 短路（无需等 3s chunk 结束）。

### 4.3 build 副本同步

`parallel/{interrupt,coordinator,worker}.py`、`gui/app.py`、`pdf2zh.py`
已同步至 `script/build/runtime/Lib/site-packages/pdf2zh/` 与
`script/build/site-packages/pdf2zh/`，哈希一致。

---

## 5. 遗留与下一步

- **CLI 直接 Ctrl+C**：主线程跑 coordinator，KeyboardInterrupt 直接到达，
  原短路已生效；新增旗标仅提供更快的轮询停止（可选收益）。
- **worker 冷启动导入窗口**：spawn 子进程在 initializer 之前还有一个“重导入主
  模块”窗口（毫秒~秒级），该窗口内 Ctrl+C 理论上仍可杀 worker。生产路径
  （GUI/CLI）该窗口远小于模型加载窗口，且 worker 死后 coordinator 有旗标短路
  兜底（不会串行兜底），故未处理；如需彻底封闭可用 `multiprocessing.spawn`
  预准备钩子，属后续可选优化。
- **验证矩阵**：在真实打包 exe 上按 V3 报告四场景复测（含“大文档中途 Ctrl+C”
  确认不再出现 `full serial fallback`）。

---

## 7. V3-5 增补：Ctrl+C 语义改为「只取消当前任务、不退出应用」（cancel_only 模式）

> 触发场景：用户按 Ctrl+C 后应用整个退出，无法再从 webUI 下载/重新提交。
> 用户预期：翻译完成或取消后应转为**空闲等待**，预览/下载/重新提交持续可用，
> 除非用户**主动选择关闭**否则不应直接终止。

### 7.1 语义变更（GUI 专属，CLI 默认不变）

| 状态 | 操作 | 行为 |
| :--- | :--- | :--- |
| 任务运行中 | 第一次 Ctrl+C | **只取消当前任务**，置中断旗标（coordinator 短路 → `_execute_task` 落 CANCELLED），GUI 保持运行进入空闲 |
| 任务结束（完成/取消/失败）后 | Ctrl+C | **直接关闭应用**（`mark_exit_pending` 已置位，无需连按两次） |
| 空闲/完成后提交新任务 | 新任务运行中 Ctrl+C | 回到“只取消任务、不退出”（`on_translate` 先 `reset_interrupt_flag()`） |
| CLI（`pdf2zh.py`） | Ctrl+C | 保持标准中断语义不变（第一次即抛 KeyboardInterrupt 退出） |

### 7.2 实现（相对 §6 V3-4 的增量）

1. **`parallel/interrupt.py`**：`install_interrupt_guard(cancel_only=...)` 新增模式参数；
   `_on_sigint` 在 cancel_only 模式下第一次 Ctrl+C 只置旗标+打印提示、**不抛**
   KeyboardInterrupt（GUI 主循环 `block_thread` 继续 sleep 保持运行）；第二次才抛
   （用户主动关闭）。新增 `mark_exit_pending()`：任务落终态后置位，空闲态下一次
   Ctrl+C 即关闭。`reset_interrupt_flag()` 同步复位 cancel-only 的“第一次”标记。
2. **`services/runtime_service.py`**：`_execute_task` 新增 `finally` —— 任务已落终态
   （COMPLETED/CANCELLED/FAILED，覆盖单/批量/v4 全路径）时调用 `mark_exit_pending()`；
   翻译运行中的第一次 Ctrl+C（coordinator 短路）仍由既有 `except KeyboardInterrupt`
   落 CANCELLED 并广播事件（§6 已实现）。
3. **`gui/app.py`**：`main()` 改为 `install_interrupt_guard(cancel_only=True)`；
   `on_translate` 提交新任务成功后 `reset_interrupt_flag()`（旧取消请求不短路新任务）。

### 7.3 时序（用户复现场景）

```
翻译运行中按 Ctrl+C
 ├─ 主线程 handler: 置旗标 + 提示，不抛 → gradio 保持 LISTENING（不退出）
 ├─ 翻译线程: coordinator 轮询 ≤0.5s 读到旗标 → KeyboardInterrupt
 │            → _execute_task except KeyboardInterrupt → 落 CANCELLED → SSE 广播
 │            → finally: mark_exit_pending()
 └─ GUI: 前端收到 cancelled → 按钮恢复、空闲等待；可下载已完成任务 / 重新提交
用户再按 Ctrl+C（空闲态）→ handler 直接抛 KeyboardInterrupt → 关闭应用（主动关闭）
```

### 7.4 验证

```
单测（tests/test_parallel_interrupt.py，29 passed，新增 6 例）：
  cancel_only 第一次不抛/第二次抛、reset 重新武装、finalizing 不抛、
  install_guard 模式标记、mark_exit_pending 空闲态一次即关闭、
  _execute_task finally 落终态后 mark_exit_pending（mock 全依赖）
真实信号冒烟（signal.raise_signal，cancel_only=True）：
  [1] first Ctrl+C cancels task, GUI alive: True
  [2] idle Ctrl+C closes app immediately: True
  [3] new task: first Ctrl+C cancels again (reset): True
  [4] idle Ctrl+C closes again: True
全量回归：276 passed（parallel 引擎 + doclayout batch + GUI/事件总线 + 入口）
build 副本：parallel/interrupt.py、services/runtime_service.py、gui/app.py 已同步至
  两处 build 目录，哈希一致。
```

### 7.5 与用户诉求的对应

- “翻译完成之后转为空闲”：正常完成路径原本就保持运行（COMPLETED 推送预览）；
  本次补齐了 **Ctrl+C 取消后同样保持运行** 的空闲语义。
- “将翻译好的页面推送到预览窗口，随后进入等待”：COMPLETED 事件驱动 `pdf_preview`
  渲染与按钮恢复（既有 SSE 链路，未改动）。
- “除非用户自己选择关闭则不应该直接终止”：CLI 直接 Ctrl+C 仍是标准关闭；GUI 场景
  下 Ctrl+C 不再直接终止，任务结束（含取消）后需再按一次才关闭（防止误触）。
