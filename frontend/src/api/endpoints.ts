/** 类型化端点封装：业务代码只依赖这些函数与 types.ts。 */

import { api } from "./client";
import type { EngineInfo, TaskState } from "./types";

export function getHealth(): Promise<{ status: string; tasks: number }> {
  return api().get("/api/health");
}

export function getEngines(): Promise<EngineInfo[]> {
  return api().get("/api/engines");
}

export function listTasks(): Promise<TaskState[]> {
  return api().get("/api/tasks");
}

export function getTask(taskId: string): Promise<TaskState> {
  return api().get(`/api/tasks/${taskId}`);
}

export interface SubmitParams {
  file?: File | null;
  sourcePath?: string;
  targetLang: string;
  sourceLang: string;
  engine: string;
  threads?: number;
  pageRange?: string;
  parseEngine?: string;
  modeChoice?: string;
  ignoreCache?: boolean;
}

export function submitTask(params: SubmitParams): Promise<{ task_id: string }> {
  const form = new FormData();
  if (params.file) form.append("file", params.file);
  if (params.sourcePath) form.append("source_path", params.sourcePath);
  form.append("target_lang", params.targetLang);
  form.append("source_lang", params.sourceLang);
  form.append("engine", params.engine);
  form.append("threads", String(params.threads ?? 4));
  if (params.pageRange) form.append("page_range", params.pageRange);
  if (params.parseEngine) form.append("parse_engine", params.parseEngine);
  if (params.modeChoice && params.modeChoice !== "auto") {
    form.append("mode_choice", params.modeChoice);
  }
  form.append("ignore_cache", String(!!params.ignoreCache));
  return api().postForm("/api/tasks", form);
}

export type TaskAction = "pause" | "resume" | "skip";

export function controlTask(
  taskId: string,
  action: TaskAction,
): Promise<Record<string, unknown>> {
  return api().request(`POST`, `/api/tasks/${taskId}/${action}`);
}

export function cancelTask(taskId: string): Promise<{ cancelled: boolean }> {
  return api().request("DELETE", `/api/tasks/${taskId}`);
}

/** 结果文件下载地址（浏览器原生 GET，尊重 apiBase 解析链）。 */
export function artifactUrl(taskId: string, index: number): string {
  const base = resolveApiBaseForHref();
  return `${base}/api/tasks/${taskId}/artifacts/${index}`;
}

function resolveApiBaseForHref(): string {
  // 与 client.resolveApiBase 相同的解析链（避免循环导入的轻量复制）
  const injected =
    typeof window !== "undefined" ? window.__PDF2ZH_RUNTIME__?.apiBase : undefined;
  if (injected) return injected.replace(/\/+$/, "");
  try {
    const overridden = window.localStorage.getItem("pdf2zh.apiBase");
    if (overridden) return overridden.replace(/\/+$/, "");
  } catch {
    /* ignore */
  }
  const envBase = import.meta.env.VITE_API_BASE as string | undefined;
  return envBase ? envBase.replace(/\/+$/, "") : "";
}
