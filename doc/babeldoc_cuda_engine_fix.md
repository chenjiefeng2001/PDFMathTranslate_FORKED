# BabelDOC 排版 + BabelDOC 引擎 + CUDA 卡死问题：架构改造落地报告

> **日期**：2026-08-26
> **范围**：把“全局串行化降并发换稳定”的旧绕路，重构为“Spawn + GPU worker
> 独立 CUDA 上下文 + 进程内多线程 + 有界并发”的架构。

---

## 一、问题回顾与核心不变量

现象：`--parse-engine babeldoc --backend cuda`（GUI 同理）在 `Parse Page
Layout` / `Extract Characters` 阶段进程永久卡死，无 Traceback、CPU/GPU 利用
率归零。根因（详见首轮报告）归纳为五类，其中两类是本项目可主动治理的：

1. **POSIX fork 后共享 CUDA 状态死锁**：父进程先初始化 CUDA/ORT 会话，再
   `fork()` 得出子进程 —— 子进程复制了带锁状态的 CUDA Driver 内存，任何
   CUDA 调用永久阻塞。
2. **cuDNN / ORT / OpenMP 跨进程线程竞争**：把并发做成了“无限开”导致
   oversubscription，以及“两个 CUDA 会话多线程并发让 cuDNN 崩”。

**核心不变量（贯穿所有改动）：**

> **线程可以共享 CUDA Session；进程不能继承/共享 CUDA Session。**

原方案（`OMP_NUM_THREADS=1` / `PDF2ZH_WORKER_ORT_THREADS=1` 全局串行化）确实
能提升稳定性，但本质是**降并发换稳定**。本文落地的是其有界并发版本：

| 维度 | 旧的“串行化”方案 | 本方案 |
| --- | --- | --- |
| fork 后 CUDA | 全关线程规避 | **spawn + 禁止 fork 携带 CUDA 状态（fork 守卫）** |
| CUDA 并发 | 全局 `_CUDA_RUN_LOCK` 串行 | **进程内 BoundedSemaphore（有界并发）** |
| 跨框架 Session | BabelDOC 与 pdf2zh 共用一个锁 | **各自独立 scope 的并发名额** |
| 线程 | 全关为 1 | **进程本地线程预算（默认 4），进程间独立** |
| 隔离 | 同进程试图切 CPU | **独立 worker 进程 + 看门狗可杀可重建** |

---

## 二、改动清单（代码级）

### 2.1 新增 `pdf2zh/gpu_governor.py`

GPU 并发调控器 + CUDA 生命周期隔离工具，是本次架构的核心：

- **`GPUConcurrencyGovernor`**：用 `threading.BoundedSemaphore(max_concurrent)`
  做**有界并发**，而非全局互斥锁。
  - 并发度 1 → 与旧互斥锁完全兼容（默认，向后稳定）；
  - 并发度 N → 同一进程内最多 N 个 `session.run` 并发，其余排队，让 ORT /
    CUDA 自身在可承受范围内并行。
  - 并发度来源：`PDF2ZH_GPU_CONCURRENCY`（全局）或
    `PDF2ZH_GPU_CONCURRENCY_<SCOPE>`（作用域专项，优先级更高）。
- **`get_governor(scope)`**：进程内单例注册表 + **PID 绑定**。fork 出的子
  进程 PID 变化后自动重建**全新、彼此独立**的 governor，绝不继承父进程的
  信号量 —— 从根上切断“fork 后共享 CUDA 同步原语”。
- **CUDA 生命周期跟踪**：`mark_cuda_initialized()` / `cuda_initialized()` /
  `reset_cuda_process_guard()`。标记绑定当前 PID，fork 子进程继承的标记自动
  失效 —— 上层可据此判断“本进程 CUDA 是否安全可用”。
- **fork 守卫**：`fork_cuda_degrade_backend()` 在
  `multiprocessing` 子进程 + 启动方式为 `fork` + `PDF2ZH_STRICT_FORK_CUDA=1`
  三个条件同时命中时，把请求的 GPU 后端降级为 `cpu`。默认关闭（不改变现有
  显式 `cuda` 行为），仅日志提示 —— 因为真正的解法是让 CUDA 只活在 spawn
  出来的 worker 进程里。
- **进程本地线程预算**：`apply_process_local_thread_budget()` 用 `setdefault`
  在每个独立进程入口设置 `OMP/MKL/OPENBLAS_NUM_THREADS` + 预留 ORT 预算，
  **有界并发而非串行**；互不覆盖其它进程的全局。
- `suggest_concurrency_for_vram()`：按显存给出保守并发建议（8G→1、12G→2、
  24G→3、更大→更高），只引导不自动生效。

### 2.2 `pdf2zh/babeldoc_onnx_backend.py`

- `_LockingSessionProxy` 改造成“有界并发代理”：默认并发度 1（兼容），可经
  `PDF2ZH_GPU_CONCURRENCY_BABELDOC` 调高；不再依赖 pdf2zh 的全局
  `_CUDA_RUN_LOCK`。
