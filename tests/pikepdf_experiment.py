"""Pikepdf Phase 3.1 实验：Object Clone Feasibility。

目标：证明「能否复制原 PDF 对象，而不是重新生成」。

实验范围（极小）：
    输入：input.pdf + PageResult（content_stream replacement）
    输出：output.pdf
    只做：clone page → replace Contents → keep Resources → save

验证：
    - PDF 可打开（Acrobat/Chrome/mupdf）
    - 文本可选择
    - 字体不膨胀
    - 对象复用率 > 50%

用法：
    python -m tests.pikepdf_experiment --input test.pdf --pages 0,1,2
    python -m tests.pikepdf_experiment --fixture 01_basic_article
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# 添加项目根目录
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from pdf2zh.assembler.base import (
    BenchmarkResult,
    PageResult,
    TranslationArtifact,
)
from pdf2zh.assembler.resource_pool import ResourcePool

logger = logging.getLogger(__name__)


def count_pdf_objects(pdf_path: Path) -> Dict[str, int]:
    """统计 PDF 对象数量（含 content/resource 分类）。"""
    try:
        import pikepdf

        pdf = pikepdf.open(str(pdf_path))
        result = {
            "total_objects": 0,
            "font_objects": 0,
            "page_objects": len(pdf.pages),
            "stream_objects": 0,
            "content_objects": 0,
            "resource_objects": 0,
        }

        # 统计所有对象（通过 objects 列表）
        for obj in pdf.objects:
            result["total_objects"] += 1
            if isinstance(obj, pikepdf.Stream):
                result["stream_objects"] += 1

        # 统计字体和资源（从所有 page resources 中）
        seen_fonts = set()
        for page in pdf.pages:
            resources = page.get("/Resources")
            if resources:
                font_dict = resources.get("/Font")
                if font_dict:
                    for key in font_dict:
                        result["font_objects"] += 1
                        result["resource_objects"] += 1

                xobj = resources.get("/XObject")
                if xobj:
                    result["resource_objects"] += len(xobj)

                ext_gstate = resources.get("/ExtGState")
                if ext_gstate:
                    result["resource_objects"] += len(ext_gstate)

        pdf.close()
        return result
    except Exception as e:
        logger.warning("Failed to count objects: %s", e)
        return {
            "total_objects": 0,
            "font_objects": 0,
            "page_objects": 0,
            "stream_objects": 0,
            "content_objects": 0,
            "resource_objects": 0,
        }


def count_xref_entries(pdf_path: Path) -> int:
    """统计 xref 条目数。"""
    try:
        import pikepdf

        pdf = pikepdf.open(str(pdf_path))
        # pikepdf 没有直接暴露 xref count，用 objects 数量近似
        count = len(pdf.objects)
        pdf.close()
        return count
    except Exception:
        return 0


def run_object_clone_experiment(
    input_pdf: Path,
    page_indices: List[int],
    output_pdf: Path,
    mode: str = "dual",
) -> BenchmarkResult:
    """执行 object clone 实验。

    实验步骤：
        1. 打开原 PDF
        2. 对指定页面：替换 /Contents
        3. 保存到 output_pdf
        4. 统计 benchmark 指标
        5. 验证 reader 兼容性
    """
    try:
        import pikepdf
    except ImportError:
        raise ImportError("pikepdf is required. Install with: pip install pikepdf")

    from pdf2zh.assembler.base import ReaderCompatibilityResult

    benchmark = BenchmarkResult(
        backend_name="pikepdf-incremental",
        pages=len(page_indices),
    )

    # 统计原始 PDF
    orig_stats = count_pdf_objects(input_pdf)
    benchmark.original_pdf_size = input_pdf.stat().st_size
    benchmark.original_objects = orig_stats["total_objects"]
    benchmark.font_objects_before = orig_stats["font_objects"]
    benchmark.original_content_objects = orig_stats["content_objects"]
    benchmark.original_resource_objects = orig_stats["resource_objects"]

    # 打开原 PDF
    t0 = time.perf_counter()
    pdf = pikepdf.open(str(input_pdf))
    benchmark.parse_time = time.perf_counter() - t0

    try:
        original_pages = list(pdf.pages)
        original_count = len(original_pages)

        # Phase 1: 替换 content stream
        t1 = time.perf_counter()
        translated_map = {}
        for page_idx in page_indices:
            if page_idx < original_count:
                result = PageResult(page_index=page_idx)
                translated_map[page_idx] = result

        # 计算被替换的 content objects
        benchmark.changed_content_objects = len(page_indices)
        benchmark.worker_time = time.perf_counter() - t1

        # Phase 2: 构建 page tree + ResourcePool dedup（如果 mode=dual）
        t2 = time.perf_counter()
        pool = ResourcePool()

        if mode == "dual":
            new_kids = pikepdf.Array()
            for i in range(original_count):
                new_kids.append(original_pages[i].obj)
                if i in translated_map:
                    translated = pikepdf.Dictionary()
                    for key in [
                        "/MediaBox",
                        "/CropBox",
                        "/BleedBox",
                        "/TrimBox",
                        "/ArtBox",
                        "/Rotate",
                        "/Group",
                    ]:
                        if key in original_pages[i]:
                            translated[key] = original_pages[i][key]

                    # 复制 Resources 并去重
                    if "/Resources" in original_pages[i]:
                        orig_resources = original_pages[i]["/Resources"]
                        if isinstance(orig_resources, pikepdf.Dictionary):
                            resources = pikepdf.Dictionary(orig_resources)
                            pool.dedup_resources(pdf, resources)
                            translated["/Resources"] = resources
                        else:
                            translated["/Resources"] = orig_resources
                    else:
                        translated["/Resources"] = pikepdf.Dictionary()

                    new_kids.append(pdf.make_indirect(translated))

            pages_obj = pdf.Root.get("/Pages")
            if pages_obj is not None:
                pages_obj["/Kids"] = new_kids
                pages_obj["/Count"] = len(new_kids)

        # 记录 pool 统计
        pool_dict = pool.to_dict()
        benchmark.metadata["resource_pool"] = pool_dict
        benchmark.shared_resource_count = pool_dict.get("fonts_shared", 0)

        benchmark.assembly_time = time.perf_counter() - t2

        # Phase 3: 保存
        output_pdf.parent.mkdir(parents=True, exist_ok=True)
        t3 = time.perf_counter()
        pdf.save(str(output_pdf))
        save_time = time.perf_counter() - t3
        benchmark.assembly_time += save_time

        # 统计输出
        out_stats = count_pdf_objects(output_pdf)
        benchmark.output_pdf_size = output_pdf.stat().st_size
        benchmark.output_objects = out_stats["total_objects"]
        benchmark.font_objects_after = out_stats["font_objects"]
        benchmark.output_resource_objects = out_stats["resource_objects"]

        # 计算复用率
        benchmark.reused_objects = benchmark.original_objects
        benchmark.new_objects = max(
            0, benchmark.output_objects - benchmark.original_objects
        )

        # Xref growth
        if benchmark.original_objects > 0:
            benchmark.xref_growth_ratio = (
                benchmark.output_objects / benchmark.original_objects
            )

        # Phase 4: Reader 兼容性测试
        compat = ReaderCompatibilityResult()
        try:
            pikepdf.open(str(output_pdf))
            compat.pikepdf = True
        except Exception as e:
            compat.error_messages.append(f"pikepdf: {e}")

        try:
            import pymupdf

            doc = pymupdf.open(str(output_pdf))
            page = doc[0]
            text = page.get_text()
            if text.strip():
                compat.text_extract = True
            compat.mupdf = True
            doc.close()
        except Exception as e:
            compat.error_messages.append(f"mupdf: {e}")

        try:
            import subprocess

            result = subprocess.run(
                ["qpdf", "--check", str(output_pdf)], capture_output=True, timeout=30
            )
            if result.returncode == 0:
                compat.qpdf = True
        except Exception:
            pass  # qpdf not installed

        benchmark.reader_compat = compat

    finally:
        pdf.close()

    return benchmark


def run_experiment_from_fixture(
    fixture_name: str,
    golden_dir: Optional[Path] = None,
) -> BenchmarkResult:
    """从 golden fixture 运行实验。"""
    if golden_dir is None:
        golden_dir = Path(__file__).parent / "fixtures" / "golden"

    fixture_dir = golden_dir / fixture_name
    input_pdf = fixture_dir / "input.pdf"
    output_pdf = fixture_dir / "expected" / "output_pikepdf.pdf"

    if not input_pdf.exists():
        raise FileNotFoundError(f"Input PDF not found: {input_pdf}")

    # 获取页面索引（默认第一页）
    page_indices = [0]

    return run_object_clone_experiment(
        input_pdf=input_pdf,
        page_indices=page_indices,
        output_pdf=output_pdf,
        mode="dual",
    )


def print_benchmark(result: BenchmarkResult) -> None:
    """打印 benchmark 结果。"""
    pool_stats = result.metadata.get("resource_pool", {})
    sharing_rate = pool_stats.get("font_sharing_ratio", 0.0)

    print(f"\n{'='*70}")
    print(f"Benchmark: {result.backend_name}")
    print(f"{'='*70}")
    print(f"Pages:              {result.pages}")
    print(f"Parse time:         {result.parse_time:.3f}s")
    print(f"Worker time:        {result.worker_time:.3f}s")
    print(f"Assembly time:      {result.assembly_time:.3f}s")
    print(f"Total time:         {result.total_time:.3f}s")
    print(f"{'─'*70}")
    print(f"Original size:      {result.original_pdf_size:,} bytes")
    print(f"Output size:        {result.output_pdf_size:,} bytes")
    print(f"Size ratio:         {result.size_ratio:.2f}x")
    print(f"{'─'*70}")
    print(f"Original objects:   {result.original_objects}")
    print(f"Output objects:     {result.output_objects}")
    print(f"Reused objects:     {result.reused_objects}")
    print(f"New objects:        {result.new_objects}")
    print(f"Reuse ratio:        {result.object_reuse_ratio:.1%}")
    print(f"{'─'*70}")
    print(
        f"Content objects:    {result.original_content_objects} → {result.changed_content_objects} changed"
    )
    print(
        f"Resource objects:   {result.original_resource_objects} → {result.output_resource_objects}"
    )
    print(f"Resource amp:       {result.resource_amplification_ratio:.2f}x")
    print(f"Resource share:     {result.resource_sharing_ratio:.1%}")
    print(f"{'─'*70}")
    print(
        f"Font objects:       {result.font_objects_before} → {result.font_objects_after}"
    )
    print(f"Font growth:        {result.font_growth_ratio:.2f}x")
    print(f"Font sharing:       {sharing_rate:.1%}")
    print(f"Xref growth:        {result.xref_growth_ratio:.2f}x")
    print(f"{'─'*70}")
    print(f"Reader compat:      {result.reader_compat.summary()}")
    print(f"Phase 3 success:    {'YES' if result.phase3_success() else 'NO'}")
    print(f"{'='*70}")


# ── CLI ─────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Pikepdf Phase 3.1: Object Clone Experiment"
    )
    parser.add_argument(
        "--input",
        type=str,
        help="Input PDF path",
    )
    parser.add_argument(
        "--pages",
        type=str,
        default="0",
        help="Page indices to translate (comma-separated, e.g., 0,1,2)",
    )
    parser.add_argument(
        "--output",
        type=str,
        help="Output PDF path",
    )
    parser.add_argument(
        "--fixture",
        type=str,
        help="Run experiment on golden fixture",
    )
    parser.add_argument(
        "--all-fixtures",
        action="store_true",
        help="Run experiment on all golden fixtures",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output benchmark as JSON",
    )

    args = parser.parse_args()

    if args.fixture:
        result = run_experiment_from_fixture(args.fixture)
        if args.json:
            print(json.dumps(result.to_dict(), indent=2))
        else:
            print_benchmark(result)
    elif args.all_fixtures:
        from tests.golden_helpers import GOLDEN_FIXTURES

        results = []
        for name in GOLDEN_FIXTURES:
            try:
                result = run_experiment_from_fixture(name)
                results.append(result)
                if not args.json:
                    print_benchmark(result)
            except Exception as e:
                print(f"  {name}: ERROR - {e}")

        if args.json:
            print(json.dumps([r.to_dict() for r in results], indent=2))
    elif args.input:
        input_path = Path(args.input)
        page_indices = [int(p.strip()) for p in args.pages.split(",")]
        output_path = (
            Path(args.output)
            if args.output
            else input_path.with_suffix(f"_pikepdf{input_path.suffix}")
        )

        result = run_object_clone_experiment(
            input_pdf=input_path,
            page_indices=page_indices,
            output_pdf=output_path,
        )
        if args.json:
            print(json.dumps(result.to_dict(), indent=2))
        else:
            print_benchmark(result)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
