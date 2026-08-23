#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
//! PDFMathTranslate 桌面外壳（Tauri v2）。
//!
//! 职责（对应 frontend/README.md 的三处接缝）：
//! 1. Sidecar：以子进程托管 REST/SSE 后端（onedir sidecar / 兼容旧单文件 /
//!    开发态 PDF2ZH_PYTHON）；
//! 2. 启动体验：先弹无装饰闪屏，API 就绪后切主窗口——冷启动（spawn +
//!    Python 导入 ~3.6s）不再表现为"点了没反应"；
//! 3. 注入：webview initialization_script 在页面脚本执行前写入
//!    `window.__PDF2ZH_RUNTIME__.apiBase` —— 前端业务代码零改动。
//!
//! 环境变量：
//! - PDF2ZH_API_PORT（默认 11009）
//! - PDF2ZH_PYTHON   （默认 "python"；打包分发时应指向捆绑解释器/sidecar 可执行文件）

use std::process::{Child, Command};
use std::sync::Mutex;
use std::time::Duration;

use tauri::Manager;

fn api_port() -> u16 {
    std::env::var("PDF2ZH_API_PORT")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(11009)
}

/// TCP 探活：端口可连接即视为 uvicorn 已就绪（REST 层启动极快）。
fn wait_for_api(port: u16, timeout_secs: u64) -> bool {
    use std::net::TcpStream;
    let deadline = std::time::Instant::now() + Duration::from_secs(timeout_secs);
    while std::time::Instant::now() < deadline {
        if TcpStream::connect(("127.0.0.1", port)).is_ok() {
            return true;
        }
        std::thread::sleep(Duration::from_millis(300));
    }
    false
}

fn spawn_api_server(port: u16) -> Child {
    // 优先级：
    // 1. 捆绑 sidecar onedir 资源（tauri resources：安装目录下
    //    pdf2zh-api-sidecar/pdf2zh-api-sidecar.exe）—— 生产/分发形态；
    // 2. 兼容旧便携布局（与主程序同目录的 sidecar 单文件）；
    // 3. PDF2ZH_PYTHON 显式解释器 → `python -m pdf2zh.pdf2zh --api` —— 开发后备。
    if let Ok(current_exe) = std::env::current_exe() {
        let exe_dir = current_exe.parent().expect("exe has no parent dir");
        for rel in [
            "pdf2zh-api-sidecar/pdf2zh-api-sidecar.exe",
            "binaries/pdf2zh-api-sidecar/pdf2zh-api-sidecar.exe",
            "pdf2zh-api-sidecar.exe",
            "pdf2zh-api-sidecar-x86_64-pc-windows-msvc.exe",
        ] {
            let candidate = exe_dir.join(rel);
            if candidate.exists() {
                return Command::new(&candidate)
                    .args(["--port", &port.to_string()])
                    .spawn()
                    .unwrap_or_else(|e| panic!("failed to spawn bundled api sidecar: {e}"));
            }
        }
    }
    let python =
        std::env::var("PDF2ZH_PYTHON").unwrap_or_else(|_| "python".to_string());
    Command::new(&python)
        .args(["-m", "pdf2zh.pdf2zh", "--api"])
        .spawn()
        .expect("failed to spawn pdf2zh API server (check PDF2ZH_PYTHON)")
}

/// 托管 API 子进程句柄；RunEvent::Exit 时回收，避免孤儿 python 进程。
struct ServerHandle(Mutex<Option<Child>>);

const SPLASH_LABEL: &str = "splash";
const MAIN_LABEL: &str = "main";

fn kill_server(handle: &tauri::AppHandle) {
    use tauri::Manager;
    if let Some(state) = handle.try_state::<ServerHandle>() {
        if let Ok(mut guard) = state.0.lock() {
            if let Some(mut child) = guard.take() {
                let _ = child.kill();
            }
        }
    }
}

fn main() {
    let port = api_port();
    let server = spawn_api_server(port);

    let app = tauri::Builder::default()
        .manage(ServerHandle(Mutex::new(Some(server))))
        .setup(move |app| {
            // 注入点：页面任何脚本执行之前生效（Phase A/B 约定的宿主接缝）。
            let init_script = format!(
                "window.__PDF2ZH_RUNTIME__ = {{ apiBase: 'http://127.0.0.1:{port}' }};"
            );

            // 闪屏：立即可见，把 3~4s 冷启动变成可感知的加载态。
            tauri::WebviewWindowBuilder::new(
                app,
                SPLASH_LABEL,
                tauri::WebviewUrl::App("splash.html".into()),
            )
            .title("PDFMathTranslate")
            .decorations(false)
            .resizable(false)
            .maximizable(false)
            .minimizable(false)
            .inner_size(380.0, 170.0)
            .center()
            .build()?;

            // 主窗口：先隐藏，API 就绪后再展示，避免前端空连报错；
            // SPA 侧另有健康门闩双保险（AppShell ReadyGate）。
            tauri::WebviewWindowBuilder::new(
                app,
                MAIN_LABEL,
                tauri::WebviewUrl::default(),
            )
            .title("PDFMathTranslate")
            .inner_size(1200.0, 860.0)
            .visible(false)
            .initialization_script(&init_script)
            .build()?;

            let handle = app.handle().clone();
            std::thread::spawn(move || {
                if !wait_for_api(port, 30) {
                    eprintln!("pdf2zh API server did not become ready on 127.0.0.1:{port}");
                    kill_server(&handle);
                    handle.exit(1);
                    return;
                }
                if let Some(main_win) = handle.get_webview_window(MAIN_LABEL) {
                    let _ = main_win.show();
                    let _ = main_win.set_focus();
                }
                if let Some(splash) = handle.get_webview_window(SPLASH_LABEL) {
                    let _ = splash.close();
                }
            });

            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application");

    app.run(move |app_handle, event| {
        // 主进程退出时回收 API 子进程。
        if matches!(event, tauri::RunEvent::Exit) {
            kill_server(app_handle);
        }
    });
}
