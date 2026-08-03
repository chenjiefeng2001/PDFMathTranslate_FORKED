"""Design system & runtime theme layer for the pdf2zh GUI (Phase 1).

Implements the "Design Tokens / App Shell / Theme Runtime" slice of the
next-generation architecture:

  * Design Tokens   - a single source of truth for color/spacing/radius,
                      defined twice (LIGHT / DARK) with identical key sets so
                      the UI can hot-swap themes without touching components.
  * Runtime Theme    - a ``data-theme`` attribute + ``dark`` class on
                      ``<html>/<body>`` toggled from the browser; dark mode
                      additionally overrides Gradio 5 theme variables so the
                      *whole* app (inputs, buttons, tabs) re-skins.
  * Session Layer    - SESSION_JS persisted client_id / theme / config / task
                      recovery (extends the legacy localStorage contract).
  * StepBar Pipeline - build_stepbar_html() renders the 4-stage progress rail
                      (上传 -> 版面分析 -> 翻译 -> 渲染).

Component modules consume ``var(--color-*)`` tokens only; they never embed
hard-coded colors. Tests assert token-key parity between the two palettes.
"""

from __future__ import annotations

from typing import Dict

# =============================================================================
# 1. Design Tokens
# =============================================================================

#: Canonical key set -- both palettes MUST expose exactly these keys.
TOKEN_KEYS = (
    # surfaces
    "bg", "surface", "surface_raised",
    # borders
    "border", "border_strong",
    # text
    "text_primary", "text_secondary", "text_tertiary",
    # action
    "accent", "accent_hover", "accent_soft",
    # semantic
    "success", "warning", "danger", "info",
    # stage states (StepBar / status badge)
    "stage_pending", "stage_running", "stage_done", "stage_error",
    # elevation
    "shadow_sm", "shadow_md",
    # radius
    "radius_sm", "radius_md", "radius_lg",
)

LIGHT_TOKENS: Dict[str, str] = {
    # surfaces
    "bg": "#f5f6fa",
    "surface": "#ffffff",
    "surface_raised": "#fafbfc",
    # borders
    "border": "#e2e6ee",
    "border_strong": "#c9cfdb",
    # text
    "text_primary": "#1d2129",
    "text_secondary": "#6b7280",
    "text_tertiary": "#9aa0ab",
    # action
    "accent": "#165dff",
    "accent_hover": "#0e42d2",
    "accent_soft": "rgba(22, 93, 255, 0.08)",
    # semantic
    "success": "#00b42a",
    "warning": "#ff7d00",
    "danger": "#f53f3f",
    "info": "#165dff",
    # stage states
    "stage_pending": "#9aa0ab",
    "stage_running": "#165dff",
    "stage_done": "#00b42a",
    "stage_error": "#f53f3f",
    # elevation
    "shadow_sm": "0 1px 2px rgba(29, 33, 41, 0.06)",
    "shadow_md": "0 4px 16px rgba(29, 33, 41, 0.10)",
    # radius
    "radius_sm": "6px",
    "radius_md": "10px",
    "radius_lg": "14px",
}

DARK_TOKENS: Dict[str, str] = {
    # surfaces
    "bg": "#17181c",
    "surface": "#1f2127",
    "surface_raised": "#26282f",
    # borders
    "border": "#33363f",
    "border_strong": "#4a4e59",
    # text
    "text_primary": "#e8eaee",
    "text_secondary": "#b0b6c0",
    "text_tertiary": "#8a8f99",
    # action
    "accent": "#4c8dff",
    "accent_hover": "#7fb0ff",
    "accent_soft": "rgba(76, 141, 255, 0.14)",
    # semantic
    "success": "#4cd265",
    "warning": "#ffa65c",
    "danger": "#ff7a7a",
    "info": "#4c8dff",
    # stage states
    "stage_pending": "#8a8f99",
    "stage_running": "#4c8dff",
    "stage_done": "#4cd265",
    "stage_error": "#ff7a7a",
    # elevation
    "shadow_sm": "0 1px 2px rgba(0, 0, 0, 0.40)",
    "shadow_md": "0 4px 16px rgba(0, 0, 0, 0.50)",
    # radius
    "radius_sm": "6px",
    "radius_md": "10px",
    "radius_lg": "14px",
}


def build_token_css(tokens: Dict[str, str]) -> str:
    """Render a palette dict into a ``--color-*`` CSS variable block."""
    lines = ["  /* design tokens */"]
    for key in TOKEN_KEYS:
        if key in tokens:
            lines.append(f"  --color-{key}: {tokens[key]};")
    return "\n".join(lines)

