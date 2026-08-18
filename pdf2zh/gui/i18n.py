"""Bilingual copy dictionary for the pdf2zh GUI.

Single source of truth for all user-facing strings. Every entry is a
``(zh, en)`` pair; components render both languages consistently with
Chinese first (the primary audience of this project).

Usage::

    from pdf2zh.gui.i18n import B, T, STAGE_LABELS
    label = B("config_engine")          # -> "翻译引擎 / Engine"
    stage = stage_text("translating")   # -> "翻译中 / Translating"

Runtime task states map to user-facing stage labels via ``STAGE_LABELS``
so the UI never leaks internal pipeline vocabulary to the user.
"""

from __future__ import annotations

from typing import Dict, Tuple

#: (zh, en) copy table. Keys are stable identifiers -- rename here, never
#: hard-code UI strings in components.
T: Dict[str, Tuple[str, str]] = {
    # ── brand / shell ────────────────────────────────────────────────────
    "brand_subtitle": ("文档智能运行时", "Document Intelligence Runtime"),
    "stepbar_aria": ("翻译流程", "Translation pipeline"),
    "header_recover_gpu": ("恢复 GPU 后端", "Recover GPU backend"),
    "recover_gpu_ok": ("已发出 GPU 后端恢复请求（set_backend auto），适配器将在下次任务初始化时重新尝试 GPU。",
                       "GPU backend recovery requested (set_backend auto); the adapter will retry GPU on the next task init."),
    "recover_gpu_fail": ("GPU 后端恢复失败", "GPU backend recovery failed"),

    # ── sections ─────────────────────────────────────────────────────────
    "section_upload": ("文件上传", "File Upload"),
    "section_config": ("翻译配置", "Translation Config"),
    "section_progress": ("执行状态", "Execution Status"),
    "section_preview": ("预览与下载", "Preview & Download"),
    "section_diagnostics": ("文档智能分析", "Document Intelligence"),

    # ── upload panel ─────────────────────────────────────────────────────
    "upload_tab_local": ("本地上传", "Local File"),
    "upload_tab_url": ("在线链接", "URL Link"),
    "upload_label_file": ("上传 PDF/DOCX 文件", "Upload PDF/DOCX"),
    "upload_label_url": ("输入 PDF 链接", "PDF URL"),
    "upload_url_hint": ("提示：仅支持可直接下载的 PDF 文件链接。",
                        "Tip: only directly downloadable PDF links are supported."),
    "upload_summary_selected": ("已选择", "Selected"),

    # ── config panel ─────────────────────────────────────────────────────
    "config_engine": ("翻译引擎", "Engine"),
    "config_lang_source": ("源语言", "Source"),
    "config_lang_target": ("目标语言", "Target"),
    "config_mode": ("引擎模式", "Engine Mode"),
    "config_mode_auto": ("自动", "Auto"),
    "config_mode_quick": ("快速", "Quick"),
    "config_mode_standard": ("标准", "Standard"),
    "config_mode_quality": ("高质量", "High Quality"),
    "config_mode_babeldoc": ("BabelDOC", "BabelDOC"),
    "config_mode_info": (
        "auto: 自动 | quick: 快速（经典引擎）| standard: 标准（生产默认）| quality: 高质量（评测+门控）| babeldoc: BabelDOC 排版引擎",
        "auto: Auto | quick: Quick (classic engine) | standard: Standard (production default) | quality: High Quality (evaluation+gate) | babeldoc: BabelDOC layout engine",
    ),
    "config_advanced": ("高级选项", "Advanced Options"),
    "config_threads": ("线程数", "Threads"),
    "config_skip_subset": ("跳过字体子集", "Skip Subset Fonts"),
    "config_ignore_cache": ("忽略缓存", "Ignore Cache"),
    "config_vfont": ("字体映射 (V-Font)", "Font Map (V-Font)"),
    "config_vchar": ("字符映射 (V-Char)", "Char Map (V-Char)"),
    "config_pages": ("页码范围", "Pages"),
    "config_prompt_env": ("Prompt 模板 (KEY=VALUE)", "Prompt Template (KEY=VALUE)"),
    "config_prompt_env_info": (
        "按 KEY=VALUE 填写 LLM 类引擎（OpenAI/Claude/Gemini 等）的翻译提示词模板，"
        "例如 PROMPT=请将以下内容翻译成简体中文。",
        "Set the translation prompt template for LLM engines (OpenAI/Claude/Gemini "
        "etc.), e.g. PROMPT=Translate the following into Chinese.",
    ),
    "config_env": ("自定义环境变量", "Custom Env Var"),
    "config_env_label": ("环境变量 {num} (KEY=VALUE)", "Env Var {num} (KEY=VALUE)"),
    "config_env_info": (
        "提供引擎所需的密钥或参数，按 KEY=VALUE 填写（键名不区分大小写，自动注入引擎），"
        "例如 OPENAI_API_KEY=sk-xxx / OPENAI_API_BASE=https://...。",
        "Engine credentials or parameters as KEY=VALUE (case-insensitive, injected "
        "into the engine), e.g. OPENAI_API_KEY=sk-xxx / OPENAI_API_BASE=https://...",
    ),
    "config_backend": ("ONNX 加速后端", "ONNX Acceleration Backend"),
    "config_backend_info": (
        "版面分析（BabelDOC / doclayout ONNX）推理后端。auto 保持默认（CPU 优先）；"
        "cuda / dml 开启 GPU 加速；GPU 不可用或崩溃时自动回退 CPU。",
        "Inference backend for layout analysis (BabelDOC / doclayout ONNX). "
        "auto keeps the default (CPU-first); cuda / dml enable GPU acceleration; "
        "falls back to CPU automatically if the GPU is unavailable or crashes.",
    ),
    "config_backend_auto": ("自动（默认）", "Auto (default)"),
    "config_backend_cpu": ("仅 CPU", "CPU only"),
    "config_backend_cuda": ("CUDA（NVIDIA GPU）", "CUDA (NVIDIA GPU)"),
    "config_ocr_mode": ("扫描版 PDF OCR", "Scanned-PDF OCR"),
    "config_ocr_mode_info": (
        "BabelDOC 对扫描版 / 无文本层 PDF 的 OCR 处理。auto 自动检测扫描并启用 OCR；"
        "on 强制对所有 PDF 执行 OCR（无文本层 PDF 首选）；off 跳过扫描检测（不做 OCR）。",
        "OCR handling for scanned / textless PDFs in BabelDOC. auto auto-detects "
        "scanned pages and enables OCR; on forces OCR for every PDF (best for "
        "textless PDFs); off skips scan detection (no OCR).",
    ),
    "config_ocr_mode_auto": ("自动（检测到扫描时启用）", "Auto (enable when scanned)"),
    "config_ocr_mode_on": ("强制开启（所有 PDF 均 OCR）", "Force on (OCR every PDF)"),
    "config_ocr_mode_off": ("关闭（跳过扫描检测）", "Off (skip scan detection)"),
    "config_parse_engine": ("解析引擎", "Parse Engine"),
    "config_parse_engine_info": (
        "PDF 解析/排版引擎切换：auto 保持历史行为（引擎模式决定）；babeldoc 走 "
        "BabelDOC 排版引擎；magicpdf 走 MinerU/magic-pdf 解析链路（未安装时自动"
        "降级回 legacy）。",
        "Switch the PDF parsing/layout engine: auto keeps historical behaviour "
        "(determined by engine mode); babeldoc uses the BabelDOC layout engine; "
        "magicpdf uses the MinerU/magic-pdf parse pipeline (auto-falls back to "
        "legacy when not installed).",
    ),
    "config_parse_engine_auto": ("自动（默认）", "Auto (default)"),
    "config_parse_engine_legacy": ("Legacy 内核", "Legacy kernel"),
    "config_parse_engine_babeldoc": ("BabelDOC 排版", "BabelDOC layout"),
    "config_parse_engine_magicpdf": ("Magic-PDF/MinerU", "Magic-PDF/MinerU"),
    "config_magicpdf_ocr": ("MagicPDF OCR", "MagicPDF OCR"),
    "config_magicpdf_ocr_info": (
        "magicpdf 解析强制 OCR：开启后对扫描版 / 无文本层 PDF 执行 OCR（对应 "
        "CLI --magicpdf-ocr）；关闭时由解析预检自动决定。",
        "Force OCR in magicpdf parsing for scanned / textless PDFs "
        "(equivalent to CLI --magicpdf-ocr); when off, the preflight decides.",
    ),
    "config_backend_dml": ("DirectML（Windows GPU）", "DirectML (Windows GPU)"),
    "config_backend_status": (
        "当前环境推理后端诊断",
        "Inference backend diagnostics",
    ),
    "backend_status_ok": ("可用", "available"),
    "backend_status_missing": ("不可用", "unavailable"),
    "backend_status_cuda_hint": (
        "未检测到 CUDAExecutionProvider。如需 CUDA 加速：先 pip uninstall "
        "onnxruntime，再安装与 CUDA/cuDNN 版本匹配的 onnxruntime-gpu，重启服务；"
        "Windows 上也可选用 DirectML 后端获得 GPU 加速（无需 CUDA 工具链）。",
        "CUDAExecutionProvider not found. For CUDA acceleration: pip uninstall "
        "onnxruntime, then install onnxruntime-gpu matching your CUDA/cuDNN, "
        "and restart; on Windows you may use the DirectML backend for GPU "
        "acceleration without a CUDA toolkit.",
    ),
    "backend_status_cuda_runtime_hint": (
        "已安装 onnxruntime-gpu 但 CUDA 运行库初始化失败（缺少 cublasLt/cuDNN DLL）。"
        "请安装与 onnxruntime-gpu 版本匹配的 CUDA + cuDNN 并加入 PATH，或降级 "
        "onnxruntime-gpu 以匹配现有 CUDA；也可改用 DirectML 后端获得 GPU 加速。",
        "onnxruntime-gpu is installed but the CUDA runtime failed to initialize "
        "(missing cublasLt/cuDNN DLLs). Install matching CUDA + cuDNN and add "
        "them to PATH, or downgrade onnxruntime-gpu to match your CUDA; you may "
        "also use the DirectML backend for GPU acceleration.",
    ),
    "backend_status_dml_hint": (
        "DirectML 不可用。可安装 onnxruntime-directml 或检查显卡驱动；"
        "NVIDIA 显卡也可安装 onnxruntime-gpu 使用 CUDA。",
        "DirectML unavailable. Install onnxruntime-directml or check your GPU "
        "driver; on NVIDIA GPUs you can install onnxruntime-gpu for CUDA.",
    ),
    "backend_status_dml_hint_gpu": (
        "当前 onnxruntime 为 GPU 发行版，不含 DirectML provider。如需 DirectML："
        "pip uninstall onnxruntime-gpu 后安装 onnxruntime-directml "
        "（含 AzureExecutionProvider），即可用 DirectML 后端驱动 NVIDIA GPU。",
        "The installed onnxruntime is a GPU build without the DirectML provider. "
        "To use DirectML: pip uninstall onnxruntime-gpu, then install "
        "onnxruntime-directml (includes AzureExecutionProvider) to drive your "
        "NVIDIA GPU.",
    ),
    "backend_status_gpu_hidden": (
        "当前环境下 CUDA 与 DirectML 均不可用（已从后端选项隐藏），将使用 CPU。",
        "Neither CUDA nor DirectML is usable in this environment (hidden from "
        "the backend options); the CPU backend will be used.",
    ),

    # ── progress panel ───────────────────────────────────────────────────
    "progress_translate": ("开始翻译", "Translate"),
    "progress_pause": ("暂停", "Pause"),
    "progress_resume": ("恢复", "Resume"),
    "progress_skip": ("跳过文件", "Skip File"),
    "progress_cancel": ("停止", "Cancel"),
    "progress_retry": ("重试", "Retry"),
    "progress_logs": ("详细日志", "Detailed Logs"),
    "progress_log_idle": ("[系统就绪]", "[System ready]"),
    "progress_log_title": ("执行日志", "Execution log"),
    "progress_aria": ("翻译进度", "Translation progress"),
    "progress_eta": ("预计剩余", "ETA"),

    # ── preview panel ────────────────────────────────────────────────────
    "preview_output": ("输出文件", "Output File"),
    "preview_download": ("下载", "Download"),
    "preview_download_all": ("下载全部 (ZIP)", "Download All (ZIP)"),
    "preview_empty": ("等待翻译完成后显示预览", "Preview appears after translation"),
    "preview_title": ("PDF 预览", "PDF preview"),
    "preview_download_label": ("下载选中的文件", "Download selected file"),
    "preview_zip_label": ("下载 ZIP 压缩包", "Download ZIP archive"),

    # ── diagnostic panel ─────────────────────────────────────────────────
    "diag_graph": ("文档概况", "Document Overview"),
    "diag_quality": ("质量评估", "Quality Assessment"),
    "diag_healing": ("诊断与自愈", "Diagnostic & Self-Healing"),
    "diag_graph_idle": ("等待翻译任务开始...", "Waiting for a translation task..."),
    "diag_quality_idle": ("翻译完成后将显示质量评分", "Quality scores appear after translation"),
    "diag_healing_idle": ("尚未运行诊断分析", "No diagnostic analysis yet"),
    "diag_healing_actions": ("自愈处置", "Healing actions"),
    "diag_heal_summary": ("自愈行程", "Healing run"),
    "diag_confidence": ("置信度", "Confidence"),
    "diag_node_heading": ("页面", "pages"),
    "diag_paragraphs": ("段落", "paragraphs"),
    "diag_headings": ("标题", "headings"),
    "diag_figures": ("图表", "figures"),
    "diag_formulas": ("公式", "formulas"),
    "diag_diagnosis": ("诊断", "Diagnosis"),
    "diag_layout": ("Layout 检查器", "Layout Inspector"),
    "diag_layout_paragraphs": ("段落", "paragraphs"),
    "diag_layout_issues": ("问题", "issues"),
    "diag_layout_align": ("对齐", "align"),
    "diag_no_task": ("等待翻译任务开始...", "Waiting for a translation task..."),

    # ── status lines ─────────────────────────────────────────────────────
    "label_status": ("状态", "Status"),
    "label_progress": ("进度", "Progress"),
    "label_error": ("错误", "Error"),
    "label_document": ("文档", "Document"),
    "label_files": ("文件数", "Files"),
    "label_message": ("信息", "Message"),
    "label_n_a": ("无", "N/A"),
    "status_ready": ("就绪", "Ready"),
    "status_paused": ("已暂停", "Paused"),
    "status_running": ("运行中", "Running"),
    "status_completed": ("完成", "Complete"),
    "status_failed": ("失败", "Failed"),
    "status_cancelled": ("已取消", "Cancelled"),
    "status_skipping": ("正在跳过当前文件...", "Skipping current file..."),
    "retry_hint": ("翻译失败，可点击『重试』重新提交。", "Translation failed. Click 'Retry' to resubmit."),

    # ── queue / misc ─────────────────────────────────────────────────────
    "waiting_task": ("等待翻译任务...", "Waiting for a translation task..."),
    "idle_diag_graph": ("等待翻译任务开始...", "Waiting for a translation task..."),
    "idle_quality": ("翻译完成后将显示质量评分", "Quality scores appear after translation"),
    "idle_diag": ("尚未运行诊断分析", "No diagnostic analysis yet"),
    "cancel_confirm": ("确定停止当前翻译任务？", "Cancel the current task?"),
    "theme_dark_label": ("深色模式", "Dark"),
    "theme_light_label": ("浅色模式", "Light"),
}

