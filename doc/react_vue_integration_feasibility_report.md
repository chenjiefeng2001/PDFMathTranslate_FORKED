# React / Vue 最新版本接入可行性报告

> 日期：2026-08-22 ｜ 分支：main ｜ 前置状态：Phase A 解耦层已落地（FastAPI REST+SSE、locale JSON、Design Tokens CSS/JSON）
> 本机工具链实测：node v22.14.0（满足 Vite 8 要求的 20.19+/22.12+）、npm 11.1.0、bun 1.3.14；npm 源可达

## 〇、本机实证结论（PoC 已跑通）

在临时目录分别以 Vite 8.2.2 真实构建两个最小应用（均直接 import Phase A 导出的
`locales/zh-CN.json` 并渲染文案），**双双成功**：

| PoC | 依赖 | 构建耗时 | 产物（gzip） |
|---|---|---|---|
| React | react@19.2.8 + @vitejs/plugin-react | **140ms** | 198.2KB（**63.8KB**） |
| Vue | vue@3.5.41 + @vitejs/plugin-vue | **200ms** | 67.4KB（**27.1KB**） |

验证点：
- Vite 8（Rolldown）在本机 Windows/Node 22.14 工作正常；
- Phase A locale 资产可被两侧直接打包消费（TS 类型化 JSON import）；
- `vite.config.ts` 中 `/api → http://127.0.0.1:11009` 代理配置即 SPA 开发联调形态
  （与 Phase A 的 `pdf2zh --api` 直接对接）。

> 结论：两个框架的接入在本机已从"理论可行"升级为"实证可行"，差异仅在包体基线
> （纯运行时 gzip：React ≈64KB vs Vue ≈27KB，引入组件库后差距收窄）。


## 一、目标框架版本核实（2026-08 实测）

| 框架 | 最新稳定版 | 发布时间 | 备注 |
|---|---|---|---|
| React | **19.2.8** | 2026-07-21 | 19.x 稳定线（19.0/19.1/19.2 并行维护）；React Compiler 已随 19.x 成熟 |
| Vue | **3.5.41** | 2026-08-05 | 当前稳定线；**3.6 已进入 RC**（Vapor Mode + alien-signals 响应式重写），尚未稳定 |
| 构建工具 | Vite **8.2.2** | 2026-04 起 | Rolldown 全面替代 ESBuild/Rollup，构建提速约 10–30×；Node 20.19+/22.12+ |
| PDF 预览 | pdfjs-dist **6.2.108** | — | 替代现有自定义 `/pdf-preview` iframe 方案 |

> 版本策略建议：Vue 生产钉 `^3.5.41`，不追 3.6 RC；React 直接 `19.2.x`。

## 二、与本项目 Phase A 资产的对接面（两框架完全等价）

Phase A 产出的三项资产均为框架无关形态，React 与 Vue 的消费方式对称：

| Phase A 资产 | React 消费方式 | Vue 消费方式 |
|---|---|---|
| SSE `/api/tasks/{id}/events`（state/progress/notice/done 帧） | EventSource → zustand store → 组件订阅 | EventSource → Pinia store → 响应式订阅 |
| `locales/{zh-CN,en}.json`（130 UI + 15 stage key） | react-i18next / 自研 30 行 hook | vue-i18n v11 |
| `tokens/*.css`（52 token × 亮暗双套） | 直接引入 CSS 变量，组件库主题对齐 | 同左 |

> 关键结论：**接入面无任何框架倾向性**——Phase A 的解耦投资对两个候选一视同仁，
> 选型可以完全基于框架自身维度决定。

## 三、逐维度对比（按本项目的应用画像加权）

本项目前端本质是「任务状态仪表盘」：表单（上传+引擎配置）×实时进度渲染×诊断报告×PDF 预览，
无复杂路由、无 SEO 需求、纯内网工具页。据此对各维度加权：

### 3.1 状态模型与 SSE 契合度（权重高）

| 维度 | React 19.2 | Vue 3.5 |
|---|---|---|
| SSE→UI 数据流 | store 订阅 + `useSyncExternalStore`/zustand，样板适中 | Pinia store 天然响应式，SSE 回调改 ref 即全链路更新，**样板最少** |
| 与 `_SYNC_COMPONENTS`（20 切片增量渲染）映射 | 切片→store selector，组件按 selector 订阅 | 切片→store state，模板自动细粒度依赖追踪 |
| 结论 | ✅ | ✅✅（响应式模型与"事件→状态→局部重渲"契约更贴合） |

### 3.2 表单与组件生态（权重高）

| 维度 | React 19.2 | Vue 3.5 |
|---|---|---|
| Dashboard/管理台组件库 | Ant Design / MUI（成熟、TS 完善） | Element Plus / Naive UI（中文文档强、AntDV 可用） |
| 表单能力（引擎 envs 动态键值、页码范围等） | antd Form / RHF，生态最大 | Element Plus Form，中文场景开箱即用 |
| 结论 | ✅✅（生态广度第一） | ✅✅（中文场景贴合本项目 i18n 现状） |

### 3.3 PDF 预览（权重中）

| 维度 | React 19.2 | Vue 3.5 |
|---|---|---|
| pdfjs-dist 封装 | react-pdf 维护活跃 | vue-pdf-embed 维护活跃 |
| 结论 | ✅✅ | ✅✅（等价；也可两侧都用裸 pdfjs-dist API） |

### 3.4 性能与包体（权重低——内网工具页）

| 维度 | React 19.2 | Vue 3.5 |
|---|---|---|
| 运行时 | Compiler 自动 memoization | 3.5 响应式已优化（alien-signals 重写将在 3.6 落地） |
| gzip 包体（含组件库） | ~110–140KB | ~95–125KB |
| Vite 8 构建 | ✅ 10–30×提速 | ✅ 同左 |
| 结论 | ✅ | ✅（差异在本场景不可感知） |

### 3.5 工程与团队因素（权重中）

| 维度 | React 19.2 | Vue 3.5 |
|---|---|---|
| TS 支持 | 优秀 | 优秀（SFC 泛型组件） |
| AI 辅助开发语料 | 最大 | 大 |
| 招聘面/社区资料 | 最大 | 中文社区最强（与本项目用户群一致） |
| 上游稳定性风险 | 低（19.x 三分支并行补丁） | 低；**若等 3.6 stable 则多一个观察周期** |

## 四、结论

1. **两者均可行且成本等价**：Phase A 资产对接面框架无关，差异仅在框架内部样板量
   （Vue 的 SSE→Pinia→模板链路最短；React 生态广度最大）。
2. **推荐**：
   - 若追求**最快落地与最低样板**：**Vue 3.5.41 + Vite 8 + Pinia + Element Plus**
     ——与项目中文用户群、现有双语字典、Element 系表单习惯最贴合；
   - 若追求**生态上限与长期人才池**：**React 19.2.8 + Vite 8 + zustand + antd**。
   - 两者不存在技术性排除项；决策可由团队栈偏好定夺。
3. **共同注意事项**：
   - Vue 不要采用 3.6 RC（Vapor Mode 未稳定）；React 采用 19.2.x 即为当前最新稳定；
   - Vite dev server 经 `server.proxy` 把 `/api` 转发到 FastAPI :11009（CORS 已放开，双保险）——**已在 PoC 中实测配置形态**；
   - 构建产物由 FastAPI StaticFiles 托管，`?ui=spa` 与 Gradio 双轨灰度（见解耦报告 Phase B）；
   - PoC 踩坑记录：Vite 8 必须有根级 `index.html` 入口（缺省报 UNRESOLVED_ENTRY）。
