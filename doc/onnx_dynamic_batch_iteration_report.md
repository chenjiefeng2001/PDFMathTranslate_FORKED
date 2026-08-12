# ONNX 动态 Batch 并行迭代报告（V3-2）
> 承接 `parallel_runtime_v3_iteration_report.md`（P1–P4 并行引擎落地后）
> 日期：2026-08-10 ｜ 范围：`pdf2zh/doclayout.py`、`pdf2zh/high_level.py`、`tests/test_doclayout_batch.py`

---

## 1. 目标与背景

`parallel_runtime_v3_iteration_report` 的 P1–P4 已把**页级并行**收敛到整页翻译的 chunk
任务模型（多进程、纯数据契约、Bounded in-flight、失败 chunk 增量降级）。版面分析阶段
（`translate_patch` 页循环）在 4 个 worker 内仍为**逐页串行 `session.run`**。

本迭代的议题：**版面分析阶段是否可利用 ONNX 模型自身的并行能力（动态 Batch / 多线程），
在不引入更多进程的前提下提升吞吐**，即把"动态 Batch 并行"与"单 Session 多线程并发"
两种 ONNX 原生并行方案落地验证，并给出**实测数据**驱动的取舍结论。

---

## 2. 落地的能力（代码级）

### 2.1 `OnnxModel.supports_batch`（`doclayout.py`）
检测模型输入 `batch` 轴是否动态（DocLayout-YOLO 以 `dynamic_axes` 导出，轴定义为
`['batch', 3, 'height', 'width']`，`shape[0]` 为 `str`/`None` 占位符即动态）。
结果缓存于 `_supports_batch`；检测失败按不支持降级。固定 batch 的旧模型返回 `False`。

### 2.2 `OnnxModel.predict_batch(images)`（`doclayout.py`）
一次 ONNX 调度批量推理多页版面：
- 逐页 letterbox（与 `predict` 完全一致：`int(h/32)*32` + aspect-preserve + stride 填充）；
- 左上角锚定放入公共 canvas `[N, 3, H, W]`（取 batch 内最大 letterbox 尺寸，空白填
  114 灰），单次 `session.run` 输出 `[N, 300, 6]`；
- 逐页用自身 `(h1, w1)` 做 `scale_boxes` 还原坐标（与 `predict` 坐标语义逐位一致；
  越界 box 由下游 clip）；
- `supports_batch is False` 时自动降级为逐页 `predict`（行为与现状完全等价）。

### 2.3 `_LayoutBatchPredictor` + `PDF2ZH_LAYOUT_BATCH` 门控（`high_level.py`）
`translate_patch` 页循环新增**批量路径**：攒够 batch 页（`PDF2ZH_LAYOUT_BATCH` ≥ 2）
后一次 `predict_images()` 批量推理，再逐页执行版面处理。逐页处理逻辑抽为
`_process_page_layout` 闭包，**逐页路径逐行保持原样**（默认零行为变化）；模型未加载
或环境变量未设置时完全走原逐页路径。批量路径日志输出调度次数/页数/总耗时统计。

### 2.4 测试（`tests/test_doclayout_batch.py`，14 项）
- `supports_batch` 动态/固定/无输入三态；
- `predict_batch` 空列表、同尺寸单次调度、混合尺寸公共 canvas、固定 batch 降级；
- `_LayoutBatchPredictor` 分批/余量/降级/统计/批大小钳制；
- `_int_env` 解析；
- 真模型冒烟（模型已下载时）：动态轴声明 + 批量/逐页结构一致性。

---

## 3. 实测数据（本机 CPU，ORT_ENABLE_ALL）

随机 800×600 页图，N=8，`doclayout_yolo_docstructbench_imgsz1024.onnx`：

| 方案 | 耗时（8 页） | 加速比 | 备注 |
| :--- | :--- | :--- | :--- |
| 逐页串行 | 2.515s | 1.00x | 当前 worker 内现状 |
| 多线程 ×2（共享 session） | 2.107s | 1.19x | 每次 `run()` 释放 GIL |
| 多线程 ×4（共享 session） | 1.928s | **1.30x** | 已接近本机物理核上限 |
| 多线程 ×8（共享 session） | 2.041s | 1.23x | 超额订阅回落 |
| 动态 Batch（一次喂 8 页） | 2.779s | **0.91x** | ORT 不做批量融合，compute 相加 |

