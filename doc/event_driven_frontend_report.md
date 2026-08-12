# PDF2ZH 前端事件驱动架构报告

> 日期：2026-08-08 | 范围：GUI 事件总线 / SSE 实时通道 / 运行时通知

---

## 一、背景与目标

旧 GUI 每 tick 全量同步 19 元组 + 逐帧推送，存在三类问题：

1. **断线必丢事件**：SSE 只推增量，浏览器重连后无法补齐错过的消息；
2. **消息语义丢失**：单调进度钳制下新 message 会被旧进度吞掉
   （见 `doc/code_bug_fix_report.md` F11）；
3. **运行时问题不可见**：CPU 降级、后端切换等只有日志、UI 无提示。

目标：**事件驱动 + 全量负载 + 断线重放 + 结构化通知**。

## 二、架构现状（会话前已完成部分）

```
Worker/前端动作 ──发布──> EventBus(pdf2zh/gui/events.py)
                            │ 全局单调 seq；按 task 留存环形历史(500)
                            ├──> TaskEventBridge(runtime_service → EventBus)
                            ├──> SSE transport(notifier.py) → 浏览器 EventSource
                            └──> Gradio 19-tuple 增量渲染(_DeltaAccumulator)
```

- `EventBus.publish`：分配全局单调 sequence，历史按 task 留存（`deque(maxlen=500)`）；
- `subscribe/unsubscribe`：按事件类型 + task_id 过滤，handler 在锁外回调（避免重入死锁）；
- `TaskEventBridge`：RuntimeService 事件监听器 → 领域事件（TaskStarted/Progress/…）；
- `_DeltaAccumulator`：只发被触碰组件的 `gr.update()` 增量，浏览器只做局部 diff。

## 三、Phase 0：SSE 全量负载 + Last-Event-ID 重放 ✅

| 模块 | 交付内容 |
|------|----------|
| `notifier.py` | `_format_frame(event)` 渲染**完整 JSON 负载**的 SSE 帧（id: seq / event: <type> / data: 全字段）；`_broadcast` 逐事件入队扇出；**移除**旧的"仅增量"推送 |
| `notifier.py` | `sse_stream(request)`：解析 `Last-Event-ID` 头，从总线 `events_after(seq)` 一次性回放遗漏事件后再进入实时流 |
| `events.py` | 新增**全局游标** `events_after(sequence)`：跨任务、跨历史地取 sequence 之后的事件（保持顺序） |
| `styles.py` | SESSION_JS 维护 `__pdf2zh_last_event_seq`：每帧更新游标；重连时把游标写进 `Last-Event-ID`；`wakeSync` 机制不变 |

**效果**：
- 断线/重连零丢失（事件幂等、有序）；
- 每帧自包含，Gradio 不做状态依赖；
- 历史有界（500/task），长时间运行内存可控。

测试：`tests/test_event_bus.py`（`test_events_after_*` 3 项）、`tests/test_gui_modules.py`
（notifier 3 项 + `test_sse_stream_replays_missed_events_on_reconnect`）。

## 四、Phase 1a：运行时通知通道 ✅

| 层 | 交付内容 |
|----|----------|
| `runtime_service.py` | `RuntimeNoticeEvent`（task_id、severity、title、detail、tip、timestamp）+ `_emit_notice()`；**CPU 降级时发出 warning 通知**（`is_cpu_degraded()` 检测点） |
| `events.py` | 领域事件 `NoticeEmitted`，注册进 `ALL_EVENT_TYPES` |
| `event_bridge.py` | 改为统一分发 `_on_event_record`：`RuntimeNoticeEvent → NoticeEmitted`，其余走进度路径 |
| `app.py` | `_ACTIVE_NOTICES` 活动通知表（模块级，task_id → markdown 行）；`_active_notice_markdown()` 追加到状态栏；`_render_notice_emitted` 渲染徽标（⚠️/ℹ️/❌）+ 终端日志；注册进 `_EVENT_RENDERERS` |

**效果**：CPU 降级、后端回退等运行时事件现在直接出现在 UI 状态栏，
不再只存在于日志文件（注：通知表为进程内存态，刷新页面后由后续事件/进度渲染重建）。

测试：`tests/test_gui_modules.py::TestNoticeChannel`（3 项：通知入表、徽标/状态渲染、消息渲染共存）。

## 五、入口 spawn 崩溃链（跨层协同修复）

Phase 0/1a 的服务端事件由 Worker 进程发布；Windows spawn 下 Worker
启动即崩溃会让所有推进失效，因此本轮同步修复：

- **根因**：spawn 启动器把 `-I --multiprocessing-fork` 交给入口脚本，
  顶层无 `__main__` 守卫 → argparse 崩溃 → BrokenProcessPool → CPU 回退；
- **修复**：`pdf2zh.py` 新增 `is_spawn_child()` / `spawn_child_yields_to()`
  （含 `freeze_support` 兜底）；`main()` 与 gui/entry、gui/app、backend、
  mcp_server、`script/build/pdf2zh.int` 全部加守卫。

详见 `doc/code_bug_fix_report.md` 第八章 F10。

## 六、测试与回归

| 套件 | 数量 |
|------|------|
| 全量 pytest（tests/） | **2061 passed / 1 skipped** |
| 本轮新增/扩展 | test_spawn_entry（5）、test_event_bus（+3）、test_gui_modules（+6） |

## 七、路线图（下一步）

1. **Phase 1b 通知中心**：通知分级持久化 + 历史面板（终端 tab 之外的可折叠列表）；
2. **Phase 1c 浏览器端重放优化**：批量重放时按 type 合并渲染，减少 Gradio 往返；
3. **spawn 修复端到端验证**：用 `script/build/pdf2zh.int` 在真机跑一次 GPU 并行任务，
   确认不再出现 `-I --multiprocessing-fork` 崩溃（当前已由单测覆盖判定逻辑）。
