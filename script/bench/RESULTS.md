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

1. **并行负扩展（最高优先级）**：10 页时 t4/t8 比 t1 慢 37–43%；20 页 t4 仅 1.43×、t8 反超回退。
   疑因组合：每任务 worker 冷启动（spawn + 每 worker 各自加载 ONNX 模型）、ORT 线程过订阅
   （worker 内默认吃满核）、免费引擎 QPS 限流重试。动作：常驻进程池复用（parallel/pool.py 已有雏形）、
   worker 强制 `PDF2ZH_WORKER_ORT_THREADS=1`、按引擎限流自适应 chunk 提交节奏；t8 档位直接隐藏或警告。
2. **峰值内存 ~2.4GB 恒定**：与页数/线程几乎无关 → 疑似 ORT arena + 每 worker 模型副本 +
   全文档字节多份拷贝。需 tracemalloc/memray 定位；短期先做 worker 模型共享与 chunk fp_bytes 引用化。
3. **首次 /api/engines ~4.9s**：SPA bootstrap 即调用 → 每次启动都白等。动作：sidecar 启动后台
   预热注册表；或把 engines 缓存序列化。收益：设置抽屉/引擎下拉即开即用。
4. **babeldoc 在小文档上慢 4.4×**（145s 花在 analyzing+parsing）：属质量换速度，建议 UI 按
   页数提示选择 quick/legacy；中期排查 analyze 阶段可否并行。
5. **冷启动 3.6s**：桌面开窗被 main.rs 的 health 等待阻塞。可与窗口打开并行（先开窗显示加载态，
   后台等 API），体感启动时间可降 ~3s。
6. **前端 bundle 1.65MB（gzip 514KB）+ pdf.worker 1.26MB**：vite 已告警；做路由级 code splitting
   与 antd 图标按需引入。

## 基线快照方式

结果 JSON 由 bench 脚本 stdout 直接输出；优化改动后重跑同矩阵对比 wall/stage/RSS 即可量化收益。
