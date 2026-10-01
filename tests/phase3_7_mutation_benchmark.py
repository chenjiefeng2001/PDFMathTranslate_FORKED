"""Phase 3.7: Translation Mutation Benchmark.

模拟真实翻译：替换 page content stream，测量 mutation radius。

核心指标：
    - objects touched / total objects = mutation ratio
    - write bytes / original size = growth ratio
    - time: full rewrite vs incremental

用法：
    python -m tests.phase3_7_mutation_benchmark --all
"""

from __future__ import annotations

import io
import os
import sys
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pikepdf

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "benchmark_mutation"
TEST_DIR = Path(__file__).parent / "file"

LARGE_FILES = [
    "2104.13478v2.pdf",
    "2407.18384v4.pdf",
    "The Rust Programming Language.pdf",
    "itbook-export.pdf",
]


@dataclass
class MutationResult:
    method: str = ""
    file_name: str = ""
    pages: int = 0
    original_size: int = 0
    output_size: int = 0
    total_objects: int = 0
    objects_touched: int = 0
    objects_appended: int = 0
    time_load: float = 0.0
    time_mutation: float = 0.0
    time_write: float = 0.0
    time_total: float = 0.0
    valid: bool = False
    page_count_check: int = 0

    @property
    def mutation_ratio(self) -> float:
        return (
            self.objects_touched / self.total_objects if self.total_objects > 0 else 0
        )

    @property
    def growth_ratio(self) -> float:
        return self.output_size / self.original_size if self.original_size > 0 else 0

    @property
    def append_bytes(self) -> int:
        return (
            self.output_size - self.original_size
            if self.output_size > self.original_size
            else 0
        )

    def row(self) -> str:
        return (
            f"{self.method:<20} {self.pages:>5} "
            f"{self.time_load:>6.2f}s {self.time_mutation:>6.2f}s "
            f"{self.time_write:>6.2f}s {self.time_total:>6.2f}s "
            f"{self.mutation_ratio:>5.1%} "
            f"{self.growth_ratio:>5.2f}x "
            f"{str(self.valid):>5}"
        )


def count_objects_deep(pdf: pikepdf.Pdf) -> int:
    """统计 PDF 中所有间接对象数量。"""
    count = 0
    for obj in pdf.objects:
        count += 1
    return count


def simulate_translation(page: pikepdf.Page, pdf: pikepdf.Pdf, page_idx: int) -> bytes:
    """模拟翻译：生成替换的 content stream。

    返回新的 compressed content stream bytes。
    """
    # 生成模拟翻译内容
    zh_text = f"[Page {page_idx + 1}] 这是模拟翻译文本。定理立即成立。"
    content = f"BT /F1 12 Tf 100 700 Td ({zh_text}) Tj ET".encode("utf-8")
    return content


def benchmark_full_rewrite(
    input_pdf: Path,
    output_dir: Path,
    max_pages: Optional[int] = None,
) -> MutationResult:
    """Case A: Full rewrite — load → replace all pages → save."""
    output_dir.mkdir(parents=True, exist_ok=True)
    output_pdf = output_dir / f"{input_pdf.stem}_full_rewrite.pdf"

    result = MutationResult(method="full_rewrite")
    result.file_name = input_pdf.name
    result.original_size = input_pdf.stat().st_size

    # Load
    t0 = time.perf_counter()
    pdf = pikepdf.open(str(input_pdf))
    result.time_load = time.perf_counter() - t0
    result.pages = len(pdf.pages)

    if max_pages:
        result.pages = min(result.pages, max_pages)

    result.total_objects = count_objects_deep(pdf)

    # Mutation: replace each page's content stream
    t1 = time.perf_counter()
    for i in range(result.pages):
        page = pdf.pages[i]
        new_content = simulate_translation(page, pdf, i)
        compressed = zlib.compress(new_content)
        stream = pikepdf.Stream(pdf, new_content)
        ref = pdf.make_indirect(stream)
        page["/Contents"] = ref
        result.objects_touched += 1  # page dict + content stream
    result.time_mutation = time.perf_counter() - t1

    # Write
    t2 = time.perf_counter()
    pdf.save(str(output_pdf))
    result.time_write = time.perf_counter() - t2
    result.time_total = result.time_load + result.time_mutation + result.time_write

    result.output_size = output_pdf.stat().st_size
    result.objects_appended = result.objects_touched  # full rewrite = all touched

    # Validate
    try:
        test = pikepdf.open(str(output_pdf))
        result.valid = True
        result.page_count_check = len(test.pages)
        test.close()
    except Exception:
        result.valid = False

    pdf.close()
    return result


