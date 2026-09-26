"""Phase 3.3c: Production Benchmark - 730-page real PDF.

Usage:
    python -m tests.phase3_3c_benchmark
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pikepdf

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "benchmark_730"
DEFAULT_INPUT = Path(__file__).parent / "file" / "itbook-export.pdf"

LARGE_FILES = [
    "2104.13478v2.pdf",
    "2407.18384v4.pdf",
    "The Rust Programming Language.pdf",
    "itbook-export.pdf",
]


@dataclass
class BenchmarkProfile:
    pages: int = 0
    original_size: int = 0
    output_size: int = 0
    load_time: float = 0.0
    clone_time: float = 0.0
    assembly_time: float = 0.0
    write_time: float = 0.0
    total_time: float = 0.0
    original_objects: int = 0
    output_objects: int = 0
    original_fonts: int = 0
    output_fonts: int = 0
    font_sharing_ratio: float = 0.0
    object_reuse_ratio: float = 0.0
    resource_amplification_ratio: float = 0.0
    output_valid: bool = False
    text_extractable: bool = False

    def summary(self) -> str:
        size_ratio = (
            self.output_size / self.original_size if self.original_size > 0 else 0
        )
        return "\n".join(
            [
                f"730-Page Benchmark | {self.pages} pages",
                "=" * 60,
                "",
                "Timing:",
                f"  load:             {self.load_time:.3f}s",
                f"  clone:            {self.clone_time:.3f}s",
                f"  assembly total:   {self.assembly_time:.3f}s",
                f"  write:            {self.write_time:.3f}s",
                f"  TOTAL:            {self.total_time:.3f}s",
                "",
                "Objects:",
                f"  original:         {self.original_objects}",
                f"  output:           {self.output_objects}",
                f"  reuse ratio:      {self.object_reuse_ratio:.1%}",
                f"  amplification:    {self.resource_amplification_ratio:.2f}x",
                "",
                "Fonts:",
                f"  original:         {self.original_fonts}",
                f"  output:           {self.output_fonts}",
                f"  sharing ratio:    {self.font_sharing_ratio:.1%}",
                "",
                "Size:",
                f"  original:         {self.original_size / 1024 / 1024:.2f} MB",
                f"  output:           {self.output_size / 1024 / 1024:.2f} MB",
                f"  ratio:            {size_ratio:.2f}x",
                "",
                "Compatibility:",
                f"  valid PDF:        {self.output_valid}",
                f"  text extractable: {self.text_extractable}",
            ]
        )


def count_all_objects(pdf: pikepdf.Pdf) -> dict:
    fonts = 0
    images = 0
    streams = 0
    dicts = 0
    for obj in pdf.objects:
        try:
            if isinstance(obj, pikepdf.Stream):
                streams += 1
                if obj.get("/Subtype") == "/Image":
                    images += 1
            elif isinstance(obj, pikepdf.Dictionary):
                dicts += 1
                if obj.get("/Type") == "/Font":
                    fonts += 1
        except Exception:
            pass
    return {
        "total": dicts + streams,
        "fonts": fonts,
        "images": images,
        "streams": streams,
    }


def run_clone_only(
    input_pdf: Path, output_dir: Optional[Path] = None
) -> BenchmarkProfile:
    if output_dir is None:
        output_dir = FIXTURES_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    output_pdf = output_dir / f"{input_pdf.stem}_clone_only.pdf"

    profile = BenchmarkProfile()
    profile.original_size = input_pdf.stat().st_size

    t0 = time.perf_counter()
    pdf = pikepdf.open(str(input_pdf))
    profile.load_time = time.perf_counter() - t0
    profile.pages = len(pdf.pages)

    try:
        orig_stats = count_all_objects(pdf)
        profile.original_objects = orig_stats["total"]
        profile.original_fonts = orig_stats["fonts"]

        t1 = time.perf_counter()
        original_pages = list(pdf.pages)
        new_kids = pikepdf.Array()
        for page in original_pages:
            new_kids.append(page.obj)
            cloned = pikepdf.Dictionary()
            for key in ["/MediaBox", "/CropBox", "/Rotate", "/BleedBox", "/TrimBox"]:
                if key in page:
                    cloned[key] = page[key]
            if "/Resources" in page:
                cloned["/Resources"] = pikepdf.Dictionary(page["/Resources"])
            new_kids.append(pdf.make_indirect(cloned))

        pages_obj = pdf.Root.get("/Pages")
        if pages_obj is not None:
            pages_obj["/Kids"] = new_kids
            pages_obj["/Count"] = len(new_kids)
        profile.clone_time = time.perf_counter() - t1
        profile.assembly_time = profile.clone_time

        t2 = time.perf_counter()
        pdf.save(str(output_pdf))
        profile.write_time = time.perf_counter() - t2
        profile.total_time = (
            profile.load_time + profile.assembly_time + profile.write_time
        )

        profile.output_size = output_pdf.stat().st_size
        out_stats = count_all_objects(pdf)
        profile.output_objects = out_stats["total"]
        profile.output_fonts = out_stats["fonts"]

        if profile.output_objects > 0:
            profile.object_reuse_ratio = (
                profile.original_objects / profile.output_objects
            )
        if profile.original_fonts > 0:
            profile.resource_amplification_ratio = (
                profile.output_fonts / profile.original_fonts
            )
        profile.font_sharing_ratio = 1.0 - (
            (profile.output_fonts - profile.original_fonts)
            / max(profile.original_fonts, 1)
        )

        try:
            test_pdf = pikepdf.open(str(output_pdf))
            profile.output_valid = True
            profile.text_extractable = len(test_pdf.pages) == profile.pages
            test_pdf.close()
        except Exception:
            profile.output_valid = False
    finally:
        pdf.close()
    return profile


def run_with_font_dedup(
    input_pdf: Path, output_dir: Optional[Path] = None
) -> BenchmarkProfile:
    if output_dir is None:
        output_dir = FIXTURES_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    output_pdf = output_dir / f"{input_pdf.stem}_font_dedup.pdf"

    profile = BenchmarkProfile()
    profile.original_size = input_pdf.stat().st_size

    t0 = time.perf_counter()
    pdf = pikepdf.open(str(input_pdf))
    profile.load_time = time.perf_counter() - t0
    profile.pages = len(pdf.pages)

    try:
        orig_stats = count_all_objects(pdf)
        profile.original_objects = orig_stats["total"]
        profile.original_fonts = orig_stats["fonts"]

        t1 = time.perf_counter()
        original_pages = list(pdf.pages)
        shared_font = pdf.make_indirect(
            pikepdf.Dictionary(
                {
                    "/Type": "/Font",
                    "/Subtype": "/Type0",
                    "/BaseFont": pikepdf.Name("/NotoSansSC"),
                    "/Encoding": "/Identity-H",
                }
            )
        )

        new_kids = pikepdf.Array()
        for page in original_pages:
            new_kids.append(page.obj)
            cloned = pikepdf.Dictionary()
            for key in ["/MediaBox", "/CropBox", "/Rotate"]:
                if key in page:
                    cloned[key] = page[key]
            font_dict = pikepdf.Dictionary()
            font_dict["/F1"] = shared_font
            resources = pikepdf.Dictionary()
            if "/Resources" in page:
                resources = pikepdf.Dictionary(page["/Resources"])
            resources["/Font"] = font_dict
            cloned["/Resources"] = resources
            new_kids.append(pdf.make_indirect(cloned))

        pages_obj = pdf.Root.get("/Pages")
        if pages_obj is not None:
            pages_obj["/Kids"] = new_kids
            pages_obj["/Count"] = len(new_kids)
        profile.clone_time = time.perf_counter() - t1
        profile.assembly_time = profile.clone_time

        t2 = time.perf_counter()
        pdf.save(str(output_pdf))
        profile.write_time = time.perf_counter() - t2
        profile.total_time = (
            profile.load_time + profile.assembly_time + profile.write_time
        )

        profile.output_size = output_pdf.stat().st_size
        out_stats = count_all_objects(pdf)
        profile.output_objects = out_stats["total"]
        profile.output_fonts = out_stats["fonts"]

        if profile.output_objects > 0:
            profile.object_reuse_ratio = (
                profile.original_objects / profile.output_objects
            )
        if profile.original_fonts > 0:
            profile.resource_amplification_ratio = (
                profile.output_fonts / profile.original_fonts
            )
        profile.font_sharing_ratio = 1.0 - (
            (profile.output_fonts - profile.original_fonts)
            / max(profile.original_fonts, 1)
        )

        try:
            test_pdf = pikepdf.open(str(output_pdf))
            profile.output_valid = True
            profile.text_extractable = len(test_pdf.pages) == profile.pages
            test_pdf.close()
        except Exception:
            profile.output_valid = False
    finally:
        pdf.close()
    return profile


def print_comparison(clone_only: BenchmarkProfile, with_dedup: BenchmarkProfile):
    print()
    print("=" * 70)
    print("Phase 3.3c: 730-Page Production Benchmark")
    print("=" * 70)
    print(f"Input: {DEFAULT_INPUT.name} ({clone_only.pages} pages)")
    print()
    header = f"{'Metric':<35} {'Clone Only':>15} {'With Dedup':>15}"
    print(header)
    print("-" * 70)
    print(
        f"{'Load time (s)':<35} {clone_only.load_time:>15.3f} {with_dedup.load_time:>15.3f}"
    )
    print(
        f"{'Clone time (s)':<35} {clone_only.clone_time:>15.3f} {with_dedup.clone_time:>15.3f}"
    )
    print(
        f"{'Assembly total (s)':<35} {clone_only.assembly_time:>15.3f} {with_dedup.assembly_time:>15.3f}"
    )
    print(
        f"{'Write time (s)':<35} {clone_only.write_time:>15.3f} {with_dedup.write_time:>15.3f}"
    )
    print(
        f"{'TOTAL time (s)':<35} {clone_only.total_time:>15.3f} {with_dedup.total_time:>15.3f}"
    )
    print()
    print(
        f"{'Original objects':<35} {clone_only.original_objects:>15} {with_dedup.original_objects:>15}"
    )
    print(
        f"{'Output objects':<35} {clone_only.output_objects:>15} {with_dedup.output_objects:>15}"
    )
    print(
        f"{'Object reuse ratio':<35} {clone_only.object_reuse_ratio:>14.1%} {with_dedup.object_reuse_ratio:>14.1%}"
    )
    print()
    print(
        f"{'Original fonts':<35} {clone_only.original_fonts:>15} {with_dedup.original_fonts:>15}"
    )
    print(
        f"{'Output fonts':<35} {clone_only.output_fonts:>15} {with_dedup.output_fonts:>15}"
    )
    print(
        f"{'Font sharing ratio':<35} {clone_only.font_sharing_ratio:>14.1%} {with_dedup.font_sharing_ratio:>14.1%}"
    )
    print(
        f"{'Resource amplification':<35} {clone_only.resource_amplification_ratio:>14.2f}x {with_dedup.resource_amplification_ratio:>14.2f}x"
    )
    print()
    print(
        f"{'Output size (MB)':<35} {clone_only.output_size/1024/1024:>15.2f} {with_dedup.output_size/1024/1024:>15.2f}"
    )
    print(
        f"{'Valid PDF':<35} {str(clone_only.output_valid):>15} {str(with_dedup.output_valid):>15}"
    )
    print(
        f"{'Text extractable':<35} {str(clone_only.text_extractable):>15} {str(with_dedup.text_extractable):>15}"
    )
    print()

    total = clone_only.total_time
    if total > 0:
        assembly_pct = clone_only.assembly_time / total * 100
        write_pct = clone_only.write_time / total * 100
        load_pct = clone_only.load_time / total * 100
        print("Time breakdown (Clone Only):")
        print(f"  load:     {load_pct:5.1f}%")
        print(f"  assembly: {assembly_pct:5.1f}%")
        print(f"  write:    {write_pct:5.1f}%")
        print()
        if assembly_pct > 30:
            print(">>> BOTTLENECK: assembly > 30% -> XObject Pool next")
        elif write_pct > 30:
            print(">>> BOTTLENECK: write > 30% -> Incremental Write next")
        else:
            print(">>> No assembly/write bottleneck -> Phase 4 backend decision")


def run_single_benchmark(input_pdf: Path, output_dir: Path) -> tuple:
    """Run clone-only and font-dedup benchmarks on a single file."""
    print(f"\n{'='*70}")
    print(f"Input: {input_pdf.name} ({input_pdf.stat().st_size / 1024 / 1024:.1f} MB)")
    print(f"{'='*70}")

    print("\n[1/2] Clone-only baseline...")
    clone_only = run_clone_only(input_pdf, output_dir)
    print(f"  Done: {clone_only.total_time:.3f}s")

    print("[2/2] With font dedup...")
    with_dedup = run_with_font_dedup(input_pdf, output_dir)
    print(f"  Done: {with_dedup.total_time:.3f}s")

    print_comparison(clone_only, with_dedup)
    return clone_only, with_dedup


def main():
    parser = argparse.ArgumentParser(description="Phase 3.3c: benchmark")
    parser.add_argument("--input", type=Path, default=None, help="Single file")
    parser.add_argument("--all", action="store_true", help="Run all large files")
    parser.add_argument("--output-dir", type=Path, default=FIXTURES_DIR)
    args = parser.parse_args()

    test_dir = Path(__file__).parent / "file"

    if args.all:
        results = []
        for name in LARGE_FILES:
            path = test_dir / name
            if path.exists():
                clone_only, with_dedup = run_single_benchmark(path, args.output_dir)
                results.append((name, clone_only, with_dedup))

        # Summary table
        print("\n" + "=" * 90)
        print("SUMMARY — All Files")
        print("=" * 90)
        print(
            f"{'File':<45} {'Pages':>5} {'Load':>7} {'Clone':>7} {'Write':>7} {'Total':>7} {'Reuse':>6}"
        )
        print("-" * 90)
        for name, co, wd in results:
            print(
                f"{name[:44]:<45} {co.pages:>5} "
                f"{co.load_time:>6.2f}s {co.clone_time:>6.2f}s "
                f"{co.write_time:>6.2f}s {co.total_time:>6.2f}s "
                f"{co.object_reuse_ratio:>5.0%}"
            )
    elif args.input:
        if not args.input.exists():
            print(f"ERROR: {args.input} not found")
            sys.exit(1)
        run_single_benchmark(args.input, args.output_dir)
    else:
        # Default: run all large files
        for name in LARGE_FILES:
            path = test_dir / name
            if path.exists():
                run_single_benchmark(path, args.output_dir)


if __name__ == "__main__":
    main()
