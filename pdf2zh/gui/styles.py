"""Design system & runtime theme layer for the pdf2zh GUI.

Implements the "Design Tokens / App Shell / Theme Runtime" slice of the
next-generation architecture:

  * Design Tokens   - a single source of truth for color/spacing/radius/
                      typography/motion, defined twice (LIGHT / DARK) with
                      identical key sets so the UI can hot-swap themes
                      without touching components. Tokens are namespaced
                      by ontology: ``--color-*``, ``--shadow-*``,
                      ``--radius-*``, ``--space-*``, ``--text-*``,
                      ``--motion-*`` and ``--brand-*``.
  * Runtime Theme    - a ``data-theme`` attribute + ``dark`` class on
                      ``<html>/<body>`` toggled from the browser; dark mode
                      additionally overrides Gradio 5 theme variables so the
                      *whole* app (inputs, buttons, tabs) re-skins. The
                      Gradio variable map is *derived* from ``DARK_TOKENS``
                      (single source -- no duplicated hex values).
  * Session Layer    - SESSION_JS persisted client_id / theme / config / task
                      recovery (extends the legacy localStorage contract).
  * StepBar Pipeline - build_stepbar_html() renders the 4-stage progress rail
                      (上传 -> 版面分析 -> 翻译 -> 渲染) with ARIA roles.

Component modules consume ``var(--*)`` tokens only; they never embed
hard-coded colors. Tests assert token-key parity between the two palettes
and that the Gradio dark variables are derived from the dark palette.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

# =============================================================================
# 1. Design Tokens
# =============================================================================

#: Color tokens (--color-*).
COLOR_KEYS: Tuple[str, ...] = (
    # surfaces
    "color_bg",
    "color_surface",
    "color_surface_raised",
    # borders
    "color_border",
    "color_border_strong",
    # text
    "color_text_primary",
    "color_text_secondary",
    "color_text_tertiary",
    # action
    "color_accent",
    "color_accent_hover",
    "color_accent_soft",
    "color_on_accent",
    # semantic
    "color_success",
    "color_warning",
    "color_danger",
    "color_info",
    # stage states (StepBar / status badge)
    "color_stage_pending",
    "color_stage_running",
    "color_stage_done",
    "color_stage_error",
)

#: Elevation tokens (--shadow-*).
SHADOW_KEYS: Tuple[str, ...] = ("shadow_sm", "shadow_md")

#: Corner radius tokens (--radius-*).
RADIUS_KEYS: Tuple[str, ...] = ("radius_sm", "radius_md", "radius_lg")

#: Spacing scale tokens (--space-*), 4px base grid.
SPACE_KEYS: Tuple[str, ...] = (
    "space_1",
    "space_2",
    "space_3",
    "space_4",
    "space_5",
    "space_6",
    "space_8",
    "space_12",
)

#: Typography tokens (--text-*).
TYPE_KEYS: Tuple[str, ...] = (
    "text_font_sans",
    "text_font_mono",
    "text_xs",
    "text_sm",
    "text_base",
    "text_md",
    "text_lg",
    "text_xl",
    "text_weight_regular",
    "text_weight_medium",
    "text_weight_bold",
    "text_line_tight",
    "text_line_body",
)

#: Motion tokens (--motion-*).
MOTION_KEYS: Tuple[str, ...] = (
    "motion_fast",
    "motion_normal",
    "motion_slow",
    "motion_ease_standard",
    "motion_ease_emphasized",
)

#: Brand tokens (--brand-*).
BRAND_KEYS: Tuple[str, ...] = ("brand_gradient",)

#: Canonical key set -- both palettes MUST expose exactly these keys.
TOKEN_KEYS: Tuple[str, ...] = (
    COLOR_KEYS
    + SHADOW_KEYS
    + RADIUS_KEYS
    + SPACE_KEYS
    + TYPE_KEYS
    + MOTION_KEYS
    + BRAND_KEYS
)

#: Token key prefix -> CSS variable namespace.
_TOKEN_NAMESPACES: List[Tuple[str, str]] = [
    ("color_", "--color-"),
    ("shadow_", "--shadow-"),
    ("radius_", "--radius-"),
    ("space_", "--space-"),
    ("text_", "--text-"),
    ("motion_", "--motion-"),
    ("brand_", "--brand-"),
]

LIGHT_TOKENS: Dict[str, str] = {
    # surfaces
    "color_bg": "#f5f6fa",
    "color_surface": "#ffffff",
    "color_surface_raised": "#fafbfc",
    # borders
    "color_border": "#e2e6ee",
    "color_border_strong": "#c9cfdb",
    # text (WCAG 2.1 AA on --color-surface: primary 13.9:1,
    # secondary 4.8:1, tertiary 4.6:1)
    "color_text_primary": "#1d2129",
    "color_text_secondary": "#6b7280",
    "color_text_tertiary": "#6e7581",
    # action
    "color_accent": "#165dff",
    "color_accent_hover": "#0e42d2",
    "color_accent_soft": "rgba(22, 93, 255, 0.08)",
    "color_on_accent": "#ffffff",
    # semantic (danger/warning tuned for AA text contrast on white)
    "color_success": "#00b42a",
    "color_warning": "#b45309",
    "color_danger": "#d93838",
    "color_info": "#165dff",
    # stage states
    "color_stage_pending": "#6e7581",
    "color_stage_running": "#165dff",
    "color_stage_done": "#00b42a",
    "color_stage_error": "#d93838",
    # elevation
    "shadow_sm": "0 1px 2px rgba(29, 33, 41, 0.06)",
    "shadow_md": "0 4px 16px rgba(29, 33, 41, 0.10)",
    # radius
    "radius_sm": "6px",
    "radius_md": "10px",
    "radius_lg": "14px",
    # spacing (4px base grid)
    "space_1": "4px",
    "space_2": "8px",
    "space_3": "12px",
    "space_4": "16px",
    "space_5": "20px",
    "space_6": "24px",
    "space_8": "32px",
    "space_12": "48px",
    # typography
    "text_font_sans": (
        'ui-sans-serif, system-ui, "Segoe UI", "PingFang SC", '
        '"Microsoft YaHei", "Noto Sans CJK SC", sans-serif'
    ),
    "text_font_mono": ('ui-monospace, "Cascadia Code", "SF Mono", Consolas, monospace'),
    "text_xs": "12px",
    "text_sm": "13px",
    "text_base": "14px",
    "text_md": "15px",
    "text_lg": "17px",
    "text_xl": "20px",
    "text_weight_regular": "400",
    "text_weight_medium": "500",
    "text_weight_bold": "700",
    "text_line_tight": "1.3",
    "text_line_body": "1.55",
    # motion
    "motion_fast": "0.15s",
    "motion_normal": "0.25s",
    "motion_slow": "0.4s",
    "motion_ease_standard": "cubic-bezier(0.2, 0, 0, 1)",
    "motion_ease_emphasized": "cubic-bezier(0.2, 0, 0, 1)",
    # brand
    "brand_gradient": "linear-gradient(135deg, #165dff, #7a5cff)",
}

DARK_TOKENS: Dict[str, str] = {
    # surfaces
    "color_bg": "#17181c",
    "color_surface": "#1f2127",
    "color_surface_raised": "#26282f",
    # borders
    "color_border": "#33363f",
    "color_border_strong": "#4a4e59",
    # text
    "color_text_primary": "#e8eaee",
    "color_text_secondary": "#b0b6c0",
    "color_text_tertiary": "#9aa0ab",
    # action
    "color_accent": "#4c8dff",
    "color_accent_hover": "#7fb0ff",
    "color_accent_soft": "rgba(76, 141, 255, 0.14)",
    "color_on_accent": "#ffffff",
    # semantic
    "color_success": "#4cd265",
    "color_warning": "#fbbf24",
    "color_danger": "#ff7a7a",
    "color_info": "#4c8dff",
    # stage states
    "color_stage_pending": "#8a8f99",
    "color_stage_running": "#4c8dff",
    "color_stage_done": "#4cd265",
    "color_stage_error": "#ff7a7a",
    # elevation
    "shadow_sm": "0 1px 2px rgba(0, 0, 0, 0.40)",
    "shadow_md": "0 4px 16px rgba(0, 0, 0, 0.50)",
    # radius
    "radius_sm": "6px",
    "radius_md": "10px",
    "radius_lg": "14px",
    # spacing (identical scale in both palettes)
    "space_1": "4px",
    "space_2": "8px",
    "space_3": "12px",
    "space_4": "16px",
    "space_5": "20px",
    "space_6": "24px",
    "space_8": "32px",
    "space_12": "48px",
    # typography
    "text_font_sans": (
        'ui-sans-serif, system-ui, "Segoe UI", "PingFang SC", '
        '"Microsoft YaHei", "Noto Sans CJK SC", sans-serif'
    ),
    "text_font_mono": ('ui-monospace, "Cascadia Code", "SF Mono", Consolas, monospace'),
    "text_xs": "12px",
    "text_sm": "13px",
    "text_base": "14px",
    "text_md": "15px",
    "text_lg": "17px",
    "text_xl": "20px",
    "text_weight_regular": "400",
    "text_weight_medium": "500",
    "text_weight_bold": "700",
    "text_line_tight": "1.3",
    "text_line_body": "1.55",
    # motion
    "motion_fast": "0.15s",
    "motion_normal": "0.25s",
    "motion_slow": "0.4s",
    "motion_ease_standard": "cubic-bezier(0.2, 0, 0, 1)",
    "motion_ease_emphasized": "cubic-bezier(0.2, 0, 0, 1)",
    # brand
    "brand_gradient": "linear-gradient(135deg, #4c8dff, #8f6bff)",
}


def build_token_css(tokens: Dict[str, str]) -> str:
    """Render a palette dict into namespaced CSS variable blocks.

    Keys are grouped by their ``*_`` prefix into ``--color-*``, ``--shadow-*``,
    ``--radius-*``, ``--space-*``, ``--text-*``, ``--motion-*`` and
    ``--brand-*`` namespaces so the CSS ontology matches the token ontology.
    Inner underscores are normalized to hyphens (``color_surface_raised`` ->
    ``--color-surface-raised``).
    """
    blocks: List[str] = []
    for prefix, var_prefix in _TOKEN_NAMESPACES:
        lines = [f"  /* {prefix[:-1]} tokens */"]
        for key in TOKEN_KEYS:
            if key.startswith(prefix) and key in tokens:
                name = key[len(prefix) :].replace("_", "-")
                lines.append(f"  {var_prefix}{name}: {tokens[key]};")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


# =============================================================================
# 3. Gradio 5 dark-mode variable overrides
#    (DERIVED from DARK_TOKENS -- single source of truth, no duplicated hex)
# =============================================================================

#: Gradio CSS variable -> design token key (value comes from the palette).
_GRADIO_TOKEN_MAP: Dict[str, str] = {
    "--body-background-fill": "color_bg",
    "--body-text-color": "color_text_primary",
    "--body-text-color-subdued": "color_text_secondary",
    "--background-fill-primary": "color_bg",
    "--background-fill-secondary": "color_surface",
    "--block-background-fill": "color_surface",
    "--block-border-color": "color_border",
    "--block-title-text-color": "color_text_primary",
    "--block-label-text-color": "color_text_secondary",
    "--block-info-text-color": "color_text_tertiary",
    "--panel-background-fill": "color_surface",
    "--panel-border-color": "color_border",
    "--input-background-fill": "color_surface_raised",
    "--input-background-fill-focus": "color_surface_raised",
    "--input-border-color": "color_border_strong",
    "--input-border-color-focus": "color_accent",
    "--input-text-color": "color_text_primary",
    "--button-primary-background-fill": "color_accent",
    "--button-primary-background-fill-hover": "color_accent_hover",
    "--link-text-color": "color_accent_hover",
    "--link-text-color-hover": "color_accent",
    "--accordion-text-color": "color_text_primary",
    "--error-border-color": "color_danger",
    "--error-text-color": "color_danger",
    "--code-background-fill": "color_surface",
    "--loader-color": "color_accent",
    "--slider-color": "color_accent",
    "--color-accent": "color_accent",
    "--color-accent-soft": "color_accent_soft",
    "--border-color-primary": "color_border",
    "--border-color-accent": "color_accent",
    "--border-color-accent-subdued": "color_border_strong",
    "--neutral-100": "color_text_primary",
    "--neutral-200": "color_border_strong",
    "--neutral-300": "color_text_secondary",
    "--neutral-400": "color_text_tertiary",
    "--neutral-500": "color_text_secondary",
    "--neutral-600": "color_border_strong",
    "--neutral-700": "color_border",
    "--neutral-800": "color_surface_raised",
    "--neutral-900": "color_surface",
    "--neutral-950": "color_bg",
}

#: Gradio CSS variables without a 1:1 token; "{token_key}" placeholders are
#: interpolated from the palette so colors still come from one source.
_GRADIO_STATIC_DARK: Dict[str, str] = {
    "--block-label-background-fill": "transparent",
    "--input-background-fill-hover": "#2b2e36",
    "--input-border-color-hover": "{color_border_strong}",
    "--input-placeholder-color": "{color_text_tertiary}",
    "--button-primary-text-color": "#ffffff",
    "--button-primary-border-color": "transparent",
    "--button-secondary-background-fill": "{color_surface_raised}",
    "--button-secondary-background-fill-hover": "#2b2e36",
    "--button-secondary-text-color": "{color_text_primary}",
    "--button-secondary-border-color": "{color_border_strong}",
    "--button-cancel-background-fill": "transparent",
    "--button-cancel-background-fill-hover": "rgba(255, 122, 122, 0.12)",
    "--button-cancel-text-color": "{color_danger}",
    "--button-cancel-border-color": "{color_border}",
    "--checkbox-background-color": "{color_surface_raised}",
    "--checkbox-background-color-selected": "{color_accent}",
    "--checkbox-label-background-fill": "{color_surface_raised}",
    "--checkbox-label-background-fill-selected": "{color_accent_soft}",
    "--checkbox-label-text-color": "{color_text_primary}",
    "--checkbox-label-text-color-selected": "#ffffff",
    "--checkbox-label-border-color": "{color_border_strong}",
    "--checkbox-label-border-color-selected": "{color_accent}",
    "--error-background-fill": "rgba(255, 122, 122, 0.12)",
    "--shadow-drop": "0 1px 4px rgba(0, 0, 0, 0.40)",
    "--shadow-drop-lg": "0 4px 16px rgba(0, 0, 0, 0.50)",
    "--table-even-background-fill": "{color_surface}",
    "--table-odd-background-fill": "{color_surface_raised}",
    "--table-text-color": "{color_text_primary}",
    "--table-border-color": "{color_border}",
    "--neutral-50": "#f5f5f5",
}


def build_gradio_dark_vars(tokens: Dict[str, str]) -> Dict[str, str]:
    """Derive the Gradio dark-mode overrides from a token palette."""
    vars_: Dict[str, str] = {}
    for gradio_var, token_key in _GRADIO_TOKEN_MAP.items():
        vars_[gradio_var] = tokens[token_key]
    for gradio_var, template in _GRADIO_STATIC_DARK.items():
        vars_[gradio_var] = template.format(**tokens)
    return vars_


#: Ready-to-use dark-mode Gradio overrides (derived, cached).
GRADIO_DARK_VARS: Dict[str, str] = build_gradio_dark_vars(DARK_TOKENS)


# =============================================================================
# 4. Component stylesheet (token-driven, theme-agnostic)
# =============================================================================

COMPONENT_CSS = r"""
/* ---- global ---- */
.gradio-container {
    max-width: 1600px !important; margin: 0 auto !important;
    font-family: var(--text-font-sans);
}
footer { visibility: hidden; }

