#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
//! PDFMathTranslate 桌面外壳（Tauri v2）。
//!
//! 职责（对应 frontend/README.md 的三处接缝）：
//! 1. Sidecar：以子进程托管 REST/SSE 后端（onedir sidecar / 兼容旧单文件 /
//!    开发态 PDF2ZH_PYTHON）；
//! 2. 启动体验：主窗口立即可见，sidecar 冷启动期由 SPA ReadyGate 呈现
//!    「连接中」状态（冷启动 trace 结论：押后显示 = 4~5s 盲等）；
//! 3. 注入：webview initialization_script 在页面脚本执行前写入
//!    `window.__PDF2ZH_RUNTIME__.apiBase` —— 前端业务代码零改动。
//!
//! 环境变量：
//! - PDF2ZH_API_PORT（默认 11009）
//! - PDF2ZH_PYTHON   （默认 "python"；打包分发时应指向捆绑解释器/sidecar 可执行文件）

use std::process::{Child, Command};
use std::sync::Mutex;
use std::time::Duration;

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

/// 把前端抓取的字节流写到用户经原生对话框选定的路径。
///
/// 只接受 dialog 插件返回的路径（前端保证来源），因此无需开放
/// tauri-plugin-fs 的任意路径写权限；父目录不存在时自动创建。
#[tauri::command]
fn save_bytes(path: String, data: Vec<u8>) -> Result<(), String> {
    if std::path::Path::new(&path).is_dir() {
        return Err("target path is a directory".into());
    }
    if let Some(parent) = std::path::Path::new(&path).parent() {
        if !parent.as_os_str().is_empty() {
            std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
        }
    }
    std::fs::write(&path, data).map_err(|e| e.to_string())
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
        .plugin(tauri_plugin_dialog::init())
        .invoke_handler(tauri::generate_handler![save_bytes])
        .manage(ServerHandle(Mutex::new(Some(server))))
        .setup(move |app| {
            // 注入点：页面任何脚本执行之前生效（Phase A/B 约定的宿主接缝）。
            let init_script = format!(
                "window.__PDF2ZH_RUNTIME__ = {{ apiBase: 'http://127.0.0.1:{port}' }};"
            );

            // 主窗口：立即可见。冷启动期（sidecar 导入+绑定端口 ~2-4s）由
            // SPA 的 ReadyGate 呈现「连接中」状态——冷启动 trace（doc/perf/
            // coldstart-trace/report.md）表明把显示押后到 API 就绪会让用户
            // 对着小闪屏盲等 4~5s，感知远差于立即给出真实 UI 骨架。
            tauri::WebviewWindowBuilder::new(
                app,
                MAIN_LABEL,
                tauri::WebviewUrl::default(),
            )
            .title("PDFMathTranslate")
            .inner_size(1200.0, 860.0)
            .initialization_script(&init_script)
            .build()?;

            let handle = app.handle().clone();
            std::thread::spawn(move || {
                // 看门狗：sidecar 超时未就绪视为启动失败（正常路径下
                // ReadyGate 先于本线程完成切换，这里不再操作窗口）。
                if !wait_for_api(port, 30) {
                    eprintln!("pdf2zh API server did not become ready on 127.0.0.1:{port}");
                    kill_server(&handle);
                    handle.exit(1);
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
