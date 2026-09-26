"""Golden fixture 生成器。

从现有测试 PDF 生成 golden 三件套：
  1. input.pdf（复制源 PDF）
  2. artifact/page_XXX.json（PageResult 序列化）
  3. expected/semantic.json（语义期望值）

用法：
  python -m tests.golden_generate
  python -m tests.golden_generate --fixture 01_basic_article
  python -m tests.golden_generate --all
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# 添加项目根目录到 sys.path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from pdf2zh.assembler.base import (
    PageGeometry,
    PageResult,
    PageSize,
    ResourceRequirement,
    SCHEMA_VERSION,
)
from pdf2zh.assembler.artifacts import (
    BlockArtifact,
    BlockType,
    LayoutPolicy,
    POLICY_TOC,
    TOCEntryMeta,
    make_toc_block,
)
from pdf2zh.assembler.contract import CONTRACT_MUPDF, CONTRACT_PIKEPDF

# ── Golden 目录 ─────────────────────────────────────────────────────

GOLDEN_DIR = Path(__file__).parent / "fixtures" / "golden"
TEST_PDF_DIR = Path(__file__).parent / "file"


# ── Fixture 配置 ────────────────────────────────────────────────────

FIXTURE_CONFIGS = {
    "01_basic_article": {
        "source_pdf": "translate.cli.plain.text.pdf",
        "description": "Simple text article, single column",
        "expected": {
            "rotation": 0,
            "page_width": 612.0,
            "page_height": 792.0,
            "has_toc": False,
            "has_formula": False,
            "has_table": False,
            "font_count_min": 0,
        },
    },
    "02_toc_heavy": {
        "source_pdf": "The Rust Programming Language.pdf",
        "description": "Book with multi-level TOC",
        "expected": {
            "rotation": 0,
            "page_width": 612.0,
            "page_height": 792.0,
            "has_toc": True,
            "has_formula": False,
            "has_table": False,
            "font_count_min": 1,
        },
    },
    "03_landscape": {
        "source_pdf": None,  # 生成合成横版 PDF
        "description": "Landscape page with rotation=90",
        "expected": {
            "rotation": 90,
            "page_width": 595.0,  # 842×595 rotation=90 → 视觉 595×842
            "page_height": 842.0,
            "has_toc": False,
            "has_formula": False,
            "has_table": False,
            "font_count_min": 0,
        },
    },
    "04_formula": {
        "source_pdf": "translate.cli.text.with.figure.pdf",
        "description": "Article with formulas",
        "expected": {
            "rotation": 0,
            "page_width": 612.0,
            "page_height": 792.0,
            "has_toc": False,
            "has_formula": True,
            "has_table": False,
            "font_count_min": 0,
        },
    },
    "05_table": {
        "source_pdf": None,  # 生成合成表格 PDF
        "description": "Article with tables",
        "expected": {
            "rotation": 0,
            "page_width": 612.0,
            "page_height": 792.0,
            "has_toc": False,
            "has_formula": False,
            "has_table": True,
            "font_count_min": 0,
        },
    },
    "06_cjk": {
        "source_pdf": None,  # 生成合成 CJK PDF
        "description": "CJK text (Chinese/Japanese/Korean)",
        "expected": {
            "rotation": 0,
            "page_width": 612.0,
            "page_height": 792.0,
            "has_toc": False,
            "has_formula": False,
            "has_table": False,
            "font_count_min": 0,
        },
    },
    "07_large": {
        "source_pdf": "The Art of Multiprocessor Programming, 2e.pdf",
        "description": "Large book (100+ pages)",
        "expected": {
            "rotation": 0,
            "page_width": 612.0,
            "page_height": 792.0,
            "has_toc": True,
            "has_formula": False,
            "has_table": False,
            "font_count_min": 1,
        },
    },
}


# ── PDF 工具 ────────────────────────────────────────────────────────


def create_synthetic_pdf(
    output_path: Path,
    width: float = 612.0,
    height: float = 792.0,
    rotation: int = 0,
    text: str = "Synthetic Test Page",
    page_count: int = 1,
) -> None:
    """创建合成 PDF（仅依赖 pymupdf）。"""
    import pymupdf

    doc = pymupdf.open()
    for i in range(page_count):
        if rotation in (90, 270):
            page = doc.new_page(width=height, height=width)
        else:
            page = doc.new_page(width=width, height=height)

        # 写入文本
        tw = pymupdf.TextWriter(page.rect)
        font = pymupdf.Font("helv")
        tw.append((50, 100), f"{text} (page {i + 1})", font=font, fontsize=12)
        tw.write_text(page)

        # 设置旋转
        if rotation:
            page.set_rotation(rotation)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output_path))
    doc.close()


def copy_source_pdf(source_path: Path, dest_path: Path) -> None:
    """复制源 PDF 到 golden fixture。"""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, dest_path)


# ── Artifact 生成 ───────────────────────────────────────────────────


def generate_artifact_from_pdf(
    pdf_path: Path,
    page_index: int = 0,
    geometry: Optional[PageGeometry] = None,
    blocks: Optional[List[BlockArtifact]] = None,
) -> PageResult:
    """从 PDF 生成 PageResult artifact（简化版：仅几何信息）。"""
    import pymupdf

    doc = pymupdf.open(str(pdf_path))
    if page_index >= len(doc):
        doc.close()
        raise ValueError(f"page_index {page_index} >= page_count {len(doc)}")

    page = doc[page_index]
    rect = page.rect
    rotation = page.rotation

    # 构建 geometry
    if geometry is None:
        media_box = (rect.x0, rect.y0, rect.x1, rect.y1)
        geometry = PageGeometry(
            media_box=media_box,
            rotation=rotation,
        )

    # 构建 page_size
    page_size = PageSize(
        width=geometry.width,
        height=geometry.height,
        media_box=geometry.media_box,
    )

    # 提取文本块（用于 blocks 生成）
    if blocks is None:
        blocks = []

    # 构建 PageResult
    result = PageResult(
        schema_version=SCHEMA_VERSION,
        page_index=page_index,
        source_page_hash=hashlib.sha256(page.get_text().encode()).hexdigest()[:16],
        page_size=page_size,
        geometry=geometry,
        content_stream=b"",  # 简化：不生成 content stream
        resources=ResourceRequirement(),
        blocks=blocks,
        annotations=[],
        links=[],
    )

    doc.close()
    return result


def save_artifact(result: PageResult, output_path: Path) -> None:
    """保存 PageResult 为 JSON。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    data = result._to_dict()
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def save_expected_semantic(
    result: PageResult,
    config: Dict,
    output_path: Path,
) -> None:
    """保存 expected semantic.json。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    expected = config["expected"].copy()
    # 覆盖为实际值（从 artifact 读取）
    expected["page_width"] = result.page_width
    expected["page_height"] = result.page_height
    expected["rotation"] = result.rotation
    expected["schema_version"] = result.schema_version
    expected["page_index"] = result.page_index
    expected["actual_page_width"] = result.page_width
    expected["actual_page_height"] = result.page_height
    expected["actual_rotation"] = result.rotation
    expected["block_count"] = len(result.blocks)
    expected["has_geometry"] = result.geometry is not None

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(expected, f, indent=2, ensure_ascii=False)


def save_backend_contract(output_path: Path) -> None:
    """保存 backend_contract.json。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    contract = {
        "mupdf": {
            "must_preserve": CONTRACT_MUPDF.must_preserve,
            "allowed_changes": CONTRACT_MUPDF.allowed_changes,
            "forbidden_changes": CONTRACT_MUPDF.forbidden_changes,
        },
        "pikepdf": {
            "must_preserve": CONTRACT_PIKEPDF.must_preserve,
            "allowed_changes": CONTRACT_PIKEPDF.allowed_changes,
            "forbidden_changes": CONTRACT_PIKEPDF.forbidden_changes,
        },
    }
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(contract, f, indent=2, ensure_ascii=False)


