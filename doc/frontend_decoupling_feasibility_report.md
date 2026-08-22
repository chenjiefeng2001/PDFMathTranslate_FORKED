# 前端解耦与现代框架引入可行性报告

> 日期：2026-08-21 ｜ 分支：main（基于 1eac5e5）｜ 调查范围：pdf2zh/gui、pdf2zh/services、pdf2zh/backend.py、pdf2zh/mcp_server.py、pdf2zh/v3/remote_runtime.py

> **✅ Phase A 已实施落地（2026-08-22）**，实施记录见文末「五、Phase A 实施记录」。
> **✅ Phase B 里程碑一已实施落地（2026-08-22）**：React SPA（生态上限路线 + Tauri v2
> 就绪性接缝）+ FastAPI 静态托管，见文末「六、Phase B 实施记录」。

## 一、前端实现现状检查

### 1.1 两套 GUI 并存，旧版已是被遮蔽的死代码

| 形态 | 位置 | 规模 | 状态 |
|---|---|---|---|
| 新版模块化 GUI | `pdf2zh/gui/` 包（11 个模块 + 5 个组件） | 约 **4660 行** | ✅ 生产入口（CLI `--interactive` 与 Dockerfile 均走此入口） |
| 旧版单体 GUI | `pdf2zh/gui.py` | 872 行 | ⚠️ **死代码**：Python 导入时包优先于同名模块遮蔽之，仓库内无生产引用 |

> 结论一：所谓"两套前端并存"实际只有一套在运行；旧 gui.py 可安全删除或归档（连带可移除
> `gradio_pdf` 依赖——仅它使用该组件）。

### 1.2 新 GUI 架构（关键事实）

```
RuntimeService(后台线程) --_emit_event--> TaskEventBridge(gui/event_bridge.py)
    → 领域事件 EventBus(gui/events.py) → EventNotifier(gui/notifier.py)
    → SSE GET /gui/events（Last-Event-ID 断线重放 + 25s keep-alive）
    → 浏览器 EventSource(styles.py 内联 JS) → 点击隐藏同步按钮
    → 按会话游标增量拉取事件 → 仅重渲染受影响组件（_SYNC_COMPONENTS，20 个）
```

- 后端从不轮询；兜底仅在「有活动任务且 SSE 断开」时浏览器端 5s 低频轮询。
- 控制/下载按钮 `queue=False` 直连；终态结果写 localStorage 跨会话恢复。
- `gui/events.py` 文档明确声明该协议**可在不改动 worker 的前提下支撑 React/Vue 前端**。

### 1.3 技术栈锁定现状

| 项 | 现状 |
|---|---|
| UI 框架 | Gradio `>=5.20,<5.36`（版本窗口窄，受上游 bug 牵制） |
| gradio 直接 import | 仅 6 个文件（app.py + 5 个 components），其余模块零 Gradio 依赖 |
| 设计系统 | 自建 750 行 styles.py：亮/暗 Design Tokens、CSS 变量注入、内联主题切换 JS、状态徽章 |
| i18n | 静态双语字典 130 key + 15 个阶段文案；无 locale 切换机制 |
| 静态资源 | 主包内无任何图片/CSS/JS/HTML 文件（全为 Python 字符串） |
| PDF 预览 | 自定义 `/pdf-preview/{path}` 路由 + iframe（非 gradio_pdf 组件） |

### 1.4 后端服务表面盘点（解耦的另一半）

| 服务 | 传输 | 状态 |
|---|---|---|
| `RuntimeService`（services/runtime_service.py） | **纯 Python 进程内 API**（submit/cancel/pause/resume/skip/get_state/list/subscribe_events/add_event_listener） | ✅ 生产核心；内存存储、无持久化 |
| Flask `backend.py` | HTTP `/v1/*`（Celery+Redis extras）、`/v2/*`（RuntimeService） | ⚠️ v2 有两处缺陷：每请求新建 RuntimeService 实例（状态跨请求不可见）；artifacts 端点缺 `import os` 会 NameError |
| MCP `mcp_server.py` | STDIO/SSE | ✅ 已参数化 engine（本系列改动） |
| `v3/remote_runtime.py` | stdlib REST（V7 DocumentRuntime） | ⚠️ 实验性，无生产接线 |

> 结论二：**不存在独立的前后端 HTTP 边界**——GUI 与翻译内核是进程内函数调用；
> 但事件协议层已经是客户端无关的。

## 二、解耦可行性报告

