# pdf2zh SPA 前端（React 19 + Vite 8 + zustand + antd）

生态上限路线的 Phase B 前端。消费 Phase A 解耦层（`pdf2zh --api`，:11009）。

## 开发

```bash
cd frontend
npm install
npm run dev          # http://localhost:5173（/api 已代理到 :11009）
```

先启动后端 API：

```bash
pdf2zh --api         # 或 python -m pdf2zh.services.api
```

## 构建

```bash
npm run build        # tsc --noEmit + vite build → dist/
```

产物由 FastAPI 托管：设置 `PDF2ZH_SPA_DIR=frontend/dist` 后启动
`pdf2zh --api`，浏览器打开 `http://127.0.0.1:11009/` 即 SPA。

## Tauri v2 就绪性设计

前端为未来 Tauri 打包预留了三处接缝，业务代码零改动即可迁移：

1. **传输层接缝**（`src/api/client.ts`）：所有网络访问经 `ApiTransport`
   接口。Tauri 外壳可实现 `TauriHttpTransport`（tauri-plugin-http +
   tauri-plugin-event）并调用 `registerApiTransport()` 注入。
2. **API 地址解析链**（优先级从高到低）：
   1. `window.__PDF2ZH_RUNTIME__.apiBase` —— 宿主初始化脚本注入
      （Tauri 场景指向 sidecar Python 进程端口）
   2. `localStorage["pdf2zh.apiBase"]` —— 用户调试覆盖
   3. `import.meta.env.VITE_API_BASE` —— 构建期配置
   4. 同源相对路径（FastAPI StaticFiles 托管的默认形态）
3. **相对资源路径**：`vite.config.ts` 的 `base: "./"`，dist 可从
   `tauri://localhost`、`file://` 或任意子路径加载。

### 未来 Tauri 外壳的最小接线示意

```
tauri-app/
  src-tauri/            # Rust 壳；sidecar 运行 pdf2zh api server
                        # （python -m pdf2zh.services.api --port 11009）
  web/                  # 本目录 dist/ 静态产物
```

Rust 侧在 webview 初始化前执行：
`window.__PDF2ZH_RUNTIME__ = { apiBase: "http://127.0.0.1:11009" }`。

## 资产共享

- 文案：直接 import `../pdf2zh/gui/assets/generated/locales/*.json`
  （与 Gradio 端同源，杜绝漂移）；
- 设计令牌：`tokens.json` 品牌色喂给 antd 主题；`tokens.css` 已复制至
  `src/theme/tokens.css` 入包。

## 目录结构

```
src/api/client.ts     传输抽象（REST+SSE）+ 地址解析链 + Tauri 注入点
src/api/types.ts      与 Python 载荷一一对应的类型
src/api/endpoints.ts  类型化端点封装
src/stores/taskStore.ts   SSE 帧 → zustand 状态切片
src/i18n/index.ts     locale 加载与切换（localStorage 持久化）
src/theme/AppShell.tsx    antd 主题（亮/暗）+ 品牌 token 对齐
src/pages/Dashboard.tsx   上传/配置/进度/控制/结果下载
```
