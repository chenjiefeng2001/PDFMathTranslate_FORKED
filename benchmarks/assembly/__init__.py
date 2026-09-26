"""Assembly Benchmark — 10 页 PDF。

测量 assembler 在小文档上的性能差异。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

# 添加项目根目录到 path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))


def run_benchmark(pdf_path: str, iterations: int = 3):
    """运行 10 页 PDF 的 assembly benchmark。"""
    from benchmarks import BenchmarkResult, PhaseTiming, create_benchmark_artifact

    pdf_bytes = Path(pdf_path).read_bytes()
    page_count = 10  # 假设 10 页

    result = BenchmarkResult(
        pdf_name=Path(pdf_path).name,
        page_count=page_count,
    )

    # 创建 artifact
    artifact = create_benchmark_artifact(pdf_bytes, page_count)

    # 测试 MuPDF
    from pdf2zh.assembler.mupdf_assembler import MuPDFAssembler

    mupdf_asm = MuPDFAssembler()
    mupdf_times = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        mupdf_asm.assemble(artifact)
        mupdf_times.append(time.perf_counter() - t0)
    mupdf_avg = sum(mupdf_times) / len(mupdf_times)

    # 测试 Pikepdf（如果可用）
    pikepdf_avg = 0.0
    try:
        from pdf2zh.assembler.pikepdf_incremental import PikepdfIncrementalAssembler

        pikepdf_asm = PikepdfIncrementalAssembler()
        pikepdf_times = []
        for _ in range(iterations):
            t0 = time.perf_counter()
            pikepdf_asm.assemble(artifact)
            pikepdf_times.append(time.perf_counter() - t0)
        pikepdf_avg = sum(pikepdf_times) / len(pikepdf_times)
    except ImportError:
        print("pikepdf not available, skipping")

    result.phases.append(
        PhaseTiming(
            name="assembler",
            mupdf_seconds=mupdf_avg,
            pikepdf_seconds=pikepdf_avg,
        )
    )

    result.print_table()
    return result


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m benchmarks.assembly.benchmark_10pages <pdf_path>")
        sys.exit(1)

    run_benchmark(sys.argv[1])