### 2.1 有利条件（解耦成本低的核心原因）

1. **事件协议天然客户端无关**：类型化领域事件 + 全局单调 sequence + Last-Event-ID 重放 +
   keep-alive，正是 SSE 标准形态，SPA 可原样消费（events.py 作者已注明 React/Vue 兼容）。
2. **状态切片契约现成**：`_SYNC_COMPONENTS`（20 个组件的更新载荷）可直接映射为 SPA
   的状态切片（store slice），语义无需重新设计。
3. **RuntimeService 是干净的领域服务**：不依赖任何 Web 框架；加一层薄壳 API 即可。
4. **技术先例齐备**：notifier.py 已用 starlette StreamingResponse 实现 SSE；
   mcp_server.py 有独立的 starlette+uvicorn 组合；fastapi/uvicorn 已是传递依赖。
5. **i18n 字典与 Design Tokens 可直接导出**：130 key 转 JSON locale 文件、
   LIGHT/DARK_TOKENS 本来就是 CSS 变量形态。

### 2.2 需要新建的部分

| 层 | 内容 | 规模估计 |
|---|---|---|
| REST API 层（新模块 `services/api.py`） | FastAPI：POST /tasks、GET /tasks/{id}、DELETE、pause/resume、GET /tasks/{id}/events(SSE)、文件上传/下载、GET /engines（引擎列表+envs schema） | 约 600–900 行 |
| 任务持久化（可选但强烈建议） | SQLite（peewee 已是依赖）/Redis 存任务表+事件流；替代内存 `_TaskStore` | 约 300–500 行 |
| 静态托管 | FastAPI StaticFiles 托管 SPA 产物；`/pdf-preview`、日志 API 迁入 | 约 100–200 行 |
| backend.py 处置 | 修复两处缺陷或直接以新 API 取代（推荐取代，v1 Celery 保留为异步扩展模式） | −216 行 |

### 2.3 工作量与风险

**总规模约 1000–1600 行新增代码**，其中大部分是把现有能力（RuntimeService 方法、
sse_stream、preview 路由）重新暴露为 HTTP，而非重写逻辑。

| 风险 | 影响 | 缓解 |
|---|---|---|
| 内存任务存储：重启丢任务、多实例不可见 | 中 | 持久化层用 peewee/SQLite（已是依赖）；单机部署可暂缓 |
| 大文件上传/下载 | 中 | 流式上传（FastAPI UploadFile）+ FileResponse，backend.py 已有雏形可参考 |
| Gradio 与新 API 并存时的端口/路由冲突 | 低 | 新 API 独立端口；或挂载至同一 ASGI（entry.py 已有 launch 后注册路由先例） |
| 引擎 envs 的敏感信息经 API 泄露 | 中 | 沿用 GUI 掩码策略（config.json `***` 回填），API 不回显密钥明文 |

**结论：解耦完全可行，且是低风险渐进工程**——事件协议与领域服务已就位，
缺的只是一层标准化的 HTTP 皮。

## 三、现代框架引入可行性报告

### 3.1 前置判断：是否需要换？

Gradio 当前痛点：版本窗口窄（5.36 白屏/5.19 jsonschema 问题被迫钉住）、定制靠 CSS/JS
注入（styles.py 750 行即为对抗性补丁）、组件布局自由度低、复杂交互（增量同步按钮 hack）
本质是在绕开框架。若产品目标是"够用的工具页"，维持 Gradio 成本最低；
若是"可长期演进的产品级前端"，引入现代框架收益明确。

### 3.2 候选对比

| 方案 | 优势 | 劣势 | 契合度 |
|---|---|---|---|
| **React 19 + Vite + TS** | 生态最大（PDF.js/react-window/表格库现成）、TS 类型化事件协议契合、AI 辅助开发成熟 | 包体较大、学习曲线中等 | ★★★★★ 推荐 |
| Vue 3 + Vite | 上手快、单文件组件直观、中文社区强 | 生态略小于 React | ★★★★ |
| Svelte 5 | 包体最小、运行时开销低 | 生态小、团队招聘面窄 | ★★★ |
| htmx + Alpine.js | 无构建链、SSE 一等公民、改动最小 | 复杂状态管理弱、组件化差 | ★★（保守备选） |

> 关键匹配点：本项目前端本质是「任务状态仪表盘」——大量实时状态渲染 +
> 少量表单 + PDF 预览。React/Vue 的响应式状态模型与现有
> `_SYNC_COMPONENTS`→状态切片的映射是一对一的。

