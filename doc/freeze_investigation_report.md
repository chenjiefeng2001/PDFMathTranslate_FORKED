# pdf2zh「实例卡死」调查报告

调查日期：2026-08-30
范围：`pdf2zh/semantic/layout/*`（本会话在改的 7F-8/7G 排版层）、
`pdf2zh/services/runtime_service.py` / `api.py`（任务调度、SSE 泵）、
`pdf2zh/parallel/coordinator.py`（进程池调度）。

结论速览：**本次排版改动的四个执行器（page_shift / page_break / page_break_continuation / packer）全部按“有界 + 无进展即停”设计，均无未受约束的循环，不是当前卡死来源**（而且该层目前只被测试引用、尚未接入生产管线）。真正的“卡死”历史与风险集中在**运行时任务/进程池/解析引擎**这几个既有层，本文用代码证据逐一列明。

---

## 1. 调查方法

对待查的所有循环点做了两种静态审计：

1. 全量圈出 `while True` / `while` / `for _ in range` / `recursion` —— 22 个源文件中的命中见附录 A；
2. 对每个脚本级循环核对三件事：**是否单调推进、是否在有界集合上迭代、是否带无进展/超预算退出**。

同时读了与卡死问题直接相关的三个层：排版执行器、运行时任务/SSE、并行进程池。

---

## 2. 结论：当前排版改动层不会卡死（有证据）

`pdf2zh/semantic/layout/` 里唯一一个原生 `while` 是 `page_break.next_free_page`：

```python
page = int(source_page) + 1
while page in taken:
    page += 1
if max_page is not None and page > int(max_page):
    return None
return page
```

它在**有限的 occupied 集合**上自增，最多迭代 `|taken|` 次即退出（要么找到空闲页，要么超过 `max_page` 返回 `None`），不会跑飞。

其余收敛路径全部是**显式有界 + 无进展即停**，且都已在配套测试里验证过 bound：

| 模块 | 循环形式 | 上界 | 无进展出口 |
|---|---|---|---|
| `page_shift.resolve_page_shifts` | `for i in range(bound)`，`bound=max_passes or len+1` | 元素数+1 | 本轮 `applicable` 为空 → `no_progress` break |
| `page_shift._ordered_cascade_plan` 阶段1 | `for _pass in range(len+1)` | 元素数+1 | `applied_this==0` break |
| `page_break_executor.execute_page_breaks` | 单遍 `for`，`budget=min(max_page_breaks, n)` | ≤块数 | 预算耗尽置 `deferred`，不循环 |
| `page_break_continuation.execute_continuation_breaks` | 单遍 `for`，`budget=min(max_splits, n)` | ≤块数 | 同上，`next_free_page` 无真实页 → `no_page` |
| `global_recovery` | `for pass in range(1, bound+1)`，`bound=n_blocks+1` | 块数+1 | 碰撞为 0 → converge；否则签名未变 → `no_progress` |
| `packer.resolve_packing` | 单遍（纯几何） | 无循环 | N/A |
| `page_break.next_free_page` | `while`（唯一） | \|taken\| | 见上 |

**关键点**：这批排版逻辑当前在**生产链路里并没有被调用** —— 全仓库对 `global_recovery` / `resolve_packing` / `execute_page_breaks` 等的引用只出现在 `tests/`（`code_search` 证据），`translate_stream` / `_execute_legacy` / `_execute_babeldoc` / `_execute_magicpdf` 走的是另一条既有渲染路径。因此**即便将来接入，也需先确认外层编排不会重复触发；现阶段它不可能是线上实例卡死的根因**。

> 接入前唯一要留意的点：`global_recovery` 的默认上界是 `n_blocks + 1` 轮，每轮 `detect_page_collisions + detect_page_overflows` 各做一次全量碰撞检测——对极厚文档是 `O(passes × block²)`。有界 ≠ 快；接入真实长书时要估算单页/单文档耗时，不要设成无上限循环。

---

## 3. 既有层的“卡死”风险点（按代码证据整理）

全部来自 `runtime_service.py` / `parallel/coordinator.py` 的注释与逻辑，均为**本仓库自述的真实历史卡死类别**，也是排查线上“卡死”时应优先怀疑的地方。

### 3.1 V4 占位引擎的 DocumentGraph 迭代死锁（历史，已下线）
`runtime_service.py` `MODE_PRESETS` 注释原文：

> 不再暴露 V4 引擎模式（v3/v4）：…曾因 **DocumentGraph 迭代死锁导致任务卡死、队列锁死**。

- 现状：所有 `use_v4_engine` 预设均已 `False`，`MODE_PIPELINES` 只映射到 legacy / babeldoc，`_execute_v4` 已不可达。
- 排查提示：若任务“新建即卡、进度不动、无 CPU 烧号”，先确认调用方没有手动把 `use_v4_engine=True` 传进来（代码仍在 `_execute_task` 分支列表里保留）。

### 3.2 进程池 / 并行 worker 的“后续文件不再翻译”（半卡死）
`coordinator.py` 与 `_reset_shared_layout_model` / `_execute_task` 的 V3-6 注释多次写到同一种现象：

> 「某个文件出错/取消后，后续所有文件都不再翻译」/「任务一直…看似卡死」

对应两种已内置的兜底（说明这是真实发生过、现已加防护的类别）：

1. **中断旗标残留**：`_execute_task` 开头 `reset_interrupt_flag()`，否则上一任务的 Ctrl+C 旗标会让新任务开局即 `KeyboardInterrupt`，表现为新文件不翻译。
2. **ONNX 版面会话损坏**：`_reset_shared_layout_model()` 在失败路径 `release_model_instance()`，否则复用一个损坏的 `InferenceSession` 会让同批次后续文件静默停摆。

