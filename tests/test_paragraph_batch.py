"""段落级 Batch 翻译单元测试（报告 §8.3.1）。

覆盖 ``pdf2zh.v3.paragraph_batch``：
- 开关关闭时退化为逐段并发翻译（与原 executor.map 语义一致）；
- 开关开启时同页多段合并翻译 + 严格还原校验；
- 还原失败（LLM 吞掉分隔符）→ 整批逐段回退；
- 纯公式占位符 / TOC 行 / 空段不参与打包；
- 输出顺序与输入严格一致。
"""

import os

import pytest

from pdf2zh.v3.paragraph_batch import PARA_BATCH_SEP, batch_translate_paragraphs


def _translate_worker(text, font_sig):
    """逐段翻译语义：每段一个请求，返回独立译文。"""
    return f"TR[{text}]"


def _sep_preserving_worker(text, font_sig):
    """模拟 LLM 批处理：保留 SEP 边界，每段输出独立译文。"""
    parts = text.split(PARA_BATCH_SEP)
    return PARA_BATCH_SEP.join(f"B{i}" for i in range(len(parts)))


def _sep_swallowing_worker(text, font_sig):
    """模拟异常 LLM：吞掉分隔符（还原校验失败 → 逐段回退）。"""
    return "MERGED"


# ── 开关关闭：逐段语义 ─────────────────────────────────────────────────
def test_disabled_falls_back_to_per_segment(monkeypatch):
    monkeypatch.delenv("PDF2ZH_PARAGRAPH_BATCH", raising=False)
    calls = []

    def _counting_worker(text, font_sig):
        calls.append(text)
        return _translate_worker(text, font_sig)

    texts = ["para A", "para B", "para C"]
    out = batch_translate_paragraphs(texts, [""] * 3, [None] * 3, _counting_worker)
    assert out == [f"TR[{t}]" for t in texts]
    assert len(calls) == 3  # 未启用 → 每段一次请求


# ── 开关开启：合并翻译 ─────────────────────────────────────────────────
def test_enabled_batches_segments(monkeypatch):
    monkeypatch.setenv("PDF2ZH_PARAGRAPH_BATCH", "1")
    calls = []

    def _recording_worker(text, font_sig):
        calls.append(text)
        return _sep_preserving_worker(text, font_sig)

    texts = ["para A", "para B", "para C"]
    out = batch_translate_paragraphs(texts, [""] * 3, [None] * 3, _recording_worker)
    assert out == ["B0", "B1", "B2"]
    assert len(calls) < 3  # 合并后请求数显著减少
    assert any(PARA_BATCH_SEP in c for c in calls)


def test_reassembly_failure_falls_back_to_per_segment(monkeypatch):
    monkeypatch.setenv("PDF2ZH_PARAGRAPH_BATCH", "1")
    calls = []

    def _recording_worker(text, font_sig):
        calls.append(text)
        if PARA_BATCH_SEP in text:
            return _sep_swallowing_worker(text, font_sig)  # 吞掉分隔符
        return _translate_worker(text, font_sig)

    texts = ["para A", "para B"]
    out = batch_translate_paragraphs(
        texts, ["f1", "f2"], [None, None], _recording_worker
    )
    # 还原失败 → 整批逐段回退：每个段落原文单独翻译（原始语义）
    assert out == [f"TR[{t}]" for t in texts]


def test_formula_toc_empty_not_packed(monkeypatch):
    monkeypatch.setenv("PDF2ZH_PARAGRAPH_BATCH", "1")
    calls = []

    def _recording_worker(text, font_sig):
        calls.append(text)
        if PARA_BATCH_SEP in text:  # 批输入 → 保留 SEP
            return _sep_preserving_worker(text, font_sig)
        return _translate_worker(text, font_sig)  # 逐段输入 → 独立译文

    texts = [
        "Intro paragraph",
        "{v1}",  # 纯公式占位符
        "",
        "Chapter 1 ..... 1",  # TOC 行
        "Body paragraph",
    ]
    toc_specs = [None, None, None, {"entry": "Chapter 1"}, None]
    out = batch_translate_paragraphs(
        texts, [""] * len(texts), toc_specs, _recording_worker
    )
    # 仅可打包段（Intro/Body）进批；公式 / 空段 / TOC 行走逐段翻译
    assert out[0] == "B0"
    assert out[1] == _translate_worker("{v1}", "")
    assert out[2] == _translate_worker("", "")
    assert out[3] == _translate_worker(texts[3], "")
    assert out[4] == "B1"


