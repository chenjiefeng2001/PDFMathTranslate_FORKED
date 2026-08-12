# Parallel Runtime V3 迭代报告
> 融合 v1（架构蓝图）/ v2（长期基线）方案与当前代码实现基线
> 日期：2026-08-10 ｜ 范围：`pdf2zh/` 并行引擎（`high_level.py` / `doclayout.py` / 新增 `parallel/` 子包）

---

## 1. 背景与目标

打包版（PyStand + Embedded Python）在长时间运行 / 大文档（218 页）翻译场景下暴露了并行引擎的可靠性问题；上一轮已通过 S0–S4 完成任务级自愈（重试止损、任务清理、GUI 队列直连、取消接线、队列看门狗），并实测确认：

- **无 Ctrl+C 时，并行路径完全健康**（12 页实测：12/12 完成、零警告、零 fallback）；
- **用户日志中的全部异常均与 Ctrl+C 强停并发 worker 初始化窗口绑定**：4 个 worker 收到 CTRL_C_EVENT 死于模型加载中途（`Exception in initializer: KeyboardInterrupt` ×4），futures 层在 spawn 管道被打断的竞态下抛出 `Condition objects should only be shared between processes through inheritance`（RuntimeError），随后被 `high_level.py:719` 捕获并**全量**回退串行，最终 `return code: 0` 完整产出。

本迭代的目标：把 v1/v2 方案中与当前任务模型兼容的核心理念（纯数据契约、主进程模型预热、Bounded in-flight、错误分类、增量降级、优雅退出）落地为**精确到模块、接口与数据流的代码级改造方案**，同时明确拒绝与现状冲突的方案点（详见 §4）。

---

## 2. 当前实现基线（FACTS）

| 模块 | 机制 | 现状 | 评价 |
| :--- | :--- | :--- | :--- |
| `high_level.py:_translate_parallel`（1253 起） | 进程池并行整页翻译：chunk 级任务（`_translate_parallel_chunk`，模块级可 pickle），payload 全为标量 + `fp_bytes` + `page_xref_map` + `_shared_cancel`（mp.Event） | 一次性 `submit` 全部 chunk（218 页 / 4 worker ≈ 13 chunk）；结果 `obj_patch` 全量驻留主进程后合并 | 纯数据契约已达标；**无 in-flight 窗口**，主进程内存随 chunk 结果线性增长 |
| `_init_worker_process`（1026 起） | worker 启动时 `set_backend` + `OnnxModel.load_available()`，模型失败仅降级为 None 并继续 | 无 ORT 线程限制（默认全核）；无 DLL 目录预注册；bootstrap 失败静默降级 | **高价值低成本改进点**：ORT 线程限制、bootstrap 失败语义化 |
| `doclayout.py:_OptimizedCache`（138 起） | `.lock` 文件锁 + `tmp` 写入 + `os.replace` 原子替换 + 锁超时安全降级 | 已实现 v2 想要的"单写者原子替换"；**从不含 threading.Condition** | 设计达标，仅缺"主进程显式预热"入口 |
| `translate_stream` 兜底（719 起） | `except (Exception, SystemExit, KeyboardInterrupt)` → 整文档 `translate_patch` 串行重跑 | 失败即全量重跑；**Ctrl+C 也被吞进串行兜底继续跑完** | 与 v2"灾难性全量降级"批评一致，**必须改** |
| 取消链路 S4 | `cancellation_event`（threading.Event）经 daemon 桥镜像为 mp.Event 传 worker | 已正确避免 threading 对象跨进程 | 达标，保持 |
| S0–S3 | 队列自愈、queue=False 直连、任务清理、重试止损 | 已交付并回归通过 | 达标，保持 |

近期另修复：CLI `parallel_pages` 重复传参（`translate_stream(s_raw, parallel_pages=..., ..., **locals())` 抛 TypeError，CLI 模式此前完全不可用）、CLI 输出目录未创建（FileNotFoundError）。