- `_init_with_providers`：GPU provider 时调用
  `get_governor("babeldoc")`（**独立于 pdf2zh 主链路的作用域**）+ 
  `mark_cuda_initialized()`；CPU 时透传。
- `_patched_init`：在解析前插入**fork 守卫** —— fork 子进程 + 严格开关时把
  `cuda/dml` 后端降级 `cpu`。

### 2.3 适配器 & worker 引导

- `babeldoc_adapter.py` / `babeldoc_next_adapter.py`：翻译入口设置进程本地
  线程预算（进程内路径，BabelDOC 的多线程/多任务能力完整保留）。
- `babeldoc_next_worker.py`（独立子进程）：`reset_cuda_process_guard()` +
  线程预算 —— 该进程不继承宿主 CUDA 状态。
- `parallel/worker.py` `init_worker_process`：同样在 bootstrap 完成
  `reset_cuda_process_guard()` + 线程预算。

### 2.4 新增/回归测试

`tests/test_gpu_governor.py`（12 项）：有界并发、单例/PID 隔离、fork 守卫、
CUDA 标记按 PID 隔离、线程预算 setdefault、显存并发建议。

---

## 二、并发模型对比

```
旧方案（串行化）                       新方案（有界并发）
Thread1 ─┐                           Thread1 ─┐
Thread2 ─┤                           Thread2 ─┤
Thread3 ─┼─ CUDA  (1 at a time)      Thread3 ─┼─ BoundedSemaphore(N)
Thread4 ─┤                           Thread4 ─┤
Thread5 ─┘                           Thread5 ─┘
```

```text
Main（pdf2zh）
├── CPU executor（多线程）
├── BabelDOC controller
│     ├── spawn GPU worker #1 ─ ORT CUDA Session ─ 进程独立 CUDA Context
│     └── spawn GPU worker #2 ─ ORT CUDA Session ─ 进程独立 CUDA Context
└── GPU Concurrency Governor（bounded，按 scope）
      ├── pdf2zh CUDA task
      └── BabelDOC CUDA task（与主链路分开的 scope）
```

关键约束（与首版“严重降并发”的本质区别）：

```text
Fork 携带 CUDA 状态      = 禁止（spawn worker + fork 守卫）
跨进程共享 ORT Session   = 禁止（get_governor 按 PID 重建）
全局 CUDA run 锁         = 禁止（改用有界并发 + session-local）
把线程全关为 1             = 禁止（改用进程本地预算，保留多线程）
无限开并发                = 禁止（并发乘积有界）
```

并发乘积公式：
```text
Total CPU threads ~= ProcessCount × (ORT_intra + ORT_inter + OMP + app)
GPU concurrency   ~= GPUProcessCount × in_flight_inference_per_process
稳定条件:
  GPU concurrency × PeakMemoryPerInference < AvailableVRAM − safety_margin
```

---

## 三、运行期环境变量

| 变量 | 默认 | 作用 |
| --- | --- | --- |
| `PDF2ZH_GPU_CONCURRENCY` | `1` | 全局 CUDA 并发上限（调高 = 有界并发而非串行） |
| `PDF2ZH_GPU_CONCURRENCY_BABELDOC` / `..._PDF2ZH` | 跟随全局 | 按 scope 专项覆盖 |
| `PDF2ZH_STRICT_FORK_CUDA` | 关 | 在 fork 子进程里强制把 GPU 后端降级 CPU |
| `PDF2ZH_PROCESS_THREADS` | 4 | 每个独立进程的 OMP/MKL/OPENBLAS 线程预算 |
| `PDF2ZH_BABELDOC_BACKEND` | auto | BabelDOC 内部 ONNX 后端（cpu/cuda/dml/auto） |

典型生产配置（保留两边多线程 + CUDA）：

```bash
# 有界并发 + spawn（POSIX 推荐 set_start_method('spawn')）
export PDF2ZH_GPU_CONCURRENCY=2
export PDF2ZH_PROCESS_THREADS=4
pdf2zh example.pdf --parse-engine babeldoc --backend cuda
```

严格隔离（fork 场景兜底）：

```bash
export PDF2ZH_STRICT_FORK_CUDA=1
```

---

## 四、后续建议（按优先级）

1. **进程内 CUDA 移除自愈**：把 GPU 推理做成独立 worker 进程 + watchdog，
   超出安全阈值直接 `terminate` 旧进程并重新 `spawn`（线程无法杀死卡在
   C++/CUDA 的 `session.run`）。本方案已把生命周期治理前置到“进程内 CUDA”，
   同进程内 fallback 需避免残留 CUDA stream 处于未知状态。
2. **BabelDOC 内部进程池显式化 stash**：彻底改掉 BabelDOC 内部的
   `multiprocessing.Pool`，用 `get_context("spawn")`。
3. **memory budget**：PyTorch 与 ORT 不互抢 —— 用 GPU 显存预算 + 进程数控制，
   而非禁掉其一。
4. **batch 推理**：对版面条目 `batch_size>1` 聚合，比“无限并发单页任务”
   更稳定（`babeldoc_large_doc_slow_progress_report` 已示 batch 硬编码为 1）。