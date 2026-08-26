/** 任务状态 store：SSE 帧 → 状态切片 → 组件局部重渲染。 */

import { create } from "zustand";
import { api, ApiError } from "../api/client";
import {
  getEngines,
  listTasks,
  submitTask,
  controlTask,
  cancelTask,
} from "../api/endpoints";
import type {
  EngineInfo,
  EventFrame,
  TaskState,
} from "../api/types";
import { isTerminal } from "../api/types";

interface AppState {
  engines: EngineInfo[];
  tasks: Record<string, TaskState>;
  activeId: string | null;
  connected: boolean;
  submitting: boolean;
  error: string | null;
  /** 每任务执行日志（ProgressPanel 渲染），环形上限 200 条 */
  logs: Record<string, string[]>;
  /** 当前活动任务的取消订阅句柄 */
  _unsub: (() => void) | null;

  bootstrap(): Promise<void>;
  refreshTasks(): Promise<void>;
  setActive(taskId: string | null): void;
  subscribeActive(): void;
  submit(params: Parameters<typeof submitTask>[0]): Promise<string | null>;
  control(action: "pause" | "resume" | "skip"): Promise<void>;
  cancelActive(): Promise<void>;
}

const LOG_CAP = 200;

function appendLog(
  logs: Record<string, string[]>,
  taskId: string,
  line: string,
): Record<string, string[]> {
  if (!line.trim()) return logs;
  const prev = logs[taskId] ?? [];
  const next = [...prev, line];
  if (next.length > LOG_CAP) next.splice(0, next.length - LOG_CAP);
  return { ...logs, [taskId]: next };
}

function applyEventToState(
  state: TaskState,
  frame: Extract<EventFrame, { event: "progress" }>["data"],
): TaskState {
  return {
    ...state,
    progress: frame.progress ?? state.progress,
    stage: frame.stage ?? state.stage,
    message: frame.message ?? state.message,
    eta: frame.eta ?? state.eta,
    // status 随增量 progress 帧下发（暂停/恢复必须靠此刷新 UI 的继续按钮）
    status: frame.status ?? state.status,
    // 细粒度计数（页/段落级）随事件搭车更新；缺省保持旧值
    stage_detail: frame.detail ?? state.stage_detail ?? null,
    updated_at: frame.timestamp ?? Date.now() / 1000,
  };
}

export const useAppStore = create<AppState>((set, get) => ({
  engines: [],
  tasks: {},
  activeId: null,
  connected: false,
  submitting: false,
  error: null,
  logs: {},
  _unsub: null,

  async bootstrap() {
    try {
      const [engines] = await Promise.all([getEngines(), get().refreshTasks()]);
      set({ engines });
    } catch (err) {
      set({ error: `bootstrap failed: ${String(err)}` });
    }
  },

  async refreshTasks() {
    const list = await listTasks();
    const tasks = { ...get().tasks };
    for (const t of list) tasks[t.task_id] = t;
    set({ tasks });
  },

  setActive(taskId) {
    if (get()._unsub) {
      get()._unsub?.();
      set({ _unsub: null });
    }
    set({ activeId: taskId });
    get().subscribeActive();
  },

  subscribeActive() {
    const taskId = get().activeId;
    if (!taskId) return;
    if (get()._unsub) get()._unsub?.();

    const upsert = (patch: Partial<TaskState>) => {
      const current = get().tasks[taskId];
      set({
        tasks: {
          ...get().tasks,
          [taskId]: { ...current, task_id: taskId, ...patch } as TaskState,
        },
      });
    };

    const log = (line: string) =>
      set({ logs: appendLog(get().logs, taskId, line) });

    const unsub = api().openEvents(`/api/tasks/${taskId}/events`, {
      onOpen: () => set({ connected: true }),
      onError: () => set({ connected: false }),
      onFrame: (frame) => {
        switch (frame.event) {
          case "state":
            set({
              connected: true,
              tasks: { ...get().tasks, [taskId]: frame.data },
            });
            if (isTerminal(frame.data.status)) set({ _unsub: null });
            break;
          case "progress": {
            const current = get().tasks[taskId];
            if (frame.data.message) log(frame.data.message);
            if (current) upsert(applyEventToState(current, frame.data));
            break;
          }
          case "notice": {
            const current = get().tasks[taskId];
            const { severity, title, message } = frame.data;
            log(`[${severity}] ${title || ""} ${message || ""}`.trim());
            if (current) {
              upsert({
                message: `${title || ""} ${message || ""}`.trim(),
              });
            }
            break;
          }
          case "done": {
            log(`[${frame.data.status}]`);
            // 终态后拉一次全量（获取 result_files 等）
            import("../api/endpoints")
              .then(({ getTask }) => getTask(taskId))
              .then((full) =>
                set({
                  tasks: { ...get().tasks, [taskId]: full },
                  connected: false,
                  _unsub: null,
                }),
              )
              .catch(() => set({ connected: false }));
            break;
          }
          default:
            break;
        }
      },
    });
    set({ _unsub: unsub });
  },

  async submit(params) {
    set({ submitting: true, error: null });
    try {
      const { task_id } = await submitTask(params);
      set({ activeId: task_id, submitting: false });
      get().subscribeActive();
      return task_id;
    } catch (err) {
      set({ submitting: false, error: String(err) });
      return null;
    }
  },

  async control(action) {
    const taskId = get().activeId;
    if (!taskId) return;
    try {
      await controlTask(taskId, action);
    } catch (err) {
      set({
        error:
          err instanceof ApiError && err.status === 404
            ? `task ${taskId} not found (already removed?)`
            : String(err),
      });
    }
  },

  async cancelActive() {
    const taskId = get().activeId;
    if (!taskId) return;
    try {
      await cancelTask(taskId);
    } catch (err) {
      set({
        error:
          err instanceof ApiError && err.status === 404
            ? `task ${taskId} not found (already removed?)`
            : String(err),
      });
    }
  },
}));
