/**
 * 设置抽屉：外观（主题/语言）+ 翻译引擎凭据状态 + 专业词表库管理 +
 * 高级（API 地址覆盖）。数据源均为现有后端端点（/api/engines、/api/glossaries）。
 */

import {
  Alert,
  Button,
  Divider,
  Drawer,
  Input,
  List,
  Select,
  Space,
  Switch,
  Tag,
  Typography,
  Upload,
  message,
} from "antd";
import { UploadOutlined, DownloadOutlined } from "@ant-design/icons";
import { useEffect, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import {
  glossaryDownloadUrl,
  getDoclayoutModelStatus,
  downloadDoclayoutModel,
  getEngines,
  getEngineEnvs,
  importGlossary,
  listGlossaries,
  selftestMagicpdf,
  setupMineru,
  getMineruSetupStatus,
  updateEngineEnvs,
  type DoclayoutModelStatus,
  type EngineEnvStatus,
} from "../api/endpoints";
import type { EngineInfo, GlossaryInfo } from "../api/types";
import { useSettingsStore } from "../stores/settingsStore";
import { switchLang, type Lang } from "../i18n";

const STORAGE_API_BASE = "pdf2zh.apiBase";

interface Props {
  open: boolean;
  onClose(): void;
}

function AppearanceSection() {
  const { t, i18n } = useTranslation();
  const dark = useSettingsStore((s) => s.dark);
  const toggleTheme = useSettingsStore((s) => s.toggleTheme);
  return (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <span>{t("ui.settings_theme")}</span>
        <Switch
          checked={dark}
          onChange={() => toggleTheme()}
          checkedChildren={t("ui.theme_dark_label")}
          unCheckedChildren={t("ui.theme_light_label")}
        />
      </div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <span>{t("ui.settings_language")}</span>
        <Select
          value={(i18n.language as Lang) || "zh-CN"}
          style={{ width: 140 }}
          onChange={(lng) => switchLang(lng as Lang)}
          options={[
            { value: "zh-CN", label: "简体中文" },
            { value: "en", label: "English" },
          ]}
        />
      </div>
    </Space>
  );
}

function EnginesSection({ active }: { active: boolean }) {
  const { t } = useTranslation();
  const [engines, setEngines] = useState<EngineInfo[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);

  useEffect(() => {
    if (!active) return;
    let cancelled = false;
    getEngines()
      .then((list) => {
        if (!cancelled) setEngines(list);
      })
      .catch(() => {
        /* 服务未就绪时静默 */
      });
    return () => {
      cancelled = true;
    };
  }, [active, refreshKey]);

  // 仅展示需要凭据的引擎；免凭据引擎（google/bing 等）无需设置。
  const credEngines = engines.filter((e) => e.envs.length > 0);
  const current =
    credEngines.find((e) => e.name === selected) ?? credEngines[0] ?? null;

  if (engines.length === 0) {
    return <Typography.Text type="secondary">{t("ui.waiting_task")}</Typography.Text>;
  }

  return (
    <Space direction="vertical" size={10} style={{ width: "100%" }}>
      <Select
        style={{ width: "100%" }}
        placeholder={t("ui.settings_engines")}
        value={current?.name}
        onChange={(name) => setSelected(name)}
        showSearch
        optionFilterProp="value"
        options={credEngines.map((e) => ({
          value: e.name,
          label:
            e.label && e.label !== e.name ? `${e.label} (${e.name})` : e.name,
        }))}
      />
      {current && (
        <EngineCredentialForm
          key={current.name}
          engine={current}
          onSaved={() => setRefreshKey((k) => k + 1)}
        />
      )}
    </Space>
  );
}

/** 单引擎凭据编辑：脱敏回显 + 覆盖输入 + 显式清除，仅提交有变更的键。 */
function EngineCredentialForm({
  engine,
  onSaved,
}: {
  engine: EngineInfo;
  onSaved(): void;
}) {
  const { t } = useTranslation();
  const [status, setStatus] = useState<EngineEnvStatus[]>([]);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [cleared, setCleared] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    let cancelled = false;
    getEngineEnvs(engine.name)
      .then((res) => {
        if (!cancelled) setStatus(res.envs);
      })
      .catch(() => {
        /* ignore */
      });
    return () => {
      cancelled = true;
    };
  }, [engine.name]);

  function draftOf(key: string): string {
    return drafts[key] ?? "";
  }

  function isCleared(key: string): boolean {
    return cleared.includes(key);
  }

  function setDraft(key: string, value: string) {
    setDrafts((d) => ({ ...d, [key]: value }));
    setCleared((c) => c.filter((k) => k !== key));
  }

  function markClear(key: string) {
    setCleared((c) => (c.includes(key) ? c : [...c, key]));
    setDrafts((d) => ({ ...d, [key]: "" }));
  }

  async function save() {
    const payload: Record<string, string> = {};
    for (const env of status) {
      if (isCleared(env.key)) payload[env.key] = "";
      else {
        const v = draftOf(env.key).trim();
        if (v) payload[env.key] = v;
      }
    }
    if (Object.keys(payload).length === 0) return;
    setSaving(true);
    try {
      const res = await updateEngineEnvs(engine.name, payload);
      setStatus(res.envs);
      setDrafts({});
      setCleared([]);
      onSaved();
      message.success(t("ui.settings_credentials_saved"));
    } catch (err) {
      message.error(String(err));
    } finally {
      setSaving(false);
    }
  }

  const dirty =
    Object.entries(drafts).some(([, v]) => v.trim()) || cleared.length > 0;

  return (
    <Space direction="vertical" size={8} style={{ width: "100%" }}>
      <Typography.Paragraph type="secondary" style={{ marginBottom: 0, fontSize: 12 }}>
        {t("ui.settings_engines_hint")} {t("ui.settings_credentials_hint")}
      </Typography.Paragraph>
      {status.map((env) => (
        <div key={env.key}>
          <div style={{ marginBottom: 4 }}>
            <Typography.Text style={{ fontFamily: "var(--text-font-mono)", fontSize: 12 }}>
              {env.key}
            </Typography.Text>
            {env.configured && !isCleared(env.key) && (
              <Button type="link" size="small" onClick={() => markClear(env.key)}>
                {t("ui.settings_credential_clear")}
              </Button>
            )}
            {isCleared(env.key) && (
              <Tag color="red" style={{ marginLeft: 8 }}>{t("ui.settings_credential_will_clear")}</Tag>
            )}
          </div>
          <Input.Password
            value={draftOf(env.key)}
            disabled={isCleared(env.key)}
            placeholder={
              isCleared(env.key)
                ? ""
                : env.configured
                  ? t("ui.settings_credential_overwrite_ph", { mask: env.masked })
                  : t("ui.settings_credential_missing_ph")
            }
            onChange={(e) => setDraft(env.key, e.target.value)}
          />
        </div>
      ))}
      <Button type="primary" size="small" loading={saving} disabled={!dirty} onClick={() => void save()}>
        {t("ui.settings_credentials_save")}
      </Button>
    </Space>
  );
}