/* ---- app shell ---- */
.app-shell { padding: var(--space-1) 0 var(--space-6); }
.app-header {
    display: flex; align-items: center; justify-content: space-between;
    gap: var(--space-4); padding: var(--space-4) var(--space-5);
    margin-bottom: var(--space-3);
    background: var(--color-surface); border: 1px solid var(--color-border);
    border-radius: var(--radius-lg); box-shadow: var(--shadow-sm);
}
.app-brand { display: flex; align-items: center; gap: var(--space-3); }
.brand-block { flex: 1 1 auto !important; min-width: 0 !important; }
.badge-block { flex: 0 0 auto !important; }
.status-badge-box { padding: var(--space-1) 0; }
.brand-logo {
    display: inline-flex; align-items: center; justify-content: center;
    width: 40px; height: 40px; border-radius: var(--radius-md);
    background: var(--brand-gradient); color: var(--color-on-accent);
    font-weight: var(--text-weight-bold); font-size: var(--text-xs);
    letter-spacing: 0.5px; flex: 0 0 auto;
}
.brand-title {
    margin: 0; font-size: var(--text-lg); line-height: var(--text-line-tight);
    font-weight: var(--text-weight-bold);
}
.brand-subtitle { margin: 0; font-size: var(--text-xs); color: var(--color-text-secondary); }
.header-actions { display: flex; align-items: center; gap: var(--space-2); }
.theme-toggle-btn { min-width: 92px !important; }

