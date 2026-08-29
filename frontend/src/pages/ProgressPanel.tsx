/**
 * 执行状态面板（ui.section_progress）：进度条 + 阶段步骤条 + ETA/文件计数 +
 * 暂停/恢复/跳过/停止控制 + 执行日志滚动区。
 */

import {
  Alert,
  Button,
  Popconfirm,
  Progress,
  Space,
  Steps,
  Tag,
} from "antd";
import {
  CaretRightOutlined,
  PauseOutlined,
  StepForwardOutlined,
  StopOutlined,
} from "@ant-design/icons";
import { useEffect, useMemo, useRef } from "react";
import { useTranslation } from "react-i18next";
import type { TaskState } from "../api/types";
import { isTerminal } from "../api/types";

/**
 * 流水线阶段与后端 runtime_service._STAGE_ORDER / _STAGE_WEIGHTS 对齐：
 *   parsing 0-10 | analyzing(+planning) 10-40 | translating 40-70 |
 *   layouting 70-85 | rendering(+evaluating) 85-100
 *
 * BabelDOC 原生阶段名会乱序到达（如 Extract Terms 先于版面分析被映射成
 * translating），直接用 stage 推导步骤会让步骤条来回跳。因此步骤索引一律
 * 由**单调的工作量百分比**推导，stage 仅用于文字标签。
 */
const PIPELINE: { key: string; label: string; pctEnd: number }[] = [
  { key: "pending", label: "stage.pending", pctEnd: 2 },
  { key: "parsing", label: "stage.parsing", pctEnd: 40 },
  { key: "translating", label: "stage.translating", pctEnd: 70 },
  { key: "layouting", label: "stage.layouting", pctEnd: 92 },
  { key: "rendering", label: "stage.rendering", pctEnd: 100 },
];

function stepIndexForPercent(pct: number, status: string): number {
  if (status === "completed") return PIPELINE.length;
  for (let i = 0; i < PIPELINE.length; i += 1) {
    if (pct < PIPELINE[i].pctEnd) return i;
  }
  return PIPELINE.length - 1;
}

export function statusLabelKey(status: string): string {
  switch (status) {
    case "pending":
      return "ui.status_ready";
    case "queued":
      return "ui.status_ready";
    case "running":
      return "ui.status_running";
    case "paused":
      return "ui.status_paused";
    case "skipping":
      return "ui.status_skipping";
    case "completed":
      return "ui.status_completed";
    case "failed":
      return "ui.status_failed";
    case "cancelled":
      return "ui.status_cancelled";
    default:
      return "";
  }
}

function formatEta(sec: number): string {
  if (sec <= 0) return "-";
  if (sec < 60) return `${Math.ceil(sec)}s`;
  const m = Math.floor(sec / 60);
  const r = Math.round(sec % 60);
  return `${m}m ${r.toString().padStart(2, "0")}s`;
}

interface Props {
  task: TaskState;
  connected: boolean;
  logs: string[];
  onPause(): void;
  onResume(): void;
  onSkip(): void;
  onCancel(): void;
}

