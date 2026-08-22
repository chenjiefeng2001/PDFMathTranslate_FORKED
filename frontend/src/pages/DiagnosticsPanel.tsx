/**
 * 诊断深度面板：渲染 TaskState 的结构化诊断/自愈/置信度字段。
 * 与 Gradio 端 diagnostic_panel 的信息对齐（SPA 侧形态：折叠分区 + 键值表）。
 */

import { Collapse, Descriptions, Empty, Space, Table, Tag } from "antd";
import type { TaskState } from "../api/types";

function asEntries(value: unknown): [string, unknown][] {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    return Object.entries(value as Record<string, unknown>);
  }
  return [];
}

/** 自愈处置明细表 */
const repairColumns = [
  { title: "code", dataIndex: "code", key: "code", width: 120 },
  { title: "page", dataIndex: "page", key: "page", width: 60 },
  { title: "severity", dataIndex: "severity", key: "severity", width: 90 },
  { title: "action", dataIndex: "action", key: "action", width: 110 },
  { title: "status", dataIndex: "status", key: "status", width: 90 },
  { title: "message", dataIndex: "message", key: "message" },
];

/** 按 pageid 键控的深度报告分区（gate/processor/toc-ir 共用渲染）。 */
function PageKeyedSection({
  data,
  emptyText,
}: {
  data: Record<string, unknown> | null;
  emptyText: string;
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
        label: `Page ${pageId}`,
        children: (
          <pre style={{ margin: 0, whiteSpace: "pre-wrap", fontSize: 12 }}>
            {JSON.stringify(payload, null, 2)}
          </pre>
        ),
      }))}
    />
  );
}

export default function DiagnosticsPanel({ task }: { task: TaskState }) {
  const hasAny =
    task.diagnostic_report ||
    task.heal_status ||
    (task.repair_records && task.repair_records.length > 0) ||
    task.confidence_stats ||
    task.gate_verdicts ||
    task.processor_reports ||
    task.toc_ir_records;

  if (!hasAny) {
    return <Empty description="No diagnostics yet" image={Empty.PRESENTED_IMAGE_SIMPLE} />;
  }

  const heal = task.heal_status ?? {};
  const conf = task.confidence_stats ?? {};

  const items = [];

  if (task.diagnostic_report) {
    items.push({
      key: "report",
      label: `Diagnostic Report (${asEntries(task.diagnostic_report).length} keys)`,
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
      label: "Self-Heal",
      children: (
        <Space direction="vertical">
          <Space wrap size={6}>
            {"ran" in heal && (
              <Tag color={heal.ran ? "green" : "default"}>ran: {String(heal.ran)}</Tag>
            )}
            <Tag>iterations: {String(heal.iterations ?? "-")}</Tag>
            <Tag>errors: {String(heal.before_errors ?? "?")} → {String(heal.after_errors ?? "?")}</Tag>
            <Tag color={(heal.improved as boolean) ? "green" : "orange"}>
              improved: {String(heal.improved ?? "-")}
            </Tag>
          </Space>
        </Space>
      ),
    });
  }

  if (task.repair_records && task.repair_records.length > 0) {
    items.push({
      key: "repairs",
      label: `Repair Records (${task.repair_records.length})`,
      children: (
        <Table
          size="small"
          rowKey={(_, i) => String(i)}
          pagination={{ pageSize: 5 }}
          columns={repairColumns}
          dataSource={task.repair_records}
        />
      ),
    });
  }

  if (task.confidence_stats) {
    items.push({
      key: "confidence",
      label: "Confidence",
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
      label: `Gate Verdicts (${Object.keys(task.gate_verdicts).length} pages)`,
      children: <PageKeyedSection data={task.gate_verdicts} emptyText="No gate verdicts" />,
    });
  }

  if (task.processor_reports) {
    items.push({
      key: "processors",
      label: `Processor Reports (${Object.keys(task.processor_reports).length} pages)`,
      children: <PageKeyedSection data={task.processor_reports} emptyText="No processor reports" />,
    });
  }

  if (task.toc_ir_records) {
    items.push({
      key: "tocir",
      label: `TOC IR (${Object.keys(task.toc_ir_records).length} pages)`,
      children: <PageKeyedSection data={task.toc_ir_records} emptyText="No TOC IR records" />,
    });
  }

  return <Collapse size="small" items={items} defaultActiveKey={["report"]} />;
}