/* ---- status badge ---- */
.status-badge {
    display: inline-flex; align-items: center; gap: var(--space-2);
    padding: var(--space-1) var(--space-3); border-radius: 999px;
    font-size: var(--text-xs); font-weight: var(--text-weight-medium);
    border: 1px solid var(--color-border);
    background: var(--color-surface-raised); color: var(--color-text-secondary);
    box-shadow: var(--shadow-sm); white-space: nowrap;
    max-width: 260px; overflow: hidden; text-overflow: ellipsis;
    transition: background-color var(--motion-fast) var(--motion-ease-standard),
                color var(--motion-fast) var(--motion-ease-standard);
}
.status-badge::before { content: "\25CF"; font-size: 0.7rem; }
.status-badge.status-running {
    color: var(--color-stage-running); background: var(--color-accent-soft);
}
.status-badge.status-running::before {
    color: var(--color-stage-running); animation: pulse-dot 1.2s infinite;
}
.status-badge.status-success { color: var(--color-stage-done); background: var(--color-accent-soft); }
.status-badge.status-success::before { color: var(--color-stage-done); }
.status-badge.status-error { color: var(--color-stage-error); background: var(--color-accent-soft); }
.status-badge.status-error::before { color: var(--color-stage-error); }
.status-badge.status-idle::before { color: var(--color-stage-pending); }

