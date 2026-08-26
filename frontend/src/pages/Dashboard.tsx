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
import { lazy, Suspense, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  artifactUrl,
  getTask,
  listGlossaries,
  listTasks,
  resultZipUrl,
  selftestMagicpdf,
} from "../api/endpoints";
import type { ResultFile, TaskState } from "../api/types";
import { isTerminal } from "../api/types";
import {
  isTauri,
  joinPath,
  pickExistingDirectory,
  pickSavePath,
  writeBytesAt,
} from "../api/nativeSave";
import { useAppStore } from "../stores/taskStore";
import DiagnosticsPanel from "./DiagnosticsPanel";
import ProgressPanel, { statusLabelKey } from "./ProgressPanel";

// pdfjs-dist（主库 ~1MB + worker 1.26MB）仅在预览渲染时才需要；
// 惰性加载把它拆出首屏 bundle。
const PdfPreview = lazy(() => import("./PdfPreview"));

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
  const [previewIndex, setPreviewIndex] = useState(0);
  const [initialOutputDir, setInitialOutputDir] = useState("");

  useEffect(() => {
    let stored = "";
    try {
      stored = window.localStorage.getItem("pdf2zh.outputDir") ?? "";
    } catch {
      /* ignore */
    }
    setInitialOutputDir(stored);
    if (stored) form.setFieldValue("output_dir", stored);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

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

  // 切换任务时预览回到第一个产物。
  useEffect(() => {
    setPreviewIndex(0);
  }, [activeId]);

  async function onSubmit(values: Record<string, unknown>) {
    // 批量提交：fileList 中的所有文件作为一个批量任务发送；
    // 后端按数量自动路由单文件/批量执行器（逐文件进度 + 结果 ZIP）。
    const picked = Array.isArray(values.file) ? values.file : [];
    const blobs = picked
      .map((f: { originFileObj?: File | null }) => f?.originFileObj ?? null)
      .filter((b: File | null): b is File => !!b);
    if (blobs.length === 0 || submitting) return;
    const taskId = await submit({
      files: blobs,
      targetLang: (values.target_lang as string) || "zh-CN",
      sourceLang: (values.source_lang as string) || "auto",
      engine: (values.engine as string) || "google",
      threads: values.threads as number,
      pageRange: (values.page_range as string) || undefined,
      parseEngine: (values.parse_engine as string) || "auto",
      modeChoice: (values.mode_choice as string) || "auto",
      ocrMode: (values.ocr_mode as string) || "auto",
      backend: (values.backend as string) || "auto",
      outputDir: ((values.output_dir as string) || "").trim(),
      ignoreCache: !!values.ignore_cache,
      glossaryNames: (values.glossary_names as string[]) || [],
    });
    if (taskId) {
      // 任务已入列：清空待提交队列，避免同一文件被重复提交；
      // 输出目录跨会话记忆。
      form.setFieldValue("file", []);
      try {
        const outDir = ((values.output_dir as string) || "").trim();
        if (outDir) window.localStorage.setItem("pdf2zh.outputDir", outDir);
      } catch {
        /* ignore */
      }
    }
  }

  const artifacts: ResultFile[] = active?.result_files ?? [];
  const pickedFiles = (Form.useWatch("file", form) ?? []) as {
    name?: string;
  }[];
  const selectedCount = pickedFiles.length;
  const selectedNames = pickedFiles
    .map((f) => f.name)
    .filter((n): n is string => !!n);

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
          backend: "auto",
          output_dir: initialOutputDir,
        }}
      >
        {/* 文件上传（支持多选/拖入多个，批量翻译） */}
        <Card
          title={t("ui.section_upload")}
          extra={
            selectedCount > 0 ? (
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                {t("ui.upload_selected_prefix")}
                {selectedNames.length > 0 ? selectedNames.join("、") : `${selectedCount} file(s)`}
              </Typography.Text>
            ) : undefined
          }
        >
          <Form.Item name="file" valuePropName="fileList" getValueFromEvent={(e) => e?.fileList}>
            <Upload.Dragger
              multiple
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
                    <Space wrap size={12}>
                      <Form.Item label={t("ui.config_backend")} name="backend" tooltip={t("ui.config_backend_info")}>
                        <Select
                          style={{ width: 200 }}
                          options={[
                            { value: "auto", label: t("ui.config_backend_auto") },
                            { value: "cpu", label: t("ui.config_backend_cpu") },
                            { value: "cuda", label: t("ui.config_backend_cuda") },
                            { value: "dml", label: t("ui.config_backend_dml") },
                          ]}
                        />
                      </Form.Item>
                      <Form.Item
                        label={t("ui.config_output_dir")}
                        name="output_dir"
                        tooltip={t("ui.config_output_dir_hint")}
                        style={{ minWidth: 320, flex: 1 }}
                      >
                        <Input placeholder={t("ui.label_n_a")} allowClear />
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

          <Button
            type="primary"
            htmlType="submit"
            loading={submitting}
            disabled={selectedCount === 0}
            block
            size="large"
          >
            {selectedCount > 1
              ? `${t("ui.progress_translate")} · ${t("ui.batch_count", { count: selectedCount })}`
              : t("ui.progress_translate")}
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
      {activeId && (
        <Card title={t("ui.section_preview")}>
          {/* 批量失败明细 */}
          {(active?.failed_files ?? 0) > 0 && (
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: 12 }}
              message={t("ui.batch_failed_files", { count: active?.failed_files ?? 0 })}
              description={
                <List
                  size="small"
                  dataSource={active?.file_failures ?? []}
                  renderItem={(item) => (
                    <List.Item style={{ paddingInline: 0 }}>
                      <Typography.Text delete style={{ fontSize: 12 }}>
                        {item.file}
                      </Typography.Text>
                      <Typography.Text type="secondary" style={{ fontSize: 12, marginLeft: 8 }}>
                        {item.error}
                      </Typography.Text>
                    </List.Item>
                  )}
                />
              }
            />
          )}
          {artifacts.length > 0 ? (
            <Space direction="vertical" size={12} style={{ width: "100%" }}>
              <div>
                <Typography.Text strong style={{ display: "block", marginBottom: 8 }}>
                  {t("ui.preview_output")}
                </Typography.Text>
                <Space wrap size={8}>
                  {artifacts.map((f, i) => (
                    <ArtifactDownload
                      key={i}
                      name={f.name || `artifact-${i}`}
                      url={artifactUrl(activeId, i)}
                    />
                  ))}
                  {artifacts.length > 1 && (
                    <BatchSaveToFolder
                      items={artifacts.map((f, i) => ({
                        name: f.name || `artifact-${i}`,
                        url: artifactUrl(activeId, i),
                      }))}
                    />
                  )}
                  {artifacts.length > 0 && (
                    <ArtifactDownload
                      name={t("ui.download_all_zip")}
                      url={resultZipUrl(activeId)}
                    />
                  )}
                </Space>
              </div>
              <div>
              <Typography.Text strong style={{ display: "block", marginBottom: 8 }}>
                {t("ui.preview_title")}
              </Typography.Text>
              <Space style={{ marginBottom: 8 }} wrap>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  {t("ui.preview_pick")}:
                </Typography.Text>
                <Select
                  size="small"
                  style={{ minWidth: 240 }}
                  value={previewIndex}
                  onChange={(v) => setPreviewIndex(v)}
                  options={artifacts.map((f, i) => ({
                    value: i,
                    label: f.name || `artifact-${i}`,
                  }))}
                />
              </Space>
              <Suspense
                fallback={
                  <div style={{ textAlign: "center", padding: 24, opacity: 0.6 }}>
                    loading…
                  </div>
                }
              >
                <PdfPreview key={previewIndex} url={artifactUrl(activeId, previewIndex)} />
              </Suspense>
              </div>
            </Space>
          ) : null}
        </Card>
      )}
    </div>
  );
}

