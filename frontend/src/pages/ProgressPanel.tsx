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
 * 步骤条边界（百分比累计），与后端 `runtime_service._STAGE_WEIGHTS` 对齐。
 *
 * 后端 7 个阶段的累计区间：
 *   parsing 0-10 | analyzing 10-30 | planning 30-40 | translating 40-70 |
 *   layouting 70-85 | rendering 85-95 | evaluating 95-100
 *
 * 这里合并为 5 步显示：「解析」吸收 parsing+analyzing+planning（0→40），
 * 「渲染」吸收 rendering+evaluating（85→100）。`pending` 的 2% 是进入
 * 第一个真实阶段前的排队余量，与后端无对应阶段。
 *
 * 旧值把 layouting 收在 92 —— 该数字既非任何后端边界，也与本文件顶部注释
 * 自称的 85 矛盾：步骤条会在排版尚未结束时就跳到「渲染」，看起来比实际进度
 * 领先。改动本表前请同步 tests/test_frontend_stage_bounds.py（该测试直接读取
 * 本常量并与后端 _STAGE_BOUNDS 比对）。
 */
export const STAGE_PCT_END: Readonly<Record<string, number>> = Object.freeze({
  pending: 2,
  parsing: 40,
  translating: 70,
  layouting: 85,
  rendering: 100,
});

const PIPELINE: { key: string; label: string; pctEnd: number }[] = [
  { key: "pending", label: "stage.pending", pctEnd: STAGE_PCT_END.pending },
  { key: "parsing", label: "stage.parsing", pctEnd: STAGE_PCT_END.parsing },
  {
    key: "translating",
    label: "stage.translating",
    pctEnd: STAGE_PCT_END.translating,
  },
  { key: "layouting", label: "stage.layouting", pctEnd: STAGE_PCT_END.layouting },
  { key: "rendering", label: "stage.rendering", pctEnd: STAGE_PCT_END.rendering },
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
  const cancelled = task.status === "cancelled";
  // 「任务完成但有文件失败」——批量任务里 failed_files 是文件级计数，与
  // 任务级 status 无关。此前只看 status，于是 10 个文件挂 3 个时进度环
  // 满绿、标签写「完成」、五个步骤全打勾，唯一的线索是行尾灰色括号。
  const partial =
    terminal && !failed && !cancelled && (task.failed_files ?? 0) > 0;

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
    : cancelled
      ? "error"
      : partial
        ? "finish"
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
                : cancelled
                  ? "normal"
                  : partial
                    ? "normal"
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
                    partial
                      ? "orange"
                      : task.status === "completed"
                        ? "green"
                        : failed || cancelled
                          ? "red"
                          : paused
                            ? "orange"
                            : "blue"
                  }
                >
                  {partial
                    ? t("ui.status_completed_partial", {
                        count: task.failed_files,
                      })
                    : t(statusKey)}
                </Tag>
              ) : (
                <Tag>{task.status}</Tag>
              )}
              {!terminal && (
                /* 断线时进度会停在最后一个已知值（百分比是单调钳制的），
                   所以「橙色 + …」会被读成「正在连接」而不是「已断开、
                   进度可能滞后」。改为红色并直说含义。 */
                <Tag color={connected ? "cyan" : "red"}>
                  {connected
                    ? t("ui.sse_live")
                    : t("ui.sse_disconnected")}
                </Tag>
              )}
              {/* defaultValue 兜底：i18n 未设 parseMissingKeyHandler，缺键会
                  直接把 key 本身当文案渲染出来。后端若新增 stage/status 值，
                  这里退化为显示原始值，而不是 "stage.whatever"。 */}
              <Tag>
                {t("ui.stage_label")}:{" "}
                {t(`stage.${task.stage || task.status}`, {
                  defaultValue: task.stage || task.status,
                })}
              </Tag>
              {task.parse_engine && (
                <Tag color="geekblue">
                  {t("ui.engine_label_magicpdf")}: {task.parse_engine}
                </Tag>
              )}
            </Space>
            {/* 部分失败必须在本卡片内可见：下面那张 batch_failed_files
                提示位于「预览与下载」卡片，用户盯着执行状态时看不到。 */}
            {partial && (
              <Alert
                type="warning"
                showIcon
                message={t("ui.batch_failed_files", { count: task.failed_files })}
              />
            )}
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
                      t(`stage.${task.stage || task.status}`, {
                        defaultValue: task.stage || task.status,
                      }),
                    current: task.stage_detail.current ?? 0,
                    total: task.stage_detail.total,
                  })}
                  {/* unit 由后端给出，magicpdf 会发 "component"；缺键时退回
                      原始值而不是把 "ui.unit_component" 打到界面上。 */}
                  {task.stage_detail.unit
                    ? ` (${t(`ui.unit_${task.stage_detail.unit}`, {
                        defaultValue: task.stage_detail.unit,
                      })})`
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
              cancelText={t("ui.label_cancel")}
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
