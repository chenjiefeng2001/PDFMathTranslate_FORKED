"""阶段十一 Multi-Agent Pipeline — unit tests.

Run with:
    python -m pytest tests/v3/test_agents.py -v
"""
from __future__ import annotations
import json

import pytest

from pdf2zh.v3.graph import DocumentGraph, DocumentNode, Edge, EdgeType, NodeType
from pdf2zh.v3.document_ir import IRBuilder
from pdf2zh.v3.agents import (
    ParserReport, LayoutPlan, TypographyPlan, ReviewOutcome, PipelineReport,
    ParserAgent, LayoutAgent, TypographyAgent, TranslateAgent,
    ReviewerAgent, AgentPipeline,
)


def make_document_graph() -> DocumentGraph:
    g = DocumentGraph()
    g.add_node(DocumentNode(id="page_1", node_type=NodeType.PAGE,
                            bbox=(0, 0, 612, 792), page_num=1))
    rows = [
        ("title", "Deep Learning", NodeType.HEADING, 72, 60),
        ("p1", "LLM models scale well on large corpora.", NodeType.PARAGRAPH, 72, 100),
        ("f1", "E = mc^2", NodeType.FORMULA, 72, 160),
        ("p2", "A second paragraph with more context.", NodeType.PARAGRAPH, 72, 220),
        ("ref", "arXiv:2501.00000", NodeType.REFERENCE, 72, 300),
    ]
    for i, (nid, text, ntype, x, y) in enumerate(rows):
        g.add_node(DocumentNode(id=nid, node_type=ntype, text=text,
                                bbox=(x, y, 300, 20), page_num=1))
        g.add_edge(Edge("page_1", nid, EdgeType.CONTAINS))
        if i > 0:
            g.add_edge(Edge(rows[i - 1][0], nid, EdgeType.FOLLOWS))
    return g


def make_ir():
    g = make_document_graph()
    return IRBuilder(title="agent_test", source_lang="en",
                     target_lang="zh-cn").build(g)


# ── Parser Agent ─────────────────────────────────────────────────────

def test_parser_verify_ok():
    report = ParserAgent().verify(make_ir())
    assert isinstance(report, ParserReport)
    assert report.ok
    assert report.node_count >= 5
    assert report.pages >= 1


def test_parser_flags_missing_text():
    g = make_document_graph()
    g.add_node(DocumentNode(id="empty", node_type=NodeType.PARAGRAPH,
                            text="   ", bbox=(72, 400, 200, 20), page_num=1))
    ir = IRBuilder(title="t", source_lang="en",
                   target_lang="zh-cn").build(g)
    report = ParserAgent().verify(ir)
    assert not report.ok
    assert "empty" in report.missing_text


# ── Layout Agent ─────────────────────────────────────────────────────

def test_layout_plan_solves_without_overlap():
    plan = LayoutAgent().plan(make_ir())
    assert isinstance(plan, LayoutPlan)
    assert plan.solved
    assert len(plan.positions) >= 5
    assert plan.overlap_rate == 0.0
    assert not plan.collisions
    json.dumps(plan.to_dict())


def test_layout_plan_orders_blocks_vertically():
    plan = LayoutAgent().plan(make_ir())
    title_y = plan.positions["title"].y
    p1_y = plan.positions["p1"].y
    f1_y = plan.positions["f1"].y
    assert title_y < p1_y < f1_y


# ── Typography Agent ─────────────────────────────────────────────────

def test_typography_plan_resizes_growing_translation():
    ir = make_ir()
    long_zh = "机器学习模型在大规模语料上表现良好且能够持续扩展规模" * 2
    translations = {
        "p1": long_zh,
        "p2": "第二段简短内容",
        "f1": "E = mc^2",
    }
    plan = TypographyAgent(container_width=180.0).plan(ir, translations)
    assert isinstance(plan, TypographyPlan)
    assert plan.resized  # at least one block grew
    assert plan.metrics["f1"].line_height > 0
    json.dumps(plan.to_dict())


# ── Translate Agent ──────────────────────────────────────────────────

def _stub_translator(text, node_id, strict=False):
    if strict:
        return text.replace("LLM", "大语言模型")
    return f"译文[{node_id}]"


def test_translate_agent_keeps_formula():
    ir = make_ir()
    out = TranslateAgent(_stub_translator).translate(ir)
    assert out["f1"] == "E = mc^2"       # formula untouched
    assert out["ref"] == "arXiv:2501.00000"  # reference untouched
    assert out["p1"].startswith("译文[")
    assert out["p1"] != "E = mc^2"


def test_translate_agent_strict_glossary():
    ir = make_ir()
    agent = TranslateAgent(_stub_translator, glossary={"LLM": "大语言模型"})
    out = agent.translate(ir, strict=True, node_ids=["p1"])
    assert "大语言模型" in out["p1"]


# ── Reviewer Agent ───────────────────────────────────────────────────

def test_reviewer_flags_missing_glossary():
    ir = make_ir()
    translations = {"p1": "模型在语料上表现良好", "p2": "第二段", "f1": "E = mc^2",
                    "title": "标题", "ref": "arXiv:2501.00000"}
    outcome = ReviewerAgent(glossary={"LLM": "大语言模型"}).review(ir, translations)
    assert not outcome.ok
    assert "p1" in outcome.flagged_nodes
    assert any("glossary" in issue for issue in outcome.issues)


def test_reviewer_flags_modified_kept_role():
    ir = make_ir()
    translations = {"f1": "被改写的公式", "p1": "ok", "p2": "ok",
                    "title": "ok", "ref": "ok"}
    outcome = ReviewerAgent().review(ir, translations)
    assert "f1" in outcome.flagged_nodes


def test_reviewer_ok_when_clean():
    ir = make_ir()
    translations = {"f1": "E = mc^2", "p1": "模型在语料上表现良好且包含大语言模型转写",
                    "p2": "ok", "title": "ok", "ref": "ok"}
    outcome = ReviewerAgent(glossary={"LLM": "大语言模型"}).review(ir, translations)
    assert outcome.ok
    assert isinstance(outcome, ReviewOutcome)


# ── AgentPipeline end-to-end ─────────────────────────────────────────

def test_pipeline_converges_with_strict_feedback():
    def translator(text, node_id, strict=False):
        if strict:
            return text.replace("LLM", "大语言模型")
        return f"译文[{node_id}]"
    pipeline = AgentPipeline(
        translator=translator, glossary={"LLM": "大语言模型"},
        max_feedback_rounds=2)
    report = pipeline.run(make_ir())
    assert isinstance(report, PipelineReport)
    assert report.converged
    assert report.rounds == 2          # one review + one strict re-translate
    assert not report.issues
    assert "大语言模型" in report.final_translations["p1"]
    assert report.final_translations["f1"] == "E = mc^2"
    assert report.stages["parser"]["ok"] is True
    json.dumps(report.to_dict())


def test_pipeline_does_not_hang_on_unfixable():
    def never_fix(text, node_id, strict=False):
        return "still wrong" if strict else f"译文[{node_id}]"
    pipeline = AgentPipeline(
        translator=never_fix, glossary={"LLM": "大语言模型"},
        max_feedback_rounds=2)
    report = pipeline.run(make_ir())
    assert report.rounds == pipeline.max_feedback_rounds
    assert not report.converged
    assert report.issues

