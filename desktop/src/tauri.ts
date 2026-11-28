declare global {
  interface Window {
    __TAURI_INTERNALS__?: unknown;
  }
}

export const isTauri = (): boolean => typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;

export async function pickDirectory(): Promise<string | null> {
  if (!isTauri()) {
    const typed = window.prompt("浏览器预览模式：请输入要授权的文件夹绝对路径（例如 D:\\科研资料）");
    return typed?.trim() ? typed.trim() : null;
  }
  const { open } = await import("@tauri-apps/plugin-dialog");
  const picked = await open({ directory: true, multiple: false, title: "选择要授权的文件夹" });
  return typeof picked === "string" ? picked : null;
}

export async function openFile(path: string): Promise<void> {
  if (isTauri()) {
    // 走 Rust 命令：默认关联打不开时回退到 Windows「打开方式」，不再静默失败。
    const { invoke } = await import("@tauri-apps/api/core");
    try {
      await invoke("open_original", { path });
    } catch (err) {
      window.alert(err instanceof Error ? err.message : String(err));
    }
    return;
  }
  await navigator.clipboard.writeText(path);
  window.alert(`浏览器预览模式无法直接打开文件，路径已复制：\n${path}`);
}
