use std::path::PathBuf;

fn main() {
    let mut attributes = tauri_build::Attributes::new();

    // MinGW 的 windres 打不开含非 ASCII 字符的路径（例如中文工作目录）。
    // 先把图标复制到 OUT_DIR（随 target 目录），再让构建脚本引用这份副本。
    if std::env::var("CARGO_CFG_TARGET_OS").as_deref() == Ok("windows") {
        println!("cargo:rerun-if-changed=icons/icon.ico");
        let icon: PathBuf = ["icons", "icon.ico"].iter().collect();
        let copied = PathBuf::from(std::env::var("OUT_DIR").expect("OUT_DIR 未设置"))
            .join("shiyuan-window-icon.ico");
        std::fs::copy(&icon, &copied).expect("复制窗口图标到 OUT_DIR 失败");
        attributes = attributes
            .windows_attributes(tauri_build::WindowsAttributes::new().window_icon_path(&copied));
    }

    tauri_build::try_build(attributes).expect("tauri-build 执行失败");
}
