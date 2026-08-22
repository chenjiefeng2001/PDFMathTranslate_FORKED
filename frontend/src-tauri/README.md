# Tauri v2 桌面外壳（PoC）

最小 Tauri v2 外壳，演示 `frontend/README.md` 约定的三处接缝在宿主侧的完整接线。
**零插件设计**：sidecar 用 `std::process::Command` 托管（无 tauri-plugin-shell 依赖），
无 IPC 命令、无 capabilities。

## 架构

```
┌─ pdf2zh-desktop (Rust) ──────────────────────────────┐
│ 1. spawn: python -m pdf2zh.pdf2zh --api   (sidecar) │
│ 2. wait_for_api: TCP 探活 :11009（≤30s）            │
│ 3. WebviewWindow(initialization_script=              │
│      window.__PDF2ZH_RUNTIME__={apiBase:...})        │
│    frontendDist = ../dist （SPA 静态产物）           │
│ 4. RunEvent::Exit → kill sidecar                     │
└──────────────────────────────────────────────────────┘
```

## 运行

```bash
cd frontend
npm run build                 # 先产出 ../dist
cd src-tauri
cargo run                     # PDF2ZH_PYTHON 指向装有 pdf2zh 的解释器（默认 "python"）
```

环境变量：
- `PDF2ZH_API_PORT`（默认 11009）
- `PDF2ZH_PYTHON`（默认 `python`；分发打包时应指向捆绑的 sidecar 解释器）

## 分发路径

1. ✅ sidecar 固化：`deploy/pdf2zh-api-sidecar.spec`（PyInstaller onefile），
   产物放入 `src-tauri/binaries/`，已接线 Tauri 官方 externalBin sidecar 机制；
2. ⬜ `bundle.active=true` 已开启，图标与 WiX/NSIS 安装包制作仍为后续项；
3. ✅ CSP 已收紧（见 tauri.conf.json `security.csp`）。

## 编译验证与网络环境

本机已通过代理完成编译与运行验证（health 探活、真实翻译任务 E2E）。
开发时若 crates.io 直连不稳定，请用环境变量配置代理
（cargo 原生支持 `HTTPS_PROXY`/`https_proxy`；勿在 `.cargo/config.toml`
硬编码个人网络配置），或启用其中的 rsproxy 镜像注释段：

```bash
cargo check && cargo run
```
