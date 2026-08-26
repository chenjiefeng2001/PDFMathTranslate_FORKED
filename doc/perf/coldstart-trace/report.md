# 桌面冷启动慢——trace 根因报告

> **日期**：2026-08-26 · 环境：本机（Windows 11，已安装 0.1.0 本地构建）
> **工具**：外部细粒度计时（`script/trace_sidecar_coldstart.ps1`）+ `py-spy` t=0 全程采样（200Hz）+ 开发环境 `-X importtime` 对照
> **原始数据**：本目录 `sidecar_external_timing.json` / `sidecar_boot_t0.json`（speedscope 可视化：https://www.speedscope.app ）

---

## 0. 优化落地与复测（同日更新）

按 §5 建议落地 P0-1/P0-2/P1-3/P1-4 后复测：

| 指标 | 优化前 | 优化后 | 变化 |
|---|---|---|---|
| sidecar TCP listen（热） | 3.4~4.4s | **1.7~2.6s** | ≈ -55% |
| sidecar health 200（热） | 3.4~4.4s | **1.8~2.6s** | ≈ -55% |
| 冷首轮 health 200 | 5.2s | **3.1s** | -40% |
| sidecar RSS | ~103-143MB | **~64MB** | pymupdf 不再常驻 |
| 桌面双击→主窗口可见 | 闪屏盲等至 API 就绪 | **<0.1s** | ReadyGate 接管等待态 |
| 桌面双击→API 就绪（暖机后） | ~5s | **3.5s** | 含桌面壳自身启动 |

落地项：
- **P0-1** `main.rs`：移除闪屏窗口，主窗口立即可见（ReadyGate 呈现「正在连接本地翻译服务…」）；wait_for_api 线程降级为 30s 启动失败看门狗。
- **P0-2** `services/api.py`：三路预热统一延迟错峰点火（registry +0s / layout +2s / pool +4s，基准延迟 `PDF2ZH_PREWARM_DELAY` 默认 2s），不再与 bind 前的关键路径抢核。
- **P1-3** `pdf2zh/__init__.py`：pymupdf/fitz 急切导入改为 meta_path 一次性拦截钩子——日志过滤器仍先于首次导入挂载（弃用提示照旧被过滤），原生 DLL 加载移出关键路径；回归测试 `TestLazyPymupdfRouting`。
- **P1-4** `installer-hooks.nsh`：POSTINSTALL 经 `cmd /c start` 分离触发对 `$INSTDIR` 的 Defender 自定义扫描，消除首启逐文件扫描惩罚（best-effort，失败静默）。

注：安装后**首次**启动若与后台扫描重叠仍可能偏慢（实测一次 20s），属一次性成本；第二次起即为上表稳态值。

---

## 1. 结论（TL;DR）

热启动到 `/api/health` 200 实测 **3.4~4.4s**，构成：

| # | 阶段 | 耗时(真实估算) | 证据 |
|---|---|---|---|
| 1 | PyInstaller bootloader + 解释器初始化 | ~0.8s | `--help` 提前退出路径实测 0.78~0.82s |
| 2 | **PYZ 归档逐模块 zlib 解压** | **~1.3s** | 主线程 t=0 起 `pyimod01_archive.extract` 连续占用（剖析 2.4s，采样开销≈2x） |
| 3 | **fastapi/pydantic 模型构建**（import 副作用） | **~1.5s** | dev 环境同链 `core_schema` self 0.94s + `openapi.models` 0.52s + `fastapi.exceptions` 0.35s |
| 4 | `create_api_app()` + uvicorn bind | ~0.3s | dev 实测 0.296s |
| 5 | 冷首轮额外 | +1.7s | Defender 首触扫描（仅首次/文件变更后） |

**叠加因素**：启动头几秒有 3 个预热后台线程并发抢占 CPU/Disk（见 §3），以及桌面壳把「主窗口显示」押后到 API 完全就绪之后（见 §4）。

感知总时长 ≈ 侧车就绪(3.9s) + 主窗口显示 + SPA 首帧 ≈ **5s 左右的闪屏等待**。

---

## 2. 关键证据

### 2.1 外部计时（安装版 sidecar 独立运行）

```
run1 [cold] listen=3537ms health=5227ms   ← 首次（Defender 扫描）
run2 [warm] listen=4360ms health=4397ms
run3 [warm] listen=3389ms health=3425ms
run4 [warm] listen=3802ms health=3833ms
run5 [warm] listen=3951ms health=3970ms
```