**结论（与"动态 Batch 最高效"的直觉相反）：**

1. **CPU 上动态 Batch 反而更慢**。ORT CPU 执行器对 Conv 类算子**不做跨 batch 融合**，
   batch 8 的 compute ≈ 8 页 compute 相加，且大 batch 单次调度内无法流水交错 ——
   实测 `batch(8)=0.91x`、4 页时更差（`ORT_DISABLE_ALL` 下 0.60x）。动态 Batch 的价值
   仅在 **GPU/DML 后端**（Tensor Core / cuDNN 批量核）或**重新导出为批量融合算子**的
   模型上成立。因此页循环默认关闭，`PDF2ZH_LAYOUT_BATCH` 为显式 opt-in。
2. **多线程共享 session 安全且数值逐位一致**（实测 thread results identical to serial
   = True），但本机只到 1.30x —— 每个 `run()` 已占满全核，多线程并发造成超额订阅；
   且收益仅限 `run()` 段，页循环其余 numpy 处理受 GIL 限制。**不构成对现有 4 进程
   chunk 并行的替代**，仅作为进程池不可用时的降级参考。
3. **数值注意**：ORT 对 batch>1 会换执行计划，`[N,300,6]` 的行序/末位数值与 batch=1
   略有差异（真模型实测：低置信度噪声行有差异，阈值 0.25 过滤 + conf 降序后 top 检测
   逐位一致；同尺寸页 batch 与逐页 letterbox 输入完全相同时，结构一致）。下游消费
   `YoloResult`（conf 降序 + 阈值过滤），不受影响。

---

## 4. 与 P1–P4 并行引擎的关系

- **默认不改变** P1–P4 的并行路径：chunk 多进程并行仍在，worker 内版面分析仍是逐页
  predict（`PDF2ZH_LAYOUT_BATCH` 未设置时零改动）。
- **批量能力作为 opt-in 加速器**：单进程/串行路径（`parallel_pages` 关闭、GPU/DML
  后端、批量融合模型）可通过 `PDF2ZH_LAYOUT_BATCH=4~8` 开启，减少 ONNX 调度次数、
  消除多进程 IPC/锁开销。
- **与 `PDF2ZH_WORKER_ORT_THREADS=1` 正交**：worker 内 ORT 线程门控与 batch 互不影响
  （batch 也受 intra_op 限制，门控仅 1 线程时 batch 收益更明显）。
- 多线程共享 session 方案（次选）经实测数值安全，保留为文档化参考，不作为默认。

---

## 5. 验证与回归

| 项目 | 结果 |
| :--- | :--- |
| `tests/test_doclayout_batch.py` | 14 passed（含真模型冒烟） |
| `tests/test_parallel_runtime.py` | 36 passed（P1–P4 并行引擎无回归） |
| `tests/test_doclayout.py::TestOnnxModel` | 4 passed（核心 predict 无回归；整文件 KeyboardInterrupt 为既有环境噪音） |
| `tests/test_high_level_backend_degrade.py` + batch | 27 passed |
| 端到端冒烟（5 页假模型，`PDF2ZH_LAYOUT_BATCH=2`） | `batch_calls=[2,2,1]`，逐页 `predict_calls=[]`，余量正确 |

---

## 6. 结论

- 用户的"首选方案（动态 Batch）"已实现为 **`OnnxModel.predict_batch` + 页循环批量路径**，
  但实测证明 **CPU 上它不是加速器**（0.91x），故默认关闭、opt-in 开启；GPU/DML 与
  批量融合导出模型是它真正的主场。
- 用户的"次选方案（多线程共享 session）"已实测：**数值安全、实现简单、但仅 1.30x**，
  远低于现有 4 进程 chunk 并行的页级收益，不作为默认路径。
- **P1–P4 的 4 进程 chunk 并行仍是 CPU 上版面阶段最高效的页级并行方式**；本迭代的
  批量能力补齐了单进程/GPU 场景的 ONNX 原生并行选项，且不影响现有并行引擎的正确性
  与回归。
