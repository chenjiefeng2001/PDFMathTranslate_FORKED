/**
 * i18n：直接消费 Phase A 导出的 locale 资产（pdf2zh/gui/assets/generated/locales），
 * 与 Gradio 端共享同一文案源，杜绝双端漂移。
 */

import i18n from "i18next";
import { initReactI18next } from "react-i18next";
import zhCN from "../../../pdf2zh/gui/assets/generated/locales/zh-CN.json";
import en from "../../../pdf2zh/gui/assets/generated/locales/en.json";

export type Lang = "zh-CN" | "en";
const STORAGE_KEY = "pdf2zh.lang";

function detectLang(): Lang {
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    if (stored === "zh-CN" || stored === "en") return stored;
  } catch {
    /* ignore */
  }
  return navigator.language.startsWith("zh") ? "zh-CN" : "en";
}

void i18n.use(initReactI18next).init({
  resources: {
    "zh-CN": { translation: zhCN },
    en: { translation: en },
  },
  lng: detectLang(),
  fallbackLng: "en",
  interpolation: { escapeValue: false },
});

export function switchLang(lang: Lang): void {
  void i18n.changeLanguage(lang);
  try {
    window.localStorage.setItem(STORAGE_KEY, lang);
  } catch {
    /* ignore */
  }
}

export function currentLang(): Lang {
  return (i18n.language as Lang) || "zh-CN";
}

export default i18n;