@keyframes pulse-dot { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }

/* ---- stepbar ---- */
.stepbar {
    display: flex; align-items: center; justify-content: space-between;
    gap: var(--space-2); padding: var(--space-4);
    margin-bottom: var(--space-3);
    background: var(--color-surface); border: 1px solid var(--color-border);
    border-radius: var(--radius-md); box-shadow: var(--shadow-sm);
}
.step-item { display: flex; align-items: center; gap: var(--space-2); flex: 1; min-width: 0; }
.step-dot {
    display: inline-flex; align-items: center; justify-content: center;
    width: 26px; height: 26px; border-radius: 50%; flex: 0 0 auto;
    font-size: var(--text-xs); font-weight: var(--text-weight-bold);
    background: var(--color-surface-raised); color: var(--color-stage-pending);
    border: 2px solid var(--color-border);
    transition: border-color var(--motion-normal) var(--motion-ease-standard),
                background-color var(--motion-normal) var(--motion-ease-standard),
                color var(--motion-normal) var(--motion-ease-standard);
}
.step-label {
    font-size: var(--text-sm); font-weight: var(--text-weight-medium);
    color: var(--color-text-secondary); white-space: nowrap;
    overflow: hidden; text-overflow: ellipsis; max-width: 96px;
}
.step-item.active .step-dot {
    border-color: var(--color-stage-running); color: var(--color-stage-running);
    box-shadow: 0 0 0 4px var(--color-accent-soft);
}
.step-item.active .step-label { color: var(--color-text-primary); }
.step-item.done .step-dot {
    border-color: var(--color-stage-done); background: var(--color-stage-done);
    color: var(--color-on-accent);
}
.step-item.done .step-label { color: var(--color-stage-done); }
.step-item.error .step-dot { border-color: var(--color-stage-error); color: var(--color-stage-error); }
.step-item.error .step-label { color: var(--color-stage-error); }
.step-connector { height: 2px; flex: 0.6; background: var(--color-border); border-radius: 1px; }
.step-connector.done { background: var(--color-stage-done); }
.step-connector.error { background: var(--color-stage-error); }
"""


COMPONENT_CSS += r"""
/* ---- section headers & panel cards ---- */
.section-header {
    margin-top: var(--space-1) !important; margin-bottom: var(--space-1) !important;
    font-size: var(--text-md) !important;
}
.panel-card {
    padding: var(--space-2) var(--space-3);
    margin-bottom: var(--space-3);
    border: 1px solid var(--color-border);
    border-radius: var(--radius-md);
    background: var(--color-surface);
    box-shadow: var(--shadow-sm);
}