# ── 主生成逻辑 ──────────────────────────────────────────────────────


def generate_fixture(fixture_name: str, force: bool = False) -> bool:
    """生成单个 golden fixture。"""
    config = FIXTURE_CONFIGS.get(fixture_name)
    if config is None:
        print(f"Unknown fixture: {fixture_name}")
        return False

    fixture_dir = GOLDEN_DIR / fixture_name
    input_pdf = fixture_dir / "input.pdf"

    # 检查是否已存在
    if input_pdf.exists() and not force:
        print(f"  {fixture_name}: already exists, skip (use --force to regenerate)")
        return True

    print(f"  {fixture_name}: generating...")

    # 1. 获取/生成 input.pdf
    source_pdf_name = config.get("source_pdf")
    if source_pdf_name and source_pdf_name != "None":
        source_path = TEST_PDF_DIR / source_pdf_name
        if not source_path.exists():
            print(f"    WARNING: source PDF not found: {source_path}")
            print(f"    Generating synthetic PDF instead")
            source_path = None
        else:
            copy_source_pdf(source_path, input_pdf)
    else:
        source_path = None

    if source_path is None:
        # 生成合成 PDF
        expected = config["expected"]
        create_synthetic_pdf(
            input_pdf,
            width=expected.get("page_width", 612.0),
            height=expected.get("page_height", 792.0),
            rotation=expected.get("rotation", 0),
            text=fixture_name,
        )

    # 2. 生成 artifact
    result = generate_artifact_from_pdf(input_pdf, page_index=0)
    artifact_path = fixture_dir / "artifact" / "page_000.json"
    save_artifact(result, artifact_path)

    # 3. 生成 expected semantic.json
    expected_path = fixture_dir / "expected" / "semantic.json"
    save_expected_semantic(result, config, expected_path)

    # 4. 生成 backend_contract.json
    contract_path = fixture_dir / "expected" / "backend_contract.json"
    save_backend_contract(contract_path)

    print(f"    input.pdf: {input_pdf.stat().st_size:,} bytes")
    print(f"    artifact/page_000.json: {artifact_path.stat().st_size:,} bytes")
    print(f"    expected/semantic.json: {expected_path.stat().st_size:,} bytes")
    print(f"    expected/backend_contract.json: {contract_path.stat().st_size:,} bytes")

    return True