### 3.3 推荐路线：两阶段渐进式（不停服）

**Phase A —— 解耦先行（1–2 天量级）**
- 新建 FastAPI 服务层（REST + SSE），Gradio 作为其中一个客户端继续运行
- i18n 字典导出为 JSON locale；Design Tokens 导出为 CSS 变量表
- 修复 backend.py 或将其标记废弃

**Phase B —— SPA 并行（2–4 周量级）**
- React + Vite + TS 脚手架，页面拆分：上传/配置/进度监控/诊断/预览（对应现有
  5 个 panel，约 40–50 个组件）
- PDF 预览用 pdfjs-dist 替代自定义 iframe 路由（顺带移除 `gradio_pdf` 依赖）
- 同源部署：FastAPI StaticFiles 托管 `frontend/dist`；`?ui=spa` 参数或子域灰度
- Gradio 保留为回退入口直至 SPA 功能对齐后移除

**Phase C —— 收尾**
- 删除 gui/ 包与旧 gui.py（−5500 行）、gradio/gradio_pdf 依赖出主依赖组
- MCP/backend/API 三通道共享同一 RuntimeService

### 3.4 风险与缓解

| 风险 | 缓解 |
|---|---|
| SPA 与 Gradio 双前端的维护期成本 | Phase B 以功能对齐清单验收（20 个状态切片逐项对照），限期收敛 |
| 事件协议在浏览器端的边界场景（重放窗口过期） | events.py 已定义全量同步兜底（sequence 窗口丢失即 full sync），照抄即可 |
| Windows 下 Node 工具链引入 CI 复杂度 | Vite 构建产物入库或 CI 缓存；后端不依赖 Node 运行时 |

## 四、总结论

1. **现状**：前端实际只有一套 Gradio 应用在生产运行（旧版为死代码）；架构已是
   事件驱动，且事件协议明确声明了 React/Vue 兼容——解耦的最大前置投资早已完成。
2. **解耦**：可行、低风险、约 1000–1600 行增量，核心是给 RuntimeService 加一层
   FastAPI（REST+SSE）与可选持久化；同时处置 backend.py 的两处既有缺陷。
3. **现代框架**：可行且推荐 React+Vite+TS 渐进式双轨迁移；i18n/Tokens/事件协议
   三大资产均可平移，预计 2–4 周达成功能对齐，最终可删除约 5500 行 Gradio 代码并
   放开版本钉。
4. **建议决策**：若近期有"远程部署/多人使用/嵌入第三方"任一需求，立即启动 Phase A；
   否则 Phase A 仍值得做（成本一天以内），Phase B 视产品路线再定。

## 五、Phase A 实施记录（已完成）

### 5.1 交付物

| 文件 | 内容 |
|---|---|
| `pdf2zh/services/api.py` | **FastAPI REST+SSE 服务层**：`create_api_app()`，10 个端点——health、engines（envs 只回显 `configured` 布尔，绝不回显值）、任务提交（multipart 上传或 source_path）、列表/状态/取消/pause/resume/skip、**SSE `/api/tasks/{id}/events`**（初始 state 帧 + progress/notice 帧 + done 终态帧 + keep-alive）、产物清单/下载 |
| `pdf2zh/services/runtime_singleton.py` | 进程级共享 RuntimeService 单例（GUI/API/Flask 三端同源） |
| `pdf2zh/gui/export_assets.py` | 资产导出器（`python -m pdf2zh.gui.export_assets`）：i18n 130 key + 15 stage → `locales/{zh-CN,en}.json`；LIGHT/DARK_TOKENS → `tokens/{light,dark}.css` + `tokens.json`（已生成于 `pdf2zh/gui/assets/generated/`） |
| `pdf2zh/pdf2zh.py` | CLI `--api` 入口（uvicorn :11009，CORS 默认放开供 Vite 开发联调） |
| `pdf2zh/backend.py` | 修复两处既有缺陷：v2 端点改用共享单例（原每请求新建实例致状态不可见）；补 `import os` |
| `pdf2zh/translator.py` | 抽取 `build_translator_registry()` 作为引擎注册的单一事实来源（工厂/API 共用） |
| `pdf2zh/gui/worker.py` | GUI 单例委托共享实例（同进程共存时任务互通） |

### 5.2 测试结果

