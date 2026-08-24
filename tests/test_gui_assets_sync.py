"""资产同步漂移校验：把 i18n/tokens 再导出到临时目录，与入库的
pdf2zh/gui/assets/generated/ 逐字节比对。

背景：generated/ 产物由 `python -m pdf2zh.gui.export_assets` 手工导出入库，
供 SPA 直接消费。若 pdf2zh/gui/i18n.py 或 pdf2zh/gui/styles.py 改动后忘记
重新导出，前端文案/配色将与 GUI 端静默漂移——本测试即为此兜底。

修复方式：运行 `python -m pdf2zh.gui.export_assets` 后提交更新后的产物。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pdf2zh.gui.export_assets import export_assets

GENERATED_DIR = (
    Path(__file__).resolve().parent.parent / "pdf2zh" / "gui" / "assets" / "generated"
)

EXPECTED_FILES = {
    "locales/zh-CN.json",
    "locales/en.json",
    "tokens/light.css",
    "tokens/dark.css",
    "tokens/tokens.json",
}

pytestmark = pytest.mark.skipif(
    not GENERATED_DIR.is_dir(), reason="generated assets 未入库（首次导出前跳过）"
)


def _walk(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def test_reexport_file_set_matches_committed(tmp_path: Path):
    """导出的文件集合必须与入库集合一致（多/少文件都视为漂移）。"""
    export_assets(tmp_path)
    exported = _walk(tmp_path)
    committed = _walk(GENERATED_DIR)
    assert exported == EXPECTED_FILES, f"导出集合异常: {sorted(exported)}"
    assert committed == EXPECTED_FILES, (
        f"入库集合漂移: 多={sorted(committed - EXPECTED_FILES)} "
        f"少={sorted(EXPECTED_FILES - committed)}"
    )


def test_reexport_bytes_match_committed(tmp_path: Path):
    """逐文件字节比对：i18n.py/styles.py 改动后未重导出会在此失败。"""
    export_assets(tmp_path)
    drifted = []
    for rel in sorted(EXPECTED_FILES):
        fresh = (tmp_path / rel).read_bytes()
        committed = (GENERATED_DIR / rel).read_bytes()
        if fresh != committed:
            drifted.append(rel)
    assert not drifted, (
        f"以下资产生成源已变化但产物未同步，请运行 "
        f"`python -m pdf2zh.gui.export_assets` 后重新提交: {drifted}"
    )
