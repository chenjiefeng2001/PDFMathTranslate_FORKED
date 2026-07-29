# pdf2zh 前端-后端数据一致性与控制台输出分析报告

> **报告日期**: 2026-07-28  
> **分析范围**: 前端 GUI (gui.py) ↔ 后端翻译管线 (high_level.py, kernel/*)  
> **分析版本**: pdf2zh v2.x (fast + precise kernel)

---

## 1. 架构概览

当前 pdf2zh 的前后端通信架构如下：

```
┌──────────────────────────────────────────────────────────────────┐
│                        浏览器 (Gradio)                           │
│  ┌──────────┐  定时轮询           ┌────────────────────────────┐ │
│  │ 前端 UI  │ ◄────────────────── │ sync_status_from_backend() │ │
│  │ 进度条   │   (每 ~0.1s 一次)   │  读取 GLOBAL_TASK_STORE    │ │
│  │ 状态标签  │                    └────────────────────────────┘ │
│  └──────────┘                                                    │
└──────────────────────────────────────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────────┐
│                   Python 后端 (同一进程)                          │
│                                                                   │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │              GLOBAL_TASK_STORE (进程内 dict)                │  │
│  │  { client_id: { status, progress, label, file_progress,   │  │
│  │                 total_progress, cancelled, paused, ... } } │  │
│  └────────────────────────────────────────────────────────────┘  │
│                           ▲                                       │
│                           │ 写入                                   │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │              background_translation_worker()                │  │
│  │  1. 状态初始化 → status="translating"                       │  │
│  │  2. 对每个文件:                                              │  │
│  │     a. 执行 kernel.translate() (ThreadPoolExecutor)          │  │
│  │     b. 启动拦截器捕获 tqdm/logging 进度消息                  │  │
│  │     c. 循环读取 progress_q → 更新 GLOBAL_TASK_STORE          │  │
│  │  3. 最终状态写入                                             │  │
│  └────────────────────────────────────────────────────────────┘  │
│                           │                                       │
│                           ▼                                       │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │                 Kernel 翻译管线                              │  │
│  │  LegacyKernel → high_level.translate() → translate_stream() │  │
│  │    → translate_patch() (tqdm.update + callback)              │  │
│  │  PreciseKernel → subprocess(venv) → v2_worker.py             │  │
│  │    → stderr JSON-lines progress events                       │  │
│  └────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────┘
```

> **关键事实**: 所有状态存储在进程内 `GLOBAL_TASK_STORE`。
> 前端通过 Gradio 的定时轮询读取状态，无主动推送机制。

---

## 2. 进度上报机制详细分析

### 2.1 三个层级的进度捕获

#### 第一层: `callback` 回调 (`high_level.py`)

```python
# high_level.py, translate_patch()
with tqdm.tqdm(total=total_pages) as progress:
    for pageno, page in enumerate(PDFPage.create_pages(doc)):
        ...
        progress.update()
        if callback:
            callback(progress)  # 每页处理完调用
```

**但在 GUI 调用链中 callback 未传入**: `kernel.translate(req, cancellation_event=...)` 没有传 callback 参数，legacy.py 中的 `callback=callback` 实际为 `None`。

#### 第二层: `_ThreadAwareLogHandler` (日志拦截)

```python
# 拦截日志中包含 "Progress:" 的消息
class _ThreadAwareLogHandler(_logging.Handler):
    def emit(self, record):
        ...
        if "Progress:" in msg:
            val = float(parts[0].strip()) * 100
            q.put(("PROGRESS", val, lbl))
```

#### 第三层: `_ThreadAwareStderr` (stderr 拦截)

```python
# 拦截 tqdm 的 stderr 输出（如 "43%|████▏..."）
class _ThreadAwareStderr:
    def _parse_for_q(self, s, q):
        if "%|" not in s:
            return
        ...
        q.put(("PROGRESS", val, lbl if lbl else "翻译中"))
```

### 2.2 进度转递完整流程

```
translate_patch()
  ├── tqdm.update  → stderr → _ThreadAwareStderr → parse → progress_q
  ├── callback     → logging → _ThreadAwareLogHandler → parse → progress_q
  │                                                              ↓
  │                 background_translation_worker 内循环:
  │                 while True:
  │                     msg_type, val, lbl = progress_q.get(timeout=0.5)
  │                     store["total_progress"] = scp
  │                     store["progress"] = scp
  │                     store["file_progress"] = last_val
  │                                                              ↓
  │                 GLOBAL_TASK_STORE[client_id] 更新
  │                                                              ↓
  │                 sync_status_from_backend() 读取 → Gradio 组件更新
```

### 2.3 进度计算公式

```python
file_progress = val           # 单文件进度 0-100
scp = (completed + errors + (last_val / 100.0)) / max(total_files, 1) * 100
# 总进度 = (已完成数 + 出错数 + 当前文件进度百分比) / 总文件数 * 100
```

#### 公式缺陷

1. **进度回退**: 当当前文件完成（last_val=100→切换到下一个文件→last_val≈0），分子骤降
2. **隐式跳变**: 过渡不连续，用户看到进度从 83% 跳到 67%
3. **缓存文件不透明**: 缓存文件计入 `completed` 但用户无感知

---

## 3. 控制台输出现状

### 3.1 前端 UI 中的输出组件

当前前端界面的"任务执行看板"包含这些组件：

| 组件 | 功能 | 信息量 |
|------|------|--------|
| `current_file_label` | 当前文件名图标文字 | 文件名 + 状态 emoji |
| `file_list_html` | 文件列表（每个文件状态） | pending/running/done/error |
| `file_progress` | 当前文件进度条百分比 | 仅数字 |
| `total_label` | 简短状态文字 | 如"翻译完成" |
| `total_progress` | 总体进度百分比 | 仅数字 |
| `batch_summary` | 最终摘要 | 仅最终形态 |

### 3.2 缺失的控制台能力

- ❌ **无滚动日志面板**: 用户看不到详细处理过程
- ❌ **无实时 API 调用日志**: LLM 调用次数、token 消耗等不展示
- ❌ **错误历史不持久**: 只截取前 50 字符，无展开查看完整错误能力
- ❌ **无扩展调试区域**: 无法展开查看更多细节

### 3.3 日志信息流断点

```
后端 logger.info/warning/error
  → stdout/stderr (仅在启动 GUI 的终端可见)
  → ❌ 前端完全看不到

后端 tqdm 进度条 → stderr → _ThreadAwareStderr
  → 只提取了百分比值，丢弃了 ETA、速度、页数等信息
  → ❌ 用户看不到"正在翻译第 X/100 页"

错误信息 → logger.error → 只取 str(e)[:50]
  → ❌ 完整 traceback 仅在终端
```

### 3.4 Precise 模式的特殊问题

PreciseKernel 使用子进程通过 stderr 输出 JSON 格式的进度事件：

```python
# v2_worker.py
print(json.dumps(progress_event), file=sys.stderr, flush=True)
```

事件格式包含：`stage`, `stage_progress`, `stage_current`, `stage_total`, `overall_progress` 等字段。

**但当前 callback 处理不兼容**:
- `precise.py` 中的 `callback(event)` 接收的是 dict
- fast 模式 callback 接收的是 tqdm 对象
- 两个 callback 接口完全不同，无法统一处理

---

## 4. 页面不活动时数据丢失的根因分析

### 4.1 问题描述

当浏览器页面长时间不活动时（用户切换到其他标签页或最小化窗口）：
1. 翻译进度停止更新（进度条卡住）
2. 任务完成后的状态更新丢失
3. 最终"翻译完成"的通知未显示

### 4.2 根因详细分析

#### 根因 1: 浏览器对后台标签页的 Timer 节流

现代浏览器对**后台标签页**的 JavaScript 定时器实施激进的节流策略：

| 浏览器 | 节流策略 |
|--------|----------|
| Chrome 84+ | 后台标签页 setInterval 限制到 ≤1 次/分钟 |
| Firefox | 后台标签页定时器节流到 ≤1 次/秒 |
| Safari | 更激进的节流 |

Gradio 的轮询机制依赖前端 JavaScript 定时器。页面进入后台后轮询可能从 100ms 间隔被节流到分钟级，极度降低了状态同步频率。

#### 根因 2: `sync_status_from_backend` 的哈希去重机制

```python
current_hash = f"{task.get('status')}_{task.get('file_progress')}_{task.get('total_progress')}_{...}"
if task.get("last_sync_hash") == current_hash:
    return (gr.update(),)*13  # ← 跳过更新！
```

去重机制在节流场景下可能引发竞态条件：

```
时间线:
T0: 轮询到 progress=50, status="translating"  → 缓存 hash="translating_50_50_0_"
T1: 页面进入后台 (浏览器节流轮询)
T2: 翻译完成 progress=100, status="done"
T3: 页面恢复, 第一次轮询触发
T3+100ms: 实际返回 done 状态 → 哈希不同 → 应该能更新
```

理论上恢复后哈希不同会触发更新，但 **实践中的失败场景**：
1. WebSocket 连接在后台期间可能断开
2. 恢复后 Gradio 需要重建 WebSocket 连接
3. 重建期间的轮询请求被丢弃，`GLOBAL_TASK_STORE` 中的状态已更新但无法同步到前端

#### 根因 3: worker 无"最终同步"推送

```python
# worker 主循环退出后，直接修改 store 并结束
# 没有任何机制通知前端"状态已变更"
if client_id in GLOBAL_TASK_STORE:
    store = GLOBAL_TASK_STORE[client_id]
    ...
    store["status"] = "done"
    # ← 这里不触发任何事件
    # ← 前端必须等待下一次轮询才能看到
```

前端完全依赖轮询。无主动推送、无 WebSocket 事件、无 Server-Sent Events。

#### 根因 4: Progress 循环退出时的微窗口漏洞

```python
while True:
    try:
        msg_type, val, lbl = progress_q.get(timeout=0.5)
        if msg_type == "DONE":
            break
    except queue.Empty:
        pass  # ← 空转，无状态变化
```

当队列收到 `"DONE"` 消息退出循环后，worker 立即写入最终状态并结束。
如果在 `break` 与 `store["status"]="done"` 之间，前端进行了最后一次轮询，
读到的是 `status="translating"`，然后前端就再也不会收到更新了（除非下一次轮询发生）。

#### 根因 5: `show_progress="hidden"` 的副作用

```python
hidden_sync_btn.click(
    sync_status_from_backend,
    outputs=[..., current_file_label, file_progress, total_label, total_progress, ...],
    show_progress="hidden"  # 隐藏进度指示
)
```

Gradio 的 `show_progress="hidden"` 影响的是按钮的加载状态显示，
**但在某些 Gradio 版本中**，如果返回的组件更新列表与实际声明不符，会导致渲染错误。

### 4.3 数据不一致的具体场景

#### 场景 A: 完成状态丢失

```
1. 前端最后成功轮询: progress=72%, status="translating"
2. 页面进入后台 (浏览器节流轮询)
3. 翻译完成: GLOBAL_TASK_STORE → progress=100%, status="done"
4. WebSocket 连接可能断开后恢复
5. 恢复后首次轮询 → Gradio 重建连接需要 1-3 秒
6. 如果在此期间 ws 重建失败 → 状态永远丢失
7. 用户看到"translating 72%"但实际已完成
```

#### 场景 B: 进度回退 (与页面无关，但与"一致性"有关)

```
total_files=3, completed=1, errors=0
文件 2 进度 50%: scp = (1+0+0.5)/3*100 = 50%
文件 2 完成 (completed=2): scp = (2+0+0)/3*100 = 66.7%
切换到文件 3, last_val≈0: scp = (2+0+0)/3*100 = 66.7%
文件 3 进度 50%: scp = (2+0+0.5)/3*100 = 83.3%

→ 注意: 66.7% → 50% 的回退实际上不会发生，
   因为 completed 在文件完成时同步递增。
   但 scp 的过渡是不平滑的。
```

#### 场景 C: `_force_sync` 缺失

当 worker 线程完成时，没有设置任何强制同步标记：

```python
# 建议添加
store["last_sync_hash"] = ""  # 清空哈希，强制前端轮询时更新
```

---

## 5. 改进建议

### 5.1 P0 - 关键修复 (可快速实施)

| # | 改进项 | 修改位置 | 改动量 |
|---|--------|---------|--------|
| 1 | Worker 结束时清空 `last_sync_hash` | `gui.py` background_translation_worker | 1行 |
| 2 | 前端添加 `visibilitychange` 事件触发强制同步 | Gradio JS / custom JS | ~5行 JS |
| 3 | 添加心跳检测，记录客户端活跃时间戳 | `sync_status_from_backend` | 2行 |

#### 建议 1: Worker 结束时强制同步

```python
# background_translation_worker 的 finally 块末尾添加
finally:
    ...
    if client_id in GLOBAL_TASK_STORE:
        GLOBAL_TASK_STORE[client_id]["last_sync_hash"] = ""  # 强制同步
```

#### 建议 2: 前端 visibilitychange 事件

```javascript
// 通过 Gradio 的 js 参数添加
document.addEventListener('visibilitychange', () => {
    if (!document.hidden) {
        // 页面恢复时强制触发一次同步
        document.querySelector('#sync-status-btn')?.click();
    }
});
```

### 5.2 P1 - 重要改进

| # | 改进项 | 工作量 |
|---|--------|--------|
| 4 | 修复进度计算公式，改为单调递增 | ~3行 |
| 5 | 修复 WebSocket 断线重连 | 中等 |
| 6 | UI 添加日志面板 | 中等 |

#### 建议 4: 单调递增的进度

```python
new_val = (completed + errors + (last_val / 100.0)) / max(total_files, 1) * 100
store["total_progress"] = max(new_val, store.get("_last_total_progress", 0))
store["_last_total_progress"] = store["total_progress"]
```

#### 建议 6: 日志面板

```python
# GUI 中添加:
with gr.Accordion("📋 详细日志", open=False):
    console_output = gr.HTML(value="", label="运行日志")

# sync_status_from_backend 中:
log_buffer = task.get("log_buffer", [])
log_html = "<div style='...'>" + "".join(
    f"<div>[{log['time']}] {log['msg']}</div>"
    for log in log_buffer[-50:]
) + "</div>"
```

### 5.3 P2 - 架构级改进

| # | 改进项 | 工作量 |
|---|--------|--------|
| 7 | 统一 fast/precise callback 接口 | 较大 |
| 8 | 引入 SSE/WebSocket 替代纯轮询 | 架构级 |
| 9 | GLOBAL_TASK_STORE 持久化/缓存 | 较大 |

---

## 6. 总结

### 核心发现

| 问题 | 严重程度 | 根因 |
|------|---------|------|
| 页面不活动时进度丢失 | **高** | 浏览器 Timer 节流 + 纯轮询 + 无保活机制 |
| 前端缺乏控制台输出 | **中** | 没有日志面板设计 |
| 进度值回退 | **低** | 计算公式缺陷 |
| 完成状态未能同步 | **中** | 无最终态推送 + 哈希去重过于激进 |
| Precise 模式进度不可见 | **高** | callback 签名不兼容 |

### 架构设计缺陷

1. **缺乏主动推送机制**: 纯轮询 + `GLOBAL_TASK_STORE` 共享内存模式无法应对页面进入后台场景
2. **双层拦截器过于脆弱**: 依赖解析格式化文本来提取进度值，而非传递结构化数据
3. **Callback 断层**: `kernel.translate()` 的 callback 在 GUI 调用链中未传入，实际依赖"旁路"的拦截器
4. **GLOBAL_TASK_STORE 无持久化**: 进程重启即丢失全部状态

### 推荐的修复顺序

```
P0 ──── ① Worker 结束清空 hash  →  ② visibilitychange 事件  →  ③ 心跳检测
          (5 分钟, 3 行代码)        (10 分钟, ~5 行 JS)          (5 分钟, 2 行)

P1 ──── ④ 单调递增进度  →  ⑤ 断线重连  →  ⑥ 日志面板
          (10 分钟)          (1-2 小时)      (2-3 小时)

P2 ──── ⑦ 统一 callback  →  ⑧ SSE/WebSocket  →  ⑨ 持久化
          (1 天)              (2-3 天)            (1 天)
```

> **最低成本修复** (30分钟): 实施 P0 全部 3 项改进 + P1 的修复进度公式，即可大幅缓解页面不活动时的数据一致性问题。