#: Internal runtime status -> user-facing stage label (zh, en).
STAGE_LABELS: Dict[str, Tuple[str, str]] = {
    "idle": ("就绪", "Ready"),
    "pending": ("排队中", "Queued"),
    "parsing": ("解析中", "Parsing"),
    "normalizing": ("规范化", "Normalizing"),
    "analyzing": ("版面分析", "Layout Analysis"),
    "planning": ("规划中", "Planning"),
    "translating": ("翻译中", "Translating"),
    "layouting": ("排版中", "Re-layout"),
    "rendering": ("渲染中", "Rendering"),
    "evaluating": ("质量评估", "Quality Check"),
    "repairing": ("自动修复", "Auto Repair"),
    "completed": ("完成", "Complete"),
    "failed": ("失败", "Failed"),
    "cancelled": ("已取消", "Cancelled"),
    "running": ("运行中", "Running"),
}


def ZH(key: str, **kwargs: object) -> str:
    """Return the Chinese half of an entry (fallback: the key itself)."""
    text = T.get(key, (key, key))[0]
    try:
        return text.format(**kwargs)
    except (KeyError, IndexError, ValueError):
        # Tolerant formatting: placeholders without matching kwargs render raw.
        return text


def EN(key: str, **kwargs: object) -> str:
    """Return the English half of an entry (fallback: the key itself)."""
    text = T.get(key, (key, key))[1]
    try:
        return text.format(**kwargs)
    except (KeyError, IndexError, ValueError):
        return text


def B(key: str, **kwargs: object) -> str:
    """Bilingual label, Chinese first: ``"中文 / English"``."""
    return f"{ZH(key, **kwargs)} / {EN(key, **kwargs)}"


def stage_text(status: str) -> str:
    """Bilingual user-facing label for an internal task status."""
    zh, en = STAGE_LABELS.get(status, (status, status))
    return f"{zh} / {en}"


__all__ = [
    "T",
    "STAGE_LABELS",
    "ZH",
    "EN",
    "B",
    "stage_text",
]