def benchmark_incremental_append(
    input_pdf: Path,
    output_dir: Path,
    max_pages: Optional[int] = None,
) -> MutationResult:
    """Case B: Incremental — load → replace → append new objects."""
    output_dir.mkdir(parents=True, exist_ok=True)
    output_pdf = output_dir / f"{input_pdf.stem}_incremental.pdf"

    result = MutationResult(method="incremental")
    result.file_name = input_pdf.name
    result.original_size = input_pdf.stat().st_size

    # Load from bytes (preserve original)
    t0 = time.perf_counter()
    source_bytes = input_pdf.read_bytes()
    pdf = pikepdf.open(io.BytesIO(source_bytes))
    result.time_load = time.perf_counter() - t0
    result.pages = len(pdf.pages)

    if max_pages:
        result.pages = min(result.pages, max_pages)

    result.total_objects = count_objects_deep(pdf)

    # Mutation: replace content streams
    t1 = time.perf_counter()
    for i in range(result.pages):
        page = pdf.pages[i]
        new_content = simulate_translation(page, pdf, i)
        stream = pikepdf.Stream(pdf, new_content)
        ref = pdf.make_indirect(stream)
        page["/Contents"] = ref
        result.objects_touched += 1
    result.time_mutation = time.perf_counter() - t1

    # Write to BytesIO first, then to file
    t2 = time.perf_counter()
    buf = io.BytesIO()
    pdf.save(buf)
    buf.seek(0)
    output_pdf.write_bytes(buf.read())
    result.time_write = time.perf_counter() - t2
    result.time_total = result.time_load + result.time_mutation + result.time_write

    result.output_size = output_pdf.stat().st_size
    result.objects_appended = result.objects_touched

    # Validate
    try:
        test = pikepdf.open(str(output_pdf))
        result.valid = True
        result.page_count_check = len(test.pages)
        test.close()
    except Exception:
        result.valid = False

    pdf.close()
    return result


