/** 仪表盘主页：上传 → 配置（基础+高级） → 提交 → 执行状态（ProgressPanel） → 结果下载与预览。 */

import {
  Alert,
  Button,
  Card,
  Collapse,
  Form,
  Input,
  InputNumber,
  List,
  Progress as MiniProgress,
  Select,
  Space,
  Switch,
  Tag,
  Upload,
  Typography,
} from "antd";
import { DownloadOutlined, InboxOutlined } from "@ant-design/icons";
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  artifactUrl,
  getTask,
  listGlossaries,
  listTasks,
  selftestMagicpdf,
} from "../api/endpoints";
import type { ResultFile, TaskState } from "../api/types";
import { isTerminal } from "../api/types";
import { useAppStore } from "../stores/taskStore";
import PdfPreview from "./PdfPreview";
import DiagnosticsPanel from "./DiagnosticsPanel";
import ProgressPanel, { statusLabelKey } from "./ProgressPanel";

const LANGS = [
  "auto", "zh-CN", "zh-TW", "en", "ja", "ko",
  "fr", "de", "ru", "es", "it", "pt", "ar", "hi",
];

function StatusTag({ status }: { status: string }) {
  const { t } = useTranslation();
  const key = statusLabelKey(status);
  const color =
    status === "completed"
      ? "green"
      : status === "failed" || status === "cancelled"
        ? "red"
        : status === "paused"
          ? "orange"
          : "blue";
  return <Tag color={color}>{key ? t(key) : status}</Tag>;
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
  const logs = useAppStore((s) => s.logs);
  const bootstrap = useAppStore((s) => s.bootstrap);
  const submit = useAppStore((s) => s.submit);
  const control = useAppStore((s) => s.control);
  const cancelActive = useAppStore((s) => s.cancelActive);

  const [glossaryOptions, setGlossaryOptions] = useState<
    { value: string; label: string }[]
  >([]);
  const [magicpdfOk, setMagicpdfOk] = useState<boolean | null>(null);

  useEffect(() => {
    listGlossaries()
      .then((items) =>
        setGlossaryOptions(
          items
            .filter((g) => !g.error)
            .map((g) => ({
              value: g.name,
              label: `${g.name} · ${g.entries ?? 0}`,
            })),
        ),
      )
      .catch(() => {
        /* 服务未就绪时静默 */
      });
    selftestMagicpdf()
      .then((r) => setMagicpdfOk(r.ok))
      .catch(() => setMagicpdfOk(null));
  }, []);

  useEffect(() => {
    void bootstrap();
  }, [bootstrap]);

  const active: TaskState | null = activeId ? tasks[activeId] ?? null : null;

  // 历史任务：新任务在前；至少一条即展示。
  const history: TaskState[] = useMemo(
    () =>
      Object.values(tasks).sort(
        (a, b) => (b.created_at ?? 0) - (a.created_at ?? 0),
      ),
    [tasks],
  );

  useEffect(() => {
    void refreshHistory();
    const timer = window.setInterval(() => void refreshHistory(), 15000);
    return () => window.clearInterval(timer);
  }, []);

  async function refreshHistory() {
    try {
      const list = await listTasks();
      useAppStore.setState((s) => {
        const merged = { ...s.tasks };
        for (const task of list) merged[task.task_id] = task;
        return { tasks: merged };
      });
    } catch {
      /* ignore */
    }
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

  async function onSubmit(values: Record<string, unknown>) {
    const file = values.file;
    const raw = Array.isArray(file) ? file[0] : undefined;
    const blob = raw?.originFileObj ?? null;
    if (!blob) return;
    await submit({
      file: blob,
      targetLang: (values.target_lang as string) || "zh-CN",
      sourceLang: (values.source_lang as string) || "auto",
      engine: (values.engine as string) || "google",
      threads: values.threads as number,
      pageRange: (values.page_range as string) || undefined,
      parseEngine: (values.parse_engine as string) || "auto",
      modeChoice: (values.mode_choice as string) || "auto",
      ocrMode: (values.ocr_mode as string) || "auto",
      ignoreCache: !!values.ignore_cache,
      glossaryNames: (values.glossary_names as string[]) || [],
    });
  }

  const artifacts: ResultFile[] = active?.result_files ?? [];
  const selectedName = Form.useWatch("file", form)?.[0]?.name as string | undefined;

  return (
    <div style={{ maxWidth: 960, margin: "0 auto", padding: 24, display: "grid", gap: 16 }}>
      {error && <Alert type="error" showIcon message={error} />}

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
          mode_choice: "auto",
          ocr_mode: "auto",
        }}
      >
        {/* 文件上传 */}
        <Card title={t("ui.section_upload")}>
          <Form.Item name="file" valuePropName="fileList" getValueFromEvent={(e) => e?.fileList}>
            <Upload.Dragger
              multiple={false}
              maxCount={1}
              accept=".pdf,.docx"
              beforeUpload={() => false}
              disabled={submitting}
            >
              <p className="ant-upload-drag-icon">
                <InboxOutlined />
              </p>
              <p className="ant-upload-text">{t("ui.upload_label_file")}</p>
              <p className="ant-upload-hint">{t("ui.upload_formats_hint")}</p>
            </Upload.Dragger>
          </Form.Item>
        </Card>

        {/* 翻译配置 */}
        <Card title={t("ui.section_config")} style={{ marginTop: 16 }}>
          <Space wrap size={12}>
            <Form.Item label={t("ui.config_lang_target")} name="target_lang">
              <Select style={{ width: 140 }} options={LANGS.map((l) => ({ value: l, label: l }))} />
            </Form.Item>
            <Form.Item label={t("ui.config_lang_source")} name="source_lang">
              <Select style={{ width: 140 }} options={LANGS.map((l) => ({ value: l, label: l }))} />
            </Form.Item>
            <Form.Item label={t("ui.config_engine")} name="engine">
              <Select
                style={{ width: 240 }}
                showSearch
                optionFilterProp="value"
                options={engines.map((e) => ({
                  value: e.name,
                  label: e.label && e.label !== e.name ? `${e.label} (${e.name})` : e.name,
                }))}
              />
            </Form.Item>
          </Space>

          <Collapse
            ghost
            items={[
              {
                key: "advanced",
                label: t("ui.config_advanced"),
                children: (
                  <Space direction="vertical" size={12} style={{ width: "100%" }}>
                    <Space wrap size={12}>
                      <Form.Item label={t("ui.config_threads")} name="threads">
                        <InputNumber min={1} max={32} />
                      </Form.Item>
                      <Form.Item label={t("ui.config_pages")} name="page_range">
                        <Input placeholder="1-5, 8" style={{ width: 120 }} allowClear />
                      </Form.Item>
                      <Form.Item label={t("ui.config_parse_engine")} name="parse_engine" tooltip={t("ui.config_parse_engine_info")}>
                        <Select
                          style={{ width: 180 }}
                          options={[
                            { value: "auto", label: t("ui.config_parse_engine_auto") },
                            { value: "legacy", label: t("ui.config_parse_engine_legacy") },
                            { value: "babeldoc", label: t("ui.config_parse_engine_babeldoc") },
                            {
                              value: "magicpdf",
                              disabled: magicpdfOk === false,
                              label:
                                t("ui.config_parse_engine_magicpdf") +
                                (magicpdfOk === false
                                  ? ` · ${t("ui.parse_engine_unavailable")}`
                                  : ""),
                            },
                          ]}
                        />
                      </Form.Item>
                    </Space>
                    <Space wrap size={12}>
                      <Form.Item label={t("ui.config_mode")} name="mode_choice" tooltip={t("ui.config_mode_info")}>
                        <Select
                          style={{ width: 160 }}
                          options={[
                            { value: "auto", label: t("ui.config_mode_auto") },
                            { value: "quick", label: t("ui.config_mode_quick") },
                            { value: "standard", label: t("ui.config_mode_standard") },
                            { value: "quality", label: t("ui.config_mode_quality") },
                            { value: "babeldoc", label: t("ui.config_mode_babeldoc") },
                          ]}
                        />
                      </Form.Item>
                      <Form.Item label={t("ui.config_ocr_mode")} name="ocr_mode" tooltip={t("ui.config_ocr_mode_info")}>
                        <Select
                          style={{ width: 220 }}
                          options={[
                            { value: "auto", label: t("ui.config_ocr_mode_auto") },
                            { value: "on", label: t("ui.config_ocr_mode_on") },
                            { value: "off", label: t("ui.config_ocr_mode_off") },
                          ]}
                        />
                      </Form.Item>
                      <Form.Item label={t("ui.config_ignore_cache")} name="ignore_cache" valuePropName="checked">
                        <Switch />
                      </Form.Item>
                    </Space>
                    {glossaryOptions.length > 0 && (
                      <Form.Item
                        label={t("ui.config_glossary_files")}
                        name="glossary_names"
                        tooltip={t("ui.config_glossary_files_info")}
                      >
                        <Select
                          mode="multiple"
                          allowClear
                          placeholder={t("ui.settings_glossary_empty")}
                          options={glossaryOptions}
                          style={{ width: "100%" }}
                        />
                      </Form.Item>
                    )}
                  </Space>
                ),
              },
            ]}
          />

          <Button type="primary" htmlType="submit" loading={submitting} disabled={!selectedName} block size="large">
            {t("ui.progress_translate")}
          </Button>
        </Card>
      </Form>

      {/* 执行状态（进度条 / 阶段 / 控制按钮 / 日志） */}
      {active && (
        <Card title={t("ui.section_progress")}>
          <ProgressPanel
            task={active}
            connected={connected}
            logs={logs[active.task_id] ?? []}
            onPause={() => void control("pause")}
            onResume={() => void control("resume")}
            onSkip={() => void control("skip")}
            onCancel={() => void cancelActive()}
          />
        </Card>
      )}

      {/* 任务历史 */}
      {history.length > 0 && (
        <Card size="small" title={t("ui.task_history")}>
          <List
            size="small"
            dataSource={history.slice(0, 10)}
            renderItem={(item) => (
              <List.Item
                style={{
                  cursor: "pointer",
                  background: item.task_id === activeId ? "var(--color-accent-soft)" : undefined,
                  borderRadius: 6,
                  paddingInline: 8,
                }}
                onClick={() => useAppStore.getState().setActive(item.task_id)}
              >
                <Space wrap size={10} style={{ width: "100%" }}>
                  <StatusTag status={item.status} />
                  <span style={{ fontFamily: "var(--text-font-mono)", fontSize: 12 }}>{item.task_id}</span>
                  <MiniProgress
                    percent={Math.round(item.progress)}
                    size="small"
                    style={{ width: 110, margin: 0 }}
                    showInfo={false}
                  />
                  <span style={{ fontSize: 12, opacity: 0.7 }}>
                    {Math.round(item.progress)}%
                  </span>
                  <span style={{ fontSize: 12, opacity: 0.55 }}>
                    {new Date(item.created_at * 1000).toLocaleString()}
                  </span>
                </Space>
              </List.Item>
            )}
          />
        </Card>
      )}

      {/* 诊断与质量评分（深度面板） */}
      {active && (active.diagnostic_summary || active.diagnostic_report || active.heal_status || active.confidence_stats) && (
        <Card size="small" title={t("ui.section_diagnostics")}>
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

      {/* 预览与下载 */}
      {artifacts.length > 0 && (
        <Card title={t("ui.section_preview")}>
          <Space direction="vertical" size={12} style={{ width: "100%" }}>
            <div>
              <Typography.Text strong style={{ display: "block", marginBottom: 8 }}>
                {t("ui.preview_output")}
              </Typography.Text>
              <Space wrap>
                {artifacts.map((f, i) => (
                  <Button key={i} icon={<DownloadOutlined />} href={artifactUrl(activeId!, i)} target="_blank">
                    {f.name || `artifact-${i}`}
                  </Button>
                ))}
              </Space>
            </div>
            <div>
              <Typography.Text strong style={{ display: "block", marginBottom: 8 }}>
                {t("ui.preview_title")}
              </Typography.Text>
              <PdfPreview url={artifactUrl(activeId!, 0)} />
            </div>
          </Space>
        </Card>
      )}
    </div>
  );
}