# =============================================================================
# 3. Gradio 5 dark-mode variable overrides
#    (scoped to html[data-theme="dark"] body so we win the cascade)
# =============================================================================

GRADIO_DARK_VARS: Dict[str, str] = {
    "--body-background-fill": "#17181c",
    "--body-text-color": "#e8eaee",
    "--body-text-color-subdued": "#b0b6c0",
    "--background-fill-primary": "#17181c",
    "--background-fill-secondary": "#1f2127",
    "--block-background-fill": "#1f2127",
    "--block-border-color": "#33363f",
    "--block-title-text-color": "#e8eaee",
    "--block-label-background-fill": "transparent",
    "--block-label-text-color": "#b0b6c0",
    "--block-info-text-color": "#8a8f99",
    "--panel-background-fill": "#1f2127",
    "--panel-border-color": "#33363f",
    "--input-background-fill": "#26282f",
    "--input-background-fill-hover": "#2b2e36",
    "--input-background-fill-focus": "#26282f",
    "--input-border-color": "#3d414b",
    "--input-border-color-hover": "#4a4e59",
    "--input-border-color-focus": "#4c8dff",
    "--input-text-color": "#e8eaee",
    "--input-placeholder-color": "#6b7280",
    "--button-primary-background-fill": "#4c8dff",
    "--button-primary-background-fill-hover": "#7fb0ff",
    "--button-primary-text-color": "#ffffff",
    "--button-primary-border-color": "#4c8dff",
    "--button-secondary-background-fill": "#26282f",
    "--button-secondary-background-fill-hover": "#2b2e36",
    "--button-secondary-text-color": "#e8eaee",
    "--button-secondary-border-color": "#3d414b",
    "--button-cancel-background-fill": "transparent",
    "--button-cancel-background-fill-hover": "rgba(255, 122, 122, 0.12)",
    "--button-cancel-text-color": "#ff7a7a",
    "--button-cancel-border-color": "#3d414b",
    "--checkbox-background-color": "#26282f",
    "--checkbox-background-color-selected": "#4c8dff",
    "--checkbox-label-background-fill": "#26282f",
    "--checkbox-label-background-fill-selected": "rgba(76, 141, 255, 0.14)",
    "--checkbox-label-text-color": "#e8eaee",
    "--checkbox-label-text-color-selected": "#ffffff",
    "--checkbox-label-border-color": "#3d414b",
    "--checkbox-label-border-color-selected": "#4c8dff",
    "--link-text-color": "#7fb0ff",
    "--link-text-color-hover": "#a9c7ff",
    "--accordion-text-color": "#e8eaee",
    "--error-background-fill": "rgba(255, 122, 122, 0.12)",
    "--error-border-color": "#ff7a7a",
    "--error-text-color": "#ff7a7a",
    "--shadow-drop": "0 1px 4px rgba(0, 0, 0, 0.40)",
    "--shadow-drop-lg": "0 4px 16px rgba(0, 0, 0, 0.50)",
    "--code-background-fill": "#1d2129",
    "--loader-color": "#4c8dff",
    "--slider-color": "#4c8dff",
    "--color-accent": "#4c8dff",
    "--color-accent-soft": "rgba(76, 141, 255, 0.14)",
    "--border-color-primary": "#33363f",
    "--border-color-accent": "#4c8dff",
    "--border-color-accent-subdued": "#3d414b",
    "--table-even-background-fill": "#1f2127",
    "--table-odd-background-fill": "#26282f",
    "--table-text-color": "#e8eaee",
    "--table-border-color": "#33363f",
    "--neutral-50": "#f5f5f5",
    "--neutral-100": "#e8eaee",
    "--neutral-200": "#c6cbd4",
    "--neutral-300": "#b0b6c0",
    "--neutral-400": "#8a8f99",
    "--neutral-500": "#6b7280",
    "--neutral-600": "#4a4e59",
    "--neutral-700": "#33363f",
    "--neutral-800": "#26282f",
    "--neutral-900": "#1f2127",
    "--neutral-950": "#17181c",
}



# =============================================================================
# 4. Component stylesheet (token-driven, theme-agnostic)
# =============================================================================

