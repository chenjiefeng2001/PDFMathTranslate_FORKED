/** App 外观：antd 主题（亮/暗）+ Design Tokens 品牌色对齐 + 全局壳（头部/设置入口）。 */

import { Button, ConfigProvider, Space, theme as antdTheme } from "antd";
import { MoonOutlined, SettingOutlined, SunOutlined } from "@ant-design/icons";
import zhCN from "antd/locale/zh_CN";
import enUS from "antd/locale/en_US";
import type { ReactNode } from "react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import tokens from "../../../pdf2zh/gui/assets/generated/tokens/tokens.json";
import { currentLang, switchLang } from "../i18n";
import { useSettingsStore } from "../stores/settingsStore";
import SettingsDrawer from "../pages/SettingsDrawer";

const BRAND = (tokens as { light: Record<string, string> }).light["color_accent"] || "#165dff";

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
        <main>{children}</main>
      </div>
      <SettingsDrawer open={settingsOpen} onClose={() => setSettingsOpen(false)} />
    </ConfigProvider>
  );
}
