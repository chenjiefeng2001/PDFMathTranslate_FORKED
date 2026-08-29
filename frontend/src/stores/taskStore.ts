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

/**
 * 每任务最近已消费的 SSE 序号（来自后端 ``id: <seq>`` 行的绝对位置）。
 *
 * 切换任务（setActive → subscribeActive）时旧 EventSource 被关闭、新流从
 * seq 0 打开会触发后端全量重放——若不做游标续传，同一批事件/日志会被
 * 重复追加。此表按任务记住断点，重开流时经 ``?since=<seq>`` 只让后端补发
 * 之后的增量，从根本上避免「切换任务导致事实日志刷新重复」。
 */
const taskSeqs: Record<string, number> = {};

/**
 * 本会话内用户主动取消（点击「取消」）的任务集。
 *
 * 后端是协作式取消，标记 CANCELLED 后待排空的流水线仍可能再产出一两帧；
 * 这些帧对用户是噪声。取消后直到收到确认（done）之前，抑制本任务的
 * 增量进度/notice 日志，让面板在点击后立即呈现「已取消、不再刷新」。
 */
const userCancelled = new Set<string>();

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

    // 断点续传：从最后一次已消费的序号往后补发；首次观看该任务则为 0
    // （后端全量重放一次，正好形成完整日志）。
    const since = taskSeqs[taskId] ?? 0;
    const unsub = api().openEvents(
      `/api/tasks/${taskId}/events`,
      {
        onOpen: () => set({ connected: true }),
        onError: () => set({ connected: false }),
        onFrame: (frame, id) => {
          // 去重：序号回退/复现的帧（断线重连窗口内补发的、已被处理过的）
          // 直接丢弃，避免把同一事件/日志重复追加。state 快照帧无序号，始终透传。
          if (id !== undefined) {
            const last = taskSeqs[taskId];
            if (last !== undefined && id <= last) return;
            taskSeqs[taskId] = id;
          }
          // 用户主动取消后、确认（done）到达前的空滞期：抑制增量帧，
          // 让面板点击后立即停刷，而不是继续展示排空流水线的残留日志。
          if (
            userCancelled.has(taskId) &&
            frame.event !== "state" &&
            frame.event !== "done"
          ) {
            return;
          }
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
            // 取消确认到达后解除抑制，后续恢复浏览该任务时日志回到正常行为。
            userCancelled.delete(taskId);
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
      },
      { since },
    );
    set({ _unsub: unsub });
  },

  async submit(params) {
    // store 级同步防连点：不依赖 React 闭包/重渲染时序。快速连点或事件重放
    // 时第二次调用直接短路，绝不重复 POST /api/tasks（后端另有指纹幂等兜底）。
    if (get().submitting) return null;
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
    // 乐观取消：点击后立即把面板置为「已取消」，隐藏控制按钮并停止累积日志，
    // 不必等后端待排空的流水线跑完才反映到 UI（后端是协作式取消）。
    userCancelled.add(taskId);
    set((state) => {
      const cur = state.tasks[taskId];
      if (!cur) return {};
      return {
        tasks: {
          ...state.tasks,
          [taskId]: { ...cur, status: "cancelled", message: "Cancelled by user" },
        },
      };
    });
    try {
      await cancelTask(taskId);
    } catch (err) {
      // 取消已被后端确认（409 等表示已终态）时不视为错误；仅记录真正的异常。
      if (!(err instanceof ApiError && err.status === 404)) {
        set({ error: String(err) });
      }
    }
  },
}));
