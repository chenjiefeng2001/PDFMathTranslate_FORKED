"""Document-level evaluation tests（阶段九：真实 PDF 文档级指标）。

用 pymupdf 合成 PDF 验证：四组指标（几何/结构/翻译/渲染）可计算、
碰撞可检出、CLI 可运行、评测报告可序列化。
"""

import json
import os
import tempfile

import pytest

from pdf2zh.evaluate import (
    build_profile,
    evaluate_translation,
    main,
)


@pytest.fixture(scope="module")
def pdfs_dir():
    d = tempfile.mkdtemp(prefix="pdf2zh_eval_")
    yield d


def _make_pdf(path, lines, page_size=(612, 792)):
    import fitz

    doc = fitz.open()
    page = doc.new_page(width=page_size[0], height=page_size[1])
    for text, x, y, size in lines:
        page.insert_text((x, y), text, fontsize=size)
    doc.save(path)
    doc.close()


@pytest.fixture(scope="module")
def clean_pdf(pdfs_dir):
    path = os.path.join(pdfs_dir, "clean.pdf")
    _make_pdf(
        path,
        [
            ("Chapter 3: Results", 72, 720, 18),
            ("Left column first paragraph line one.", 72, 690, 10),
            ("Left column second paragraph line two.", 72, 675, 10),
            ("Right column paragraph begins here.", 330, 690, 10),
            ("Right column continued second line.", 330, 675, 10),
            ("Figure 1: Overview of the system.", 72, 630, 10),
            ("1. Introduction .......... 3", 72, 200, 10),
            ("2. Methods .............. 12", 72, 185, 10),
            ("42", 280, 30, 10),
        ],
    )
    return path


@pytest.fixture(scope="module")
def overlapping_pdf(pdfs_dir):
    path = os.path.join(pdfs_dir, "overlap.pdf")
    # 同一位置两次插入 → 必然重叠
    _make_pdf(
        path,
        [
            ("Chapter 3: Results", 72, 720, 18),
            ("Overlapping paragraph one.", 72, 690, 10),
            ("Overlapping paragraph one.", 72, 690, 10),
            ("Overlapping paragraph two.", 72, 675, 10),
            ("Overlapping paragraph two.", 72, 675, 10),
            ("42", 280, 30, 10),
        ],
    )
    return path


@pytest.fixture(scope="module")
def chinese_pdf(pdfs_dir):
    path = os.path.join(pdfs_dir, "chinese.pdf")
    import fitz

    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_font(fontname="china-s")
    page.insert_text(
        (72, 700), "第三章：实验结果与分析", fontname="china-s", fontsize=14
    )
    page.insert_text(
        (72, 670), "本节介绍实验设置与主要结论。", fontname="china-s", fontsize=10
    )
    page.insert_text(
        (72, 650), "模型在多个基准上取得了显著提升。", fontname="china-s", fontsize=10
    )
    doc.save(path)
    doc.close()
    return path


class TestProfile:
    def test_profile_counts(self, clean_pdf):
        prof = build_profile(clean_pdf, target_lang="en")
        assert prof.page_count == 1
        assert prof.paragraph_count >= 5
        assert prof.headings >= 1
        assert prof.toc_entries >= 2
        assert prof.captions >= 1
        assert prof.page_numbers >= 1

    def test_profile_cjk(self, chinese_pdf):
        prof = build_profile(chinese_pdf, target_lang="zh-CN")
        assert prof.cjk_count > 0
        assert prof.char_count > 0


class TestMetrics:
    def test_identical_documents_score_high(self, clean_pdf):
        rep = evaluate_translation(clean_pdf, clean_pdf, target_lang="en")
        assert rep.geometry["overlap_rate"] == 0.0
        assert rep.geometry["geometry_score"] > 90
        assert rep.structure["structure_score"] > 90
        assert rep.rendering["collision_rate"] == 0.0
        assert rep.overall_score > 80

    def test_overlap_detected(self, clean_pdf, overlapping_pdf):
        rep = evaluate_translation(clean_pdf, overlapping_pdf, target_lang="en")
        assert rep.geometry["collision_rate"] > 0.0
        assert rep.geometry["duplicate_rate"] > 0.0
        assert rep.rendering["collision_rate"] > 0.0
        assert rep.geometry["geometry_score"] < 100

    def test_translation_coverage_cjk(self, clean_pdf, chinese_pdf):
        rep = evaluate_translation(clean_pdf, chinese_pdf, target_lang="zh-CN")
        assert rep.translation["target_coverage"] > 0.5
        assert rep.translation["translation_score"] > 0

    def test_page_drift_when_page_counts_differ(self, clean_pdf, chinese_pdf):
        rep = evaluate_translation(clean_pdf, chinese_pdf, target_lang="en")
        assert rep.geometry["page_drift"] >= 0.0

    def test_report_serializable(self, clean_pdf):
        rep = evaluate_translation(clean_pdf, clean_pdf, target_lang="en")
        data = rep.to_dict()
        json.dumps(data)  # must not raise
        assert "overall_score" in data
        assert set(data["geometry"]) >= {"overlap_rate", "overflow_rate"}
        assert "ir_snapshot" in rep.__dict__

    def test_ir_snapshot_attached(self, clean_pdf):
        rep = evaluate_translation(clean_pdf, clean_pdf, target_lang="en")
        assert rep.ir_snapshot.get("schema") == "pdf2zh.v3.ir-snapshot"


class TestCLI:
    def test_cli_runs_and_writes_json(self, clean_pdf, pdfs_dir):
        out = os.path.join(pdfs_dir, "report.json")
        rc = main([clean_pdf, clean_pdf, "--json", out, "--target-lang", "en"])
        assert rc == 0
        assert os.path.exists(out)
        with open(out, encoding="utf-8") as f:
            data = json.load(f)
        assert data["overall_score"] > 0

    def test_cli_missing_ir_flag(self, clean_pdf, pdfs_dir):
        out = os.path.join(pdfs_dir, "report_noir.json")
        rc = main([clean_pdf, clean_pdf, "--json", out, "--no-ir"])
        assert rc == 0
        with open(out, encoding="utf-8") as f:
            data = json.load(f)
        assert "ir_snapshot" not in data

    def test_cli_max_pages(self, clean_pdf, pdfs_dir):
        out = os.path.join(pdfs_dir, "report_mp.json")
        rc = main([clean_pdf, clean_pdf, "--json", out, "--max-pages", "1"])
        assert rc == 0
