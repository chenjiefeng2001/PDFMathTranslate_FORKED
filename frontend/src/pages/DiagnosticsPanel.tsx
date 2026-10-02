/**
 * 诊断深度面板：渲染 TaskState 的结构化诊断/自愈/置信度字段。
 * 与 Gradio 端 diagnostic_panel 的信息对齐（SPA 侧形态：折叠分区 + 键值表）。
 *
 * 此前本文件完全硬编码英文，却挂在 Dashboard 的
 * `<Card title={t("ui.section_diagnostics")}>`（中文标题）之下 —— 中文界面里
 * 出现整块英文正文。现在全部走 i18n；后端原样吐出的键名（report 的字段名、
 * confidence 的 snake_case 指标）保持不译，因为它们是用户回查日志时要搜的
 * 字面量，翻译反而增加检索成本。
 */

import { Collapse, Descriptions, Empty, Space, Table, Tag } from "antd";
import type { TableProps } from "antd";
import { useTranslation } from "react-i18next";
import type { TaskState } from "../api/types";

function asEntries(value: unknown): [string, unknown][] {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    return Object.entries(value as Record<string, unknown>);
  }
  return [];
}

/** 自愈处置明细表。列头是数据字段名，保持英文以便与后端日志对照。 */
function repairColumns(t: (key: string) => string): TableProps["columns"] {
  return [
    { title: t("ui.diag_col_code"), dataIndex: "code", key: "code", width: 120 },
    { title: t("ui.diag_col_page"), dataIndex: "page", key: "page", width: 60 },
    {
      title: t("ui.diag_col_severity"),
      dataIndex: "severity",
      key: "severity",
      width: 90,
    },
    { title: t("ui.diag_col_action"), dataIndex: "action", key: "action", width: 110 },
    { title: t("ui.diag_col_status"), dataIndex: "status", key: "status", width: 90 },
    { title: t("ui.diag_col_message"), dataIndex: "message", key: "message" },
  ];
}

/** 按 pageid 键控的深度报告分区（gate/processor/toc-ir 共用渲染）。 */
function PageKeyedSection({
  data,
  emptyText,
  t,
}: {
  data: Record<string, unknown> | null;
  emptyText: string;
  t: (key: string, opts?: Record<string, unknown>) => string;
}) {
  const pages = Object.entries(data ?? {});
  if (pages.length === 0) {
    return <Empty description={emptyText} image={Empty.PRESENTED_IMAGE_SIMPLE} />;
  }
  return (
    <Collapse
      size="small"
      items={pages.map(([pageId, payload]) => ({
        key: pageId,
        label: t("ui.diag_page", { page: pageId }),
        children: (
          <pre style={{ margin: 0, whiteSpace: "pre-wrap", fontSize: 12 }}>
            {JSON.stringify(payload, null, 2)}
          </pre>
        ),
      }))}
    />
  );
}

export function hasDiagnostics(task: TaskState): boolean {
  /** 该任务是否有任何可展示的诊断数据。

   *  Dashboard 的卡片显隐条件曾与本组件的 `hasAny` 各写一份，且只检查 4 个
   *  字段（diagnostic_summary/report、heal_status、confidence_stats）——
   *  于是只带 gate_verdicts / processor_reports / toc_ir_records / repair_records
   *  的任务会**完全不显示**诊断卡片，而本组件明明为它们准备了渲染器。
   *  导出这一个函数，让两处不可能再分叉。
   */
  return Boolean(
    task.diagnostic_report ||
      task.heal_status ||
      (task.repair_records && task.repair_records.length > 0) ||
      task.confidence_stats ||
      task.gate_verdicts ||
      task.processor_reports ||
      task.toc_ir_records,
  );
}

export default function DiagnosticsPanel({ task }: { task: TaskState }) {
  const { t } = useTranslation();

  if (!hasDiagnostics(task)) {
    return (
      <Empty
        description={t("ui.diag_panel_empty")}
        image={Empty.PRESENTED_IMAGE_SIMPLE}
      />
    );
  }

  const heal = task.heal_status ?? {};
  const conf = task.confidence_stats ?? {};

  const items = [];

  if (task.diagnostic_report) {
    items.push({
      key: "report",
      label: t("ui.diag_report_keys", {
        count: asEntries(task.diagnostic_report).length,
      }),
      children: (
        <Descriptions size="small" column={1} bordered>
          {asEntries(task.diagnostic_report).map(([k, v]) => (
            <Descriptions.Item key={k} label={k}>
              <pre style={{ margin: 0, whiteSpace: "pre-wrap", fontSize: 12 }}>
                {typeof v === "string" ? v : JSON.stringify(v, null, 2)}
              </pre>
            </Descriptions.Item>
          ))}
        </Descriptions>
      ),
    });
  }

  if (task.heal_status) {
    items.push({
      key: "heal",
      label: t("ui.diag_heal"),
      children: (
        <Space direction="vertical">
          <Space wrap size={6}>
            {"ran" in heal && (
              <Tag color={heal.ran ? "green" : "default"}>
                {t("ui.diag_heal_ran")}: {String(heal.ran)}
              </Tag>
            )}
            <Tag>
              {t("ui.diag_heal_iterations")}: {String(heal.iterations ?? "-")}
            </Tag>
            <Tag>
              {t("ui.diag_heal_errors")}: {String(heal.before_errors ?? "?")} →{" "}
              {String(heal.after_errors ?? "?")}
            </Tag>
            <Tag color={heal.improved as boolean ? "green" : "orange"}>
              {t("ui.diag_heal_improved")}: {String(heal.improved ?? "-")}
            </Tag>
          </Space>
        </Space>
      ),
    });
  }

  if (task.repair_records && task.repair_records.length > 0) {
    items.push({
      key: "repairs",
      label: t("ui.diag_repairs_count", { count: task.repair_records.length }),
      children: (
        <Table
          size="small"
          rowKey={(_, i) => String(i)}
          pagination={{ pageSize: 5 }}
          columns={repairColumns(t)}
          dataSource={task.repair_records}
        />
      ),
    });
  }

  if (task.confidence_stats) {
    items.push({
      key: "confidence",
      label: t("ui.diag_confidence"),
      children: (
        <Space wrap size={6}>
          {Object.entries(conf).map(([k, v]) => (
            <Tag key={k}>
              {k}: {typeof v === "number" ? v.toFixed(3) : String(v)}
            </Tag>
          ))}
        </Space>
      ),
    });
  }

  if (task.gate_verdicts) {
    items.push({
      key: "gates",
      label: t("ui.diag_gate_verdicts_pages", {
        count: Object.keys(task.gate_verdicts).length,
      }),
      children: (
        <PageKeyedSection
          data={task.gate_verdicts}
          emptyText={t("ui.diag_no_gate_verdicts")}
          t={t}
        />
      ),
    });
  }

  if (task.processor_reports) {
    items.push({
      key: "processors",
      label: t("ui.diag_processor_reports_pages", {
        count: Object.keys(task.processor_reports).length,
      }),
      children: (
        <PageKeyedSection
          data={task.processor_reports}
          emptyText={t("ui.diag_no_processor_reports")}
          t={t}
        />
      ),
    });
  }

  if (task.toc_ir_records) {
    items.push({
      key: "tocir",
      label: t("ui.diag_toc_ir_pages", {
        count: Object.keys(task.toc_ir_records).length,
      }),
      children: (
        <PageKeyedSection
          data={task.toc_ir_records}
          emptyText={t("ui.diag_no_toc_ir")}
          t={t}
        />
      ),
    });
  }

  return <Collapse size="small" items={items} defaultActiveKey={["report"]} />;
}