export default function ProgressPanel({
  task,
  connected,
  logs,
  onPause,
  onResume,
  onSkip,
  onCancel,
}: Props) {
  const { t } = useTranslation();
  const terminal = isTerminal(task.status);
  const paused = task.status === "paused";
  const failed = task.status === "failed";

  // 百分比单调钳制：SSE 帧偶发乱序时进度条绝不回退。
  const maxPctRef = useRef(0);
  const rawPct = Math.min(100, Math.max(0, Math.round(task.progress)));
  if (terminal) maxPctRef.current = rawPct;
  else if (rawPct > maxPctRef.current) maxPctRef.current = rawPct;
  // 任务切换（task_id 变化）时重置。
  const lastTaskRef = useRef(task.task_id);
  if (lastTaskRef.current !== task.task_id) {
    lastTaskRef.current = task.task_id;
    maxPctRef.current = rawPct;
  }
  const percent = maxPctRef.current;

  const currentIdx = stepIndexForPercent(percent, task.status);

  const stepStatus = failed
    ? "error"
    : task.status === "cancelled"
      ? "error"
      : terminal
        ? "finish"
        : paused
          ? "wait"
          : "process";

  const statusKey = statusLabelKey(task.status);

  return (
    <section aria-label={t("ui.progress_aria")}>
      <Space direction="vertical" size={12} style={{ width: "100%" }}>
        {/* 主进度行 */}
        <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
          <Progress
            type="dashboard"
            percent={percent}
            size={96}
            status={
              failed
                ? "exception"
                : task.status === "completed"
                  ? "success"
                  : paused
                    ? "normal"
                    : "active"
            }
          />
          <Space direction="vertical" size={4} style={{ flex: 1, minWidth: 0 }}>
            <Space wrap size={8}>
              {statusKey ? (
                <Tag
                  color={
                    task.status === "completed"
                      ? "green"
                      : failed || task.status === "cancelled"
                        ? "red"
                        : paused
                          ? "orange"
                          : "blue"
                  }
                >
                  {t(statusKey)}
                </Tag>
              ) : (
                <Tag>{task.status}</Tag>
              )}
              {!terminal && (
                <Tag color={connected ? "cyan" : "orange"}>
                  SSE · {connected ? "live" : "…"}
                </Tag>
              )}
              <Tag>{t("ui.stage_label")}: {t(`stage.${task.stage || task.status}`)}</Tag>
              {task.parse_engine && (
                <Tag color="geekblue">
                  {t("ui.engine_label")}: {task.parse_engine}
                </Tag>
              )}
            </Space>
            <span style={{ opacity: 0.65 }}>
              {t("ui.progress_eta")}: {terminal && task.eta <= 0 ? "-" : formatEta(task.eta)}
              {"　"}
              {t("ui.label_files")}: {task.completed_files}/{task.total_files}
              {task.failed_files > 0 ? ` (${t("ui.status_failed")} ${task.failed_files})` : ""}
            </span>
            {task.current_file_name && (
              <span
                style={{
                  fontSize: 12,
                  opacity: 0.75,
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  whiteSpace: "nowrap",
                }}
                title={task.current_file_name}
              >
                {t("ui.current_file")}: {task.current_file_name}
              </span>
            )}
            {/* 细粒度进度：引擎上报的「第几页/共几页」级计数（BabelDOC 页级、
                段落翻译段落级等）。仅运行中且有有效计数时显示。 */}
            {!terminal &&
              task.stage_detail &&
              (task.stage_detail.total ?? 0) > 0 && (
                <span
                  style={{ fontSize: 12, opacity: 0.75 }}
                  title={task.stage_detail.raw_stage}
                >
                  {t("ui.progress_detail", {
                    stage:
                      task.stage_detail.raw_stage ||
                      t(`stage.${task.stage || task.status}`),
                    current: task.stage_detail.current ?? 0,
                    total: task.stage_detail.total,
                  })}
                  {task.stage_detail.unit
                    ? ` (${t(`ui.unit_${task.stage_detail.unit}`)})`
                    : ""}
                </span>
              )}
          </Space>
        </div>

        {/* 阶段步骤条 */}
        <Steps
          size="small"
          aria-label={t("ui.stepbar_aria")}
          current={currentIdx}
          status={stepStatus as "error" | "finish" | "process" | "wait"}
          items={PIPELINE.map((p) => ({ title: t(p.label) }))}
        />

        {/* 控制按钮 */}
        {!terminal && (
          <Space wrap>
            {paused ? (
              <Button icon={<CaretRightOutlined />} onClick={onResume}>
                {t("ui.progress_resume")}
              </Button>
            ) : (
              <Button icon={<PauseOutlined />} onClick={onPause}>
                {t("ui.progress_pause")}
              </Button>
            )}
            <Button icon={<StepForwardOutlined />} onClick={onSkip}>
              {t("ui.progress_skip")}
            </Button>
            <Popconfirm
              title={t("ui.cancel_confirm")}
              okText={t("ui.progress_cancel")}
              cancelText={t("ui.label_n_a")}
              onConfirm={onCancel}
            >
              <Button danger icon={<StopOutlined />}>{t("ui.progress_cancel")}</Button>
            </Popconfirm>
          </Space>
        )}

        {/* 执行日志 */}
        <div>
          <div style={{ fontWeight: 500, marginBottom: 4 }}>{t("ui.progress_log_title")}</div>
          <div
            style={{
              maxHeight: 140,
              overflowY: "auto",
              padding: "6px 10px",
              borderRadius: 6,
              background: "var(--color-surface-raised)",
              border: "1px solid var(--color-border)",
              fontSize: 12,
              lineHeight: 1.6,
            }}
          >
            {logs.length === 0 ? (
              <span style={{ opacity: 0.55 }}>{t("ui.progress_log_idle")}</span>
            ) : (
              logs.map((line, i) => (
                <div key={`${i}-${line.slice(0, 12)}`}>{line}</div>
              ))
            )}
          </div>
        </div>

        {task.message && !logs.includes(task.message) && (
          <Alert type="info" showIcon message={task.message} />
        )}
        {task.error_message && <Alert type="error" showIcon message={task.error_message} />}
        {task.status === "failed" && (
          <Alert type="warning" message={t("ui.retry_hint")} />
        )}
      </Space>
    </section>
  );
}
