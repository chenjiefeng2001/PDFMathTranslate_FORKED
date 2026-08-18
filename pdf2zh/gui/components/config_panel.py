"""Translation configuration panel component.

Replaces the 21-parameter inline form from Legacy gui.py with a
grouped, progressively-disclosed configuration form:

  * Basic   - engine / source / target language (filterable dropdowns)
  * Service - engine mode (v0..v4 pipeline selector with user-facing labels)
  * Advanced- threads, font/char mapping, page range, custom env (collapsed)

All component keys returned here are part of the app.py wiring contract
and MUST stay stable.
"""

from __future__ import annotations

import gradio as gr

from pdf2zh.gui.i18n import B

# Available translation engines (from pdf2zh translators)
ENGINES = [
    ("Google", "google"),
    ("DeepL", "deepl"),
    ("OpenAI", "openai"),
    ("Claude", "claude"),
    ("Gemini", "gemini"),
    ("DeepSeek", "deepseek"),
    ("Qwen", "qwen"),
    ("Azure OpenAI", "azure_openai"),
    ("SiliconFlow", "siliconflow"),
    ("Bing", "bing"),
    ("Microsoft", "microsoft"),
    ("Aliyun", "aliyun"),
    ("Tencent", "tencent"),
    ("VolcEngine", "volcengine"),
    ("Dify", "dify"),
    ("AnythingLLM", "anythingllm"),
]

LANGUAGES = [
    ("中文 (简体)", "zh-CN"),
    ("中文 (繁體)", "zh-TW"),
    ("English", "en"),
    ("日本語", "ja"),
    ("한국어", "ko"),
    ("Français", "fr"),
    ("Deutsch", "de"),
    ("Русский", "ru"),
    ("Español", "es"),
    ("Português", "pt"),
    ("Italiano", "it"),
    ("العربية", "ar"),
    ("हिन्दी", "hi"),
    ("auto", "auto"),
]

#: Pipeline mode choices surfaced in the Service group. Display labels are
#: user-facing quality levels; values remain the internal pipeline selector.
#: Every value maps to a working pipeline (legacy ``translate_stream`` or the
#: independent BabelDOC layout engine) - see MODE_PRESETS / resolve_pipeline
#: in ``pdf2zh.services.runtime_service``.
MODE_CHOICES = [
    (B("config_mode_auto"), "auto"),
    (B("config_mode_quick"), "quick"),
    (B("config_mode_standard"), "standard"),
    (B("config_mode_quality"), "quality"),
    (B("config_mode_babeldoc"), "babeldoc"),
]


def _available_backend_choices() -> list:
    """按执行级探测结果过滤不可用的 GPU 后端选项。

    DirectML/CUDA provider 即使已注册也可能因设备/运行库初始化失败而无法真正
    执行（ORT 静默回退 CPU，``get_providers()`` 仍返回 GPU 名）。据此在 UI 层
    直接隐藏不可用后端，避免用户选到一个"看似可用实则跑 CPU"的选项。
    状态面板（:func:`backend_status_markdown`）仍会显示缺失原因与修复提示。
    """
    from pdf2zh.doclayout import get_runtime_provider_status

    choices = [
        (B("config_backend_auto"), "auto"),
        (B("config_backend_cpu"), "cpu"),
    ]
    try:
        status = get_runtime_provider_status()
    except Exception:  # noqa: BLE001 -- 探测失败只保留 CPU 选项
        import logging  # noqa: PLC0415

        logging.getLogger(__name__).warning(
            "backend availability probe failed", exc_info=True
        )
        return choices
    if status.get("cuda"):
        choices.append((B("config_backend_cuda"), "cuda"))
    if status.get("dml"):
        choices.append((B("config_backend_dml"), "dml"))
    return choices


def backend_status_markdown() -> str:
    """Build a one-shot diagnostics Markdown of available ONNX backends."""
    from pdf2zh.doclayout import get_runtime_provider_status

    status = get_runtime_provider_status()
    ok = B("backend_status_ok")
    missing = B("backend_status_missing")
    lines = [
        f"**{B('config_backend_status')}**  ·  ONNX Runtime {status['onnxruntime']}",
        f"- {B('config_backend_cuda')}: {ok if status['cuda'] else missing}",
        f"- {B('config_backend_dml')}: {ok if status['dml'] else missing}",
        f"- {B('config_backend_cpu')}: {ok}",
        "",
        f"- 已注册 / registered: `{'、'.join(status['available']) or '-'}`",
        f"- 实际生效 / effective: `{'、'.join(status['effective']) or '-'}`",
    ]
    if not status["cuda"]:
        if "CUDAExecutionProvider" in status["available"]:
            # 已装 onnxruntime-gpu 但创建会话时 CUDA 运行库初始化失败
            lines.append(f"  › {B('backend_status_cuda_runtime_hint')}")
        else:
            lines.append(f"  › {B('backend_status_cuda_hint')}")
    if not status["dml"]:
        if not any(
            p in ("AzureExecutionProvider", "DmlExecutionProvider")
            for p in status["available"]
        ):
            # 当前发行版不含 DirectML provider（如 onnxruntime-gpu）
            lines.append(f"  › {B('backend_status_dml_hint_gpu')}")
        else:
            lines.append(f"  › {B('backend_status_dml_hint')}")
    if not (status["cuda"] or status["dml"]):
        lines.append(B("backend_status_gpu_hidden"))
    return "\n".join(lines)