- **单元测试**：`tests/test_services_api.py` 新增 9 个（健康检查、引擎注册表与密钥掩码约束、表单字段映射与线程钳制、上传落盘、坏 JSON 拒绝、404 语义、空路径快速失败、SSE 初始帧+终态帧、单例同一性）
- **真实冒烟**：后台启动 `pdf2zh --api` → health OK / engines=25 含 opencode → POST 提交 → GET 状态（failed 快速失败可见）→ SSE 收到完整 `event: state` 帧与 `event: done` 帧
- **回归**：相关套件 154 passed；ruff 关键规则改动行零新增问题（backend.py 仅剩既有的 F811 重复定义）

### 5.3 与报告第二节对照

| 计划项 | 状态 |
|---|---|
| REST API 层（600–900 行） | ✅ 约 380 行实现全部端点（复用 sse_stream 先例与 RuntimeService 方法） |
| 任务持久化（可选） | ⏸ 未实施（内存存储维持现状；peewee/SQLite 方案留待多实例需求出现时） |
| 静态托管 SPA | ⏸ 属 Phase B（API 已具备 StaticFiles 接入点） |
| backend.py 处置 | ✅ 采用修复方案（与新 API 并存；v1 Celery 保留） |

### 5.4 Phase B 启动条件已就绪

SPA 所需的三项资产均已产出并可消费：
1. **事件协议**：SSE 端点与 Gradio 内部 `/gui/events` 同构（state/progress/done 帧 + Last-Event-ID 兼容的重连策略可后续加）
2. **locale 文件**：`assets/generated/locales/*.json`
3. **设计令牌**：`assets/generated/tokens/*.css|.json`

## 六、Phase B 实施记录（里程碑一已完成，2026-08-22）

按「生态上限」路线落地 React SPA，并为后续 Tauri v2 打包预留健壮性接缝。

### 6.1 技术栈（版本均为 2026-08 实测最新稳定）

| 层 | 选型 | 版本 |
|---|---|---|
| UI 框架 | React + antd（生态上限） | react 19.2.8 / antd 6.6.1 |
| 状态 | zustand（SSE→store→切片订阅） | 5.0.15 |
| i18n | react-i18next，直接消费 Phase A locale JSON | 17.0.12 / i18next 26.4.0 |
| 构建 | Vite 8（Rolldown）+ TS 5.9 严格模式 | 8.2.2 |

### 6.2 新增目录 `frontend/`（约 900 行 TS/TSX）

```
src/api/client.ts        ★ 传输抽象 ApiTransport + registerApiTransport() 注入点
                         ★ API 地址解析链：window.__PDF2ZH_RUNTIME__.apiBase（宿主注入）
                           → localStorage 覆盖 → VITE_API_BASE → 同源相对路径
src/api/types.ts         与 Python 载荷一一对应的类型
src/api/endpoints.ts     类型化端点封装
src/stores/taskStore.ts  SSE 帧 → zustand 切片；done 后全量回拉 result_files
src/i18n/index.ts        locale 加载/切换（localStorage 持久化）
src/theme/AppShell.tsx   antd 亮暗主题；tokens.json 品牌色喂 ConfigProvider
src/pages/Dashboard.tsx  上传/引擎配置/SSE 进度条/pause·resume·skip·cancel/产物下载
vite.config.ts           base:"./"（任意 origin 加载）+ dev proxy /api → :11009
```

### 6.3 Tauri v2 就绪性（三处接缝，迁移零业务改动）

1. **传输接缝**：所有网络访问经 `ApiTransport` 接口；未来实现
   `TauriHttpTransport`（tauri-plugin-http/event）后 `registerApiTransport()` 注入；
2. **地址注入链**：Tauri 外壳在 webview 初始化前写
   `window.__PDF2ZH_RUNTIME__ = { apiBase: "http://127.0.0.1:<sidecar端口>" }` 即完成接线；
3. **相对资源路径**：`base: "./"` 使 dist 可从 `tauri://localhost`、`file://`、任意子路径加载。

### 6.4 验证结果

- `npm run build`（tsc --noEmit + vite build）：**453ms 通过**；产物 JS 831KB（gzip 270KB，含 antd 全量）/ CSS 1.4KB
- FastAPI 静态托管：`PDF2ZH_SPA_DIR=frontend/dist pdf2zh --api` 后 `/` 返回 SPA，
  `/api/*` 同源共存，资产 200——**真实冒烟 SMOKE_OK**
- 回归：API+SPA+翻译器相关套件 **157 passed**；ruff 零新增

