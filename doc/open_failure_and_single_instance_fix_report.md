# 桌面「打开失败」根因修复与单实例守卫——实施报告

> **日期**：2026-08-26 · 环境：Windows 11 · 范围：`frontend/src-tauri/src/main.rs`、`Cargo.toml`、`frontend/src/theme/AppShell.tsx`、`frontend/src/i18n/index.ts`
> **前置调查**：doc/perf/coldstart-trace/report.md（冷启动优化后遗留的打开失败面）

---

## 0. 结论（TL;DR）

冷启动优化落地后，用户仍偶发「双击无响应 / 秒退 / 无限转圈」。归因后确认全部指向同一根源：**固定端口 11009 + 壳层对子进程零观测**。本次一次性落地五项修复：

| # | 故障面 | 修复 |
|---|---|---|
| 1 | 固定端口被占（孤儿 sidecar / 双开 / 其他软件）→ `[Errno 10048]` 秒死 | **动态临时端口**；`PDF2ZH_API_PORT` 显式指定时仍尊重用户 |
| 2 | sidecar stderr 完全丢失，排障靠猜 | stdout/stderr **落盘 `%TEMP%\pdf2zh-sidecar.log`** |
| 3 | 子进程提前死亡时壳层傻等满超时 | 看门狗 **fail-fast**：`try_wait` 双监测，死亡立即报错弹窗（含日志路径）并退出；超时 30s→60s（覆盖 AV 首扫） |
| 4 | 双开：第二实例在插件生效前已完成「taskkill 清场 + 拉起 sidecar」，**误杀第一实例后端** | `main()` 最前面加**内核命名互斥体**拦截；`tauri-plugin-single-instance` 降为第二道防线（聚焦已有窗口） |
| 5 | 前端 ReadyGate 极端情况下无限转圈 | 连续失败约 40s 后切换**可重试错误态**（i18n 中英文案齐备） |

---

## 1. 根因链

```
固定端口 11009
 ├─ 上次会话孤儿 sidecar 存活 ──┐
 ├─ 用户双开第二实例 ───────────┼─► 新 sidecar bind [Errno 10048] 秒死
 │    └─ 且其 taskkill 清场会先误杀第一实例的后端（插件生效前的窗口期）
 ├─ 其他软件恰好占用该端口 ─────┘
 └─ 壳层既不感知子进程存活，也无 stderr 重定向
      └─ wait_for_api 只探 TCP，傻等满 30s → kill_server → exit(1)，无任何提示
           └─ 用户视角：闪一下就消失 / 偶发无限转圈（前端 ReadyGate 无失败兜底）
```

---

## 2. 关键实现决策

### 2.1 动态端口（`pick_free_port`）
绑定 `127.0.0.1:0` 由系统分配后立即释放。理论上的释放-复用竞窗概率极低，且后果退化为旧行为（bind 失败被看门狗捕获并弹窗），不会更糟。

### 2.2 单实例的双保险顺序
内核互斥体（`CreateMutexW` + `GetLastError()==ERROR_ALREADY_EXISTS`）在 `main()` 最前面执行，早于一切副作用（taskkill 清场、sidecar spawn）。第二实例温和提示后静默退出。插件的聚焦回调保留：处理「重复启动想把已有窗口带到前台」的交互预期。

### 2.3 spawn 前清场保留但收窄
单实例锁保证此刻不该有存活者，故按映像名 `taskkill /F /IM pdf2zh-api-sidecar.exe` 清掉强杀/崩溃遗留的僵尸（白白占 ~64MB）。仅在捆绑 sidecar 路径执行；开发态 python 后备不清理，避免误伤用户其他 python 进程。

### 2.4 失败可见性
看门狗失败路径改为 `blocking_show()` 错误弹窗（含失败原因 + 日志文件路径），随后回收子进程并 `exit(1)`。日志每次启动截断重开，句柄创建失败则退回 inherit 的旧行为。

### 2.5 前端兜底阈值
60 次 × 700ms ≈ 42s，略大于看门狗的 60s 弹窗路径之外的自然失败面（如后端活着但 health 卡死）。重试按钮清零计数重新进入轮询。

---

## 3. 验证

| 项 | 结果 |
|---|---|
| `cargo build`（含 MSVC 链接） | ✅ 通过 |
| `cargo clippy` | ✅ 无警告 |
| `cargo fmt --check` | ✅ 已格式化 |
| `tsc --noEmit`（前端类型检查） | ✅ 通过 |

**续接事故记录**：上个会话崩溃于链接验证环节——FFI 声明误用小写 `getlasterror`/`messageboxw`（真实符号 `GetLastError`/`MessageBoxW`），`cargo check` 不做链接因此未暴露，`cargo build` 报 LNK2019 两处未解析外部符号。已修正符号大小写后全量构建通过。

**教训**：涉及 `extern` 块的改动，验证下限必须是 `cargo build`（链接），不能只跑 `cargo check`。

---

## 4. 残留与后续

- 单实例互斥体名 `Local\PDFMathTranslate.SingleInstance` 为硬编码；若未来支持便携版多份并存需求，需改为按安装路径派生。
- `taskkill` 清场是 Windows-only best-effort；跨平台分发时需对应方案。
- sidecar 日志固定写 `%TEMP%`，未随设置里的输出目录走（保持简单，弹窗已指路）。