def print_comparison(full: MutationResult, incr: MutationResult):
    """打印对比表。"""
    print()
    print(f"{'Metric':<30} {'Full Rewrite':>15} {'Incremental':>15}")
    print("-" * 65)
    print(f"{'Pages':<30} {full.pages:>15} {incr.pages:>15}")
    print(f"{'Total objects':<30} {full.total_objects:>15} {incr.total_objects:>15}")
    print(
        f"{'Objects touched':<30} {full.objects_touched:>15} {incr.objects_touched:>15}"
    )
    print(
        f"{'Mutation ratio':<30} {full.mutation_ratio:>14.1%} {incr.mutation_ratio:>14.1%}"
    )
    print()
    print(f"{'Time load (s)':<30} {full.time_load:>15.3f} {incr.time_load:>15.3f}")
    print(
        f"{'Time mutation (s)':<30} {full.time_mutation:>15.3f} {incr.time_mutation:>15.3f}"
    )
    print(f"{'Time write (s)':<30} {full.time_write:>15.3f} {incr.time_write:>15.3f}")
    print(f"{'Time total (s)':<30} {full.time_total:>15.3f} {incr.time_total:>15.3f}")
    print()
    print(
        f"{'Output size (MB)':<30} {full.output_size/1024/1024:>15.2f} {incr.output_size/1024/1024:>15.2f}"
    )
    print(
        f"{'Growth ratio':<30} {full.growth_ratio:>14.2f}x {incr.growth_ratio:>14.2f}x"
    )
    print(
        f"{'Append bytes (KB)':<30} {full.append_bytes/1024:>14.1f} {incr.append_bytes/1024:>14.1f}"
    )
    print(f"{'Valid':<30} {str(full.valid):>15} {str(incr.valid):>15}")
    print()

    # Mutation radius analysis
    print("Mutation Radius Analysis:")
    print(f"  Total objects:    {full.total_objects}")
    print(f"  Objects touched:  {full.objects_touched} ({full.mutation_ratio:.1%})")
    print(
        f"  Objects NOT touched: {full.total_objects - full.objects_touched} ({1-full.mutation_ratio:.1%})"
    )
    print()

    if full.time_write > 0:
        write_speedup = full.time_write / incr.time_write if incr.time_write > 0 else 0
        print(f"Write speedup: {write_speedup:.2f}x")
    if full.output_size > 0:
        size_diff = incr.output_size - full.output_size
        print(
            f"Size difference: {size_diff/1024:.1f} KB ({size_diff/full.output_size*100:.1f}%)"
        )


def run_single(input_pdf: Path, output_dir: Path, max_pages: Optional[int] = None):
    """Run full rewrite vs incremental for a single file."""
    print(f"\n{'='*75}")
    print(f"File: {input_pdf.name} ({input_pdf.stat().st_size / 1024 / 1024:.1f} MB)")
    print(f"{'='*75}")

    print("\n[1/2] Full rewrite...")
    full = benchmark_full_rewrite(input_pdf, output_dir, max_pages)
    print(
        f"  Done: {full.time_total:.3f}s (mutation={full.time_mutation:.3f}s, write={full.time_write:.3f}s)"
    )

    print("[2/2] Incremental append...")
    incr = benchmark_incremental_append(input_pdf, output_dir, max_pages)
    print(
        f"  Done: {incr.time_total:.3f}s (mutation={incr.time_mutation:.3f}s, write={incr.time_write:.3f}s)"
    )

    print_comparison(full, incr)
    return full, incr


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Phase 3.7: Translation Mutation Benchmark"
    )
    parser.add_argument("--input", type=Path, default=None)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--max-pages", type=int, default=None)
    parser.add_argument("--output-dir", type=Path, default=FIXTURES_DIR)
    args = parser.parse_args()

    if args.all:
        results = []
        for name in LARGE_FILES:
            path = TEST_DIR / name
            if path.exists():
                full, incr = run_single(path, args.output_dir, args.max_pages)
                results.append((name, full, incr))

        # Summary
        print("\n" + "=" * 100)
        print("SUMMARY — Mutation Radius")
        print("=" * 100)
        print(
            f"{'File':<40} {'Pages':>5} {'Objects':>7} {'Touched':>7} {'Ratio':>6} {'Full':>7} {'Incr':>7} {'Speedup':>7}"
        )
        print("-" * 100)
        for name, full, incr in results:
            speedup = full.time_write / incr.time_write if incr.time_write > 0 else 0
            print(
                f"{name[:39]:<40} {full.pages:>5} "
                f"{full.total_objects:>7} {full.objects_touched:>7} "
                f"{full.mutation_ratio:>5.0%} "
                f"{full.time_write:>6.2f}s {incr.time_write:>6.2f}s "
                f"{speedup:>6.2f}x"
            )
    elif args.input:
        if not args.input.exists():
            print(f"ERROR: {args.input} not found")
            sys.exit(1)
        run_single(args.input, args.output_dir, args.max_pages)
    else:
        for name in LARGE_FILES:
            path = TEST_DIR / name
            if path.exists():
                run_single(path, args.output_dir, args.max_pages)


if __name__ == "__main__":
    main()
