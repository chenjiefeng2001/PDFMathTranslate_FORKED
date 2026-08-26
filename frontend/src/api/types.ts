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

/** 词表库条目（GET /api/glossaries）。 */
export interface GlossaryInfo {
  name: string;
  path: string;
  entries: number | null;
  error?: string;
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
  /** 逐文件失败明细（批量任务）：{file, error} */
  file_failures?: { file: string; error: string }[] | null;
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
  /** 最新一次细粒度进度快照（轮询/重连恢复「第几页」级显示） */
  stage_detail?: ProgressDetail | null;
  created_at: number;
  updated_at: number;
}

/** 细粒度进度细节（后端 TaskProgressEvent.detail / TaskState.stage_detail） */
export interface ProgressDetail {
  engine?: string;
  /** 引擎原始阶段名（如 "Parse Page Layout"），原样透传 */
  raw_stage?: string;
  /** 计数单位：page / paragraph / term / batch */
  unit?: string;
  current?: number;
  total?: number;
  /** 组件加载场景（magicpdf/mineru 的模型组件） */
  component?: string;
}

export interface ProgressEventPayload {
  type: "TaskProgressEvent";
  task_id: string;
  stage: string;
  progress: number;
  message: string;
  eta: number;
  detail?: ProgressDetail | null;
  /** 当前任务状态（paused/running/...），随增量 progress 帧下发，用于刷新状态。 */
  status?: string;
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
