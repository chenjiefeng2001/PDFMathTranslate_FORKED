/** App 外观：antd 主题（亮/暗）+ Design Tokens 品牌色对齐 + 全局壳（头部/设置入口）。 */

import { Button, ConfigProvider, Space, Spin, Typography, theme as antdTheme } from "antd";
import { MoonOutlined, SettingOutlined, SunOutlined } from "@ant-design/icons";
import zhCN from "antd/locale/zh_CN";
import enUS from "antd/locale/en_US";
import type { ReactNode } from "react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import tokens from "../../../pdf2zh/gui/assets/generated/tokens/tokens.json";
import { getHealth } from "../api/endpoints";
import { currentLang, switchLang } from "../i18n";
import { useSettingsStore } from "../stores/settingsStore";
import SettingsDrawer from "../pages/SettingsDrawer";

const BRAND = (tokens as { light: Record<string, string> }).light["color_accent"] || "#165dff";

/**
 * 健康门闩：主窗口在 sidecar 就绪前显示，轮询 /api/health 通过后才挂载
 * 业务页面。打开失败调查显示极端情况（后端异常退出、端口被外部程序占用
 * 又不响应等）会无限转圈——连续失败约 40s 后切换为可重试的错误态兜底。
 */
function ReadyGate({ children }: { children: ReactNode }) {
  const { t } = useTranslation();
  const [ready, setReady] = useState(false);
  const [failed, setFailed] = useState(false);
  const [attempts, setAttempts] = useState(0);

  useEffect(() => {
    if (ready || failed) return undefined;
    let cancelled = false;
    let timer = 0;
    async function poll() {
      try {
        await getHealth();
        if (!cancelled) setReady(true);
        return;
      } catch {
        /* 服务未就绪，继续等 */
      }
      if (cancelled) return;
      setAttempts((n) => n + 1);
      timer = window.setTimeout(() => void poll(), 700);
    }
    void poll();
    return () => {
      cancelled = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [ready, failed]);

  useEffect(() => {
    if (attempts >= 60) setFailed(true);
  }, [attempts]);

  if (ready) return <>{children}</>;
  if (failed) {
    return (
      <div
        style={{
          height: "60vh",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          flexDirection: "column",
          gap: 12,
        }}
      >
        <Typography.Text type="danger">{t("ui.connect_failed")}</Typography.Text>
        <Button
          onClick={() => {
            setAttempts(0);
            setFailed(false);
          }}
        >
          {t("ui.connect_retry")}
        </Button>
      </div>
    );
  }
  return (
    <div
      style={{
        height: "60vh",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        flexDirection: "column",
        gap: 12,
      }}
    >
      <Spin size="large" />
      <Typography.Text type="secondary" style={{ fontSize: 13 }}>
        {t("ui.connecting")}
      </Typography.Text>
    </div>
  );
}

export function AppShell({ children }: { children: ReactNode }) {
  const { t } = useTranslation();
  const dark = useSettingsStore((s) => s.dark);
  const toggleTheme = useSettingsStore((s) => s.toggleTheme);
  const [lang, setLangState] = useState(currentLang);
  const [settingsOpen, setSettingsOpen] = useState(false);

  // data-theme 挂在 <html> 上：tokens.css 的暗色变量覆盖与原生控件
  // color-scheme 都依赖它；body 背景同步，避免 overscroll 露白。
  useEffect(() => {
    document.documentElement.dataset.theme = dark ? "dark" : "light";
    document.body.style.background = dark ? "#14161b" : "#f5f6fa";
  }, [dark]);

  return (
    <ConfigProvider
      locale={lang === "zh-CN" ? zhCN : enUS}
      theme={{
        algorithm: dark ? antdTheme.darkAlgorithm : antdTheme.defaultAlgorithm,
        token: {
          colorPrimary: BRAND,
          colorLink: BRAND,
          borderRadius: 6,
        },
      }}
    >
      <div
        style={{
          minHeight: "100vh",
          background: "var(--color-bg)",
          color: "var(--color-text-primary)",
          transition: "background var(--motion-normal) ease",
        }}
      >
        <header
          style={{
            position: "sticky",
            top: 0,
            zIndex: 100,
            display: "flex",
            alignItems: "center",
            gap: 12,
            padding: "10px 20px",
            background: "var(--color-surface)",
            borderBottom: "1px solid var(--color-border)",
          }}
        >
          <div>
            <strong style={{ fontSize: 16 }}>PDFMathTranslate</strong>
            <div style={{ fontSize: 12, opacity: 0.6 }}>{t("ui.brand_subtitle")}</div>
          </div>
          <span style={{ flex: 1 }} />
          <Space size={8}>
            <Button
              type="text"
              icon={dark ? <SunOutlined /> : <MoonOutlined />}
              onClick={() => toggleTheme()}
              aria-label={dark ? t("ui.theme_light_label") : t("ui.theme_dark_label")}
            >
              {dark ? t("ui.theme_light_label") : t("ui.theme_dark_label")}
            </Button>
            <Button
              type="text"
              onClick={() => {
                const next = lang === "zh-CN" ? "en" : "zh-CN";
                switchLang(next);
                setLangState(next);
              }}
            >
              {lang === "zh-CN" ? "English" : "中文"}
            </Button>
            <Button
              type="text"
              icon={<SettingOutlined />}
              onClick={() => setSettingsOpen(true)}
            >
              {t("ui.header_settings")}
            </Button>
          </Space>
        </header>
        <main>
          <ReadyGate>{children}</ReadyGate>
        </main>
      </div>
      <SettingsDrawer open={settingsOpen} onClose={() => setSettingsOpen(false)} />
    </ConfigProvider>
  );
}
