/** 仪表盘主页：上传/配置 → 提交 → SSE 进度 → 控制 → 结果下载。 */

import {
  Alert,
  Button,
  Card,
  Descriptions,
  Form,
  Input,
  InputNumber,
  List,
  Progress,
  Select,
  Space,
  Tag,
  Upload,
  UploadFile,
  message,
} from "antd";
import { InboxOutlined, DownloadOutlined } from "@ant-design/icons";
import { useEffect } from "react";
import { useTranslation } from "react-i18next";
import { artifactUrl, getTask, listTasks } from "../api/endpoints";
import type { ResultFile, TaskState } from "../api/types";
import { isTerminal } from "../api/types";
import { useAppStore } from "../stores/taskStore";
import PdfPreview from "./PdfPreview";
import DiagnosticsPanel from "./DiagnosticsPanel";

const LANGS = [
  "auto", "zh-CN", "zh-TW", "en", "ja", "ko",
  "fr", "de", "ru", "es", "it", "pt", "ar", "hi",
];

function stageLabel(t: (k: string) => string, s: TaskState): string {
  const key = s.stage || s.status;
  return t(`stage.${key}`);
}

export default function Dashboard() {
  const { t } = useTranslation();
  const [form] = Form.useForm();
  const engines = useAppStore((s) => s.engines);
  const tasks = useAppStore((s) => s.tasks);
  const activeId = useAppStore((s) => s.activeId);
  const connected = useAppStore((s) => s.connected);
  const submitting = useAppStore((s) => s.submitting);
  const error = useAppStore((s) => s.error);
  const bootstrap = useAppStore((s) => s.bootstrap);
  const submit = useAppStore((s) => s.submit);
  const control = useAppStore((s) => s.control);
  const cancelActive = useAppStore((s) => s.cancelActive);

  useEffect(() => {
    void bootstrap();
  }, [bootstrap]);

  const active: TaskState | null = activeId ? tasks[activeId] ?? null : null;

  async function onSubmit(values: Record<string, unknown>) {
    const files: UploadFile[] = (values.file as UploadFile[]) || [];
    const file = files[0]?.originFileObj ?? null;
    await submit({
      file,
      targetLang: (values.target_lang as string) || "zh-CN",
      sourceLang: (values.source_lang as string) || "auto",
      engine: (values.engine as string) || "google",
      threads: values.threads as number,
      pageRange: (values.page_range as string) || undefined,
      parseEngine: (values.parse_engine as string) || "auto",
      ignoreCache: !!values.ignore_cache,
    });
  }

  async function refreshArtifacts() {
    if (!activeId) return;
    try {
      const full = await getTask(activeId);
      useAppStore.setState((s) => ({
        tasks: { ...s.tasks, [activeId]: full },
      }));
    } catch {
      /* 忽略 */
    }
  }

  useEffect(() => {
    if (active && isTerminal(active.status)) void refreshArtifacts();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active?.status]);

  const artifacts: ResultFile[] = active?.result_files ?? [];

  // 历史任务列表（终态/活动均可点击切换）
  const history: TaskState[] = useAppStore((s) => s.refreshTasks) ? Object.values(tasks).sort((a, b) => b.created_at - a.created_at) : [];

  async function refreshHistory() {
    try {
      await listTasks().then((list) =>
        useAppStore.setState((s) => {
          const merged = { ...s.tasks };
          for (const t of list) merged[t.task_id] = t;
          return { tasks: merged };
        }),
      );
    } catch {
      /* ignore */
    }
  }

  useEffect(() => {
    void refreshHistory();
    const timer = window.setInterval(() => void refreshHistory(), 15000);
    return () => window.clearInterval(timer);
  }, []);

  return (
    <div style={{ maxWidth: 960, margin: "0 auto", padding: 24, display: "grid", gap: 16 }}>
      {error && <Alert type="error" showIcon message={error} />}

      <Card title={t("ui.section_config")}>
        <Form
          form={form}
          layout="vertical"
          onFinish={onSubmit}
          initialValues={{
            target_lang: "zh-CN",
            source_lang: "auto",
            engine: "google",
            threads: 4,
            parse_engine: "auto",
          }}
        >
          <Form.Item label={t("ui.upload_title")} name="file" valuePropName="fileList">
            <Upload.Dragger multiple={false} maxCount={1} beforeUpload={() => false}>
              <p className="ant-upload-drag-icon">
                <InboxOutlined />
              </p>
              <p className="ant-upload-text">{t("ui.upload_drag")}</p>
            </Upload.Dragger>
          </Form.Item>

          <Space wrap size={12}>
            <Form.Item label={t("ui.config_lang_target")} name="target_lang">
              <Select style={{ width: 130 }} options={LANGS.map((l) => ({ value: l, label: l }))} />
            </Form.Item>
            <Form.Item label={t("ui.config_lang_source")} name="source_lang">
              <Select style={{ width: 130 }} options={LANGS.map((l) => ({ value: l, label: l }))} />
            </Form.Item>
            <Form.Item label={t("ui.config_engine")} name="engine">
              <Select
                style={{ width: 220 }}
                showSearch
                options={engines.map((e) => ({ value: e.name, label: e.name }))}
              />
            </Form.Item>
            <Form.Item label="Threads" name="threads">
              <InputNumber min={1} max={32} />
            </Form.Item>
            <Form.Item label="Pages" name="page_range">
              <Input placeholder="e.g. 1-5" style={{ width: 110 }} />
            </Form.Item>
            <Form.Item label="Parse" name="parse_engine">
              <Select
                style={{ width: 120 }}
                options={["auto", "legacy", "babeldoc", "magicpdf"].map((v) => ({ value: v, label: v }))}
              />
            </Form.Item>
          </Space>

          <Button type="primary" htmlType="submit" loading={submitting} block>
            {t("ui.btn_translate")}
          </Button>
        </Form>
      </Card>

      {active && (
        <Card
          title={
            <Space>
              <span>{t("stage.translating")}</span>
              <Tag color={isTerminal(active.status) ? (active.status === "completed" ? "green" : "red") : "blue"}>
                {active.status}
              </Tag>
              {!isTerminal(active.status) && (
                <Tag color={connected ? "cyan" : "orange"}>
                  SSE {connected ? "live" : "reconnecting"}
                </Tag>
              )}
            </Space>
          }
          extra={
            !isTerminal(active.status) && (
              <Space>
                <Button size="small" onClick={() => void control("pause")}>⏸</Button>
                <Button size="small" onClick={() => void control("resume")}>▶️</Button>
                <Button size="small" onClick={() => void control("skip")}>⏭</Button>
                <Button size="small" danger onClick={() => void cancelActive()}>✖</Button>
              </Space>
            )
          }
        >
          <Progress percent={Math.round(active.progress)} status={isTerminal(active.status) ? undefined : "active"} />
          <Descriptions size="small" column={2} style={{ marginTop: 8 }}>
            <Descriptions.Item label="Stage">{stageLabel(t, active)}</Descriptions.Item>
            <Descriptions.Item label="ETA">
              {active.eta > 0 ? `${Math.ceil(active.eta)}s` : "-"}
            </Descriptions.Item>
            {active.total_files > 1 && (
              <>
                <Descriptions.Item label="Files">
                  {active.completed_files}/{active.total_files}
                  {active.failed_files > 0 ? ` (${active.failed_files} failed)` : ""}
                </Descriptions.Item>
                <Descriptions.Item label="Current">{active.current_file_name || "-"}</Descriptions.Item>
              </>
            )}
          </Descriptions>
          {active.message && <div style={{ opacity: 0.7 }}>{active.message}</div>}
          {active.error_message && (
            <Alert type="error" message={active.error_message} style={{ marginTop: 8 }} />
          )}
        </Card>
      )}

      {/* 任务历史（点击切换活动任务） */}
      {history.length > 1 && (
        <Card size="small" title="Tasks">
          <List
            size="small"
            dataSource={history.slice(0, 10)}
            renderItem={(item) => (
              <List.Item
                style={{ cursor: "pointer", fontWeight: item.task_id === activeId ? 700 : 400 }}
                onClick={() => useAppStore.getState().setActive(item.task_id)}
              >
                <Space>
                  <Tag
                    color={
                      isTerminal(item.status)
                        ? item.status === "completed" ? "green" : "red"
                        : "blue"
                    }
                  >
                    {item.status}
                  </Tag>
                  <span>
                    {item.task_id} · {Math.round(item.progress)}% ·{" "}
                    {new Date(item.created_at * 1000).toLocaleTimeString()}
                  </span>
                </Space>
              </List.Item>
            )}
          />
        </Card>
      )}

      {/* 诊断与质量评分（深度面板） */}
      {active && (active.diagnostic_summary || active.diagnostic_report || active.heal_status || active.confidence_stats) && (
        <Card size="small" title="Diagnostics">
          <Space direction="vertical" style={{ width: "100%" }}>
            {active.diagnostic_summary && <div>{active.diagnostic_summary}</div>}
            {active.quality_scores && (
              <Space wrap>
                {Object.entries(active.quality_scores).map(([k, v]) => (
                  <Tag key={k}>
                    {k}: {typeof v === "number" ? v.toFixed(2) : String(v)}
                  </Tag>
                ))}
              </Space>
            )}
            <DiagnosticsPanel task={active} />
          </Space>
        </Card>
      )}

      {artifacts.length > 0 && (
        <Card title={t("ui.result_files")}>
          <Space direction="vertical" style={{ width: "100%" }}>
            {artifacts.map((f, i) => (
              <Button key={i} icon={<DownloadOutlined />} href={artifactUrl(activeId!, i)} target="_blank">
                {f.name || `artifact-${i}`}
              </Button>
            ))}
          </Space>
        </Card>
      )}

      {/* 内嵌 PDF 预览（首个产物） */}
      {artifacts.length > 0 && (
        <Card size="small" title="Preview">
          <PdfPreview url={artifactUrl(activeId!, 0)} />
        </Card>
      )}
    </div>
  );
}
