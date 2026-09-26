"""Assembler Benchmark Suite — 分阶段测量 assembly 性能。

目的：
    不是测"整个 PDF 翻译多快"。
    而是证明"assembly 是新的优化边界"。

用法：
    python -m benchmarks.assembly.benchmark_10pages
    python -m benchmarks.assembly.benchmark_100pages
    python -m benchmarks.assembly.benchmark_730pages

输出格式：
    Phase              | MuPDF    | Pikepdf  | Speedup
    -------------------|----------|----------|--------
    parse              | X.XXs    | X.XXs    | X.Xx
    layout             | X.XXs    | X.XXs    | X.Xx
    translation        | X.XXs    | X.XXs    | X.Xx
    PageResult build   | X.XXs    | X.XXs    | X.Xx
    assembler          | X.XXs    | X.XXs    | X.Xx
    write              | X.XXs    | X.XXs    | X.Xx
    -------------------|----------|----------|--------
    TOTAL              | X.XXs    | X.XXs    | X.Xx
    assembler/total    | XX%      | XX%      |
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class PhaseTiming:
    """单阶段计时结果。"""

    name: str
    mupdf_seconds: float = 0.0
    pikepdf_seconds: float = 0.0

    @property
    def speedup(self) -> float:
        if self.pikepdf_seconds > 0:
            return self.mupdf_seconds / self.pikepdf_seconds
        return 0.0


@dataclass
class BenchmarkResult:
    """完整 benchmark 结果。"""

    pdf_name: str
    page_count: int
    phases: List[PhaseTiming] = field(default_factory=list)

    @property
    def mupdf_total(self) -> float:
        return sum(p.mupdf_seconds for p in self.phases)

    @property
    def pikepdf_total(self) -> float:
        return sum(p.pikepdf_seconds for p in self.phases)

    @property
    def total_speedup(self) -> float:
        if self.pikepdf_total > 0:
            return self.mupdf_total / self.pikepdf_total
        return 0.0

    @property
    def mupdf_assembler_ratio(self) -> float:
        if self.mupdf_total > 0:
            assembler_time = next(
                (p.mupdf_seconds for p in self.phases if p.name == "assembler"), 0
            )
            return assembler_time / self.mupdf_total
        return 0.0

    @property
    def pikepdf_assembler_ratio(self) -> float:
        if self.pikepdf_total > 0:
            assembler_time = next(
                (p.pikepdf_seconds for p in self.phases if p.name == "assembler"), 0
            )
            return assembler_time / self.pikepdf_total
        return 0.0

    def print_table(self):
        """打印格式化的 benchmark 表格。"""
        print(f"\n{'='*60}")
        print(f"Benchmark: {self.pdf_name} ({self.page_count} pages)")
        print(f"{'='*60}")
        print(f"{'Phase':<25} | {'MuPDF':>10} | {'Pikepdf':>10} | {'Speedup':>10}")
        print(f"{'-'*25}-+-{'-'*10}-+-{'-'*10}-+-{'-'*10}")

        for phase in self.phases:
            mupdf_str = f"{phase.mupdf_seconds:.2f}s"
            pikepdf_str = f"{phase.pikepdf_seconds:.2f}s"
            speedup_str = f"{phase.speedup:.1f}x" if phase.speedup > 0 else "N/A"
            print(
                f"{phase.name:<25} | {mupdf_str:>10} | {pikepdf_str:>10} | {speedup_str:>10}"
            )

        print(f"{'-'*25}-+-{'-'*10}-+-{'-'*10}-+-{'-'*10}")
        print(
            f"{'TOTAL':<25} | {self.mupdf_total:.2f}s | "
            f"{self.pikepdf_total:.2f}s | {self.total_speedup:.1f}x"
        )
        print(
            f"{'assembler/total':<25} | "
            f"{self.mupdf_assembler_ratio*100:.0f}%{'':<5} | "
            f"{self.pikepdf_assembler_ratio*100:.0f}%{'':<5} |"
        )
        print(f"{'='*60}\n")


def create_benchmark_artifact(
    pdf_bytes: bytes,
    page_count: int,
    mode: str = "dual",
):
    """创建 benchmark 用的 TranslationArtifact（模拟 worker 输出）。"""
    from pdf2zh.assembler import (
        FontIdentity,
        FontRequirement,
        PageMetrics,
        PageResult,
        PageSize,
        ResourceRequirement,
        TranslationArtifact,
    )

    # 模拟每页的翻译结果
    pages = []
    for i in range(page_count):
        # 模拟 content stream（实际 benchmark 中应使用真实翻译结果）
        content_stream = (
            f"BT /F1 12 Tf 100 700 Td " f"(Translated text for page {i + 1}) Tj ET"
        ).encode()

        result = PageResult(
            page_index=i,
            page_size=PageSize(width=612, height=792),
            content_stream=content_stream,
            resources=ResourceRequirement(
                fonts=[
                    FontRequirement(
                        identity=FontIdentity(
                            family="GoNotoKurrent",
                            weight=400,
                            full_name="GoNotoKurrent-Regular",
                        ),
                        file_path="",  # 实际 benchmark 中应提供真实路径
                    )
                ]
            ),
            metrics=PageMetrics(
                elapsed=0.0,
                translation_chars=100,
            ),
        )
        pages.append(result)

    return TranslationArtifact(
        pages=pages,
        mode=mode,
        original_pdf=pdf_bytes,
        skip_subset_fonts=False,
    )
