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
use std::sync::{Arc, Mutex};
use std::time::Duration;

/// 选一个当前空闲的 TCP 端口（绑定 127.0.0.1:0 由系统分配后立即释放）。
///
/// 冷启动排障（doc/perf/coldstart-trace）之后的打开失败调查表明：固定
/// 11009 是一类故障的总根源——上次会话的孤儿 sidecar / 双开实例 / 其他
/// 软件占用都会让新 sidecar `[Errno 10048]` 秒死，而壳层既不感知子进程
/// 存活也不清理孤儿。动态端口从根上消灭冲突面；理论上的释放-复用竞窗
/// 概率极低，且后果退化为旧行为而非更糟。
fn pick_free_port() -> u16 {
    std::net::TcpListener::bind("127.0.0.1:0")
        .expect("failed to bind ephemeral port for sidecar")
        .local_addr()
        .expect("ephemeral listener has no local addr")
        .port()
}

fn api_port() -> u16 {
    if let Ok(v) = std::env::var("PDF2ZH_API_PORT") {
        if let Ok(p) = v.parse() {
            return p; // 显式指定时尊重用户（开发/调试场景）
        }
    }
    pick_free_port()
}

/// TCP 探活 + 子进程存活监测：端口可连接即视为就绪；sidecar 进程提前
/// 死亡（典型：端口被占 bind 失败、依赖损坏）立即返回失败，不再傻等满
/// 超时。返回 (是否成功, 失败原因)。
fn wait_for_api(port: u16, child: &mut Child, timeout_secs: u64) -> (bool, String) {
    use std::net::TcpStream;
    let deadline = std::time::Instant::now() + Duration::from_secs(timeout_secs);
    loop {
        if TcpStream::connect(("127.0.0.1", port)).is_ok() {
            return (true, String::new());
        }
        // fail-fast：子进程已退出（含 panic/绑定失败），继续轮询毫无意义。
        if let Some(status) = child.try_wait().ok().flatten() {
            return (
                false,
                format!("sidecar process exited early (code {status:?}); see log file"),
            );
        }
        if std::time::Instant::now() >= deadline {
            return (false, format!("not ready within {timeout_secs}s"));
        }
        std::thread::sleep(Duration::from_millis(300));
    }
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

/// sidecar stdout/stderr 落盘路径（每次启动截断重开）。
///
/// 打开失败调查显示：sidecar 的 `[Errno 10048]` 等关键错误此前完全丢失
/// （子进程 stderr 无重定向），排障只能靠猜。落盘后失败弹窗可直接指路。
fn sidecar_log_path() -> std::path::PathBuf {
    std::env::temp_dir().join("pdf2zh-sidecar.log")
}

fn spawn_api_server(port: u16, log_path: &std::path::Path) -> Child {
    use std::process::Stdio;

    // 优先级：
    // 1. 捆绑 sidecar onedir 资源（tauri resources：安装目录下
    //    pdf2zh-api-sidecar/pdf2zh-api-sidecar.exe）—— 生产/分发形态；
    // 2. 兼容旧便携布局（与主程序同目录的 sidecar 单文件）；
    // 3. PDF2ZH_PYTHON 显式解释器 → `python -m pdf2zh.pdf2zh --api` —— 开发后备。
    //
    // 动态端口下理论上不再有同映像孤儿挡路；但强杀/崩溃遗留的僵尸仍会
    // 白白占着 ~64MB 内存，且单实例锁保证此刻不该有任何存活者——
    // spawn 前按映像名清场（best-effort，失败忽略）。开发态 python 后备
    // 不做此清理，避免误伤用户其他 python 进程。
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
                // best-effort 清场：单实例锁保证此刻不该有其他存活 sidecar
                let cleanup = std::process::Command::new("taskkill")
                    .args(["/F", "/IM", "pdf2zh-api-sidecar.exe"])
                    .output();
                if let Ok(out) = cleanup {
                    eprintln!(
                        "stale sidecar cleanup rc={}",
                        out.status.code().unwrap_or(-1)
                    );
                }
                let (out, err): (Stdio, Stdio) = match std::fs::File::create(log_path) {
                    Ok(file) => {
                        let dup = file.try_clone().expect("clone sidecar log handle");
                        (file.into(), dup.into())
                    }
                    Err(_) => (Stdio::inherit(), Stdio::inherit()), // 退回旧行为
                };
                return Command::new(&candidate)
                    .args(["--port", &port.to_string()])
                    .stdout(out)
                    .stderr(err)
                    .spawn()
                    .unwrap_or_else(|e| panic!("failed to spawn bundled api sidecar: {e}"));
            }
        }
    }
    let python = std::env::var("PDF2ZH_PYTHON").unwrap_or_else(|_| "python".to_string());
    Command::new(&python)
        .args(["-m", "pdf2zh.pdf2zh", "--api"])
        .spawn()
        .expect("failed to spawn pdf2zh API server (check PDF2ZH_PYTHON)")
}

