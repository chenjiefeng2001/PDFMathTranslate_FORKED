/**
 * 与 pdf2zh/services/api.py 的载荷一一对应的类型定义。
 * 纯 TS，无任何运行时/宿主假设（浏览器 / Tauri webview 通用）。
 */

export interface EngineEnv {
  key: string;
  configured: boolean;
}

export interface EngineInfo {
  name: string;
  label: string;
  envs: EngineEnv[];
}

export interface ResultFile {
  index?: string;
  path?: string;
  name?: string;
  type?: string;
}

export interface TaskState {
  task_id: string;
  status: string; // pending|parsing|...|completed|cancelled|failed
  progress: number;
  message: string;
  stage: string;
  mode_choice: string;
  file_progress: number;
  total_progress: number;
  current_file_name: string;
  file_list: string[];
  total_files: number;
  completed_files: number;
  failed_files: number;
  result_files: ResultFile[];
  result_zip: string | null;
  preview_path: string | null;
  diagnostic_summary: string | null;
  quality_scores: Record<string, number> | null;
  /** 结构化诊断报告（legacy: errors/warnings/admissible/issues；V4: records/pass_rate） */
  diagnostic_report: Record<string, unknown> | null;
  /** 自愈行程摘要（ran/iterations/before_errors/after_errors/improved） */
  heal_status: Record<string, unknown> | null;
  /** 自愈处置记录：每个 issue 的处置明细 */
  repair_records: Record<string, unknown>[] | null;
  /** 文档置信度统计（annotated/avg/min/max） */
  confidence_stats: Record<string, number> | null;
  /** V8.4 写回门控裁决（pageid -> verdict） */
  gate_verdicts: Record<string, unknown> | null;
  /** V9.0 Processor 语义通道报告（pageid -> PipelineReport） */
  processor_reports: Record<string, unknown> | null;
  /** 目录条目 IR 记录（pageid -> toc_to_ir_records 输出） */
  toc_ir_records: Record<string, unknown> | null;
  eta: number;
  error_message: string | null;
  created_at: number;
  updated_at: number;
}

export interface ProgressEventPayload {
  type: "TaskProgressEvent";
  task_id: string;
  stage: string;
  progress: number;
  message: string;
  eta: number;
  timestamp: number;
}

export interface NoticeEventPayload {
  type: "RuntimeNoticeEvent";
  task_id: string;
  severity: "info" | "warning" | "error";
  title: string;
  detail: string;
  tip: string;
  message: string;
  timestamp: number;
}

export type EventFrame =
  | { event: "state"; data: TaskState }
  | { event: "progress"; data: ProgressEventPayload }
  | { event: "notice"; data: NoticeEventPayload }
  | { event: "done"; data: { status: string } }
  | { event: "error"; data: { message: string } };

export const TERMINAL_STATUSES = new Set(["completed", "cancelled", "failed"]);

export function isTerminal(status: string): boolean {
  return TERMINAL_STATUSES.has(status);
}
