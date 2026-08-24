"""V8.1 Migration Diff Harness + 阶段零 IR snapshot baseline — unit tests.

Run with:
    python -m pytest tests/v3/test_migration_diff.py -v
"""

from __future__ import annotations
import json

import pytest

from pdf2zh.v3.migration_diff import (
    BlockRecord,
    normalize_block,
    dice_similarity,
    overlap_rate,
    MigrationDiffReport,
    MigrationDiffHarness,
    snapshot_ir,
    SyntheticCorpus,
)

# ── Dice similarity ──────────────────────────────────────────────────


def test_dice_identical_and_disjoint():
    assert dice_similarity("hello world", "hello world") == 1.0
    assert dice_similarity("hello world", "foo bar baz") == 0.0


def test_dice_empty_inputs():
    assert dice_similarity("", "") == 1.0
    assert dice_similarity("a b", "") == 0.0


def test_dice_partial_overlap():
    assert dice_similarity("a b c", "a b d") == pytest.approx(2.0 / 3.0)


# ── normalize_block ──────────────────────────────────────────────────


def test_normalize_block_from_dict():
    b = normalize_block(
        {"id": "n1", "text": "hi", "page_num": 2, "bbox": (10, 20, 110, 40)}
    )
    assert isinstance(b, BlockRecord)
    assert b.node_id == "n1"
    assert b.page == 2
    assert (b.x, b.y, b.width, b.height) == (10, 20, 100, 20)
    assert b.x1 == 110 and b.y1 == 40


def test_normalize_block_from_object_and_tuple_bbox():
    class Legacy:
        id = "lg"
        text = "legacy line"
        page_num = 1
        bbox = (0.0, 0.0, 200.0, 15.0)

    b = normalize_block(Legacy(), fallback_page=0)
    assert b.node_id == "lg" and b.width == 200.0

    b2 = normalize_block(("t", (5, 5, 105, 25)), fallback_page=3)
    assert b2.page == 3 and b2.text == "t"
    assert b2.width == 100.0 and b2.height == 20.0


def test_normalize_block_missing_bbox():
    b = normalize_block({"id": "n", "text": "x"})
    assert (b.x, b.y, b.width, b.height) == (0.0, 0.0, 0.0, 0.0)


# ── overlap rate ─────────────────────────────────────────────────────


def test_overlap_rate_empty():
    assert overlap_rate([]) == 0.0


def test_overlap_rate_detects_pair():
    a = BlockRecord("a", 0, "a", 0, 0, 100, 20)
    b = BlockRecord("b", 0, "b", 50, 10, 100, 20)
    c = BlockRecord("c", 1, "c", 0, 0, 100, 20)  # different page
    assert overlap_rate([a, b, c]) == pytest.approx(1.0 / 3.0)


# ── Harness regression baseline ──────────────────────────────────────


def _blocks_a():
    return [
        {
            "id": "p1",
            "text": "Introduction to machine learning",
            "page_num": 1,
            "bbox": (72, 72, 372, 92),
        },
        {
            "id": "p2",
            "text": "A second paragraph",
            "page_num": 1,
            "bbox": (72, 120, 372, 140),
        },
        {"id": "p3", "text": "Conclusion", "page_num": 2, "bbox": (72, 72, 372, 92)},
    ]


def test_harness_pass_when_identical():
    blocks = _blocks_a()
    report = MigrationDiffHarness().compute(blocks, blocks)
    assert isinstance(report, MigrationDiffReport)
    assert report.passed
    assert report.text_similarity == 1.0
    assert report.node_match_ratio == 1.0
    assert report.page_diff == 0
    json.dumps(report.to_dict())


def test_harness_flags_text_drift():
    legacy = _blocks_a()
    v4 = [dict(b, text="Totally unrelated content") for b in legacy]
    report = MigrationDiffHarness().compute(legacy, v4)
    assert not report.passed
    assert any("text similarity" in r for r in report.regressions)


def test_harness_flags_node_loss():
    legacy = _blocks_a()
    v4 = legacy[:-1]
    report = MigrationDiffHarness().compute(legacy, v4)
    assert not report.passed
    assert any("node match" in r for r in report.regressions)


def test_harness_page_drift():
    legacy = _blocks_a()
    v4 = _blocks_a() + [
        {"id": "extra", "text": "x", "page_num": 9, "bbox": (0, 0, 10, 10)}
    ]
    report = MigrationDiffHarness().compute(legacy, v4)
    assert not report.passed
    assert any("page count drift" in r for r in report.regressions)


def test_harness_custom_thresholds():
    blocks = _blocks_a()
    # a tiny displacement is allowed under relaxed thresholds
    report = MigrationDiffHarness(thresholds={"bbox_displacement": 1000.0}).compute(
        blocks, blocks
    )
    assert report.passed


def test_harness_bbox_displacement_regression():
    legacy = _blocks_a()
    v4 = [
        dict(b, bbox=(b["bbox"][0], b["bbox"][1] + 200, *b["bbox"][2:])) for b in legacy
    ]
    report = MigrationDiffHarness().compute(legacy, v4)
    assert not report.passed
    assert any("bbox displacement" in r for r in report.regressions)


# ── IR snapshot baseline ─────────────────────────────────────────────


def test_ir_roundtrip_baseline():
    """阶段零 round-trip 基线: IR → JSON → IR preserves nodes, roles, text."""
    from pdf2zh.v3.document_ir import DocumentIR, IRBuilder

    corpus = SyntheticCorpus(count=1, seed=3)
    g = corpus.make_document_graph(0, "roundtrip")
    ir = IRBuilder(title="roundtrip", source_lang="en", target_lang="zh-cn").build(g)
    restored = DocumentIR.from_json(ir.to_json())
    assert restored.title == ir.title
    assert restored.node_count == ir.node_count
    assert restored.source_lang == "en" and restored.target_lang == "zh-cn"
    for n in ir.nodes():
        r = restored.get_node(n.id)
        assert r is not None
        assert r.text == n.text
        assert r.semantic == n.semantic
        assert r.bbox == n.bbox
        assert r.page_num == n.page_num


def test_snapshot_ir_buckets():
    corpus = SyntheticCorpus(count=1, seed=7)
    snap = corpus.snapshot(0, "sample")
    assert snap["schema"] == "pdf2zh.v3.ir-snapshot"
    assert snap["title"] == "sample"
    assert snap["node_count"] > 0
    assert snap["paragraphs"] or snap["headings"]
    for bucket in (
        "paragraphs",
        "captions",
        "tables",
        "headings",
        "formulas",
        "references",
        "others",
    ):
        assert bucket in snap
    json.dumps(snap)


def test_synthetic_corpus_100_docs_deterministic():
    corpus = SyntheticCorpus(count=100, seed=42)
    run1 = corpus.run()
    run2 = corpus.run()
    assert len(run1) == 100
    assert run1 == run2  # deterministic golden baseline
    assert len({s["title"] for s in run1}) == 100  # unique titles
    # every layout family is represented in the corpus snapshot set
    assert any(s["formulas"] for s in run1)  # textbook
    assert any(s["references"] for s in run1)  # paper_two_column
    assert any(s["tables"] for s in run1)  # figure_heavy


def test_corpus_covers_layout_families():
    corpus = SyntheticCorpus(count=3, seed=1)
    snaps = [corpus.snapshot(i) for i in range(3)]
    families = [corpus.TEMPLATES[i % len(corpus.TEMPLATES)] for i in range(3)]
    # textbook family exposes formulas; paper family exposes references
    assert any(s["formulas"] for s, f in zip(snaps, families) if f == "textbook")