---

## 3. v1 / v2 方案评述与适配性分析

| v1/v2 理念 | 现状对照 | 采纳度 | 落地方式 |
| :--- | :--- | :--- | :--- |
| 纯数据契约（禁传 Lock/Condition/Model/Converter） | 已达成（标量 + bytes + mp.Event），且任务模型为**整页翻译**而非"渲染图片→版面分析" | ✅ 保持 | 不引入 v1/v2 的 `PageTask(image_bytes)` 任务模型（§4 拒绝项①） |
| 异常分类体系（`errors.py`） | 无；兜底靠裸 `except RuntimeError` | ✅ 采纳 | 新增 `pdf2zh/parallel/errors.py`，语义化 719 兜底与 chunk 重试 |
| 主进程模型预热（单写者原子） | `_OptimizedCache` 已具备原子性；缺显式入口 | ✅ 采纳 | 增补 `DocLayoutModel.ensure_model_prewarmed()`；worker 只读缓存路径 |
| Worker ORT 线程限制 | 未设置（默认全核 × worker 数 = 争抢） | ✅ 采纳 | worker bootstrap 内 `intra/inter_op_num_threads=1` + `ORT_SEQUENTIAL` |
| Bounded in-flight 窗口 | 一次性全提交 | ✅ 采纳 | 改造 `_translate_parallel` 为窗口提交 + 流式合并 |
| 增量恢复 / 失败页重试（Manifest） | 全量串行兜底 | ⚠️ 部分采纳 | 页级 manifest 与补丁树模型不兼容（§4 拒绝项②）；改为 **chunk 级有限重试 + 失败 chunk 串行补跑** |
| 致命/非致命错误分离 | worker 崩溃与单页异常混在一起 | ✅ 采纳 | `WorkerProcessError` / `PageProcessingError` 分流；崩溃 chunk 标记失败并降级 |
| Ctrl+C 优雅退出（`shutdown(wait=False, cancel_futures=True)` 后重抛） | 被 719 吞掉进全量串行 | ✅ 采纳 | coordinator 捕获 KeyboardInterrupt → 关池 → 重抛，不触发串行兜底 |
| 致命错误写 stderr | 静默 | ✅ 采纳 | bootstrap 失败 `[Worker FATAL]` 写 stderr + raise |
| DLL 精确预注册（`onnxruntime.__file__` 目录） | 无（依赖 PATH 继承） | ✅ 采纳（防御项） | `init_worker_process` 内 `os.add_dll_directory` 指向 ORT 模块目录 |

---

## 4. 明确拒绝的方案点与理由

1. **拒绝 `PageTask(image_bytes)` 传输式并行模型**（v1/v2 把"页面渲染→版面分析"当并行单元）。pdf2zh 的并行单元是**整页翻译 + 补丁回写**（`translate_patch(chunk_pages)`），收益远大于版面分析；且现有 `fp_bytes`（一次共享）+ 各 worker 自建 `fitz.Document` 的设计已规避 SWIG 句柄 pickle 问题，无需改造成图片字节流。
2. **拒绝页级 `JobManifest` 断点续跑**。补丁（obj_patch）是全局逐字合并的，页级结果无法独立落盘/续跑；`mark_completed/_persist` 对整文档翻译没有断点语义。改为 chunk 级（`manifest` 的思想保留在 chunk 粒度：哪些 chunk 成功 / 失败 / 需串行补跑，仅内存态，不落盘）。
3. **拒绝在 doclayout 引入 Condition/Lock 单例作为跨进程同步**。`_OptimizedCache` 已用文件锁 + 原子替换达成同目标（且 Worker 从不同时写同一文件——由 `acquire()` 的持有/等待语义保证），不引入新同步原语。

---

## 5. V3 迭代方案（代码级）

### 5.1 新增 `pdf2zh/parallel/` 子包

