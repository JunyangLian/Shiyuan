use std::path::PathBuf;
use std::sync::Mutex;

use tauri::{Manager, RunEvent};
use tauri_plugin_shell::process::CommandChild;
use tauri_plugin_shell::ShellExt;

/// 本地核心的默认监听地址（与 core/shiyuan/__main__.py 保持一致）。
const CORE_ADDR: &str = "127.0.0.1:8737";

/// 由桌面端拉起的 sidecar 进程句柄；退出时需要一并结束，避免残留后台服务。
struct CoreProcess(Mutex<Option<CommandChild>>);

fn core_is_running() -> bool {
    let addr = match CORE_ADDR.parse() {
        Ok(addr) => addr,
        Err(_) => return false,
    };
    std::net::TcpStream::connect_timeout(&addr, std::time::Duration::from_millis(300)).is_ok()
}

/// 读取核心启动时写入的进程级访问令牌，交给前端放进 Authorization 头。
#[tauri::command]
fn api_token() -> Result<String, String> {
    let base = if let Ok(home) = std::env::var("SHIYUAN_HOME") {
        PathBuf::from(home)
    } else if let Ok(local) = std::env::var("LOCALAPPDATA") {
        PathBuf::from(local).join("ShiyuanData")
    } else {
        return Err("无法确定应用数据目录".to_string());
    };
    std::fs::read_to_string(base.join("api-token"))
        .map(|value| value.trim().to_string())
        .map_err(|err| format!("读取本地访问令牌失败：{err}"))
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![api_token])
        .setup(|app| {
            // 调试模式下由开发者自行启动核心（npm run core），安装包运行时才托管 sidecar；
            // 若已有核心在监听（例如上一进程残留），则直接复用，不重复拉起。
            let mut child = None;
            if !cfg!(debug_assertions) && !core_is_running() {
                match app.shell().sidecar("shiyuan-core") {
                    Ok(command) => match command.spawn() {
                        Ok((_events, spawned)) => child = Some(spawned),
                        Err(err) => eprintln!("启动本地核心失败：{err}"),
                    },
                    Err(err) => eprintln!("未找到本地核心 sidecar：{err}"),
                }
            }
            app.manage(CoreProcess(Mutex::new(child)));
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("启动拾源桌面端失败");

    app.run(|app_handle, event| {
        if let RunEvent::Exit = event {
            if let Some(state) = app_handle.try_state::<CoreProcess>() {
                if let Some(child) = state.0.lock().expect("核心进程锁被污染").take() {
                    let _ = child.kill();
                }
            }
        }
    });
}
