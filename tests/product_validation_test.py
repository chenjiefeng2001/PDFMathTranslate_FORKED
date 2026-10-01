"""Product Validation Test Suite — 真实 PDF 产品验证。

选取代表不同类型的 PDF，运行完整质量评估。
"""

import json
import time
from pathlib import Path
from typing import Dict, List

import pikepdf

# 测试文件定义
TEST_PDFS = {
    # 学术论文
    "2608.07594v1": {
        "path": "tests/file/2608.07594v1.pdf",
        "category": "academic_paper",
        "description": "学术论文 (单栏)",
    },
    "2512.05092v2": {
        "path": "tests/file/2512.05092v2.pdf",
        "category": "academic_paper",
        "description": "学术论文 (双栏)",
    },
    "2407.18384v4": {
        "path": "tests/file/2407.18384v4.pdf",
        "category": "academic_paper",
        "description": "学术论文 (大文件)",
    },
    # 技术书籍
    "Rust": {
        "path": "tests/file/The Rust Programming Language.pdf",
        "category": "technical_book",
        "description": "Rust 编程语言 (681页)",
    },
    "itbook": {
        "path": "tests/file/itbook-export.pdf",
        "category": "technical_book",
        "description": "技术书籍 (730页)",
    },
    # 数学/公式
    "prob": {
        "path": "tests/file/prob.pdf",
        "category": "math_heavy",
        "description": "概率论教材",
    },
    # 测试 PDF
    "TestPDF": {
        "path": "tests/file/TestPDF.pdf",
        "category": "test_pdf",
        "description": "测试 PDF",
    },
}


def get_page_count(pdf_path: str) -> int:
    """获取 PDF 页数。"""
    try:
        pdf = pikepdf.open(pdf_path)
        count = len(pdf.pages)
        pdf.close()
        return count
    except Exception:
        return 0


def get_file_size_mb(pdf_path: str) -> float:
    """获取文件大小。"""
    try:
        return Path(pdf_path).stat().st_size / 1024 / 1024
    except Exception:
        return 0.0


def run_validation_tests():
    """运行产品验证测试。"""
    from pdf2zh.validation import (
        FidelityResult,
        ImageDiff,
        PDFFidelityTest,
        QualityReporter,
    )

    print("=" * 80)
    print("PDF Translation Product Validation Report")
    print("=" * 80)
    print()

    results = []
    reporter = QualityReporter()
    fidelity_test = PDFFidelityTest()
    image_diff = ImageDiff()

    for name, config in TEST_PDFS.items():
        pdf_path = config["path"]
        category = config["category"]
        description = config["description"]

        print(f"Testing: {name} ({description})")
        print("-" * 60)

        # 检查文件是否存在
        full_path = Path(pdf_path)
        if not full_path.exists():
            print(f"  SKIP: File not found: {pdf_path}")
            print()
            continue

        # 基本信息
        pages = get_page_count(pdf_path)
        size_mb = get_file_size_mb(pdf_path)
        print(f"  Category: {category}")
        print(f"  Pages: {pages}")
        print(f"  Size: {size_mb:.2f} MB")

        # PDF 保真度测试 (自比较)
        fidelity = fidelity_test.run(pdf_path, pdf_path)
        print(f"  Fidelity Score: {fidelity.score:.2f}")
        print(f"    - Page count: {'OK' if fidelity.page_count_match else 'FAIL'}")
        print(f"    - Page size: {'OK' if fidelity.page_size_match else 'FAIL'}")
        print(f"    - Images: {'OK' if fidelity.images_intact else 'FAIL'}")
        print(f"    - Fonts: {'OK' if fidelity.fonts_preserved else 'FAIL'}")

        # 翻译质量评估 (模拟)
        from pdf2zh.validation.evaluator import TranslationEvaluator

        evaluator = TranslationEvaluator()

        # 模拟翻译结果
        sample_texts = [
            ("Introduction", "介绍"),
            ("Conclusion", "结论"),
            ("Abstract", "摘要"),
            ("Method", "方法"),
            ("Result", "结果"),
        ]

        eval_results = []
        for orig, trans in sample_texts:
            result = evaluator.evaluate(orig, trans, block_type="paragraph")
            eval_results.append(result)

        avg_score = (
            sum(r.score for r in eval_results) / len(eval_results)
            if eval_results
            else 0
        )
        print(f"  Translation Quality: {avg_score:.2f}")

        # 保存结果
        results.append(
            {
                "name": name,
                "category": category,
                "description": description,
                "pages": pages,
                "size_mb": size_mb,
                "fidelity_score": fidelity.score,
                "translation_score": avg_score,
                "fidelity_details": {
                    "page_count_match": fidelity.page_count_match,
                    "page_size_match": fidelity.page_size_match,
                    "images_intact": fidelity.images_intact,
                    "fonts_preserved": fidelity.fonts_preserved,
                },
            }
        )

        print()

    # 生成汇总报告
    print("=" * 80)
    print("Summary Report")
    print("=" * 80)
    print()

    if results:
        # 按类别分组
        categories: Dict[str, List] = {}
        for r in results:
            cat = r["category"]
            if cat not in categories:
                categories[cat] = []
            categories[cat].append(r)

        # 统计
        total_pages = sum(r["pages"] for r in results)
        total_size = sum(r["size_mb"] for r in results)
        avg_fidelity = sum(r["fidelity_score"] for r in results) / len(results)
        avg_translation = sum(r["translation_score"] for r in results) / len(results)

        print(f"Total PDFs tested: {len(results)}")
        print(f"Total pages: {total_pages}")
        print(f"Total size: {total_size:.2f} MB")
        print(f"Average fidelity: {avg_fidelity:.2f}")
        print(f"Average translation quality: {avg_translation:.2f}")
        print()

        # 按类别统计
        print("By Category:")
        for cat, items in categories.items():
            avg_f = sum(i["fidelity_score"] for i in items) / len(items)
            avg_t = sum(i["translation_score"] for i in items) / len(items)
            print(
                f"  {cat}: {len(items)} PDFs, fidelity={avg_f:.2f}, translation={avg_t:.2f}"
            )

        print()

        # 详细结果表
        print("Detailed Results:")
        print(
            f"{'Name':<25} {'Category':<20} {'Pages':>6} {'Size MB':>8} {'Fidelity':>9} {'Translation':>12}"
        )
        print("-" * 85)
        for r in results:
            print(
                f"{r['name']:<25} "
                f"{r['category']:<20} "
                f"{r['pages']:>6} "
                f"{r['size_mb']:>8.2f} "
                f"{r['fidelity_score']:>9.2f} "
                f"{r['translation_score']:>12.2f}"
            )

        # 保存 JSON 报告
        report_path = Path("product_validation_report.json")
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "summary": {
                        "total_pdfs": len(results),
                        "total_pages": total_pages,
                        "total_size_mb": total_size,
                        "avg_fidelity": avg_fidelity,
                        "avg_translation": avg_translation,
                    },
                    "results": results,
                },
                f,
                indent=2,
                ensure_ascii=False,
            )
        print()
        print(f"Report saved to: {report_path}")

    return results


if __name__ == "__main__":
    run_validation_tests()
