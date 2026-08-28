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
      progress_detail: "{{stage}}：{{current}}/{{total}}",
      unit_page: "页",
      unit_paragraph: "段",
      unit_term: "词条",
      unit_batch: "批",
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
      upload_formats_hint:
        "支持 PDF / DOCX，可拖入或点击选择；选中后可在下方列表移除或再次选择进行替换。",
      batch_count: "批量 {{count}} 个文件",
      download_all_zip: "全部下载（ZIP）",
      batch_failed_files: "{{count}} 个文件翻译失败",
      settings_models: "版面模型（GPU）",
      settings_models_hint:
        "doclayout ONNX 模型用于 GPU 加速的版面分析，按需下载到本地缓存（~/.cache/babeldoc），不随安装包分发。首次翻译前建议先下载。",
      settings_models_download: "下载模型",
      settings_models_downloading: "下载中…",
      settings_models_ready: "已就绪",
      settings_models_invalid: "已存在但校验失败，请重新下载",
      settings_models_missing: "未下载",
      settings_models_failed: "下载失败",
      settings_gpu_provider: "GPU 布局加速（CUDA 执行器）",
      settings_gpu_provider_hint:
        "CUDA 执行器（约 164MB）不随安装包分发：需要 NVIDIA GPU 加速版面分析时再点击下载，将从 PyPI 获取与内置 onnxruntime 同版本的组件并安装到应用目录；未安装或本机缺少 CUDA 运行时则自动回退 CPU，不影响翻译。",
      settings_gpu_provider_download: "下载并启用 CUDA",
      settings_gpu_provider_downloading: "下载中… {{percent}}%",
      settings_gpu_provider_active: "已生效",
      settings_gpu_provider_installed: "已安装（等待生效）",
      settings_gpu_provider_missing: "未安装（CPU 推理）",
      settings_gpu_provider_remove: "移除",
      settings_gpu_provider_failed: "下载失败",
      parse_engine_unavailable: "未安装（桌面版不内置，需本地部署 magic-pdf/MinerU）",
      config_output_dir: "输出目录（下载存放位置）",
      config_output_dir_hint: "翻译结果 mono/dual PDF 的保存位置；留空使用源文件所在目录。修改后自动记住。",
      config_mineru_vram: "MinerU 显存预算 (GB)",
      config_mineru_vram_info:
        "MinerU GPU 推理的显存预算（对应 MINERU_VIRTUAL_VRAM_SIZE）：留空自动按显存保守估算（8GB 卡 → 6，batch 减半防 OOM）；小显存卡建议设 4–6，大显存卡可设 8+。",
      config_mineru_window: "MinerU 每批页数",
      config_mineru_window_info:
        "MinerU 处理窗口页数（对应 MINERU_PROCESSING_WINDOW_SIZE）：留空用引擎默认 64；小显存卡建议设 8–16 进一步降低显存峰值。",
      config_mineru_auto: "自动",
      config_mineru_parse_method: "MinerU 解析方法",
      config_mineru_parse_method_info:
        "显式切换 MinerU 解析方法（对应 do_parse parse_method）：auto=常规文本解析，ocr=强制 OCR（扫描件），txt=纯文本。留空跟随「OCR 模式」开关。",
      config_mineru_backend: "MinerU 解析后端",
      config_mineru_backend_info:
        "显式切换 MinerU 解析后端：pipeline=本地模型（默认），hybrid=混合，vlm=视觉语言模型（需对应服务/模型就绪）。留空用 pipeline。",
      settings_mineru: "MinerU / magic-pdf 高级解析",
      settings_mineru_hint:
        "桌面安装包因体积上限不内置 MinerU 与 torch。点击「一键安装 MinerU」即可在用户数据目录构建隔离环境（torch 等重依赖与应用目录分离，需本机有 Python 3.10–3.13）；也可在独立 Python 环境执行下方命令。模型在首次解析时自动下载到用户缓存。",
      settings_mineru_ready: "本机已检测到，magicpdf 链路可用",
      settings_mineru_install: "一键安装 MinerU",
      settings_mineru_installing: "正在安装 MinerU（下载 torch 等，请稍候）",
      settings_mineru_cuda_enable: "启用 MinerU GPU",
      settings_mineru_cuda_enabling: "正在安装 CUDA torch（~2GB，请稍候）",
      settings_mineru_cuda_ready: "MinerU GPU 已启用",
      settings_mineru_cuda_cpu: "MinerU 当前为 CPU 推理",
      settings_copy: "复制命令",
      settings_copied: "已复制",
      download_done: "已下载",
      download_failed: "下载失败",
      download_folder_batch: "保存到文件夹…",
      download_folder_pick_title: "选择保存位置",
      connecting: "正在连接本地翻译服务…",
      connect_failed: "连接本地翻译服务失败。请重启应用；若反复出现，请查看 %TEMP%\\pdf2zh-sidecar.log",
      connect_retry: "重试",
      download_save_as: "另存为…",
      download_preview_action: "预览",
      preview_pick_hint: "点击文件行在下方预览 · 行内图标单独保存",
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
      progress_detail: "{{stage}}: {{current}}/{{total}}",
      unit_page: "pages",
      unit_paragraph: "paragraphs",
      unit_term: "terms",
      unit_batch: "batches",
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
      batch_count: "Batch · {{count}} files",
      download_all_zip: "Download all (ZIP)",
      batch_failed_files: "{{count}} file(s) failed",
      settings_models: "Layout model (GPU)",
      settings_models_hint:
        "The doclayout ONNX model powers GPU-accelerated layout analysis. It is downloaded on demand into the local cache (~/.cache/babeldoc) and is not shipped with the installer.",
      settings_models_download: "Download model",
      settings_models_downloading: "Downloading…",
      settings_models_ready: "Ready",
      settings_models_invalid: "Present but verification failed — re-download",
      settings_models_missing: "Not downloaded",
      settings_models_failed: "Download failed",
      settings_gpu_provider: "GPU layout acceleration (CUDA EP)",
      settings_gpu_provider_hint:
        "The CUDA execution provider (~164MB) is not bundled with the installer: download it on demand for NVIDIA GPU-accelerated layout analysis. It is fetched from PyPI matching the bundled onnxruntime version and installed into the app directory; if missing or the machine lacks a CUDA runtime, translation safely falls back to CPU.",
      settings_gpu_provider_download: "Download & enable CUDA",
      settings_gpu_provider_downloading: "Downloading… {{percent}}%",
      settings_gpu_provider_active: "Active",
      settings_gpu_provider_installed: "Installed (awaiting effect)",
      settings_gpu_provider_missing: "Not installed (CPU inference)",
      settings_gpu_provider_remove: "Remove",
      settings_gpu_provider_failed: "Download failed",
      parse_engine_unavailable:
        "Not installed (not bundled with the desktop build; deploy magic-pdf/MinerU locally)",
      config_output_dir: "Output directory (download location)",
      config_output_dir_hint:
        "Where translated mono/dual PDFs are saved; empty = the source file's folder. Remembered across sessions.",
      config_mineru_vram: "MinerU VRAM budget (GB)",
      config_mineru_vram_info:
        "VRAM budget for MinerU GPU inference (MINERU_VIRTUAL_VRAM_SIZE): empty = auto conservative estimate (8GB card → 6, halved batch to avoid OOM); small cards: 4–6, large cards: 8+.",
      config_mineru_window: "MinerU pages per batch",
      config_mineru_window_info:
        "MinerU processing window size (MINERU_PROCESSING_WINDOW_SIZE): empty = engine default 64; small cards: 8–16 to lower VRAM peaks.",
      config_mineru_auto: "Auto",
      config_mineru_parse_method: "MinerU parse method",
      config_mineru_parse_method_info:
        "Explicit MinerU parse method (do_parse parse_method): auto=normal text parse, ocr=force OCR (scanned), txt=plain text. Empty follows the OCR mode toggle.",
      config_mineru_backend: "MinerU backend",
      config_mineru_backend_info:
        "Explicit MinerU backend: pipeline=local models (default), hybrid, vlm (needs a ready VLM service/models). Empty uses pipeline.",
      settings_mineru: "MinerU / magic-pdf advanced parsing",
      settings_mineru_hint:
        "The desktop installer cannot bundle MinerU and torch due to size limits. Click \"Install MinerU (one-click)\" to build an isolated env in the user data dir (heavy deps like torch stay separate from the app; a local Python 3.10–3.13 is required); or run the command below in a separate Python env. Models auto-download into the user cache on first parse.",
      settings_mineru_ready: "Detected on this machine — magicpdf chain available",
      settings_mineru_install: "Install MinerU (one-click)",
      settings_mineru_installing: "Installing MinerU (downloading torch etc., please wait)",
      settings_mineru_cuda_enable: "Enable MinerU GPU",
      settings_mineru_cuda_enabling: "Installing CUDA torch (~2GB, please wait)",
      settings_mineru_cuda_ready: "MinerU GPU enabled",
      settings_mineru_cuda_cpu: "MinerU runs on CPU",
      settings_copy: "Copy command",
      settings_copied: "Copied",
      download_done: "Downloaded",
      download_failed: "Download failed",
      download_folder_batch: "Save to folder…",
      download_folder_pick_title: "Choose destination folder",
      connecting: "Connecting to the local translation service…",
      connect_failed:
        "Failed to reach the local translation service. Please restart the app; if it persists, check %TEMP%\\pdf2zh-sidecar.log",
      connect_retry: "Retry",
      download_save_as: "Save as…",
      download_preview_action: "Preview",
      preview_pick_hint: "Click a row to preview · use row icons to save",
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