/* ---- upload ---- */
.upload-area {
    border: 2px dashed var(--color-border) !important;
    border-radius: var(--radius-md) !important;
    transition: border-color var(--motion-fast) var(--motion-ease-standard);
}
.upload-area:hover { border-color: var(--color-accent) !important; }
.file-summary {
    padding: var(--space-2) var(--space-1); font-size: var(--text-sm);
    color: var(--color-text-secondary); line-height: var(--text-line-body);
}
.file-summary-item {
    display: inline-flex; align-items: center; gap: var(--space-1);
    margin: var(--space-1) var(--space-2) var(--space-1) 0;
    padding: var(--space-1) var(--space-2);
    background: var(--color-surface-raised); border: 1px solid var(--color-border);
    border-radius: 999px; font-size: var(--text-xs);
}

/* ---- progress ---- */
.progress-active, .progress-idle {
    padding: var(--space-3); border-radius: var(--radius-md);
    background: var(--color-surface-raised); border: 1px solid var(--color-border);
    margin-bottom: var(--space-2);
}
.progress-active { border-left: 3px solid var(--color-stage-running); }
.progress-active.progress-done { border-left-color: var(--color-stage-done); }
.progress-active.progress-error { border-left-color: var(--color-stage-error); }
.progress-head {
    display: flex; justify-content: space-between; align-items: baseline;
    font-size: var(--text-sm); font-weight: var(--text-weight-medium);
    margin-bottom: var(--space-2);
}
.progress-head .pct {
    color: var(--color-stage-running); font-variant-numeric: tabular-nums;
    font-weight: var(--text-weight-bold);
}
.progress-track { background: var(--color-border); border-radius: 4px; height: 8px; overflow: hidden; }
.progress-fill {
    height: 100%; min-width: 2px;
    background: linear-gradient(90deg, var(--color-stage-running), var(--color-accent-hover));
    border-radius: 4px;
    transition: width var(--motion-slow) var(--motion-ease-emphasized);
}
.progress-active.progress-done .progress-fill { background: var(--color-stage-done); }
.progress-active.progress-error .progress-fill { background: var(--color-stage-error); }
.progress-msg { margin-top: var(--space-2); font-size: var(--text-sm); color: var(--color-text-secondary); }
.progress-eta {
    margin-top: var(--space-1); font-size: var(--text-sm);
    color: var(--color-text-secondary); font-variant-numeric: tabular-nums;
}
.status-text { padding: var(--space-1) 0; }
.log-output {
    max-height: 300px; overflow-y: auto; padding: var(--space-2) var(--space-3);
    background: var(--color-surface-raised); color: var(--color-text-primary);
    border: 1px solid var(--color-border); border-radius: var(--radius-sm);
    font-family: var(--text-font-mono); font-size: var(--text-xs);
    line-height: var(--text-line-body);
}