def create_config_panel() -> dict:
    """Create translation configuration form panel.

    Returns:
        dict of Gradio component references
    """
    with gr.Group(elem_classes="panel-card"):
        gr.Markdown(f"## {B('section_config')}", elem_classes="section-header")

        # ---- 基础设置 / Basic ----
        with gr.Row():
            service = gr.Dropdown(
                choices=[e[1] for e in ENGINES],
                value="google",
                label=B("config_engine"),
                filterable=True,
                elem_classes="config-dropdown",
            )
            lang_from = gr.Dropdown(
                choices=[l[1] for l in LANGUAGES],
                value="auto",
                label=B("config_lang_source"),
                filterable=True,
                elem_classes="config-dropdown",
            )
            lang_to = gr.Dropdown(
                choices=[l[1] for l in LANGUAGES],
                value="zh-CN",
                label=B("config_lang_target"),
                filterable=True,
                elem_classes="config-dropdown",
            )

        # ---- 服务模式 / Service ----
        mode_choice = gr.Radio(
            choices=MODE_CHOICES,
            value="auto",
            label=B("config_mode"),
            info=B("config_mode_info"),
        )


        # 版面分析（BabelDOC / doclayout ONNX）推理后端开关。auto 保持原有
        # 默认行为（CPU 优先）；cuda / dml 开启 GPU 加速，GPU 不可用/崩溃时
        # 自动回退 CPU（由 doclayout.resolve_providers + 降级机制保证）。
        backend = gr.Radio(
            choices=_available_backend_choices(),
            value="auto",
            label=B("config_backend"),
            info=B("config_backend_info"),
        )
        backend_status = gr.Markdown(
            value=backend_status_markdown(),
            elem_classes="backend-status",
        )

        # 扫描版 / 无文本层 PDF 的 OCR 处理开关。auto 保持默认（BabelDOC
        # 自动检测扫描并启用 OCR workaround）；on 强制对所有 PDF 执行 OCR；
        # off 跳过扫描检测（不做 OCR）。对应环境变量 PDF2ZH_BABELDOC_OCR。
        ocr_mode = gr.Radio(
            choices=[
                (B("config_ocr_mode_auto"), "auto"),
                (B("config_ocr_mode_on"), "on"),
                (B("config_ocr_mode_off"), "off"),
            ],
            value="auto",
            label=B("config_ocr_mode"),
            info=B("config_ocr_mode_info"),
        )

        # 解析引擎切换（--parse-engine 语义）：auto 保持历史行为（引擎模式决定
        # legacy/BabelDOC）；babeldoc 显式走 BabelDOC 排版引擎；magicpdf 走
        # MinerU/magic-pdf 解析链路（引擎未安装时自动熔断降级 legacy）。
        parse_engine = gr.Radio(
            choices=[
                (B("config_parse_engine_auto"), "auto"),
                (B("config_parse_engine_legacy"), "legacy"),
                (B("config_parse_engine_babeldoc"), "babeldoc"),
                (B("config_parse_engine_magicpdf"), "magicpdf"),
            ],
            value="auto",
            label=B("config_parse_engine"),
            info=B("config_parse_engine_info"),
        )
        magicpdf_ocr = gr.Checkbox(
            value=False,
            label=B("config_magicpdf_ocr"),
            info=B("config_magicpdf_ocr_info"),
        )
        # ---- 高级选项 / Advanced ----
        with gr.Accordion(B("config_advanced"), open=False):
            with gr.Row():
                threads = gr.Slider(
                    minimum=1, maximum=16, value=4, step=1,
                    label=B("config_threads"),
                )
                skip_subset_fonts = gr.Checkbox(
                    value=False, label=B("config_skip_subset"),
                )
                ignore_cache = gr.Checkbox(
                    value=False, label=B("config_ignore_cache"),
                )

            with gr.Row():
                vfont = gr.Textbox(
                    label=B("config_vfont"),
                    placeholder="e.g. sans-serif:serif",
                )
                vchar = gr.Textbox(
                    label=B("config_vchar"),
                    placeholder="e.g. ...",
                )

            with gr.Row():
                page_range = gr.Textbox(
                    label=B("config_pages"),
                    placeholder="e.g. 1-5, 7, 10-12",
                )
                prompt_env = gr.Textbox(
                    label=B("config_prompt_env"),
                    info=B("config_prompt_env_info"),
                    placeholder="PROMPT=...",
                    lines=2,
                )

            with gr.Row():
                env0 = gr.Textbox(
                    label=B("config_env_label", num=1),
                    info=B("config_env_info"),
                    placeholder="OPENAI_API_KEY=sk-...",
                )
                env1 = gr.Textbox(
                    label=B("config_env_label", num=2),
                    info=B("config_env_info"),
                    placeholder="OPENAI_API_BASE=https://...",
                )
                env2 = gr.Textbox(
                    label=B("config_env_label", num=3),
                    info=B("config_env_info"),
                    placeholder="ANY_ENGINE_KEY=...",
                )

    return {
        "service": service,
        "lang_from": lang_from,
        "lang_to": lang_to,
        "mode_choice": mode_choice,
        "backend": backend,
        "ocr_mode": ocr_mode,
        "parse_engine": parse_engine,
        "magicpdf_ocr": magicpdf_ocr,
        "backend_status": backend_status,
        "threads": threads,
        "skip_subset_fonts": skip_subset_fonts,
        "ignore_cache": ignore_cache,
        "vfont": vfont,
        "vchar": vchar,
        "page_range": page_range,
        "prompt_env": prompt_env,
        "env0": env0,
        "env1": env1,
        "env2": env2,
    }