/** 抓取产物为 Blob（大小校验），供原生写盘与锚点两条路径共用。 */
async function fetchArtifactBlob(url: string): Promise<Blob> {
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  const blob = await resp.blob();
  if (blob.size === 0) throw new Error("empty file");
  return blob;
}

/** 浏览器回退：objectURL + 锚点点击（落到 webview 默认下载目录）。 */
function saveViaAnchor(blob: Blob, name: string): void {
  const objectUrl = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = objectUrl;
  anchor.download = name;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(objectUrl), 60_000);
}

/** 单个产物下载：桌面壳走系统「另存为」对话框 + 写盘命令；纯浏览器回退锚点。 */
function ArtifactDownload({ name, url }: { name: string; url: string }) {
  const { t } = useTranslation();
  const [state, setState] = useState<"idle" | "busy" | "done" | "error">("idle");
  const [detail, setDetail] = useState("");

  async function download() {
    setState("busy");
    setDetail("");
    try {
      const blob = await fetchArtifactBlob(url);
      const sizeMb = `${(blob.size / 1048576).toFixed(2)} MB`;
      if (isTauri()) {
        const path = await pickSavePath(name);
        if (!path) {
          setState("idle"); // 用户取消，不视为失败
          return;
        }
        await writeBytesAt(path, new Uint8Array(await blob.arrayBuffer()));
        setDetail(`${sizeMb} · ${path}`);
      } else {
        saveViaAnchor(blob, name);
        setDetail(sizeMb);
      }
      setState("done");
    } catch (err) {
      setDetail(String(err));
      setState("error");
    }
  }

  return (
    <Space size={6}>
      <Button
        icon={<DownloadOutlined />}
        loading={state === "busy"}
        onClick={() => void download()}
      >
        {name}
      </Button>
      {state === "done" && (
        <Tag color="green">
          {t("ui.download_done")}
          {detail ? ` · ${detail}` : ""}
        </Tag>
      )}
      {state === "error" && (
        <Tag color="red">
          {t("ui.download_failed")}
          {detail ? ` · ${detail}` : ""}
        </Tag>
      )}
    </Space>
  );
}