/* ---- control row ---- */
.control-row { flex-wrap: wrap; gap: var(--space-2) !important; }

/* ---- preview ---- */
.preview-toolbar { display: flex; align-items: center; gap: var(--space-2); margin-bottom: var(--space-2); }
.preview-empty {
    display: flex; align-items: center; justify-content: center;
    min-height: var(--space-12); padding: var(--space-4);
    background: var(--color-surface-raised);
    border: 1px dashed var(--color-border); border-radius: var(--radius-md);
    color: var(--color-text-tertiary); font-size: var(--text-md);
    text-align: center;
}
.pdf-iframe-container {
    width: 100%; height: min(72vh, 640px); min-height: 420px;
    border: 1px solid var(--color-border); border-radius: var(--radius-md);
    overflow: hidden;
}
.pdf-iframe-container iframe { width: 100%; height: 100%; border: none; }
.result-select { min-width: 220px; }

/* ---- diagnostic ---- */
.diagnostic-overview, .quality-scores, .diagnostic-status {
    min-height: var(--space-12); padding: var(--space-2);
    background: var(--color-surface-raised); border-radius: var(--radius-sm);
}

/* ---- misc ---- */
.config-dropdown { min-width: 0; }

/* ---- a11y ---- */
.sr-only {
    position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
    overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0;
}
button:focus-visible, [role="button"]:focus-visible {
    outline: 2px solid var(--color-accent); outline-offset: 2px;
}