listen→health 差值热态仅 ~30ms ⇒ **瓶颈完全在进程启动到端口绑定之前**。

### 2.2 主线程时间轴（py-spy 自 t=0 托管启动）

```
t=0~5s : extract(pyimod01_archive.py) ←←← PyInstaller PYZ 解压（最大单项）
t=4~8s : pydantic/fastapi 模型机器（__init__/add_fns_to_class/
         validate_core_schema/__set_name__）
t=8s~  : _poll（uvicorn 事件循环 = 服务已就绪）
```

类别占比（可重叠）：pydantic/fastapi/starlette 41% · uvicorn 25% · import 机制本体仅 1.8%
⇒ 慢的不是 import 的"查找"，而是 **解压（PYZ）与模块级副作用（pydantic 模型构建）的 CPU 时间**。

### 2.3 冻结 vs 开发环境对照

同一导入链 dev 热态仅 **1.19s**（api）+0.48s（uvicorn）；冻结侧多出 **~1.3s 即 PYZ 逐模块 zlib 解压**——开发态读的是 OS 缓存里的松散 `.pyc`，无需解压。

---

## 3. 启动期后台线程争抢（不阻塞 bind，但拖慢 §2 全程）

py-spy 多线程画像（12s 采样窗口内的线程耗时）：

| 线程 | 热点 | 耗时 |
|---|---|---|
| registry-prewarm | `ssl.create_default_context`（翻译引擎客户端初始化） | **4.15s CPU** |
| layout-model-prewarm | ORT `_create_inference_session`（doclayout 会话） | 1.36s |
| （babeldoc 资产校验） | `verify_file` + `extract` | ~2.4s |

这些 daemon 线程在 `create_api_app()` 里立即点火，与主线程的解压/模型构建并发抢核。多核下影响有限，但低核数机器上会放大 §2 各项。

---

## 4. 桌面壳放大感知延迟

`main.rs` 现状：主窗口 `visible(false)` 构建（webview 其实已在后台加载 SPA），但**等 API 就绪才 show**；期间用户只能看到 380×170 的小闪屏 4~5s。加上 `wait_for_api` 300ms 轮询粒度（均值 +150ms）。

---

## 5. 优化建议（按性价比排序）

| 优先级 | 措施 | 预期收益 | 成本 |
|---|---|---|---|
| **P0-1** | **主窗口立即 show**：ReadyGate 已能优雅处理「API 未就绪」，改为启动即显示主窗体（内部呈现连接中状态），闪屏随即关闭 | 感知延迟 4~5s 盲等 → **<1s 见到真实 UI** | 改 main.rs 十余行 |
| **P0-2** | 预热线程延迟点火（bind 后 sleep 2s 再跑）或降优先级 | bind 提前 ~0.3-0.8s（低核机更多），首秒 UI 更跟手 | api.py 小改 |
| P1-3 | `pdf2zh/__init__.py` 的 `pymupdf/fitz` 急切导入改惰性（消息过滤注册延后到首次使用） | 关键路径 -0.38s | 低，需回归 fitz 提示抑制 |
| P1-4 | 冷首轮 Defender：安装器完成后对安装目录做预扫描（`Start-MpScan`）或引导用户加排除项 | 首启 -1.7s | 中（文档/脚本） |
| P2-5 | PYZ 解压为 PyInstaller 固定行为，无官方压缩开关；缩减捆绑纯模块数才能降低实际被导入集合，收益有限 | — | 不建议动 |

> 说明：fastapi/pydantic 的 import 副作用（~1.5s）是生态固有成本，除非换路由栈否则不可消除；上述组合把「感知冷启动」压到 1s 内已是最优路径。

---

## 6. 复现命令

```powershell
# 外部计时
pwsh -File script\trace_sidecar_coldstart.ps1 -Runs 5

# t=0 采样（speedscope 查看）
py-spy record --duration 15 --rate 200 --format speedscope `
  -o doc\perf\coldstart-trace\sidecar_boot_t0.json -- `
  "C:\Users\14977\AppData\Local\PDFMathTranslate\pdf2zh-api-sidecar\pdf2zh-api-sidecar.exe" --port 11097

# 开发环境导入分解
python -X importtime -c "import uvicorn; from pdf2zh.services.api import create_api_app" 2>it.err
```
