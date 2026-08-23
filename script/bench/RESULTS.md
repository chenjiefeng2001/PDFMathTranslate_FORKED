# 性能基准报告（基线 v0.1.x-desktop）

- 测量对象：frozen onedir sidecar（`deploy/_build_sidecar/dist/pdf2zh-api-sidecar/`）
- 驱动：`script/bench/*.py`（源码 Python 经 REST/SSE 驱动交付物）
- 环境：Windows / 本机回环网络 / google 免费引擎（外网有方差，看相对值）
- 负载：合成 PDF（`bench_common.gen_pdf`，10p≈75KB / 20p）
- 复现：
  ```
  python script/bench/bench_startup.py --runs 5
  python script/bench/bench_api.py --n 200
  python script/bench/bench_translate.py --pages 10|20 --configs legacy_t1,legacy_t4,legacy_t8,babeldoc_t4
  ```

## B1 启动（5 次取中位）

| 指标 | 值 |
|---|---|
| 冷启动 spawn→health | **3.6 s**（2.1–4.6） |
| 首次 GET /api/engines | **4.9 s**（懒加载 translator 注册表） |
| 空闲 RSS | 109 MB |

## B2 API 微基准（n=200）

| 端点 | p50 | p95 | p99 |
|---|---|---|---|
| /api/health | 3.7ms | 22.2ms | 28.0ms |
| /api/engines | 4.1ms | 22.8ms | 26.6ms |
| /api/tasks | 3.9ms | 21.7ms | 24.9ms |

16 线程 health 吞吐 ≈454 req/s。

## B3 翻译管线（google, ignore_cache=true）

10 页：

| config | wall | translating 段 | peak RSS |
|---|---|---|---|
| legacy_t1 | **44.5s** | 38.7s | 2336 MB |
| legacy_t4 | 61.0s | 60.4s | 2358 MB |
| legacy_t8 | 63.6s | 63.2s | 2380 MB |
| babeldoc_t4 | 194.9s | 26.7s（analyzing 90.7 + parsing 54.3） | 2380 MB |

20 页：

| config | wall | 加速比 |
|---|---|---|
| legacy_t1 | 113.0s | 1.00× |
| legacy_t4 | 78.8s | **1.43×** |
| legacy_t8 | 91.6s | 1.23×（较 t4 劣化） |

## 结论：存在明确优化空间

### ✅ 已实施：Warm Pool 服务化（优化 #1，2026-08-23）

服务形态默认启用常驻 worker 池 + worker ORT 单线程 + 启动即预热：

- `create_api_app` 设 `PDF2ZH_WARM_POOL=1` / `PDF2ZH_WORKER_ORT_THREADS=1`（CLI 单次任务不受影响）
- 启动后台预热 2-4 worker（按核数），首个用户任务免付 spawn + ONNX 加载（实测 ~8s）
- `get_shared_pool` 复用策略：池够大时不重建（t8→t4 不再反复 respawn）

| 场景 | 前 | 后 | Δ |
|---|---|---|---|
| 10p legacy_t4 | 61.0s | **40.7s**† | **-33%** |
| 10p legacy_t8 | 63.6s | **31.2s**† | **-51%（不再劣化）** |
| 20p legacy_t4 | 78.8s | **37.0s** | **-53%** |
| 20p legacy_t8 | 91.6s | **33.8s** | **-63%** |
| 首任务附加成本 | ~8s（池 spawn+模型） | 0s（启动时预热） | — |

† 多轮中位；google 免费引擎单轮方差 ±30%，看趋势与多轮中位。
代价：空闲 RSS 从 109MB 升至 ~2.3GB 常驻池水位（worker 各持模型副本）；babeldoc 路径不经过进程池，其内存为其管线自身。

### 其余待办（按优先级）

2. **峰值内存 ~2.4GB 恒定**：需 tracemalloc/memray 定位；短期先做 worker 模型共享与 chunk fp_bytes 引用化。
3. ~~**首次 /api/engines ~4.9s**~~ ✅ 已实施：create_api_app 后台预热 translator 注册表，
   预热完成后稳态 **13ms**（实测 25 引擎）；SPA bootstrap 与预热线程竞态时最多等一次构建。
4. **babeldoc 在小文档上慢 4.4×**（analyzing+parsing 占 145s）：UI 按页数提示选择 quick/legacy；中期排查 analyze 并行化。
5. ~~**冷启动 3.6s 黑等**~~ ✅ 已实施：Tauri 无装饰闪屏立即可见（0.8s 实测出窗），
   API 就绪后切主窗口 + 关闪屏；SPA 侧 ReadyGate 轮询 /api/health 双保险。
6. **前端 bundle 1.65MB**：路由级 code splitting 与 antd 图标按需引入。

## 原始基线快照（优化前）

1. **并行负扩展**：10 页时 t4/t8 比 t1 慢 37–43%；20 页 t4 仅 1.43×、t8 反超回退。
   疑因组合：每任务 worker 冷启动（spawn + 每 worker 各自加载 ONNX 模型）、ORT 线程过订阅
   （worker 内默认吃满核）、免费引擎 QPS 限流重试。→ 已由 Warm Pool 修复（见上表）。
2. **峰值内存 ~2.4GB 恒定**：与页数/线程几乎无关 → 待定位。
3. **首次 /api/engines ~4.9s**：SPA bootstrap 即调用 → 每次启动都白等。
4. **babeldoc 在小文档上慢 4.4×**：属质量换速度。
5. **冷启动 3.6s**：桌面开窗被 main.rs 的 health 等待阻塞。
6. **前端 bundle 1.65MB（gzip 514KB）+ pdf.worker 1.26MB**。

## 基线快照方式

结果 JSON 由 bench 脚本 stdout 直接输出；优化改动后重跑同矩阵对比 wall/stage/RSS 即可量化收益。
