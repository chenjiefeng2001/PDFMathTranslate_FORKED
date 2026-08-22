"""Phase A 资产导出：i18n 字典 → JSON locale；Design Tokens → CSS 变量表。

SPA（Phase B）直接消费这些静态资产，避免前端硬编码文案与颜色：

    python -m pdf2zh.gui.export_assets            # 写入默认目录
    python -m pdf2zh.gui.export_assets --out DIR

产物（默认 pdf2zh/gui/assets/generated/）：
    locales/zh-CN.json, locales/en.json      {"ui": {...}, "stage": {...}}
    tokens/light.css, tokens/dark.css        :root{--color-bg: ...;} 形态
    tokens/tokens.json                       两套调色板的 JSON 形式
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict


def _kebab(key: str) -> str:
    return "--" + key.replace("_", "-")


def _css_vars(tokens: Dict[str, str], selector: str) -> str:
    lines = [f"{selector} {{"]
    lines += [f"  {_kebab(k)}: {v};" for k, v in sorted(tokens.items())]
    lines.append("}")
    return "\n".join(lines) + "\n"


def export_assets(out_dir: Path | None = None) -> Dict[str, Path]:
    """导出 i18n locale 与 design tokens，返回写入的文件路径表。"""
    from pdf2zh.gui.i18n import STAGE_LABELS, T
    from pdf2zh.gui.styles import DARK_TOKENS, LIGHT_TOKENS

    out_dir = Path(out_dir) if out_dir else (
        Path(__file__).parent / "assets" / "generated"
    )
    locales_dir = out_dir / "locales"
    tokens_dir = out_dir / "tokens"
    locales_dir.mkdir(parents=True, exist_ok=True)
    tokens_dir.mkdir(parents=True, exist_ok=True)

    ui_zh = {k: v[0] for k, v in T.items()}
    ui_en = {k: v[1] for k, v in T.items()}
    stage = dict(STAGE_LABELS)
    written: Dict[str, Path] = {}

    for name, payload in {
        "zh-CN.json": {"ui": ui_zh, "stage": stage},
        "en.json": {"ui": ui_en, "stage": stage},
    }.items():
        path = locales_dir / name
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        written[name] = path

    (tokens_dir / "light.css").write_text(
        _css_vars(LIGHT_TOKENS, ":root"), encoding="utf-8"
    )
    (tokens_dir / "dark.css").write_text(
        _css_vars(DARK_TOKENS, ":root[data-theme='dark']"), encoding="utf-8"
    )
    tokens_json = out_dir / "tokens" / "tokens.json"
    tokens_json.write_text(
        json.dumps(
            {"light": LIGHT_TOKENS, "dark": DARK_TOKENS},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return written


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Export GUI i18n/tokens assets")
    parser.add_argument("--out", default="", help="output directory")
    args = parser.parse_args()

    paths = export_assets(Path(args.out) if args.out else None)
    for name, path in sorted(paths.items()):
        print(f"{name}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