/**
 * 批量保存到指定文件夹（桌面壳专属）：一次系统「选择文件夹」对话框选定
 * 目标目录后，逐个抓取全部产物并按原名写入该目录。部分失败不中断其余
 * 文件，结束时以明细回显成功/失败清单；取消选夹静默复位。
 */
function BatchSaveToFolder({ items }: { items: { name: string; url: string }[] }) {
  const { t } = useTranslation();
  const [state, setState] = useState<"idle" | "busy" | "done" | "error">("idle");
  const [detail, setDetail] = useState("");

  async function run() {
    const dir = await pickExistingDirectory(t("ui.download_folder_pick_title"));
    if (!dir) return; // 用户取消
    setState("busy");
    setDetail("");
    const failed: string[] = [];
    let savedPath = "";
    for (const item of items) {
      try {
        const blob = await fetchArtifactBlob(item.url);
        const target = joinPath(dir, item.name);
        await writeBytesAt(target, new Uint8Array(await blob.arrayBuffer()));
        savedPath = dir;
      } catch (err) {
        failed.push(`${item.name}: ${String(err)}`);
      }
    }
    setDetail(
      `${items.length - failed.length}/${items.length}${savedPath ? ` · ${savedPath}` : ""}` +
        (failed.length > 0 ? ` · ${failed.join("; ")}` : ""),
    );
    setState(failed.length === 0 ? "done" : "error");
  }

  return (
    <Space size={6}>
      <Button
        icon={<DownloadOutlined />}
        loading={state === "busy"}
        disabled={!isTauri()}
        onClick={() => void run()}
      >
        {t("ui.download_folder_batch", { count: items.length })}
      </Button>
      {state === "done" && (
        <Tag color="green">
          {t("ui.download_done")}
          {detail ? ` · ${detail}` : ""}
        </Tag>
      )}
      {state === "error" && (
        <Tag color="red">
          {t("ui.download_failed")}
          {detail ? ` · ${detail}` : ""}
        </Tag>
      )}
    </Space>
  );
}