```text
pdf2zh/
├── high_level.py            # 编排不变；并行逻辑调用本子包（保持向后兼容的旧函数外壳）
└── parallel/
    ├── __init__.py          # re-export TaskCoordinator / errors
    ├── errors.py            # 异常分类（如下）
    ├── chunk.py             # ChunkResult / ChunkManifest（内存态，不落盘）
    ├── worker.py            # init_worker_process / execute_chunk（迁自 high_level，线程限制+DLL注册）
    └── coordinator.py       # Bounded in-flight 调度 + 有限重试 + 增量降级 + KeyboardInterrupt
```

**`errors.py` 分类**（语义边界即兜底策略边界）：

```python
class ParallelError(Exception): ...                  # 基类：整体串行兜底
class WorkerBootstrapError(ParallelError): ...       # worker 冷启动失败：整体串行兜底
class WorkerProcessError(ParallelError): ...         # 进程崩溃（死 worker）：该 chunk 失败→串行补跑
class PageProcessingError(ParallelError): ...        # 单 chunk 内计算异常：失败→串行补跑
class ProtocolViolationError(ParallelError): ...     # pickle 违例（如混入 Lock/Condition）：整体串行兜底
```

**`chunk.py`**（内存态，替代 v2 落盘 manifest）：

```python
@dataclass(frozen=True, slots=True)
class ChunkTask:          # 纯标量契约（等价现有 _translate_parallel_chunk 参数集）
    chunk_pages: tuple[int, ...]
    fp_bytes: bytes
    page_xref_map: dict | None
    cancel_event: object = None        # 仅允许 mp.Event 或 None（协议强制）
    **_SCALAR_FIELDS                   # 现有 scalar_args 全量保留

@dataclass
class ChunkManifest:      # 增量恢复的内存态
    chunk_status: dict[int, str]       # pending / running / ok / failed
    failed_indices: list[int]          # 待串行补跑
    def mark_ok / mark_failed / pending_chunks -> list[int]
```

### 5.2 `worker.py` —— worker 硬化（迁移 + 加固 `_init_worker_process`）

不变量：worker 进程除了 `mp.Event`（取消）与进程池管道外，**不得持有或继承任何主进程同步原语**；bootstrap 失败必须可见（stderr + 异常），不再静默降级为 `ModelInstance=None` 继续跑：

```python
def init_worker_process(backend: str | None) -> None:   # 签名保持，供现有/新 executor 复用
    # 1) DLL 预注册（防御）：仅 onnxruntime 模块目录，替代 os.walk(site-packages)
    #    onnxruntime.__file__ → os.add_dll_directory(os.path.dirname(...))
    # 2) set_backend(backend)（现有逻辑保留）
    # 3) WorkerBootstrapError 语义化：
    #    - onnxruntime 导入失败 / Session 初始化失败 → raise WorkerBootstrapError
    #    - 模型文件缺失但后端允许 → 保持 ModelInstance=None（版面侧天然降级），仅加日志
    # 4) ORT 资源限定（新增，高价值）：
    #    SessionOptions().intra_op_num_threads = 1
    #    SessionOptions().inter_op_num_threads = 1
    #    SessionOptions().execution_mode = ORT_SEQUENTIAL
    #    应用在 doclayout.OnnxModel.__init__（通过环境变量/参数门控，串行路径不受影响：
    #    PDF2ZH_WORKER_ORT_THREADS=1 时生效，默认行为不变）
```

数据流：`main --spawn--> worker` 只有 `initargs=(backend,)` 与 `ChunkTask`（纯标量）；返回 `ChunkResult`（obj_patch dict / obs bundle / elapsed / error_message / is_fatal）。

### 5.3 `doclayout.py` —— 主进程预热入口（增量）

现有 `_OptimizedCache`（文件锁 + 原子替换）不变，增补显式预热 API，供 `translate_stream` 在启动并行前调用一次：

