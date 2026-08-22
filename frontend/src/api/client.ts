/**
 * 传输层：REST + SSE 的宿主无关抽象（Tauri v2 健壮性核心接缝）。
 *
 * 设计约定：
 * - 所有网络访问都经过 ApiTransport 接口；未来 Tauri 外壳只需实现
 *   TauriHttpTransport / TauriEventTransport 并调用 registerApiTransport()，
 *   业务代码零改动。
 * - API 地址解析链（后者覆盖前者）：
 *     1. window.__PDF2ZH_RUNTIME__.apiBase   ← 宿主启动时注入（Tauri/部署脚本）
 *     2. localStorage["pdf2zh.apiBase"]      ← 用户调试覆盖
 *     3. import.meta.env.VITE_API_BASE       ← 构建期配置
 *     4. ""（同源）                          ← FastAPI StaticFiles 托管的默认形态
 */

import type {
  EventFrame,
} from "./types";

export interface ApiTransport {
  /** JSON GET */
  get<T>(path: string): Promise<T>;
  /** multipart 表单提交，返回 JSON */
  postForm<T>(path: string, form: FormData): Promise<T>;
  /** 其余方法（PUT/DELETE 等），body 为可选 JSON 载荷 */
  request<T>(method: string, path: string, body?: unknown): Promise<T>;
  /** 打开 SSE 流，返回取消订阅函数。onError 后由实现方自动重连。 */
  openEvents(
    path: string,
    handlers: {
      onFrame: (frame: EventFrame) => void;
      onOpen?: () => void;
      onError?: () => void;
    },
  ): () => void;
}

declare global {
  interface Window {
    __PDF2ZH_RUNTIME__?: { apiBase?: string };
  }
}

const STORAGE_KEY = "pdf2zh.apiBase";

/** 携带 HTTP 状态码的 API 错误；调用方可按 status 区分呈现方式。 */
export class ApiError extends Error {
  readonly status: number;

  constructor(method: string, path: string, status: number, detail?: string) {
    super(detail || `${method} ${path} → ${status}`);
    this.name = "ApiError";
    this.status = status;
  }
}

export function resolveApiBase(): string {
  if (typeof window !== "undefined") {
    const injected = window.__PDF2ZH_RUNTIME__?.apiBase;
    if (injected) return injected.replace(/\/+$/, "");
    try {
      const overridden = window.localStorage.getItem(STORAGE_KEY);
      if (overridden) return overridden.replace(/\/+$/, "");
    } catch {
      /* localStorage 不可用（隐私模式）时忽略 */
    }
  }
  const envBase = import.meta.env.VITE_API_BASE as string | undefined;
  if (envBase) return envBase.replace(/\/+$/, "");
  return "";
}

export class HttpTransport implements ApiTransport {
  constructor(private base: string = resolveApiBase()) {}

  private url(path: string): string {
    return `${this.base}${path}`;
  }

  async get<T>(path: string): Promise<T> {
    const resp = await fetch(this.url(path));
    if (!resp.ok) {
      const detail = await this.safeDetail(resp);
      throw new ApiError("GET", path, resp.status, detail);
    }
    return (await resp.json()) as T;
  }

  async postForm<T>(path: string, form: FormData): Promise<T> {
    const resp = await fetch(this.url(path), { method: "POST", body: form });
    if (!resp.ok) {
      const detail = await this.safeDetail(resp);
      throw new ApiError("POST", path, resp.status, detail);
    }
    return (await resp.json()) as T;
  }

  async request<T>(method: string, path: string, body?: unknown): Promise<T> {
    const resp = await fetch(this.url(path), {
      method,
      headers: body === undefined ? undefined : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (!resp.ok) {
      // 404 同样抛错（如任务已被移除后的 pause/resume/cancel），
      // 由调用方按 status 决定呈现方式
      const detail = await this.safeDetail(resp);
      throw new ApiError(method, path, resp.status, detail);
    }
    return (await resp.json().catch(() => ({}))) as T;
  }

  private async safeDetail(resp: Response): Promise<string> {
    try {
      const body = (await resp.json()) as { detail?: string };
      return typeof body.detail === "string" ? body.detail : "";
    } catch {
      return "";
    }
  }

  openEvents(
    path: string,
    handlers: {
      onFrame: (frame: EventFrame) => void;
      onOpen?: () => void;
      onError?: () => void;
    },
  ): () => void {
    // EventSource 仅支持同源或显式完整 URL —— base 为空时天然同源。
    const source = new EventSource(this.url(path));
    const frameNames = [
      "state",
      "progress",
      "notice",
      "done",
      "error",
    ] as const;

    for (const name of frameNames) {
      source.addEventListener(name, (evt) => {
        try {
          const data = JSON.parse((evt as MessageEvent).data);
          handlers.onFrame({ event: name, data } as EventFrame);
        } catch {
          /* 忽略坏帧 */
        }
        if (name === "done") {
          source.close();
        }
      });
    }
    source.onopen = () => handlers.onOpen?.();
    source.onerror = () => handlers.onError?.();
    return () => source.close();
  }
}

let transport: ApiTransport = new HttpTransport();

/** 宿主（未来的 Tauri v2 外壳）注入自定义传输适配器。 */
export function registerApiTransport(next: ApiTransport): void {
  transport = next;
}

export function api(): ApiTransport {
  return transport;
}