def test_ordering_preserved(monkeypatch):
    monkeypatch.setenv("PDF2ZH_PARAGRAPH_BATCH", "1")
    texts = [f"paragraph {i}" for i in range(9)]
    out = batch_translate_paragraphs(
        texts, [""] * 9, [None] * 9, _sep_preserving_worker
    )
    assert out == [f"B{i}" for i in range(9)]  # 顺序与输入严格一致


def test_budget_splits_batches(monkeypatch):
    monkeypatch.setenv("PDF2ZH_PARAGRAPH_BATCH", "1")
    monkeypatch.setenv("PDF2ZH_PARAGRAPH_BATCH_CHARS", "60")  # 强制多批
    calls = []

    def _recording_worker(text, font_sig):
        calls.append(text)
        return _sep_preserving_worker(text, font_sig)

    texts = [f"paragraph {i} text" for i in range(6)]
    out = batch_translate_paragraphs(texts, [""] * 6, [None] * 6, _recording_worker)
    assert len(out) == 6  # 顺序/数量保持
    assert len(calls) > 1  # 预算限制 → 多批
    assert all(PARA_BATCH_SEP not in o for o in out)  # 分隔符不泄漏进译文


def test_single_packable_segment_no_batch(monkeypatch):
    monkeypatch.setenv("PDF2ZH_PARAGRAPH_BATCH", "1")
    calls = []

    def _recording_worker(text, font_sig):
        calls.append(text)
        return _translate_worker(text, font_sig)

    texts = ["only one paragraph"]
    out = batch_translate_paragraphs(texts, [""], [None], _recording_worker)
    assert out == [f"TR[{texts[0]}]"]
    assert len(calls) == 1


# ── Phase 1：keep 掩码（代码保护）──────────────────────────────────
def test_keep_mask_passthrough_when_batch_enabled(monkeypatch):
    monkeypatch.setenv("PDF2ZH_PARAGRAPH_BATCH", "1")
    calls = []

    def _counting_worker(text, font_sig):
        calls.append(text)
        return _translate_worker(text, font_sig)

    texts = ["def foo():\n    x = 1", "body paragraph", "more\n    code"]
    keep = [True, False, True]
    out = batch_translate_paragraphs(
        texts, [""] * 3, [None, None, None], _counting_worker, keep=keep
    )
    # keep 段落原样保留（绝不进翻译器）；非 keep 正常翻译
    assert out == [texts[0], f"TR[{texts[1]}]", texts[2]]
    assert calls == ["body paragraph"]  # 仅非 keep 段落触达翻译器


def test_keep_mask_passthrough_when_batch_disabled(monkeypatch):
    monkeypatch.delenv("PDF2ZH_PARAGRAPH_BATCH", raising=False)
    calls = []

    def _counting_worker(text, font_sig):
        calls.append(text)
        return _translate_worker(text, font_sig)

    texts = ["code line", "prose"]
    out = batch_translate_paragraphs(
        texts, ["", ""], [None, None], _counting_worker, keep=[True, False]
    )
    assert out == ["code line", f"TR[prose]"]
    assert calls == ["prose"]


def test_keep_mask_applies_when_few_packable(monkeypatch):
    # 可打包段 < 2 走 short-circuit；keep 仍须生效（回归：防止 keep 段被翻译器翻译）
    monkeypatch.setenv("PDF2ZH_PARAGRAPH_BATCH", "1")
    calls = []

    def _counting_worker(text, font_sig):
        calls.append(text)
        return _translate_worker(text, font_sig)

    texts = ["only code block", "single prose"]
    out = batch_translate_paragraphs(
        texts, ["", ""], [None, None], _counting_worker, keep=[True, False]
    )
    assert out == ["only code block", f"TR[single prose]"]
    assert calls == ["single prose"]