```python
@classmethod
def ensure_model_prewarmed(cls) -> str | None:
    """主进程单写者预热：下载/校验模型、若可缓存则生成 optimized 缓存并原子发布。
    返回模型路径（str）或 None（不可用）。保证此后 worker 的 acquire() 直接命中
    state=="cached"，绝无并发写竞争。"""
    # 实现 = get_doclayout_onnx_model_path() 存在性校验 + OnnxModel 的
    # _OptimizedCache 独占 acquire→tmp 写入→publish 最小化路径
```

`high_level.translate_stream` 集成：并行分支（`page_count > 5`）先 `ensure_model_prewarmed()`；失败时记录并跳过并行（等价于整体串行兜底），**不让预热异常重复进 worker 初始化**。

### 5.4 `coordinator.py` —— Bounded in-flight + 增量降级 + 优雅退出

替换 `_translate_parallel` 内部实现（函数签名 `translate_stream` 侧保持不变）：

```python
class TaskCoordinator:
    def __init__(self, max_workers: int = 4, in_flight_multiplier: int = 2):
        self.max_in_flight = min(len(chunks), max_workers * in_flight_multiplier)

    def run(self, chunk_tasks: list[ChunkTask], progress_cb) -> tuple[dict, list, bool]:
        # 1) 预热完成断言（§5.3）后才允许创建池
        # 2) 窗口调度：初始提交 max_in_flight 个；每完成一个补一个（v2 的 Lazy 提交思想，
        #    只是"构建"是纯内存常量，无渲染开销，收益落在结果合并的内存上）
        # 3) 结果流式合并：完成即 obj_patch.update + obs 合并（现 1370-1393 逻辑内联到窗口循环）
        # 4) chunk 失败处理：
        #    - 进程崩溃（BrokenProcessPool / WorkerProcessError）：该 chunk 记 failed
        #    - 单 chunk 异常（PageProcessingError）：记 failed
        #    - 池整体不可用（ProtocolViolation / WorkerBootstrap）：raise → 外部整体串行
        # 5) 有限重试：failed chunk 先原地重试 1 次（新提交）；再失败则进 serial_patch_list
        # 6) KeyboardInterrupt：executor.shutdown(wait=False, cancel_futures=True)；
        #    绝不进入串行兜底，直接重抛给上层关闭流程（GUI 优雅关闭 / CLI 退出）
        # 7) 全部完成或降级清单就绪后：shutdown(wait=True)
        # 返回 (obj_patch, obs_bundles, serial_patch_indices)
```

### 5.5 `high_level.py` —— 兜底语义化（719 行改造）

```python
if parallel_pages and page_count > 5:
    try:
        obj_patch, observations, serial_indices = coordinator.run(...)
        if serial_indices:                       # 只有失败 chunk 走串行，不是整文档
            logger.warning("Incremental serial fallback for %d chunk(s)", len(serial_indices))
            for idx in serial_indices:
                obj_chunk, obs_chunk = translate_patch(fp, pages=chunk_pages[idx], **scalar_locals)
                obj_patch.update(obj_chunk)      # 合并回主 obj_patch
    except KeyboardInterrupt:
        raise                                   # 不可能被吞；上层负责关闭
    except ParallelError as e:
        logger.warning("Parallel engine degraded cleanly (%s); full serial fallback", e)
        obj_patch = translate_patch(fp, **dict(locals()))
```

语义对照：`ProtocolViolation / WorkerBootstrap` → 整体串行（合理）；`WorkerProcess / PageProcessing` → chunk 级补跑（**不再全量重跑**）；`KeyboardInterrupt` → 直接传播。

---

## 6. 验证矩阵（对齐 v1/v2 四场景）

