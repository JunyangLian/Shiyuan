use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;

use tauri::{Manager, RunEvent};

/// 本地核心的默认监听地址（与 core/shiyuan/__main__.py 保持一致）。
const CORE_ADDR: &str = "127.0.0.1:8737";

/// 由桌面端拉起的核心进程句柄；退出时需要一并结束，避免残留后台服务。
struct CoreProcess(Mutex<Option<Child>>);

fn core_is_running() -> bool {
    let addr = match CORE_ADDR.parse() {
        Ok(addr) => addr,
        Err(_) => return false,
    };
    std::net::TcpStream::connect_timeout(&addr, std::time::Duration::from_millis(300)).is_ok()
}

/// 核心以 onedir 形式随安装包分发，启动时不再解压 180MB 到临时目录。
fn core_executable(app: &tauri::AppHandle) -> Option<PathBuf> {
    let root = app.path().resource_dir().ok()?;
    let candidate = root
        .join("binaries")
        .join("shiyuan-core")
        .join("shiyuan-core.exe");
    candidate.exists().then_some(candidate)
}

/// 读取核心启动时写入的进程级访问令牌，交给前端放进 Authorization 头。
/// 数据目录规则与核心 config.app_home() 保持一致：
/// SHIYUAN_HOME > 安装目录 data\（release）> %LOCALAPPDATA%\ShiyuanData（源码调试）。
#[tauri::command]
fn api_token(app: tauri::AppHandle) -> Result<String, String> {
    let base = if let Ok(home) = std::env::var("SHIYUAN_HOME") {
        PathBuf::from(home)
    } else if !cfg!(debug_assertions) {
        app.path()
            .resource_dir()
            .map_err(|err| format!("无法确定安装目录：{err}"))?
            .join("data")
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
            // 调试模式下由开发者自行启动核心（npm run core），安装包运行时才托管核心进程；
            // 若已有核心在监听（例如上一进程残留），则直接复用，不重复拉起。
            let mut child = None;
            if !cfg!(debug_assertions) && !core_is_running() {
                match core_executable(app.handle()) {
                    Some(exe) => match Command::new(&exe)
                        .stdin(Stdio::null())
                        .stdout(Stdio::null())
                        .stderr(Stdio::null())
                        .spawn()
                    {
                        Ok(spawned) => child = Some(spawned),
                        Err(err) => eprintln!("启动本地核心失败：{err}"),
                    },
                    None => eprintln!("未找到内置核心可执行文件"),
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
                if let Some(mut child) = state.0.lock().expect("核心进程锁被污染").take() {
                    let _ = child.kill();
                }
            }
        }
    });
}
