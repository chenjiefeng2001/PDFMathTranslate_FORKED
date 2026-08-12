# 前端框架替换可行性报告：Gradio → 商业级前端

> 日期：2026-08-08 | 范围：pdf2zh GUI 前端（pdf2zh/gui/*）
> 结论摘要：**替换可行且推荐，分层清晰** —— 事件总线 / SSE 传输 / 业务服务三层已与
> Gradio 完全解耦，可 100% 复用；强耦合仅存在于"面板组件 + 服务端渲染器"一层。
> 建议走 **B 路线（双轨渐进替换）**，先以独立 SPA 消费现有 API/SSE 契约，再逐面板下线 Gradio。

---

## 一、结论速览

| 维度 | 结论 |
|------|------|
| 可行性 | ✅ 高。核心数据链路（EventBus→SSE→页面）**与 Gradio 无关**，已按"事件驱动"设计 |
| 可复用率 | 后端业务层 ~100%、事件总线 ~100%、SSE 传输 ~90%、状态渲染器 ~70%（改输出格式）、UI 面板 ~30%（重写） |
| 推荐路线 | B：FastAPI 主机 + SPA 前端（Vue3/React 任选）+ 现行动态接口，渐进替换 |
| 主要成本 | 面板重写（上传/配置表单/诊断/预览/日志）、鉴权与文件服务改造、E2E 测试 |
| 主要收益 | 商业级鉴权（OAuth2/SSO）、主题品牌化、前端构建体系、监控埋点、包体与启动性能、可测试性 |
| 风险 | 中级：双轨并存期维护两套 UI；需以"渲染契约冻结"控制双写成本 |

---

## 二、当前实现盘点（2026-08）

### 2.1 技术栈

| 项 | 现状 | 位置 |
|----|------|------|
| GUI 框架 | Gradio 5（`gr.Blocks` + `gr.queue`） | `pyproject.toml:29`（`>=5.20,<5.36`） |
| Web 服务 | Gradio 内部 FastAPI/Starlette 应用（`gui.app`） | `pdf2zh/gui/app.py:1194` |
| 状态同步 | **服务端渲染**：20 元组增量契约 + `gr.State` 游标 | `pdf2zh/gui/app.py:870-946` |
| 实时推送 | SSE 自研（非 Gradio 提供），`/gui/events` 挂到 FastAPI | `pdf2zh/gui/notifier.py:127` |
| 页面 JS | 内嵌原生 JS（`SESSION_JS`：SSE 唤醒、主题切换、localStorage 持久化） | `pdf2zh/gui/styles.py:529-763` |
| 样式 | 手写 CSS + Gradio Base 主题（明/暗双主题） | `pdf2zh/gui/styles.py` |
| 国际化 | 服务端双语字典（zh/en），随 HTML 注入 | `pdf2zh/gui/i18n.py` |
| 鉴权 | 仅 Gradio `auth=[user,pass]` 基础认证 | `pdf2zh/gui/entry.py:59-61` |
| 上传/下载 | Gradio 的 `Upload/File` 组件 + `/pdf-preview/` 自定义路由 | `app.py:1182-1261` |

### 2.2 运行时数据链路（已事件驱动）

```
Worker/翻译核心 ──发布──> EventBus(events.py，纯 Python，无 Gradio)
                            │ 全局单调 seq；按任务环形历史(500/任务)
                            ├─> TaskEventBridge ─ 运行时状态订阅（纯业务）
                            ├─> EventNotifier ──SSE(Starlette 端点)──> 浏览器 EventSource
                            │    每帧全量 JSON + id:游标 + Last-Event-ID 断线重放
                            │    （事件驱动前端报告即本链路的规范，见 doc/event_driven_frontend_report.md）
                            └─> app.drain_events：delta 增量 → 20 元组 → Gradio 组件更新
```

### 2.3 耦合度分层评估（替换的关键依据）

| 层 | 文件 | 依赖 Gradio | 可复用性 | 说明 |
|----|------|:---:|:---:|------|
| 业务/翻译核心 | high_level.py / converter.py / services/runtime_service.py | ❌ | 100% | 纯 Python，与 UI 无关 |
| 事件总线 | `gui/events.py`（TaskEvent、EventBus、`events_after`） | ❌ | 100% | 无 Gradio import，纯线程模型 |
| 运行时桥接 | `gui/event_bridge.py` | ❌ | 100% | RuntimeService → EventBus 适配 |
| SSE 传输 | `gui/notifier.py` | ❌ | 100% | 仅依赖 Starlette Request/Response，可直接挂到任何 ASGI 应用 |
| 浏览器唤醒 JS | `gui/styles.py` SESSION_JS → `wakeSync()` | 半 | ~90% | 依赖 Gradio 生成的 DOM 之外的逻辑均为通用 JS；`wakeSync` 本身只点一个元素 |
| 状态渲染器 | `app.py` `_fill_full_state` / `_render_*` | 中 | ~70% | 全部状态/HTML 片段在 Python 生成（`build_*_html`），只需把"写 gr 组件"改成"写 JSON" |
| 面板组件 | `gui/components/*.py`（5 个面板） | ✅ | ~0% | `gr.` 组件树需按新框架重写 |
| 服务编排 | `gui/app.py:create_gui`（布局/绑定/queue/auth） | ✅ | ~10% | `gr.Blocks` 生命周期与 `launch` 逻辑废弃 |

**关键事实**：整个 `gui/` 下只有 `app.py` 与 5 个 `components/*.py` 直接 `import gradio`
（已核验：`worker.py`、`state.py`、`events.py`、`notifier.py`、`event_bridge.py`、`i18n.py`、
`logger.py` 均零 Gradio 依赖）。这是替换工程**最低风险的结构性前提**。

### 2.4 现状已具备的"商业级"能力（应保留）

- ✅ 事件驱动实时推送（SSE 全量帧 + 断线重放，无轮询）；
- ✅ 状态恢复：`localStorage` 保存上次任务/配置/结果；
- ✅ 明暗主题持久化（`TOGGLE_THEME_JS`）；
- ✅ 上传大小可配（`PDF2ZH_MAX_FILE_SIZE`，默认 100MB）；
- ✅ 双语 i18n（zh/en，全服务端渲染，改造成本低）；
- ✅ 任务并发限制（`queue(default_concurrency_limit=2, max_size=10)`）；
- ✅ 日志背板（`/gui/logs` 环形缓冲 API）与终端面板；
- ✅ 运行时通知（CPU 降级告警等，见 `doc/event_driven_frontend_report.md` Phase 1a）。

### 2.5 商业化差距项（替换的动机）

| # | 差距 | 现状 | 商业方案 |
|---|------|------|----------|
| D1 | 鉴权/多租户 | Gradio 基础认证（单用户对） | OAuth2/JWT/Session，可选 SSO 企业登录，按租户隔离任务目录 |
| D2 | 品牌与主题 | Gradio 主题 + 手写 CSS | 全面主题化（CSS 变量 / 设计令牌 / 白标 logo/文案） |
| D3 | 元件生态 | Gradio 20+ 组件弱定制 | 成熟组件库（Element Plus / Ant Design）：表格、弹窗、抽屉、消息、上传拖拽、分页 |
| D4 | 构建体系 | 无前端构建链（字符串 JS/CSS 内嵌） | Vite + TS 构建管线，可做代码拆分、`fingerprint`、按需加载 |
| D5 | 可观测性 | 无 | 前端埋点（Posthog/自建）、请求链路 trace、性能指标（FCP/LCP） |
| D6 | 测试 | 单元测试覆盖 Python 渲染器 | Playwright E2E（上传→翻译→下载全流程） |
| D7 | 部署形态 | 单进程 Gradio 服务（Docker 亦可） | 标准 FastAPI 应用：多进程 worker、反向代理友好、健康检查、优雅停机 |
| D8 | 包体/内存 | Gradio 全家桶较重（含前端 bundle 全局~几十 MB 依赖） | 自研 SPA 仅加载所需依赖，`enabled` 可裁剪 |
| D9 | 交付许可 | Gradio Apache-2 | 商业闭源无传播约束（若选 Apache 生态组件仍免费） |

---

## 三、替换工程量与方法论

### 3.1 可保留的"API 契约"（零重写）

1. **事件契约**（`events.py`）：`TaskEvent.to_dict()` × N 种事件类型 —— **直接就是新前端的 JSON 协议**；
2. **SSE 契约**（`notifier.py`）：`/api/events` 全量帧 + `id` 游标 + `Last-Event-ID` 重放 —— 新前端 `EventSource` 原样使用；
3. **状态契约**（`app.py` 渲染器）：`_fill_full_state(acc, svc, tid)` 目前把状态折叠进 20 元组——**只需把它改成 `status_snapshot(task_id) -> dict`** 一份 JSON（保留 HTML fragment 字段供渐进式首启使用）；
4. **HTTP 辅助端点**：`/api/logs`（现 `/gui/logs`）、`/api/preview`（现 `/pdf-preview/`）—— 保留语义即可；
5. **业务服务**：`RuntimeService` 任务生命周期接口（提交/取消/暂停/重试/拉取状态）—— 目前部分经由 `gr.State` 与 form 参数传递，需改造成 REST 形式。

> 核心判断：**当前架构的"事件→状态→视图"三层中，视图如何呈现（Gradio DOM 还是 Vue DOM）是最后 1/3 的工作量**。此前已将前置的"谁负责生成状态"（服务端）与"谁负责渲染"（浏览器）之争固定下来：状态生成在服务端（100% 保留），浏览器只需消费 JSON/HTML-fragment。

### 3.2 需要重写的部分（一次成本）

- **前端 SPA 壳**：布局（头部/主流程/侧栏）、路由、状态管理（Vuex/Pinia 或 Zustand）、组件化；
- **面板组件**（5 个面板重写）：
  - 上传面板（文件拖拽/URL/进度/类型校验）；
  - 配置面板（21 个参数的表单：翻译服务、语言对、页码范围、线程数、字体选项、模式切换、API Key，等——**现状同时处理"首次渲染值"与"用户改动写入 localStorage"，重写时保持**）；
  - 进度面板（进度条/StepBar/状态徽标/按钮组/日志终端）；
  - 诊断面板（质量分数、诊断报告 markdown、修复状态）；
  - 预览面板（`iframe` PDF 预览 + 结果选择器 + 下载按钮组）；
3. **服务端瘦身**：去掉 20 元组编排、`sync-trigger` 隐藏按钮唤醒机制、`gr.State` 游标；改为：
   - `GET /api/tasks/:id/status`（快照）
   - `GET /api/tasks/:id/events`（SSE，已存在可改名）
   - `POST /api/tasks`（上传+参数，返回 task_id）
   - `POST /api/tasks/:id/cancel|pause|resume|skip|retry`
   - `GET /api/tasks/:id/download?format=...|zip`
   - `GET /api/config|GET/POST /api/settings`（服务端配置面板）
4. **鉴权/文件服务**：从 `gui.launch(auth=[..])` 迁移到 FastAPI 中间件（HTTP Basic 或 Session/令牌），`/api/download` 加权限校验（现 `/pdf-preview/` 无鉴权，属地风险）。
5. **前端 E2E**（Playwright）：翻译旅程脚本。

### 3.3 三条技术路线

#### 路线 A：保留 Gradio + 深度定制（成本最低，能力受限）
- 做法：继续利用现有 `app.py`，叠加自定义 CSS/JS/主题、把鉴权换为反代上的企业层、隐藏 Gradio 品牌。
- 优点：几周即可；现有 2061 测试全部保留。
- 缺点：**定制天花板**（Gradio 组件/Svelte 内部报告无法穿透）、前端体系无法引入组件库、多租户/审计/埋点仍受限、包体大；对"商业级"愿景仅治标。

#### 路线 B：双轨渐进替换（推荐）
- 做法：
  1. **M0（1-2 周）**：抽刚需 JSON API（3.1/3.2 中服务端部分），抽象统一"状态导出"，与现有 20 元组并跑（加 `?ui=spa` 开关先灰度）；
  2. **M1（3-4 周）**：新建 SPA（Vue3+Element Plus **或** React+AntD），`STATIC` 由同一 uvicorn 托管，同域否则无 CORS 问题；先做只读视图（状态/日志/预览）；
  3. **M2（4-6 周）**：操作闭环（上传/配置/控制/下载）在 SPA 完成，与 Gradio 并行救火期；
  4. **M3（2-3 周）**：默认路由切 SPA，`--legacy-gui` 保留 Gradio 旧 UI；后续下掉。
- 优点：风险逐段控制；任何节点可回退；新前端完全复用已生产的测试；
- 缺点：并行维护两个 UI 一段窗口（约 1-2 个月）。

#### 路线 C：一步到位自研 SPA（丝滑但高风险）
- 做法：直接重写前端 + 后端 API 化，一次性下线 Gradio。
- 优点：无并行期，代码库最干净；
- 缺点：窗口期内**所有前端回归都要自己背**（2061 测试中 UI 相关、依赖现有 UI 手势的用例量放大），且完全依赖"API 契约冻结"执行力，风险集中在团队成熟度。

### 3.4 工时粗估（前端熟练度 1 人估算）

| 阶段 | 内容 | 人-周 |
|------|------|------|
| M0 | 现网 API 契约冻结 / 状态快照 JSON 化 | 2 |
| M1 | SPA 骨架 + 上传/进度/日志/预览只读链路 | 3 |
| M2 | 配置表单 + 操作闭环 + 下载/zip | 4 |
| M3 | 鉴权(OAuth2/JWT) + 权限细化 + 主题品牌化 | 2 |
| M4 | E2E + 监控与埋点 + 文档 | 2 |
| 合计 | | **约 13 人-周（不含后端 API 部分 ≈3 人-周）** |

> 注：如选依赖较少（Alpine.js + 原生组件）则 M1-M2 可压缩到 4-5 周，但"商业级组件/表单体系"输出较弱，适合对品牌要求中等、快速上线的场景。

### 3.5 替代框架选型对比

| 框架 | 生态/组件库 | 学习曲线 | 与 Python 团队契合 | 包体积 | 适用性 |
|------|------------|---------|------------------|--------|--------|
| Vue3 + Element Plus | 中文生态最大，组件丰富 | 低 | 高（vs-code 提示成熟） | 中 | **首选**（L10N/表单重场景） |
| React + Ant Design | 国际生态最大 | 中 | 中 | 中 | 商务/企业集成场景多 |
| Svelte + shadcn-svelte | 轻、新 | 低 | 低 | 极小 | 原型快，生态较小 |
| Alpine.js/htmx + 轻 | 无重型组件库 | 极低 | — | 最小 | 非商业级 UI（不推荐为目标形态） |

### 3.6 与现有架构的关系（业务不变式）

- **事件驱动模型不变**：SSE 全量帧 + 断线重放（Phase 0 成果）直接内嵌新前端；浏览器唤醒改为做客监听（Vue `useEventSource` 或原生 `EventSource`），不再需要"隐藏按钮"技巧——此"技巧"正是依赖 Gradio 的特性之一，替换后反而更简洁；
- **任务状态机不变**：`RuntimeService`/`TaskState` 全部复用；
- **测试资产不变**：现有 pytest（2061 例）全为服务端/单元级，可继续守护；
- **入口交锋点**：Gradio-Free 后 `spawn` 修复（F10）继续生效，无需改。

---

## 四、目标架构（B 路线终态）

```
浏览器 SPA (Vue3 + Element Plus / 主题 CSS 变量)
   │  REST API                           EventSource
   ▼                                        │ (frames: id+JSON)
FastAPI 应用（单进程，可多 worker）
   ├─ /api/tasks     上传/新建/控制（状态推导）
   ├─ /api/events    SSE（现有 EVENT_NOTIFIER 直接复用）
   ├─ /api/logs      日志环形缓冲 JSON
   ├─ /api/preview   PDF 预览（鉴权改造）
   ├─ /api/download  结果下载（体积/鉴权）
   └─ /static       SPA 产物（dist）
        │
        ▼
   RuntimeService + Worker（多线程/多进程，含 GPU/CPU 回退）
   EventBus（纯 Python，全局唯一事件源）
```

关键差异点（相对现状）：

| 项 | 现状 | 目标 |
|----|------|------|
| 状态传递 | 20 元组 `gr.update()` + `gr.State` 游标 | JSON 快照 + 事件增量（浏览器纯状态管理） |
| 唤醒机制 | 隐藏按钮 `#sync-trigger`.click() | 浏览器直接订阅 SSE 并 dispatch 到 store |
| 渲染位置 | **服务端**（Python 拼 HTML） | 混合：首批用 HTML fragment（复用现有 `build_*_html`），后续渐进迁移到前端组件 |
| 鉴权 | `launch(auth=[u,p])` | FastAPI 中间件：Basic 兼容 → Session/JWT，可选 SSO |
| 文件访问 | 目录透传 `/pdf-preview/{path}` | 基于 task 授权的下载端点 |
| 启动 | `gui.launch()`(重建 app、路由时序坑) | FastAPI 单应用，路由/中间件自主可控 |

---

## 六、验收标准与里程碑

| 里程碑 | 交付物 | 通过标准 |
|--------|--------|----------|
| M0 | API 契约文档 + `snapshot` 服务端实现 | 2061 测试全绿；快照与 20 元组渲染结果等价（服务端对比） |
| M1 | SPA 只读壳 | Playwright 脚本：上传→看进度→看日志→看预览，全程零 Gradio |
| M2 | 操作闭环 | 与 Gradio AI 实时并行同跑（同一后端），功能对拍；旧 UI 可随时切回 |
| M3 | 商业层 | SSO/JWT 登录、租户目录隔离、主题白标（配置级） |
| M4 | 下线 | 移除 gradio import/app.py 与 components/，依赖裁剪；e2e + 监控报告归档 |

---

## 七、结论与建议

1. **技术上完全可行**：事件总线/SSE/业务层已无 Gradio 依赖，替换只涉及"渲染与编排"一层；
2. **推荐 B 路线**：R 12 周内的双轨替换可消除最佳风险，同时守卫既有 2061 个测试的持续有效性；驳回 A（能力受限）与 C（风险集中）作为主选；
3. **立即开始**：第一支 2 周的 M0 专注把 `snapshot` 接口与契约文档定下来——这是后期所有前端工作的地基，且不改任何运行态行为；
4. **决策征询点**（管理层拍板）：
   - 前端技术栈倾向（Vue3+Element / React+Ant Design / 其他）；
   - 认证级别要求（基础账号密码 → SSO/租户）；
   - 部署形态（内网单机 / 云多租户 / 桌面打包）会显著影响"鉴权与文件服务"的设计。

---

## 附录：关键代码索引

| 说明 | 位置 |
|------|------|
| `gr.Blocks` 构建入口 | `pdf2zh/gui/app.py:1003` |
| 20 件套同步契约（sync_outputs） | `pdf2zh/gui/app.py:1124-1145` |
| 增量渲染 Accumulator | `pdf2zh/gui/app.py:326-348` |
| delta 处理 `drain_events` | `pdf2zh/gui/app.py:895-946` |
| 隐藏唤醒按钮 `#sync-trigger` | `pdf2zh/gui/app.py:1159-1163` |
| 全量状态渲染 `_fill_full_state` | `pdf2zh/gui/app.py:743-867` |
| SSE 端点（Starlette） | `pdf2zh/gui/notifier.py:127-187` |
| 浏览器 EventSource JS | `pdf2zh/gui/styles.py:729-762` |
| 面板组件（5 文件） | `pdf2zh/gui/components/*.py` |
| Gradio 依赖声明 | `pyproject.toml:29` |
| CLI 入口 `setup_gui` | `pdf2zh/gui/entry.py:38-94`；`pdf2zh/pdf2zh.py:332-346` |