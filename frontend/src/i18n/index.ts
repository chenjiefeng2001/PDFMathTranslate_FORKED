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

/**
 * SPA 专属文案 overlay：generated 资产与 Gradio 端共享，不直接改写；
 * 这里仅在运行时合并桌面端新增的键（ui.* 命名空间），避免双端漂移。
 */
const SPA_OVERLAY = {
  "zh-CN": {
    ui: {
      header_settings: "设置",
      settings_appearance: "外观",
      settings_theme: "主题",
      settings_language: "界面语言",
      settings_engines: "翻译引擎凭据",
      settings_engines_hint:
        "凭据来自环境变量或用户级配置文件；配置后重启服务生效。",
      settings_glossaries: "专业词表库",
      settings_glossaries_hint:
        "导入 CSV 词表（表头：source,target[,tgt_lng]），提交任务时可直接按名称引用。",
      settings_glossary_import: "导入 CSV",
      settings_glossary_empty: "暂无词表，点击上方按钮导入",
      settings_glossary_entries: "{{count}} 条词条",
      settings_advanced: "高级",
      settings_api_base: "API 地址覆盖",
      settings_api_base_hint:
        "留空使用默认地址（桌面版自动注入）。修改后需刷新页面生效。",
      settings_save_reload: "保存并刷新",
      task_history: "任务历史",
      stage_label: "阶段",
      current_file: "当前文件",
      upload_selected_prefix: "已选择：",
      preview_unavailable: "预览不可用（文件缺失或非 PDF）",
      settings_credentials_edit: "凭据",
      settings_credentials_save: "保存",
      settings_credentials_saved: "凭据已保存，提交翻译任务时立即生效",
      settings_credentials_hint: "输入新值覆盖原凭据；仅修改填写的项，其余保持不变。",
      settings_credential_missing_ph: "未配置 —— 请输入",
      settings_credential_overwrite_ph: "已配置 {{mask}}，输入新值可覆盖",
      settings_credential_clear: "清除",
      settings_credential_will_clear: "将清除",
      upload_formats_hint: "支持 PDF / DOCX，可拖入或点击选择；选中后可在下方列表移除或再次选择进行替换。",
      settings_models: "版面模型（GPU）",
      settings_models_hint:
        "doclayout ONNX 模型用于 GPU 加速的版面分析，按需下载到本地缓存（~/.cache/babeldoc），不随安装包分发。首次翻译前建议先下载。",
      settings_models_download: "下载模型",
      settings_models_downloading: "下载中…",
      settings_models_ready: "已就绪",
      settings_models_invalid: "已存在但校验失败，请重新下载",
      settings_models_missing: "未下载",
      settings_models_failed: "下载失败",
      parse_engine_unavailable: "未安装（桌面版不内置，需本地部署 magic-pdf/MinerU）",
    },
  },
  en: {
    ui: {
      header_settings: "Settings",
      settings_appearance: "Appearance",
      settings_theme: "Theme",
      settings_language: "Language",
      settings_engines: "Engine credentials",
      settings_engines_hint:
        "Credentials come from environment variables or user-level config; restart the service after changing them.",
      settings_glossaries: "Glossary store",
      settings_glossaries_hint:
        "Import glossary CSVs (header: source,target[,tgt_lng]) and reference them by name when submitting tasks.",
      settings_glossary_import: "Import CSV",
      settings_glossary_empty: "No glossaries yet. Import one above.",
      settings_glossary_entries: "{{count}} entries",
      settings_advanced: "Advanced",
      settings_api_base: "API base override",
      settings_api_base_hint:
        "Leave empty for the default (auto-injected in the desktop shell). Reload to apply.",
      settings_save_reload: "Save & reload",
      task_history: "Tasks",
      stage_label: "Stage",
      current_file: "Current file",
      upload_selected_prefix: "Selected: ",
      preview_unavailable: "Preview unavailable (file missing or not a PDF)",
      settings_credentials_edit: "Credentials",
      settings_credentials_save: "Save",
      settings_credentials_saved:
        "Credentials saved. Applied to the next translation task.",
      settings_credentials_hint:
        "Enter a new value to overwrite; only filled fields are changed.",
      settings_credential_missing_ph: "Not configured — enter a value",
      settings_credential_overwrite_ph: "Configured {{mask}}; enter to overwrite",
      settings_credential_clear: "Clear",
      settings_credential_will_clear: "will be cleared",
      upload_formats_hint:
        "Accepts PDF / DOCX. Drag & drop or click to select; remove or replace the file in the list below.",
      settings_models: "Layout model (GPU)",
      settings_models_hint:
        "The doclayout ONNX model powers GPU-accelerated layout analysis. It is downloaded on demand into the local cache (~/.cache/babeldoc) and is not shipped with the installer.",
      settings_models_download: "Download model",
      settings_models_downloading: "Downloading…",
      settings_models_ready: "Ready",
      settings_models_invalid: "Present but verification failed — re-download",
      settings_models_missing: "Not downloaded",
      settings_models_failed: "Download failed",
      parse_engine_unavailable:
        "Not installed (not bundled with the desktop build; deploy magic-pdf/MinerU locally)",
    },
  },
} as const;

function mergeOverlay(
  base: Record<string, unknown>,
  overlay: Record<string, unknown>,
): Record<string, unknown> {
  const out: Record<string, unknown> = { ...base };
  for (const [ns, patch] of Object.entries(overlay)) {
    out[ns] = {
      ...((out[ns] as Record<string, unknown>) ?? {}),
      ...(patch as Record<string, unknown>),
    };
  }
  return out;
}

/**
 * generated 资产中 stage.* 的值是 `[中文, English]` 双语数组（Gradio 端按下标
 * 取用）。i18next 无法渲染数组，会报 "returned an object instead of string"；
 * 在装载时按目标语言展平为字符串。
 */
function flattenBilingualArrays(
  base: Record<string, unknown>,
  langIndex: 0 | 1,
): void {
  const stage = base.stage;
  if (!stage || typeof stage !== "object") return;
  for (const [key, value] of Object.entries(stage as Record<string, unknown>)) {
    if (Array.isArray(value)) {
      (stage as Record<string, unknown>)[key] =
        String(value[langIndex] ?? value[0] ?? key);
    }
  }
}

function detectLang(): Lang {
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    if (stored === "zh-CN" || stored === "en") return stored;
  } catch {
    /* ignore */
  }
  return navigator.language.startsWith("zh") ? "zh-CN" : "en";
}

const zhResource = mergeOverlay(
  zhCN as Record<string, unknown>,
  SPA_OVERLAY["zh-CN"],
);
flattenBilingualArrays(zhResource, 0);
const enResource = mergeOverlay(en as Record<string, unknown>, SPA_OVERLAY.en);
flattenBilingualArrays(enResource, 1);

void i18n.use(initReactI18next).init({
  resources: {
    "zh-CN": { translation: zhResource },
    en: { translation: enResource },
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