| 场景 | 验证目标 | 预期结果 |
| :--- | :--- | :--- |
| 小文档（8 页） | worker 冷启动 + 基本运行 | 0 报错、输出正常、进程正确回收（`exe` 全流程） |
| 大文档（218 页级） | in-flight 窗口内存恒定 | 主进程内存平稳（无全量 obj_patch 驻留）；无 BrokenProcessPool、无假进度 |
| 中途 Ctrl+C | 优雅退出与资源清理 | 主进程即时响应（不再被串行兜底拖住）、worker 立即终止、无孤儿进程；`[Worker FATAL]`/KeyboardInterrupt 短路正确 |
| 模型损坏 / 网络中断 | 异常隔离与降级 | `WorkerBootstrapError` → 干净整体串行；单 chunk 崩溃 → 该 chunk 串行补跑，其余成果保留 |
| 回归 | S0–S4 全量单测 + exe 冒烟 | 137 项测试全绿；1 进程、7860 LISTENING、无级联 |

---

## 7. 实施顺序与工作量

| 阶段 | 内容 | 涉及文件 | 预计 |
| :--- | :--- | :--- | :--- |
| P1 | `parallel/errors.py` + `chunk.py` + `worker.py`（迁移 + ORT 线程 + DLL 注册 + stderr） | 新增 3 文件；`doclayout.py` 增加 Session 线程门控 | 0.5 天 |
| P2 | 预热入口 `ensure_model_prewarmed` + `translate_stream` 集成 | `doclayout.py`、`high_level.py` | 0.5 天 |
| P3 | `coordinator.py` 窗口调度 + 有限重试 + chunk 级降级 + KeyboardInterrupt 短路；719 兜底语义化 | `coordinator.py`、`high_level.py` | 1 天 |
| P4 | 单测（errors 映射、chunk 降级、窗口调度、Ctrl+C 短路）+ 双构建副本同步 + 四场景验证矩阵 + exe 冒烟 | `tests/`、`script/build/*` | 1 天 |

**风险与回滚**：P1–P3 均收敛在并行分支内，串行路径零改动（除 Session 线程门控默认关闭）；任何阶段 `ParallelError` 兜底保证产物可生成；回滚 = 保留旧 `_translate_parallel` 函数体即可。

---

## 8. 总结

- 现状已满足 v1/v2 的**纯数据契约、原子缓存、取消语义**；本迭代聚焦四个真实缺口：**bootstrap 语义化与 ORT 线程限制、主进程预热入口、Bounded in-flight 调度、失败 chunk 增量降级 + Ctrl+C 短路**。
- 明确拒绝与整页翻译任务模型冲突的"图片传输式并行"与"页级断点 manifest"，避免为蓝图正确性牺牲现有架构收益。
- 交付物：`pdf2zh/parallel/` 子包 + `doclayout` 预热入口 + 兜底语义化；验证对齐四场景矩阵并与 S0–S4 全量回归合并执行。

---

## 9. V3-3 增补：GUI Ctrl+C 竞态实测缺口与修复

V3 的“Ctrl+C 短路”在 **GUI 实测**下暴露出两条未被覆盖的链路（详见
`doc/ctrl_c_worker_init_report.md`）：

1. gradio 在主线程吞掉 KeyboardInterrupt，后台翻译线程**永远收不到** →
   coordinator 的 `except KeyboardInterrupt: raise` 在 GUI 场景是死代码；
2. Windows 控制台 CTRL_C_EVENT 广播杀死正在加载模型的 worker →
   `BrokenProcessPool` → `WorkerBootstrapError` → **整文档串行兜底**
   （Ctrl+C 反而触发最长执行路径，复现日志见新报告 §1）。

修复为三层：worker initializer `SIG_IGN`（新增 `parallel/worker.py`）、
主进程中断旗标 `parallel/interrupt.py`（GUI/CLI 启动处安装）、coordinator
提交/轮询/池崩三处旗标短路。验证：49 项并行单测 + 32 项入口回归 + 真实
spawn 冒烟（worker 免疫确认、运行中 0.53s 短路）全绿。