/* ---- responsive ---- */
@media (max-width: 1100px) {
    .app-main-row { flex-wrap: wrap !important; }
    .app-col-main, .app-col-side { flex: 1 1 100% !important; max-width: 100% !important; }
    .app-header { flex-wrap: wrap; }
}
@media (max-width: 760px) {
    .stepbar { flex-wrap: wrap; gap: var(--space-2); }
    .step-item { flex: 1 1 45%; }
    .step-connector { display: none; }
    .step-label { max-width: 140px; }
    .preview-toolbar { flex-wrap: wrap; }
    .progress-head { flex-wrap: wrap; gap: var(--space-1); }
}
@media (max-width: 480px) {
    .step-item { flex: 1 1 100%; }
    .brand-logo { display: none; }
    .status-badge { max-width: 200px; }
}

/* ---- reduced motion ---- */
@media (prefers-reduced-motion: reduce) {
    *, *::before, *::after {
        animation-duration: 0.01ms !important;
        animation-iteration-count: 1 !important;
        transition-duration: 0.01ms !important;
    }
}
"""


def build_ui_css() -> str:
    """Assemble the full UI stylesheet (light tokens + dark tokens + overrides)."""
    dark_vars = "\n".join(f"  {k}: {v};" for k, v in GRADIO_DARK_VARS.items())
    css = (
        f":root {{\n{build_token_css(LIGHT_TOKENS)}\n}}\n\n"
        f'html[data-theme="dark"] {{\n{build_token_css(DARK_TOKENS)}\n}}\n\n'
        f'html[data-theme="dark"] body {{\n{dark_vars}\n}}\n\n'
        f"{COMPONENT_CSS}\n"
    )
    return css


UI_CSS: str = build_ui_css()


# =============================================================================
# 5. Session layer (client identity, theme, config & task recovery)
# =============================================================================

#: JS helper that keeps the header theme-toggle label in sync with the
#: active theme (it is a plain button whose text is swapped client-side).
_THEME_LABEL_JS = (
    "function syncThemeLabel(dark) {"
    "var btn = document.getElementById('theme-toggle-btn');"
    "if (btn) { btn.textContent = dark"
    " ? '\u6d45\u8272\u6a21\u5f0f / Light'"
    " : '\u6df1\u8272\u6a21\u5f0f / Dark';"
    "}"
    "}"
)

SESSION_JS = (
    """<script>