def generate_all(force: bool = False) -> None:
    """生成所有 golden fixtures。"""
    print("Generating golden fixtures...")
    print(f"  Target directory: {GOLDEN_DIR}")
    print()

    success = 0
    failed = 0
    for name in FIXTURE_CONFIGS:
        try:
            if generate_fixture(name, force):
                success += 1
            else:
                failed += 1
        except Exception as e:
            print(f"    ERROR: {e}")
            failed += 1

    print()
    print(f"Done: {success} success, {failed} failed")


# ── CLI ─────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Generate golden regression fixtures")
    parser.add_argument(
        "--fixture",
        type=str,
        help="Generate specific fixture (e.g., 01_basic_article)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Generate all fixtures",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Regenerate existing fixtures",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List available fixtures",
    )

    args = parser.parse_args()

    if args.list:
        print("Available fixtures:")
        for name, config in FIXTURE_CONFIGS.items():
            source = config.get("source_pdf") or "(synthetic)"
            print(f"  {name}: {config['description']}")
            print(f"    source: {source}")
        return

    if args.fixture:
        generate_fixture(args.fixture, force=args.force)
    elif args.all:
        generate_all(force=args.force)
    else:
        print("Usage: python -m tests.golden_generate --all")
        print("       python -m tests.golden_generate --fixture 01_basic_article")
        print("       python -m tests.golden_generate --list")


if __name__ == "__main__":
    main()
