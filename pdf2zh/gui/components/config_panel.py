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
MODE_CHOICES = [
    (B("config_mode_auto"), "auto"),
    (B("config_mode_v0"), "v0"),
    (B("config_mode_v1"), "v1"),
    (B("config_mode_v2"), "v2"),
    (B("config_mode_v3"), "v3"),
    (B("config_mode_v4"), "v4"),
]


def create_config_panel() -> dict:
    """Create translation configuration form panel.

    Returns:
        dict of Gradio component references
    """
    with gr.Group(elem_classes="panel-card"):
        gr.Markdown(f"## ⚙️ {B('section_config')}", elem_classes="section-header")

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

        # ---- 高级选项 / Advanced ----
        with gr.Accordion(f"🔧 {B('config_advanced')}", open=False):
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
                    placeholder="PROMPT=...",
                    lines=1,
                )

            with gr.Row():
                env0 = gr.Textbox(label=f"{B('config_env')} 0", placeholder="KEY=VALUE")
                env1 = gr.Textbox(label=f"{B('config_env')} 1", placeholder="KEY=VALUE")
                env2 = gr.Textbox(label=f"{B('config_env')} 2", placeholder="KEY=VALUE")

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