(function() {
    // ---- theme (persisted, falls back to system preference) ----
    var root = document.documentElement;
    var storedTheme = null;
    try { storedTheme = localStorage.getItem("pdf2zh_theme"); } catch (e) {}
    var useSystem = !storedTheme;
    var isDark = storedTheme
        ? (storedTheme === "dark")
        : (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches);
    """
    + _THEME_LABEL_JS
    + """
    function applyTheme(dark) {
        root.setAttribute("data-theme", dark ? "dark" : "light");
        root.classList.toggle("dark", dark);
        if (document.body) { document.body.classList.toggle("dark", dark); }
        syncThemeLabel(dark);
    }
    applyTheme(isDark);
    if (window.matchMedia && useSystem) {
        window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", function (ev) {
            if (!localStorage.getItem("pdf2zh_theme")) { applyTheme(ev.matches); }
        });
    }
    // Gradio renders the header lazily; keep polling until the toggle exists.
    (function waitThemeBtn() {
        var tries = 0;
        var iv = setInterval(function () {
            tries++;
            syncThemeLabel(root.getAttribute("data-theme") === "dark");
            if (tries > 25) { clearInterval(iv); }
        }, 200);
    })();

    // ---- client identity ----
    var cid = localStorage.getItem("pdf2zh_client_id");
    if (!cid) {
        cid = "client_" + Math.random().toString(36).substr(2, 12);
        try { localStorage.setItem("pdf2zh_client_id", cid); } catch (e) {}
    }
    window.__pdf2zh_client_id = cid;

    // ---- task recovery across refresh ----
    try {
        var savedTaskId = localStorage.getItem("pdf2zh_last_task_id");
        if (savedTaskId) {
            window.__pdf2zh_last_task_id = savedTaskId;
            window.__pdf2zh_last_preview = localStorage.getItem("pdf2zh_last_preview_path") || "";
            window.__pdf2zh_last_results = JSON.parse(localStorage.getItem("pdf2zh_last_results") || "[]");
        }
    } catch (e) { window.__pdf2zh_last_results = []; }

    // ---- persist config panel values to localStorage when changed ----
    var configKeys = ["service","lang_from","lang_to","mode_choice","backend","parse_engine","magicpdf_ocr","threads",
        "ocr_mode",
        "skip_subset_fonts","ignore_cache","vfont","vchar","page_range",
        "prompt_env","env0","env1","env2"];
    document.addEventListener("change", function (e) {
        var t = e.target;
        if (!t || !t.id) return;
        for (var i = 0; i < configKeys.length; i++) {
            var k = configKeys[i];
            if (t.id.indexOf(k) !== -1) {
                try { localStorage.setItem("pdf2zh_config_" + k, t.value); } catch (ex) {}
                break;
            }
        }
    });

    // ---- event-driven sync (SSE push + auto replay, no polling Timer) ----
    // The server pushes the FULL JSON payload per published domain event and
    // embeds each frame with an SSE "id:" cursor. On reconnect, EventSource
    // automatically sends the browser's Last-Event-ID, so the server replays
    // every missed event before resuming the live stream -- a dropped
    // connection loses nothing. Each message wakes the hidden sync-trigger
    // button which pulls one delta sync (rendering stays server-side). If the
    // stream is down, a browser-side fallback polls at low frequency until
    // EventSource reconnects (the backend itself never polls).
    function wakeSync() {
        var btn = document.getElementById("sync-trigger");
        if (btn) { btn.click(); }
    }
    var fallbackPoll = null;
    function stopFallback() {
        if (fallbackPoll) { clearInterval(fallbackPoll); fallbackPoll = null; }
    }
    function connectEvents() {
        var es;
        try {
            es = new EventSource(window.location.origin + "/gui/events");
        } catch (e) { es = null; }
        if (!es) return;
        es.onopen = function () { stopFallback(); };
        es.onmessage = function (ev) {
            try {
                var data = JSON.parse(ev.data);
                if (data && data.task_id) { window.__pdf2zh_last_task_id = data.task_id; }
                window.__pdf2zh_last_event_seq = (data && data.seq) || 0;
            } catch (e) {}
            wakeSync();
        };
        es.onerror = function () {
            // Browser-side fallback ONLY while a task is active and the SSE
            // stream is down; EventSource auto-reconnects (onopen stops it).
            // No task -> no polling -> the idle page is never disturbed.
            if (!fallbackPoll && window.__pdf2zh_last_task_id) {
                fallbackPoll = setInterval(wakeSync, 5000);
            }
        };
    }
    if (window.EventSource) { connectEvents(); }
})();
</script>"""
)


#: Frontend handler wired to the theme toggle button (no backend round-trip).
#: Also flips the button label to announce the *next* action.
TOGGLE_THEME_JS = """() => {
    const root = document.documentElement;
    const next = root.getAttribute("data-theme") === "dark" ? "light" : "dark";
    const dark = next === "dark";
    root.setAttribute("data-theme", next);
    root.classList.toggle("dark", dark);
    document.body.classList.toggle("dark", dark);
    try { localStorage.setItem("pdf2zh_theme", next); } catch (e) {}
    const btn = document.getElementById("theme-toggle-btn");
    if (btn) {
        btn.textContent = dark
            ? "浅色模式 / Light"
            : "深色模式 / Dark";
    }
}"""


def build_status_badge_html(status: str, message: str = "") -> str:
    """Build the header status badge HTML from a runtime task status."""
    if status == "completed":
        cls, label = "status-success", "✓ 完成 / Done"
    elif status in ("failed", "cancelled"):
        cls, label = "status-error", (
            "已取消 / Cancelled" if status == "cancelled" else "失败 / Failed"
        )
    elif status in ("idle", ""):
        cls, label = "status-idle", "就绪 / Ready"
    else:
        cls, label = "status-running", "运行中 / Running"
    if message:
        label += f" · {message[:24]}"
    return f'<span class="status-badge {cls}" role="status">{label}</span>'


__all__ = [
    "TOKEN_KEYS",
    "LIGHT_TOKENS",
    "DARK_TOKENS",
    "GRADIO_DARK_VARS",
    "COMPONENT_CSS",
    "UI_CSS",
    "SESSION_JS",
    "TOGGLE_THEME_JS",
    "build_token_css",
    "build_gradio_dark_vars",
    "build_ui_css",
    "build_status_badge_html",
]
