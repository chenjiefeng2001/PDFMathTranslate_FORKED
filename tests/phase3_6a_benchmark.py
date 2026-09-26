"""Phase 3.6a: Incremental Write Benchmark — 730 pages.

对比：full save vs 手动 incremental vs pikepdf minimal
测量：write time, output size, append ratio
"""

from __future__ import annotations

import io
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pikepdf

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "benchmark_730"
DEFAULT_INPUT = Path(__file__).parent / "file" / "itbook-export.pdf"


@dataclass
class WriteBenchmark:
    method: str = ""
    pages: int = 0
    write_time: float = 0.0
    output_size: int = 0
    original_size: int = 0
    valid: bool = False
    page_count_check: int = 0
    append_ratio: float = 0.0

    @property
    def size_ratio(self) -> str:
        if self.original_size > 0:
            return f"{self.output_size / self.original_size:.2f}x"
        return "N/A"

    def row(self) -> str:
        return (
            f"{self.method:<30} "
            f"{self.write_time:>8.3f}s "
            f"{self.output_size/1024/1024:>7.2f}MB "
            f"{self.size_ratio:>7} "
            f"{self.append_ratio:>6.1%} "
            f"{str(self.valid):>5}"
        )


def benchmark_full_save(input_pdf: Path, output_dir: Path) -> WriteBenchmark:
    """Full save: load -> patch all pages -> save (baseline)."""
    output_dir.mkdir(parents=True, exist_ok=True)
    output_pdf = output_dir / f"{input_pdf.stem}_full_save.pdf"

    bm = WriteBenchmark(method="full_save")
    bm.original_size = input_pdf.stat().st_size

    pdf = pikepdf.open(str(input_pdf))
    bm.pages = len(pdf.pages)

    dummy_content = b"BT /F1 12 Tf 100 700 Td (Translated) Tj ET"

    t0 = time.perf_counter()
    for i, page in enumerate(pdf.pages):
        stream = pikepdf.Stream(pdf, dummy_content)
        ref = pdf.make_indirect(stream)
        page["/Contents"] = ref
    patch_time = time.perf_counter() - t0

    t1 = time.perf_counter()
    pdf.save(str(output_pdf))
    bm.write_time = time.perf_counter() - t1
    bm.output_size = output_pdf.stat().st_size
    bm.append_ratio = 1.0

    try:
        test = pikepdf.open(str(output_pdf))
        bm.valid = True
        bm.page_count_check = len(test.pages)
        test.close()
    except Exception:
        bm.valid = False

    pdf.close()
    return bm


def benchmark_manual_incremental(input_pdf: Path, output_dir: Path) -> WriteBenchmark:
    """Manual incremental: append only new objects."""
    from pdf2zh.assembler.incremental.writer import IncrementalWriter

    output_dir.mkdir(parents=True, exist_ok=True)
    output_pdf = output_dir / f"{input_pdf.stem}_manual_incr.pdf"

    bm = WriteBenchmark(method="manual_incremental")
    bm.original_size = input_pdf.stat().st_size

    writer = IncrementalWriter()
    writer.open(input_pdf)
    bm.pages = writer._store._next_objnum  # rough

    dummy_content = b"BT /F1 12 Tf 100 700 Td (Translated) Tj ET"

    t0 = time.perf_counter()
    for i in range(730):
        writer.replace_page_content(i, dummy_content)
    patch_time = time.perf_counter() - t0

    t1 = time.perf_counter()
    stats = writer.commit(output_pdf)
    bm.write_time = time.perf_counter() - t1
    bm.output_size = output_pdf.stat().st_size

    if bm.original_size > 0:
        bm.append_ratio = (bm.output_size - bm.original_size) / bm.original_size

    bm.valid = stats.get("valid", False)
    bm.page_count_check = stats.get("page_count", 0)

    return bm


def benchmark_pikepdf_bytes(input_pdf: Path, output_dir: Path) -> WriteBenchmark:
    """Pikepdf full save but write to BytesIO first (measure raw write time)."""
    output_dir.mkdir(parents=True, exist_ok=True)
    output_pdf = output_dir / f"{input_pdf.stem}_pikepdf_bytes.pdf"

    bm = WriteBenchmark(method="pikepdf_bytes")
    bm.original_size = input_pdf.stat().st_size

    pdf = pikepdf.open(str(input_pdf))
    bm.pages = len(pdf.pages)

    dummy_content = b"BT /F1 12 Tf 100 700 Td (Translated) Tj ET"

    for i, page in enumerate(pdf.pages):
        stream = pikepdf.Stream(pdf, dummy_content)
        ref = pdf.make_indirect(stream)
        page["/Contents"] = ref

    # Save to BytesIO first (measures serialization time)
    buf = io.BytesIO()
    t1 = time.perf_counter()
    pdf.save(buf)
    bm.write_time = time.perf_counter() - t1

    # Then write to file
    buf.seek(0)
    output_pdf.write_bytes(buf.read())
    bm.output_size = output_pdf.stat().st_size
    bm.append_ratio = 1.0

    try:
        test = pikepdf.open(str(output_pdf))
        bm.valid = True
        bm.page_count_check = len(test.pages)
        test.close()
    except Exception:
        bm.valid = False

    pdf.close()
    return bm


def main():
    print("Phase 3.6a: Incremental Write Benchmark")
    print(
        f"Input: {DEFAULT_INPUT.name} ({DEFAULT_INPUT.stat().st_size / 1024 / 1024:.1f} MB)"
    )
    print()

    output_dir = FIXTURES_DIR

    print("[1/3] Full save baseline...")
    full = benchmark_full_save(DEFAULT_INPUT, output_dir)
    print(f"  Done: {full.write_time:.3f}s")

    print("[2/3] Manual incremental...")
    manual = benchmark_manual_incremental(DEFAULT_INPUT, output_dir)
    print(f"  Done: {manual.write_time:.3f}s")

    print("[3/3] Pikepdf bytes...")
    pikepdf_bm = benchmark_pikepdf_bytes(DEFAULT_INPUT, output_dir)
    print(f"  Done: {pikepdf_bm.write_time:.3f}s")

    print()
    print(
        f"{'Method':<30} {'Write':>8} {'Size':>7} {'Ratio':>7} {'Append':>6} {'Valid':>5}"
    )
    print("-" * 75)
    print(full.row())
    print(manual.row())
    print(pikepdf_bm.row())
    print()

    if full.write_time > 0:
        speedup_manual = (
            full.write_time / manual.write_time if manual.write_time > 0 else 0
        )
        print(f"Manual incremental speedup: {speedup_manual:.2f}x")
        print()
        print("Analysis:")
        print(f"  Full save:  rewrites {full.output_size/1024/1024:.2f} MB")
        print(
            f"  Manual:     appends {manual.append_ratio:.1%} = {(manual.output_size - manual.original_size)/1024:.1f} KB"
        )
        if manual.valid:
            print(f"  Valid PDF:  True")
        else:
            print(f"  Valid PDF:  False (incremental xref may be incomplete)")


if __name__ == "__main__":
    main()
