"""Validation — 生产验证 + 产品级质量评估。

Phase 4.5 + Phase 12-13 (Product Validation):
    - RuntimeMode: 双运行模式
    - SemanticComparator: 语义回归测试
    - RuntimeBenchmark: 双模式性能对比
    - PDFRenderer: PDF 页面渲染
    - ImageDiff: 图片差异比较
    - TranslationEvaluator: 翻译质量评估
    - PDFFidelityTest: PDF 保真度测试
    - QualityReporter: 产品质量报告
    - PDFProfile: PDF 难度分类
    - SemanticValidator: 语义验证
    - FailureTaxonomy: 失败分类
    - TranslationBenchmark: 真实翻译 benchmark

用法：
    from pdf2zh.validation import PDFProfile, SemanticValidator, FailureTaxonomy

    # PDF 分析
    profile = PDFProfile.from_pdf("input.pdf")
    print(profile.summary())

    # 语义验证
    validator = SemanticValidator()
    result = validator.validate("100%", "100%")

    # 失败分类
    taxonomy = FailureTaxonomy()
    category = taxonomy.classify("translation overflow")
"""

from pdf2zh.validation.benchmark import RuntimeBenchmark, RuntimeComparison
from pdf2zh.validation.regression import PageDiff, SemanticComparator, SemanticDiff
from pdf2zh.runtime.mode import RuntimeMode
from pdf2zh.validation.renderer import PDFRenderer, RenderOptions
from pdf2zh.validation.image_diff import ImageDiff, ImageDiffResult
from pdf2zh.validation.evaluator import TranslationEvaluator, EvalResult
from pdf2zh.validation.fidelity import PDFFidelityTest, FidelityResult
from pdf2zh.validation.quality_report import QualityReporter, ProductQualityReport

__all__ = [
    "RuntimeMode",
    "SemanticComparator",
    "SemanticDiff",
    "PageDiff",
    "RuntimeBenchmark",
    "RuntimeComparison",
    # Product Validation
    "PDFRenderer",
    "RenderOptions",
    "ImageDiff",
    "ImageDiffResult",
    "TranslationEvaluator",
    "EvalResult",
    "PDFFidelityTest",
    "FidelityResult",
    "QualityReporter",
    "ProductQualityReport",
]
