/** 类型化端点封装：业务代码只依赖这些函数与 types.ts。 */

import { api } from "./client";
import type { EngineInfo, GlossaryInfo, TaskState } from "./types";

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
  /** 批量上传：一个或多个文件，逐个以重复的 ``files`` 部件发送。 */
  files?: File[];
  sourcePath?: string;
  targetLang: string;
  sourceLang: string;
  engine: string;
  threads?: number;
  pageRange?: string;
  parseEngine?: string;
  modeChoice?: string;
  ocrMode?: string;
  backend?: string;
  outputDir?: string;
  ignoreCache?: boolean;
  glossaryNames?: string[];
  /** MinerU 显存预算（GB，空=auto 自动保守估算），对应 MINERU_VIRTUAL_VRAM_SIZE。 */
  mineruVramSize?: string;
  /** MinerU 处理窗口页数（空=引擎默认），对应 MINERU_PROCESSING_WINDOW_SIZE。 */
  mineruWindowSize?: string;
}

export function submitTask(params: SubmitParams): Promise<{ task_id: string }> {
  const form = new FormData();
  for (const f of params.files ?? []) {
    if (f) form.append("files", f);
  }
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
  if (params.ocrMode) form.append("ocr_mode", params.ocrMode);
  if (params.backend) form.append("backend", params.backend);
  if (params.outputDir) form.append("output_dir", params.outputDir);
  form.append("ignore_cache", String(!!params.ignoreCache));
  if (params.mineruVramSize) form.append("mineru_vram_size", params.mineruVramSize);
  if (params.mineruWindowSize) form.append("mineru_window_size", params.mineruWindowSize);
  if (params.glossaryNames?.length) {
    form.append("glossary_files", params.glossaryNames.join(","));
  }
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

/* ── 词表库（设置界面） ──────────────────────────────────────────── */

export function listGlossaries(): Promise<GlossaryInfo[]> {
  return api().get("/api/glossaries");
}

export function importGlossary(
  file: File,
  name?: string,
): Promise<GlossaryInfo> {
  const form = new FormData();
  form.append("file", file);
  if (name) form.append("name", name);
  return api().postForm("/api/glossaries", form);
}

export function glossaryDownloadUrl(name: string): string {
  return `${resolveApiBaseForHref()}/api/glossaries/${encodeURIComponent(name)}/download`;
}

/* ── 引擎凭据（设置界面） ────────────────────────────────────────── */

export interface EngineEnvStatus {
  key: string;
  configured: boolean;
  /** 脱敏回显（如 "sk-••••ab12"），未配置为空串 */
  masked: string;
}

export function getEngineEnvs(engineName: string): Promise<{
  name: string;
  envs: EngineEnvStatus[];
}> {
  return api().get(`/api/engines/${encodeURIComponent(engineName)}/envs`);
}

export function updateEngineEnvs(
  engineName: string,
  envs: Record<string, string>,
): Promise<{ name: string; envs: EngineEnvStatus[] }> {
  return api().request("PUT", `/api/engines/${encodeURIComponent(engineName)}/envs`, { envs });
}

/* ── 模型管理 / 可用性探测 ───────────────────────────────────────── */

export interface DoclayoutModelStatus {
  path: string;
  exists: boolean;
  size_bytes: number;
  sha_ok: boolean;
  downloading: boolean;
  last_error: string | null;
}

export function getDoclayoutModelStatus(): Promise<DoclayoutModelStatus> {
  return api().get("/api/models/doclayout");
}

export function downloadDoclayoutModel(): Promise<{ started: boolean; reason?: string }> {
  return api().request("POST", "/api/models/doclayout/download");
}

export interface GpuProviderStatus {
  onnxruntime_version: string;
  target_path: string;
  cuda_dll_present: boolean;
  cuda_dll_size_bytes: number;
  available_providers: string[];
  cuda_active: boolean;
  downloading: boolean;
  progress_bytes: number;
  total_bytes: number;
  done: boolean;
  last_error: string | null;
}

export function getGpuProviderStatus(): Promise<GpuProviderStatus> {
  return api().get("/api/gpu/provider");
}

export function downloadGpuProvider(): Promise<{ started: boolean; reason?: string }> {
  return api().request("POST", "/api/gpu/provider/download");
}

export function removeGpuProvider(): Promise<{ removed: boolean }> {
  return api().request("POST", "/api/gpu/provider/remove");
}

export interface SelftestMagicpdfResult {
  ok: boolean;
  backend: string;
  hint: string;
  /** MinerU 隔离 venv 的 torch 是否 CUDA 可用（子进程解析实际使用）。 */
  mineru_cuda: boolean;
  mineru_venv: string;
}

export function selftestMagicpdf(): Promise<SelftestMagicpdfResult> {
  return api().get("/api/selftest/magicpdf");
}

export interface MineruSetupStatus {
  running: boolean;
  done: boolean;
  error: string | null;
  interpreter: string | null;
}

export function setupMineru(): Promise<{ started: boolean; reason?: string }> {
  return api().request("POST", "/api/setup/mineru");
}

export function getMineruSetupStatus(): Promise<MineruSetupStatus> {
  return api().get("/api/setup/mineru");
}

/** 启用 MinerU GPU：后台把隔离 venv 的 torch 升级为 CUDA 版。 */
export function setupMineruCuda(): Promise<{ started: boolean; reason?: string }> {
  return api().request("POST", "/api/setup/mineru/cuda");
}

export function getMineruCudaSetupStatus(): Promise<MineruSetupStatus> {
  return api().get("/api/setup/mineru/cuda");
}

/** 结果文件下载地址（浏览器原生 GET，尊重 apiBase 解析链）。 */
export function artifactUrl(taskId: string, index: number): string {
  const base = resolveApiBaseForHref();
  return `${base}/api/tasks/${taskId}/artifacts/${index}`;
}

/** 批量任务「全部下载（ZIP）」地址。 */
export function resultZipUrl(taskId: string): string {
  const base = resolveApiBaseForHref();
  return `${base}/api/tasks/${taskId}/result-zip`;
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