排查提示：遇到“某文件出错后，后文全部不走、任务永远 RUNNING，CPU 又很平静”时——这是**进程池/会话级半卡死**的典型签名，重点看这两处是否生效、以及 `interrupt.py` 的旗标是否被清。

### 3.3 MinerU / magicpdf 长文档被误判“卡死”（慢 ≠ 假死）
`_MINERU_LONG_DOC_PAGES = 300` 与注释原文：

> 修复 #5：**1262 页书被误判「假死」的预防性提示**。MinerU pipeline 本地解析约 0.5–2 秒/页且默认 CPU…

- 超过 300 页时，代码会在解析启动前给用户“改用 BabelDOC / 页码范围分批”的可操作提示。
- 这是**“假死”**：任务其实在推进，只是数百页 CPU 解析期间长时间没有任何进度事件，观感上等于卡死。
- 排查提示：STUCK 在 `parsing` 段、CPU 满载、日志在逐页刷——属于本条，靠 ETA/`task/stage_detail` 确认推进即可，不是 bug。若进程完全无日志且 CPU 为 0，才回到 3.2/3.4。

### 3.4 翻译服务限流 / CAPTCHA 健康检查刷屏失败（配额型卡死）
`_ENGINE_COOLDOWN_SECONDS = 60.0` 及注释：

> 当翻译服务被限流（HTTP 429 / reCAPTCHA…）时，健康检查会持续失败数分钟；…表现为“任务一直刷屏失败 / 看似卡死”。

- 已内置冷却表：限流失败进入 60s 冷却，同引擎组合直接快速失败，不再重复探测。
- 排查提示：任务在 `translating` 段反复失败/重试、curl 手工 `translate("Hello")` 报 rate-limit/captcha —— 属本条，属外部依赖被限，与应用无循环无关。

### 3.5 SSE / 事件泵与订阅循环（已安全收口）
- `runtime_service.subscribe_events`：按 `terminal status` 退出；
- `api.py` SSE 泵：有独立 `pump_thread`，`stop.wait(0.4)` 轮询等待，队列排空 + 泵退出才收尾；
- `_pause_guard`：`while pe.is_set()`，由 cancel 事件中断；
- `_sweeper_loop`：daemon + `sleep` 节流。

这四个 `while True` 均有明确退出/中断条件，不构成卡死源。

---

## 4. 卡死类别 → 症状对照表（排查用的最短路径）

| 症状 | CPU | 日志 | 最可能类别 | 先查 |
|---|---|---|---|---|
| 新建任务即不动，进度 0% | 低 | 无 | 中断旗标残留 / 会话损坏 / 手动 V4 | 3.2 / 3.1 |
| 卡在 `parsing`，日志逐页刷 | 满载 | 在推进 | MinerU 长书“假死” | 3.3 |
| `translating` 反复失败重试 | 低 | rate-limit/captcha | 外部限流 | 3.4 |
| 某文件出错后全文不译 | 低 | 失败后静默 | 池/会话半卡死 | 3.2 |
| 排版层（当前改动） | — | — | **非根因**，尚未接入 | 2 / 5 |

---

## 5. 建议的加固动作（按优先级）

1. **接入排版层时先加“绝对轮次上限 + 每次外层重复编排的防抖”**：现有 `global_recovery` 有界，但若给它外层再套 detect→recover 循环，必须沿用同一套 no-progress / budget 语义，否则长书会变成 `O(passes×block²)` 的肉眼卡顿。
2. **给 MinerU/magicpdf 长文档一个“仍在推进”的落点**：300 页提示已存在，建议在逐页解析中按 N 页发一次 `parsing` 进度事件/ETA，彻底消除“假死”观感（现有 `stage_detail` 通道可直接复用它）。
3. **给任务增加 watch-dog 兜底**：目前“旗标残留/会议损坏”全靠启动时 reset + 失败路径 release 防御；可加一个 per-task 心跳，超过阈值把 RUNNING 任务标 FAILED 并 `_reset_shared_layout_model()`，把“看似卡死”变成“明确失败”。
4. **保留对手动 `use_v4_engine=True` 的拒绝/告警**，防止历史死锁路径被绕过预设重新打开。

---

## 附录 A — 全仓循环命中清单（已逐条核对）

- 排版层：`page_break.py:256`（有界 while，见 §2）。其余为注释中的描述性文字。
- `runtime_service.py`：`1074`=`_pause_guard`（cancel 可中断）、`3486`=`_sweeper_loop`（daemon+睡眠）。
- `api.py:1099`：SSE 泵（队列排空+泵退出收尾）。
- `coordinator.py:221`：`concurrent.futures.wait` 轮询窗口（有 timeout，Ctrl+C 可短路）。
- `translator.py:154`、`v3/planner.py:494`：按字符上限切块的 `while`，单调推进、必停。
- `v3/image_calibrate.py:135`：需重点复核（本次改动未涉及，但列为待人工确认项）。
- `vendor/MinerU/*`、`pdf2zh/kernel/*`（submodule/第三方）：不计入本次范围。

## 附录 B — 结论一句话

当前 7F-8/7G 排版改动**不是**线上卡死的根因（有界且未接入生产）；真实卡死风险在 minRuntime 的**进程池中断旗标 / ONNX 会话损坏**、**MinerU 长书假死**与**外部翻译限流**三类，属既有防护项，建议按 §5 的 watch-dog 与进度落点收口。