COMPONENT_CSS = r"""
/* ---- global ---- */
.gradio-container { max-width: 1600px !important; margin: 0 auto !important; }
footer { visibility: hidden; }

/* ---- app shell ---- */
.app-shell { padding: 0.25rem 0 1.5rem; }
.app-header {
    display: flex; align-items: center; justify-content: space-between;
    gap: 1rem; padding: 1rem 1.25rem; margin-bottom: 0.75rem;
    background: var(--color-surface); border: 1px solid var(--color-border);
    border-radius: var(--color-radius-lg); box-shadow: var(--color-shadow-sm);
}
.app-brand { display: flex; align-items: center; gap: 0.75rem; }
.brand-block { flex: 1 1 auto !important; min-width: 0 !important; }
.badge-block { flex: 0 0 auto !important; }
.status-badge-box { padding: 0.2rem 0; }
.brand-logo {
    display: inline-flex; align-items: center; justify-content: center;
    width: 40px; height: 40px; border-radius: 10px;
    background: linear-gradient(135deg, var(--color-accent), #8f5bff);
    color: #fff; font-weight: 700; font-size: 13px; letter-spacing: 0.5px;
}
.brand-title { margin: 0; font-size: 1.15rem; line-height: 1.3; font-weight: 700; }
.brand-subtitle { margin: 0; font-size: 0.8rem; color: var(--color-text-secondary); }
.header-actions { display: flex; align-items: center; gap: 0.6rem; }
.theme-toggle-btn { min-width: 92px !important; }

/* ---- status badge ---- */
.status-badge {
    display: inline-flex; align-items: center; gap: 0.4rem;
    padding: 0.28rem 0.7rem; border-radius: 999px;
    font-size: 0.78rem; font-weight: 600; border: 1px solid transparent;
    background: var(--color-surface_raised); color: var(--color-text-secondary);
}
.status-badge::before { content: "\25CF"; font-size: 0.7rem; }
.status-badge.status-running { color: var(--color-stage-running); background: var(--color-accent-soft); }
.status-badge.status-running::before { color: var(--color-stage-running); animation: pulse-dot 1.2s infinite; }
.status-badge.status-success { color: var(--color-stage-done); background: var(--color-accent-soft); }
.status-badge.status-success::before { color: var(--color-stage-done); }
.status-badge.status-error { color: var(--color-stage-error); background: var(--color-accent-soft); }
.status-badge.status-error::before { color: var(--color-stage-error); }

@keyframes pulse-dot { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }

/* ---- stepbar ---- */
.stepbar {
    display: flex; align-items: center; justify-content: space-between;
    gap: 0.5rem; padding: 0.9rem 1.1rem; margin-bottom: 0.75rem;
    background: var(--color-surface); border: 1px solid var(--color-border);
    border-radius: var(--color-radius-md); box-shadow: var(--color-shadow-sm);
}
.step-item { display: flex; align-items: center; gap: 0.5rem; flex: 1; }
.step-dot {
    display: inline-flex; align-items: center; justify-content: center;
    width: 26px; height: 26px; border-radius: 50%; flex: 0 0 auto;
    font-size: 0.75rem; font-weight: 700;
    background: var(--color-surface_raised); color: var(--color-stage-pending);
    border: 2px solid var(--color-border); transition: all 0.25s ease;
}
.step-label { font-size: 0.82rem; font-weight: 600; color: var(--color-text-secondary); white-space: nowrap; }
.step-item.active .step-dot { border-color: var(--color-stage-running); color: var(--color-stage-running); box-shadow: 0 0 0 4px var(--color-accent-soft); }
.step-item.active .step-label { color: var(--color-text-primary); }
.step-item.done .step-dot { border-color: var(--color-stage-done); background: var(--color-stage-done); color: #fff; }
.step-item.done .step-label { color: var(--color-stage-done); }
.step-item.error .step-dot { border-color: var(--color-stage-error); color: var(--color-stage-error); }
.step-item.error .step-label { color: var(--color-stage-error); }
.step-connector { height: 2px; flex: 0.6; background: var(--color-border); border-radius: 1px; }
.step-connector.done { background: var(--color-stage-done); }
.step-connector.error { background: var(--color-stage-error); }
"""