### 6.5 里程碑进展（2026-08-22 第二轮落地）

| 项 | 状态 | 说明 |
|---|---|---|
| **SSE Last-Event-ID 断线续传** | ✅ 已完成 | 服务端重构为游标轮询泵：帧携带 `id: <绝对序号>`，浏览器 EventSource 重连自动回传 Last-Event-ID → 从 `get_task_events(since=)` 重放缺失帧；RuntimeService 新增公共 `get_task_events()`；`retry: 3000` 重连提示；终态任务重连亦可补帧。专项测试验证「已消费帧不重放、缺失帧补齐」 |
| **pdfjs-dist 内嵌预览** | ✅ 已完成 | `PdfPreview.tsx`：pdfjs-dist 6.2.108 + Vite `?url` worker 导入（产物自包含，Tauri 离线友好）；翻页控件；渲染失败降级提示 |
| **SPA 功能对齐（部分）** | ✅ 首批完成 | 任务历史列表（15s 轮询 + 点击切换活动任务）、批量文件进度（completed/total/failed）、诊断卡片（diagnostic_summary + quality_scores） |
| **端到端真实冒烟** | ✅ SMOKE_OK | 上传样张 PDF（google 引擎）→ SSE 收 17 帧（末帧 id=7）→ 以 Last-Event-ID=7 重连仅得 state+done → 任务 completed 且 mono/dual 产物齐全 → SPA 同源托管可达 |
| 功能对齐剩余 | ✅ **已完成** | `DiagnosticsPanel` 七分区全量对齐 Gradio 端：Diagnostic Report / Self-Heal / Repair Records / Confidence / **Gate Verdicts / Processor Reports / TOC IR**（pageid 键控折叠 JSON） |
| **Tauri 外壳 PoC** | ✅ **已完成并编译/运行验证** | `frontend/src-tauri/`：零插件设计（sidecar 托管、TCP 健康等待 ≤30s、`initialization_script` 注入 `__PDF2ZH_RUNTIME__`、Exit 时回收子进程）；tauri 2.11.5 全树编译通过；网络经验：schannel 直连 crates.io 握手被重置，**项目本地 `.cargo/config.toml` 配置 `[http] proxy = "http://127.0.0.1:7890"` 后解决** |
| **分发固化（sidecar）** | ✅ **已完成并端到端验证** | `deploy/pdf2zh-api-sidecar.spec`：PyInstaller onefile（~2.2GB，含 onnxruntime/pymupdf）；冻结版实测 health ok、25 引擎、真实翻译任务 completed 双产物；Tauri `externalBin` 已接线——外壳启动即托管捆绑 sidecar（target/debug 自动复制），`PDF2ZH_PYTHON` 保留为开发后备。安装包（WiX/NSIS）仍为后续项 |
| **CSP 收紧** | ✅ 已完成 | tauri.conf.json 启用白名单 CSP（self + 127.0.0.1:* connect-src；style 允许 inline 供 antd cssinjs） |
| **CI 前端工作流** | ✅ 新增 | `.github/workflows/frontend-build.yml`：node22 + npm ci + typecheck + build + dist artifact |
| 双轨收敛 | ⏳ | 灰度对照期后移除 gui/ 包 |

回归：API+SPA+翻译器相关套件 **158 passed**。

### 6.6 全链路有效性验证（7 链路集成，2026-08-22）

| 链路 | 内容 | 结果 |
|---|---|---|
| C1 | legacy translate_stream × opencode（CLI 模式，真实翻译） | PASS |
| C2 | BabelDOC YADT 桥 × opencode（真实调用返回中文译文） | PASS |
| C3 | REST API 全生命周期（上传→SSE 游标帧 id=7→completed→双产物） | PASS |
| C4 | MCP stdio 客户端（list_tools 四工具 + get_document_diagnostics 调用） | PASS |
| C5 | SPA 托管（index/资产 200 + /api 同源） | PASS |
| C6 | Gradio GUI Blocks 构建（旧前端不受影响） | PASS |
| C7 | Tauri 外壳 sidecar（进程存活 + API 就绪 + 关闭回收） | PASS |

> 依赖备注：安装 mcp extras 时 pydantic 被抬升到 2.13 与 gradio(<2.12)/magic-pdf(<2.11)
> 冲突；已回退 `pydantic==2.10.6` 并将 mcp 钉在 `<1.10`（移除 mcp-types 依赖），
> 三方约束同时满足。
