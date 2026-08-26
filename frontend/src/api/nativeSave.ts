/**
 * 原生保存能力封装：桌面壳（Tauri v2）内经系统「另存为 / 选择文件夹」对话框
 * 决定落盘位置，再通过自定义 `save_bytes` 命令写入；纯浏览器环境由调用方
 * 回退到锚点下载。对话框与写盘命令均只在 Tauri 宿主内调用。
 */

declare global {
  interface Window {
    __TAURI_INTERNALS__?: Record<string, unknown>;
  }
}

/** 是否运行在 Tauri 桌面壳内（main.rs initialization_script 所在宿主）。 */
export function isTauri(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

/** 系统「另存为」对话框；返回选定绝对路径，取消返回 null。 */
export async function pickSavePath(defaultFileName: string): Promise<string | null> {
  const { save } = await import("@tauri-apps/plugin-dialog");
  const picked = await save({
    defaultPath: defaultFileName,
    title: defaultFileName,
  });
  return typeof picked === "string" && picked.length > 0 ? picked : null;
}

/** 系统「选择文件夹」对话框；取消或无效选择返回 null。 */
export async function pickExistingDirectory(title: string): Promise<string | null> {
  const { open } = await import("@tauri-apps/plugin-dialog");
  const picked = await open({ directory: true, multiple: false, title });
  return typeof picked === "string" && picked.length > 0 ? picked : null;
}

/** 把字节流写到指定绝对路径（宿主 save_bytes 命令，自动补建父目录）。 */
export async function writeBytesAt(path: string, data: Uint8Array): Promise<void> {
  const { invoke } = await import("@tauri-apps/api/core");
  // Vec<u8> 经 JSON 数组传输（产物为 MB 级 PDF/ZIP，可接受）。
  await invoke("save_bytes", { path, data: Array.from(data) });
}

/** 目录 + 文件名拼接（Windows 反斜杠优先，兼容正斜杠根）。 */
export function joinPath(dir: string, name: string): string {
  const sep = dir.endsWith("\\") || dir.endsWith("/") ? "" : "\\";
  return `${dir}${sep}${name}`;
}