COMPONENT_CSS += r"""
/* ---- section headers & panel cards ---- */
.section-header { margin-top: 0.35rem !important; margin-bottom: 0.2rem !important; font-size: 0.95rem; }
.panel-card { padding: 0.6rem 0.9rem; }

/* ---- upload ---- */
.upload-area { border: 2px dashed var(--color-border) !important; border-radius: var(--color-radius-md) !important; transition: border-color 0.2s ease; }
.upload-area:hover { border-color: var(--color-accent) !important; }
.file-summary { padding: 0.45rem 0.1rem; font-size: 0.82rem; color: var(--color-text-secondary); }
.file-summary-item {
    display: inline-flex; align-items: center; gap: 0.35rem;
    margin: 0.15rem 0.35rem 0.15rem 0; padding: 0.2rem 0.55rem;
    background: var(--color-surface_raised); border: 1px solid var(--color-border);
    border-radius: 999px; font-size: 0.78rem;
}

/* ---- progress ---- */
.progress-active, .progress-idle {
    padding: 0.7rem 0.85rem; border-radius: var(--color-radius-md);
    background: var(--color-surface_raised); border: 1px solid var(--color-border);
    margin-bottom: 0.4rem;
}
.progress-active { border-left: 3px solid var(--color-stage-running); }
.progress-active.progress-done { border-left-color: var(--color-stage-done); }
.progress-active.progress-error { border-left-color: var(--color-stage-error); }
.progress-head { display: flex; justify-content: space-between; align-items: baseline; font-size: 0.85rem; font-weight: 600; margin-bottom: 0.35rem; }
.progress-head .pct { color: var(--color-stage-running); font-variant-numeric: tabular-nums; }
.progress-track { background: var(--color-border); border-radius: 4px; height: 8px; overflow: hidden; }
.progress-fill { height: 100%; background: var(--color-stage-running); border-radius: 4px; transition: width 0.4s ease; }
.progress-active.progress-done .progress-fill { background: var(--color-stage-done); }
.progress-active.progress-error .progress-fill { background: var(--color-stage-error); }
.progress-msg { margin-top: 0.4rem; font-size: 0.82em; color: var(--color-text-secondary); }
.status-text { padding: 0.2rem 0.1rem; }
.log-output {
    max-height: 300px; overflow-y: auto; padding: 0.6rem 0.8rem;
    background: var(--color-surface_raised); color: var(--color-text-primary);
    border: 1px solid var(--color-border); border-radius: var(--color-radius-sm);
    font-family: ui-monospace, "Cascadia Code", Consolas, monospace; font-size: 12px;
}

/* ---- preview ---- */
.preview-toolbar { display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.5rem; }
.preview-empty {
    display: flex; align-items: center; justify-content: center;
    min-height: 420px; background: var(--color-surface_raised);
    border: 1px dashed var(--color-border); border-radius: var(--color-radius-md);
    color: var(--color-text-tertiary); font-size: 0.9rem;
}
.pdf-iframe-container { width: 100%; min-height: 520px; border: 1px solid var(--color-border); border-radius: var(--color-radius-md); overflow: hidden; }
.pdf-iframe-container iframe { width: 100%; min-height: 520px; border: none; }
.result-select { min-width: 220px; }

/* ---- diagnostic ---- */
.diagnostic-overview, .quality-scores, .diagnostic-status {
    min-height: 48px; padding: 0.5rem 0.6rem;
    background: var(--color-surface_raised); border-radius: var(--color-radius-sm);
}

/* ---- misc ---- */
.config-dropdown { min-width: 0; }
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

SESSION_JS = """<script>
(function() {
    // ---- theme (persisted, falls back to system preference) ----
    var root = document.documentElement;
    var storedTheme = null;
    try { storedTheme = localStorage.getItem("pdf2zh_theme"); } catch (e) {}
    var useSystem = !storedTheme;
    var isDark = storedTheme
        ? (storedTheme === "dark")
        : (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches);
    function applyTheme(dark) {
        root.setAttribute("data-theme", dark ? "dark" : "light");
        root.classList.toggle("dark", dark);
        if (document.body) { document.body.classList.toggle("dark", dark); }
    }
    applyTheme(isDark);
    if (window.matchMedia && useSystem) {
        window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", function (ev) {
            if (!localStorage.getItem("pdf2zh_theme")) { applyTheme(ev.matches); }
        });
    }

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
    var configKeys = ["service","lang_from","lang_to","mode_choice","threads",
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
})();
</script>"""


#: Frontend handler wired to the theme toggle button (no backend round-trip).
TOGGLE_THEME_JS = """() => {
    const root = document.documentElement;
    const next = root.getAttribute("data-theme") === "dark" ? "light" : "dark";
    const dark = next === "dark";
    root.setAttribute("data-theme", next);
    root.classList.toggle("dark", dark);
    document.body.classList.toggle("dark", dark);
    try { localStorage.setItem("pdf2zh_theme", next); } catch (e) {}
}"""


def build_status_badge_html(status: str, message: str = "") -> str:
    """Build the header status badge HTML from a runtime task status."""
    if status == "completed":
        cls, label = "status-success", "Complete"
    elif status in ("failed", "cancelled"):
        cls, label = "status-error", "Cancelled" if status == "cancelled" else "Failed"
    elif status in ("idle", ""):
        cls, label = "status-idle", "Ready"
    else:
        cls, label = "status-running", "Running"
    if message:
        label += f" · {message[:24]}"
    return f'<span class="status-badge {cls}">{label}</span>'


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
    "build_ui_css",
    "build_status_badge_html",
]