function GlossariesSection({ active }: { active: boolean }) {
  const { t } = useTranslation();
  const [items, setItems] = useState<GlossaryInfo[]>([]);
  const [busy, setBusy] = useState(false);

  async function reload() {
    try {
      setItems(await listGlossaries());
    } catch {
      /* 服务未就绪时静默 */
    }
  }

  useEffect(() => {
    if (active) void reload();
  }, [active]);

  async function onImport(file: File) {
    setBusy(true);
    try {
      await importGlossary(file);
      void message.success(t("ui.backend_status_ok"));
      await reload();
    } catch (err) {
      void message.error(String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Space direction="vertical" size={8} style={{ width: "100%" }}>
      <Upload
        accept=".csv"
        showUploadList={false}
        beforeUpload={(file) => {
          void onImport(file);
          return false;
        }}
      >
        <Button icon={<UploadOutlined />} loading={busy}>
          {t("ui.settings_glossary_import")}
        </Button>
      </Upload>
      {items.length === 0 ? (
        <Typography.Text type="secondary">{t("ui.settings_glossary_empty")}</Typography.Text>
      ) : (
        <List
          size="small"
          dataSource={items}
          rowKey={(g) => g.name}
          renderItem={(g) => (
            <List.Item
              style={{ paddingBlock: 4 }}
              actions={[
                <Button key="dl" size="small" type="link" href={glossaryDownloadUrl(g.name)} target="_blank">
                  CSV
                </Button>,
              ]}
            >
              <Space size={8}>
                <span>{g.name}</span>
                {typeof g.entries === "number" ? (
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    {t("ui.settings_glossary_entries", { count: g.entries })}
                  </Typography.Text>
                ) : (
                  <Tag color="red">{g.error || t("ui.status_failed")}</Tag>
                )}
              </Space>
            </List.Item>
          )}
        />
      )}
    </Space>
  );
}

/** doclayout ONNX 版面模型：状态查询 + 按需下载（轮询进度）。 */
function ModelsSection({ active }: { active: boolean }) {
  const { t } = useTranslation();
  const [status, setStatus] = useState<DoclayoutModelStatus | null>(null);
  const [starting, setStarting] = useState(false);

  async function refresh() {
    try {
      setStatus(await getDoclayoutModelStatus());
    } catch {
      /* 服务未就绪时静默 */
    }
  }

  useEffect(() => {
    if (active) void refresh();
  }, [active]);

  useEffect(() => {
    if (!status?.downloading) return;
    const timer = window.setInterval(() => void refresh(), 2000);
    return () => window.clearInterval(timer);
  }, [status?.downloading]);

  async function start() {
    setStarting(true);
    try {
      await downloadDoclayoutModel();
      await refresh();
    } catch (err) {
      message.error(String(err));
    } finally {
      setStarting(false);
    }
  }

  let tag = <Tag>{t("ui.settings_models_missing")}</Tag>;
  if (status?.downloading) tag = <Tag color="processing">{t("ui.settings_models_downloading")}</Tag>;
  else if (status?.last_error) tag = <Tag color="red">{t("ui.settings_models_failed")}</Tag>;
  else if (status?.exists && status.sha_ok)
    tag = <Tag color="green">{t("ui.settings_models_ready")}</Tag>;
  else if (status?.exists && !status.sha_ok)
    tag = <Tag color="warning">{t("ui.settings_models_invalid")}</Tag>;

  return (
    <Space direction="vertical" size={8} style={{ width: "100%" }}>
      <Typography.Paragraph type="secondary" style={{ marginBottom: 0, fontSize: 12 }}>
        {t("ui.settings_models_hint")}
      </Typography.Paragraph>
      <Space wrap size={8}>
        {tag}
        {status?.exists && (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {(status.size_bytes / 1048576).toFixed(1)} MB
          </Typography.Text>
        )}
        {status?.last_error && (
          <Typography.Text type="danger" style={{ fontSize: 12 }}>
            {status.last_error}
          </Typography.Text>
        )}
      </Space>
      <Button
        icon={<DownloadOutlined />}
        loading={starting}
        disabled={!!status?.downloading}
        onClick={() => void start()}
      >
        {status?.downloading ? t("ui.settings_models_downloading") : t("ui.settings_models_download")}
      </Button>
    </Space>
  );
}

/** MinerU / magic-pdf 高级解析：探测状态 + 一键构建隔离 venv（模型与应用分离）。 */
function MineruSection() {
  const { t } = useTranslation();
  const [state, setState] = useState<{ ok: boolean; hint: string } | null>(null);
  const [copied, setCopied] = useState(false);
  const [installing, setInstalling] = useState(false);
  const [installError, setInstallError] = useState<string | null>(null);

  async function refresh() {
    try {
      const r = await selftestMagicpdf();
      setState({ ok: r.ok, hint: r.hint ?? "" });
    } catch {
      /* 服务未就绪时静默 */
    }
  }

  useEffect(() => {
    void refresh();
  }, []);

  useEffect(() => {
    if (!installing) return;
    const timer = window.setInterval(async () => {
      try {
        const s = await getMineruSetupStatus();
        if (!s.running) {
          setInstalling(false);
          window.clearInterval(timer);
          if (s.error) setInstallError(s.error);
          else setInstallError(null);
          void refresh();
        }
      } catch {
        /* ignore */
      }
    }, 2000);
    return () => window.clearInterval(timer);
  }, [installing]);

  async function copyCommand() {
    const cmd = state?.hint || 'pip install -U "magic-pdf[full]"';
    try {
      await navigator.clipboard.writeText(cmd);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      message.error(t("ui.label_error"));
    }
  }

  async function startInstall() {
    setInstallError(null);
    setInstalling(true);
    try {
      const res = await setupMineru();
      if (!res.started) {
        setInstalling(false);
        if (res.reason) setInstallError(res.reason);
      }
    } catch (err) {
      setInstalling(false);
      setInstallError(String(err));
    }
  }

  return (
    <Space direction="vertical" size={8} style={{ width: "100%" }}>
      <Typography.Paragraph type="secondary" style={{ marginBottom: 0, fontSize: 12 }}>
        {t("ui.settings_mineru_hint")}
      </Typography.Paragraph>
      {state?.ok ? (
        <Tag color="green">{t("ui.settings_mineru_ready")}</Tag>
      ) : (
        <Space direction="vertical" size={6} style={{ width: "100%" }}>
          <Button
            type="primary"
            size="small"
            loading={installing}
            onClick={() => void startInstall()}
          >
            {installing
              ? t("ui.settings_mineru_installing")
              : t("ui.settings_mineru_install")}
          </Button>
          {installError && (
            <Typography.Text type="danger" style={{ fontSize: 12 }}>
              {installError}
            </Typography.Text>
          )}
          {state?.hint && (
            <Input.TextArea
              value={state.hint}
              readOnly
              autoSize
              style={{ fontFamily: "var(--text-font-mono)", fontSize: 12 }}
            />
          )}
          <Button size="small" onClick={() => void copyCommand()}>
            {copied ? t("ui.settings_copied") : t("ui.settings_copy")}
          </Button>
        </Space>
      )}
    </Space>
  );
}

function ConnectionSection() {
  const { t } = useTranslation();
  const [value, setValue] = useState("");

  useEffect(() => {
    try {
      setValue(window.localStorage.getItem(STORAGE_API_BASE) ?? "");
    } catch {
      /* ignore */
    }
  }, []);

  function save() {
    try {
      if (value.trim()) {
        window.localStorage.setItem(STORAGE_API_BASE, value.trim().replace(/\/+$/, ""));
      } else {
        window.localStorage.removeItem(STORAGE_API_BASE);
      }
      window.location.reload();
    } catch {
      void message.error(t("ui.label_error"));
    }
  }

  return (
    <Space direction="vertical" size={8} style={{ width: "100%" }}>
      <Input
        value={value}
        onChange={(e) => setValue(e.target.value)}
        placeholder="http://127.0.0.1:11009"
        allowClear
      />
      <Button onClick={save}>{t("ui.settings_save_reload")}</Button>
    </Space>
  );
}

export default function SettingsDrawer({ open, onClose }: Props) {
  const { t } = useTranslation();

  function section(title: string, node: ReactNode) {
    return (
      <>
        <Divider titlePlacement="left" style={{ marginTop: 16 }}>
          {title}
        </Divider>
        {node}
      </>
    );
  }

  return (
    <Drawer
      title={t("ui.header_settings")}
      placement="right"
      width={420}
      open={open}
      onClose={onClose}
    >
      {section(t("ui.settings_appearance"), <AppearanceSection />)}
      {section(
        t("ui.settings_engines"),
        <Space direction="vertical" size={6} style={{ width: "100%" }}>
          <Typography.Paragraph type="secondary" style={{ marginBottom: 0, fontSize: 12 }}>
            {t("ui.settings_engines_hint")}
          </Typography.Paragraph>
          <EnginesSection active={open} />
        </Space>,
      )}
      {section(
        t("ui.settings_models"),
        <ModelsSection active={open} />,
      )}
      {section(t("ui.settings_mineru"), <MineruSection />)}
      {section(
        t("ui.settings_glossaries"),
        <Space direction="vertical" size={6} style={{ width: "100%" }}>
          <Typography.Paragraph type="secondary" style={{ marginBottom: 0, fontSize: 12 }}>
            {t("ui.settings_glossaries_hint")}
          </Typography.Paragraph>
          <GlossariesSection active={open} />
        </Space>,
      )}
      {section(
        t("ui.settings_advanced"),
        <Space direction="vertical" size={6} style={{ width: "100%" }}>
          <Typography.Paragraph type="secondary" style={{ marginBottom: 0, fontSize: 12 }}>
            {t("ui.settings_api_base_hint")}
          </Typography.Paragraph>
          <ConnectionSection />
        </Space>,
      )}
      <Alert
        style={{ marginTop: 24 }}
        type="info"
        showIcon
        message={`${t("ui.brand_subtitle")} · v${__APP_VERSION__}`}
      />
    </Drawer>
  );
}
