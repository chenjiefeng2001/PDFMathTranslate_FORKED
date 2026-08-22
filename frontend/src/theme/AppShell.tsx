/** App 外观：antd 主题（亮/暗）+ Phase A Design Tokens 的品牌色对齐。 */

import { ConfigProvider, theme as antdTheme } from "antd";
import zhCN from "antd/locale/zh_CN";
import enUS from "antd/locale/en_US";
import type { ReactNode } from "react";
import { useState } from "react";
import tokens from "../../../pdf2zh/gui/assets/generated/tokens/tokens.json";
import { currentLang, switchLang, type Lang } from "../i18n";

const BRAND = (tokens as { light: Record<string, string> }).light["color_accent"] || "#165dff";

export function AppShell({ children }: { children: ReactNode }) {
  const [dark, setDark] = useState<boolean>(() => {
    try {
      return window.matchMedia?.("(prefers-color-scheme: dark)").matches ?? false;
    } catch {
      return false;
    }
  });
  const [lang, setLangState] = useState<Lang>(currentLang());

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
      <div data-theme={dark ? "dark" : "light"} style={{ minHeight: "100vh" }}>
        <header
          style={{
            display: "flex",
            alignItems: "center",
            gap: 12,
            padding: "12px 20px",
            borderBottom: "1px solid rgba(128,128,128,.2)",
          }}
        >
          <strong style={{ fontSize: 16 }}>PDFMathTranslate</strong>
          <span style={{ opacity: 0.6 }}>SPA</span>
          <span style={{ flex: 1 }} />
          <a onClick={() => setDark(!dark)} role="button">
            {dark ? "☀️ Light" : "🌙 Dark"}
          </a>
          <a
            role="button"
            onClick={() => {
              const next: Lang = lang === "zh-CN" ? "en" : "zh-CN";
              switchLang(next);
              setLangState(next);
            }}
          >
            {lang === "zh-CN" ? "EN" : "中文"}
          </a>
        </header>
        <main>{children}</main>
      </div>
    </ConfigProvider>
  );
}
