"""Translation configuration panel component.

Replaces the 21-parameter inline form from Legacy gui.py with a
grouped, progressively-disclosed configuration form:

  * Basic   - engine / source / target language
  * Service - engine mode (v0..v4 pipeline selector)
  * Advanced- threads, font/char mapping, page range, custom env (collapsed)

All component keys returned here are part of the app.py wiring contract
and MUST stay stable.
"""

from __future__ import annotations

import gradio as gr

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

#: Pipeline mode choices surfaced in the Service group
MODE_CHOICES = ["auto", "v0", "v1", "v2", "v3", "v4"]
MODE_INFO = "v0: 基础 | v1: 普通 | v2: 高质量 | v3: 精准 | v4: 布局优先"


def create_config_panel() -> dict:
    """Create translation configuration form panel.

    Returns:
        dict of Gradio component references
    """
    with gr.Group(elem_classes="panel-card"):
        gr.Markdown("## ⚙️ 翻译配置 / Translation Config", elem_classes="section-header")

        # ---- 基础设置 / Basic ----
        with gr.Row():
            service = gr.Dropdown(
                choices=[e[1] for e in ENGINES],
                value="google",
                label="翻译引擎 / Engine",
                elem_classes="config-dropdown",
            )
            lang_from = gr.Dropdown(
                choices=[l[1] for l in LANGUAGES],
                value="auto",
                label="源语言 / Source",
                elem_classes="config-dropdown",
            )
            lang_to = gr.Dropdown(
                choices=[l[1] for l in LANGUAGES],
                value="zh-CN",
                label="目标语言 / Target",
                elem_classes="config-dropdown",
            )

        # ---- 服务模式 / Service ----
        mode_choice = gr.Radio(
            choices=MODE_CHOICES,
            value="auto",
            label="引擎模式 / Engine Mode",
            info=MODE_INFO,
        )

        # ---- 高级选项 / Advanced ----
        with gr.Accordion("🔧 高级选项 / Advanced Options", open=False):
            with gr.Row():
                threads = gr.Slider(
                    minimum=1, maximum=16, value=4, step=1,
                    label="线程数 / Threads",
                )
                skip_subset_fonts = gr.Checkbox(
                    value=False, label="跳过字体子集 / Skip Subset Fonts",
                )
                ignore_cache = gr.Checkbox(
                    value=False, label="忽略缓存 / Ignore Cache",
                )

            with gr.Row():
                vfont = gr.Textbox(
                    label="字体映射 / Font Map (V-Font)",
                    placeholder="e.g. sans-serif:serif",
                )
                vchar = gr.Textbox(
                    label="字符映射 / Char Map (V-Char)",
                    placeholder="e.g. ...",
                )

            with gr.Row():
                page_range = gr.Textbox(
                    label="页码范围 / Pages",
                    placeholder="e.g. 1-5, 7, 10-12",
                )
                prompt_env = gr.Textbox(
                    label="自定义 Prompt 环境变量",
                    placeholder="PROMPT=...",
                    lines=1,
                )

            with gr.Row():
                env0 = gr.Textbox(label="自定义环境变量 0", placeholder="KEY=VALUE")
                env1 = gr.Textbox(label="自定义环境变量 1", placeholder="KEY=VALUE")
                env2 = gr.Textbox(label="自定义环境变量 2", placeholder="KEY=VALUE")

    return {
        "service": service,
        "lang_from": lang_from,
        "lang_to": lang_to,
        "mode_choice": mode_choice,
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

