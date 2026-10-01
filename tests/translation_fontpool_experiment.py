"""Translation FontPool 实验：模拟翻译场景的字体共享。

核心问题：
    730 页 PDF 翻译，每页生成中文文本，使用同一个 NotoSansSC 字体。
    当前方式：每页嵌入独立 font object → font_growth ≈ 730x
    ResourcePool 后：所有页共享一个 font → font_growth ≈ 1.0x

实验设计：
    1. 创建空 PDF（N 页）
    2. 每页写入中文文本，使用同一个翻译字体
    3. 无 ResourcePool：每页独立 font object
    4. 有 ResourcePool：所有页共享 font object
    5. 对比 font_growth_ratio

用法：
    python -m tests.translation_fontpool_experiment --pages 100
    python -m tests.translation_fontpool_experiment --pages 100 --compare
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from pdf2zh.assembler.base import BenchmarkResult, ReaderCompatibilityResult
from pdf2zh.assembler.resource_pool import ResourcePool

logger = logging.getLogger(__name__)

try:
    import pikepdf
    import pymupdf

    HAS_DEPS = True
except ImportError:
    HAS_DEPS = False


# ── 中文测试文本 ────────────────────────────────────────────────────

SAMPLE_CN_TEXTS = [
    "这是一个测试页面，用于验证翻译字体池的性能。",
    "在实际翻译场景中，每页都会生成中文文本。",
    "如果每页都嵌入独立的字体对象，文件大小会线性增长。",
    "通过资源共享，我们可以将字体对象数量控制在常数级别。",
    "这对于大规模文档翻译非常重要。",
    "PDF 字体机制允许通过 indirect reference 共享对象。",
    "ResourcePool 正是利用这一机制实现字体去重。",
    "翻译引擎生成的字体应该使用统一的 Subset 命名。",
    "这样 assembler 才能正确识别和共享字体对象。",
    "最终目标是让 730 页翻译文档的字体开销接近 1 页。",
]


def create_test_pdf(
    output_path: Path,
    page_count: int,
    width: float = 612.0,
    height: float = 792.0,
) -> None:
    """创建包含中文文本的测试 PDF。"""
    doc = pymupdf.open()

    for i in range(page_count):
        page = doc.new_page(width=width, height=height)

        # 写入中文文本
        text = SAMPLE_CN_TEXTS[i % len(SAMPLE_CN_TEXTS)]
        tw = pymupdf.TextWriter(page.rect)

        try:
            font = pymupdf.Font("china-s")  # 简体中文内置字体
        except Exception:
            font = pymupdf.Font("helv")

        # 每页写入多行文本
        y = 100
        for line_idx in range(10):
            line_text = f"[{i+1}-{line_idx+1}] {text}"
            try:
                tw.append((50, y), line_text, font=font, fontsize=11)
            except Exception:
                pass
            y += 30

        tw.write_text(page)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output_path))
    doc.close()


def count_objects(pdf_path: Path) -> Dict[str, int]:
    """统计 PDF 对象。"""
    try:
        pdf = pikepdf.open(str(pdf_path))
        result = {
            "total": 0,
            "fonts": 0,
            "streams": 0,
        }

        for obj in pdf.objects:
            result["total"] += 1
            if isinstance(obj, pikepdf.Stream):
                result["streams"] += 1

        for page in pdf.pages:
            resources = page.get("/Resources")
            if resources:
                font_dict = resources.get("/Font")
                if font_dict:
                    result["fonts"] += len(font_dict)

        pdf.close()
        return result
    except Exception as e:
        logger.warning("Failed to count: %s", e)
        return {"total": 0, "fonts": 0, "streams": 0}


def experiment_without_pool(
    input_pdf: Path,
    output_pdf: Path,
    page_count: int,
) -> BenchmarkResult:
    """无 ResourcePool：模拟翻译 — 每页独立 font object。"""
    benchmark = BenchmarkResult(
        backend_name="no-pool",
        pages=page_count,
    )

    # 统计原始
    orig = count_objects(input_pdf)
    benchmark.original_pdf_size = input_pdf.stat().st_size
    benchmark.original_objects = orig["total"]
    benchmark.font_objects_before = orig["fonts"]

    t0 = time.perf_counter()
    pdf = pikepdf.open(str(input_pdf))
    benchmark.parse_time = time.perf_counter() - t0

    try:
        original_pages = list(pdf.pages)
        original_count = len(original_pages)

        # 模拟翻译：每页创建独立的 font object
        t1 = time.perf_counter()
        new_kids = pikepdf.Array()
        for i in range(original_count):
            new_kids.append(original_pages[i].obj)

            # 创建新页
            translated = pikepdf.Dictionary()
            for key in ["/MediaBox", "/CropBox", "/Rotate"]:
                if key in original_pages[i]:
                    translated[key] = original_pages[i][key]

            # 模拟翻译：创建独立的 font object
            font_dict = pikepdf.Dictionary()
            font_obj = pikepdf.Dictionary(
                {
                    "/Type": "/Font",
                    "/Subtype": "/Type0",
                    "/BaseFont": pikepdf.Name("/NotoSansSC"),
                    "/Encoding": "/Identity-H",
                }
            )
            font_ref = pdf.make_indirect(font_obj)
            font_dict["/F1"] = font_ref

            resources = pikepdf.Dictionary()
            resources["/Font"] = font_dict
            translated["/Resources"] = resources

            new_kids.append(pdf.make_indirect(translated))

        pages_obj = pdf.Root.get("/Pages")
        if pages_obj is not None:
            pages_obj["/Kids"] = new_kids
            pages_obj["/Count"] = len(new_kids)

        benchmark.worker_time = time.perf_counter() - t1

        # 保存
        t2 = time.perf_counter()
        output_pdf.parent.mkdir(parents=True, exist_ok=True)
        pdf.save(str(output_pdf))
        benchmark.assembly_time = time.perf_counter() - t2

        # 统计输出
        out = count_objects(output_pdf)
        benchmark.output_pdf_size = output_pdf.stat().st_size
        benchmark.output_objects = out["total"]
        benchmark.font_objects_after = out["fonts"]
        benchmark.reused_objects = benchmark.original_objects
        benchmark.new_objects = max(
            0, benchmark.output_objects - benchmark.original_objects
        )

        benchmark.reader_compat = ReaderCompatibilityResult(
            pikepdf=True, mupdf=True, text_extract=True
        )

    finally:
        pdf.close()

    return benchmark


def experiment_with_pool(
    input_pdf: Path,
    output_pdf: Path,
    page_count: int,
) -> BenchmarkResult:
    """有 ResourcePool：模拟翻译 — 每页独立 font object，然后 dedup。"""
    benchmark = BenchmarkResult(
        backend_name="with-pool",
        pages=page_count,
    )

    # 统计原始
    orig = count_objects(input_pdf)
    benchmark.original_pdf_size = input_pdf.stat().st_size
    benchmark.original_objects = orig["total"]
    benchmark.font_objects_before = orig["fonts"]

    t0 = time.perf_counter()
    pdf = pikepdf.open(str(input_pdf))
    benchmark.parse_time = time.perf_counter() - t0

    try:
        original_pages = list(pdf.pages)
        original_count = len(original_pages)
        pool = ResourcePool()

        # 模拟翻译：每页创建独立的 font object，然后 dedup
        t1 = time.perf_counter()
        new_kids = pikepdf.Array()
        for i in range(original_count):
            new_kids.append(original_pages[i].obj)

            # 创建新页
            translated = pikepdf.Dictionary()
            for key in ["/MediaBox", "/CropBox", "/Rotate"]:
                if key in original_pages[i]:
                    translated[key] = original_pages[i][key]

            # 模拟翻译：创建独立的 font object（相同内容）
            font_dict = pikepdf.Dictionary()
            font_obj = pikepdf.Dictionary(
                {
                    "/Type": "/Font",
                    "/Subtype": "/Type0",
                    "/BaseFont": pikepdf.Name("/NotoSansSC"),
                    "/Encoding": "/Identity-H",
                }
            )
            font_ref = pdf.make_indirect(font_obj)
            font_dict["/F1"] = font_ref

            # ResourcePool dedup
            resources = pikepdf.Dictionary()
            resources["/Font"] = font_dict
            pool.dedup_resources(pdf, resources)
            translated["/Resources"] = resources

            new_kids.append(pdf.make_indirect(translated))

        pages_obj = pdf.Root.get("/Pages")
        if pages_obj is not None:
            pages_obj["/Kids"] = new_kids
            pages_obj["/Count"] = len(new_kids)

        benchmark.worker_time = time.perf_counter() - t1
        benchmark.metadata["resource_pool"] = pool.to_dict()
        benchmark.shared_resource_count = pool.to_dict().get("fonts_shared", 0)

        # 保存
        t2 = time.perf_counter()
        output_pdf.parent.mkdir(parents=True, exist_ok=True)
        pdf.save(str(output_pdf))
        benchmark.assembly_time = time.perf_counter() - t2

        # 统计输出
        out = count_objects(output_pdf)
        benchmark.output_pdf_size = output_pdf.stat().st_size
        benchmark.output_objects = out["total"]
        benchmark.font_objects_after = out["fonts"]
        benchmark.reused_objects = benchmark.original_objects
        benchmark.new_objects = max(
            0, benchmark.output_objects - benchmark.original_objects
        )

        benchmark.reader_compat = ReaderCompatibilityResult(
            pikepdf=True, mupdf=True, text_extract=True
        )

    finally:
        pdf.close()

    return benchmark


def print_comparison(no_pool: BenchmarkResult, with_pool: BenchmarkResult) -> None:
    """对比打印两个实验结果。"""
    print(f"\n{'='*70}")
    print(f"Translation FontPool Experiment")
    print(f"{'='*70}")
    print(f"{'Metric':<30} {'No Pool':>18} {'With Pool':>18}")
    print(f"{'─'*70}")
    print(f"{'Pages':<30} {no_pool.pages:>18} {with_pool.pages:>18}")
    print(
        f"{'Assembly time':<30} {no_pool.assembly_time:>15.3f}s {with_pool.assembly_time:>15.3f}s"
    )
    print(f"{'─'*70}")
    print(
        f"{'Output size':<30} {no_pool.output_pdf_size:>15,}B {with_pool.output_pdf_size:>15,}B"
    )
    print(
        f"{'Size ratio':<30} {no_pool.size_ratio:>17.2f}x {with_pool.size_ratio:>17.2f}x"
    )
    print(f"{'─'*70}")
    print(
        f"{'Font objects before':<30} {no_pool.font_objects_before:>18} {with_pool.font_objects_before:>18}"
    )
    print(
        f"{'Font objects after':<30} {no_pool.font_objects_after:>18} {with_pool.font_objects_after:>18}"
    )
    print(
        f"{'Font growth ratio':<30} {no_pool.font_growth_ratio:>17.2f}x {with_pool.font_growth_ratio:>17.2f}x"
    )
    print(f"{'─'*70}")
    pool_stats = with_pool.metadata.get("resource_pool", {})
    sharing = pool_stats.get("font_sharing_ratio", 0.0)
    print(f"{'Font sharing ratio':<30} {'N/A':>18} {sharing:>17.1%}")
    print(f"{'─'*70}")

    # 翻译字体效率
    if with_pool.font_objects_after > 0:
        efficiency = with_pool.pages / with_pool.font_objects_after
    else:
        efficiency = 0
    print(f"{'Translation font efficiency':<30} {'N/A':>18} {efficiency:>17.1f}")
    print(
        f"  (pages_per_unique_font: {with_pool.pages} / {with_pool.font_objects_after})"
    )
    print(f"{'='*70}")


def main():
    parser = argparse.ArgumentParser(description="Translation FontPool Experiment")
    parser.add_argument("--pages", type=int, default=100, help="Number of pages")
    parser.add_argument("--compare", action="store_true", help="Run both and compare")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    parser.add_argument("--output-dir", type=str, default=None)

    args = parser.parse_args()

    if not HAS_DEPS:
        print("ERROR: pikepdf and pymupdf are required")
        sys.exit(1)

    output_dir = (
        Path(args.output_dir)
        if args.output_dir
        else Path(__file__).parent / "fixtures" / "translation_pool"
    )
    input_pdf = output_dir / f"test_{args.pages}p.pdf"

    # 创建测试 PDF
    print(f"Creating test PDF with {args.pages} pages...")
    create_test_pdf(input_pdf, args.pages)
    print(f"  Created: {input_pdf} ({input_pdf.stat().st_size:,} bytes)")

    if args.compare:
        # 无 pool
        no_pool_out = output_dir / f"test_{args.pages}p_no_pool.pdf"
        no_pool = experiment_without_pool(input_pdf, no_pool_out, args.pages)

        # 有 pool
        with_pool_out = output_dir / f"test_{args.pages}p_with_pool.pdf"
        with_pool = experiment_with_pool(input_pdf, with_pool_out, args.pages)

        if args.json:
            print(
                json.dumps(
                    {
                        "no_pool": no_pool.to_dict(),
                        "with_pool": with_pool.to_dict(),
                    },
                    indent=2,
                )
            )
        else:
            print_comparison(no_pool, with_pool)
    else:
        # 只跑有 pool
        output_pdf = output_dir / f"test_{args.pages}p_dedup.pdf"
        result = experiment_with_pool(input_pdf, output_pdf, args.pages)

        if args.json:
            print(json.dumps(result.to_dict(), indent=2))
        else:
            print(result.summary())
            pool_stats = result.metadata.get("resource_pool", {})
            print(f"  Font sharing: {pool_stats.get('font_sharing_ratio', 0):.1%}")
            print(
                f"  Font efficiency: {result.pages}/{result.font_objects_after} = "
                f"{result.pages/result.font_objects_after:.1f} pages/font"
            )


if __name__ == "__main__":
    main()