const MAIN_LABEL: &str = "main";

fn kill_server(server: &Arc<Mutex<Option<Child>>>) {
    if let Ok(mut guard) = server.lock() {
        if let Some(mut child) = guard.take() {
            let _ = child.kill();
        }
    }
}

/// 单实例守卫（早于一切副作用执行）。
///
/// 双开调查发现：tauri-plugin-single-instance 在 builder 初始化时才生效，
/// 而第二实例在那之前已经完成「taskkill 清场 + 拉起自己的 sidecar」——
/// 会把第一实例的后端误杀。这里用内核命名互斥体在 main() 最前面拦截，
/// 第二实例直接静默退出；插件的聚焦回调仍作为第二道防线保留。
#[cfg(windows)]
fn ensure_single_instance() {
    const ERROR_ALREADY_EXISTS: u32 = 183;
    #[link(name = "kernel32")]
    extern "system" {
        fn CreateMutexW(
            lpattributes: *mut core::ffi::c_void,
            binitialowner: i32,
            lpname: *const u16,
        ) -> *mut core::ffi::c_void;
        fn GetLastError() -> u32;
    }
    #[link(name = "user32")]
    extern "system" {
        fn MessageBoxW(
            hwnd: *mut core::ffi::c_void,
            text: *const u16,
            caption: *const u16,
            utype: u32,
        ) -> i32;
    }
    let name: Vec<u16> = std::os::windows::ffi::OsStrExt::encode_wide(std::ffi::OsStr::new(
        "Local\\PDFMathTranslate.SingleInstance",
    ))
    .chain(std::iter::once(0))
    .collect();
    unsafe {
        let _ = CreateMutexW(core::ptr::null_mut(), 0, name.as_ptr());
        if GetLastError() == ERROR_ALREADY_EXISTS {
            // 已有实例在运行：温和提示后退出（不做窗口聚焦——那是插件的事）。
            let msg: Vec<u16> = std::os::windows::ffi::OsStrExt::encode_wide(std::ffi::OsStr::new(
                "PDFMathTranslate 已在运行",
            ))
            .chain(std::iter::once(0))
            .collect();
            let cap: Vec<u16> = std::os::windows::ffi::OsStrExt::encode_wide(std::ffi::OsStr::new(
                "PDFMathTranslate",
            ))
            .chain(std::iter::once(0))
            .collect();
            MessageBoxW(
                core::ptr::null_mut(),
                msg.as_ptr(),
                cap.as_ptr(),
                0x40, // MB_ICONINFORMATION
            );
            std::process::exit(0);
        }
        // 故意不关闭互斥体句柄：持有到进程退出即所需语义。
    }
}

#[cfg(not(windows))]
fn ensure_single_instance() {}

fn main() {
    ensure_single_instance();
    let port = api_port();
    let log_path = sidecar_log_path();
    let server = spawn_api_server(port, &log_path);
    let server_slot = Arc::new(Mutex::new(Some(server)));
    let watchdog_slot = server_slot.clone();
    let exit_slot = server_slot.clone();

    let app = tauri::Builder::default()
        // 单实例：双开时第二实例立即退出并把已有主窗拉到前台。
        // 双开后端（内存×2、任务状态分裂）是打开失败调查中确认的故障面之一。
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            use tauri::Manager;
            if let Some(w) = app.get_webview_window(MAIN_LABEL) {
                let _ = w.show();
                let _ = w.set_focus();
            }
        }))
        .plugin(tauri_plugin_dialog::init())
        .invoke_handler(tauri::generate_handler![save_bytes])
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
                // 看门狗：TCP 探活 + 子进程存活双监测。sidecar 提前死亡
                // （bind 冲突/依赖损坏）立即失败，不再傻等满超时；超时上限
                // 60s（实测 AV 与安装后扫描叠加时首启可达 20s+）。
                let mut guard = watchdog_slot.lock().ok();
                let outcome = match guard.as_mut().and_then(|c| c.as_mut()) {
                    Some(child) => match wait_for_api(port, child, 60) {
                        (true, _) => None,
                        (false, why) => Some(why),
                    },
                    None => Some("internal error: server handle missing".to_string()),
                };
                if let Some(why) = outcome {
                    eprintln!("pdf2zh API server failed on 127.0.0.1:{port}: {why}");
                    use tauri_plugin_dialog::{DialogExt, MessageDialogKind};
                    let _ = handle
                        .dialog()
                        .message(format!(
                            "PDFMathTranslate 启动失败 / Failed to start\n\n{why}\n\n日志 / log:\n{}",
                            log_path.display()
                        ))
                        .kind(MessageDialogKind::Error)
                        .blocking_show();
                    kill_server(&watchdog_slot);
                    handle.exit(1);
                }
            });

            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application");

    app.run(move |_app_handle, event| {
        // 主进程退出时回收 API 子进程。
        if matches!(event, tauri::RunEvent::Exit) {
            kill_server(&exit_slot);
        }
    });
}
