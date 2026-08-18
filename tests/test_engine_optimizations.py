"""引擎优化点单元测试（报告 §8.2.2 / §8.3.2 / §8.4.1）。

覆盖：
- translator OpenAI / AzureOpenAI client 进程内缓存（``_get_openai_client`` /
  ``_get_azure_openai_client``）：同参复用、异参重建、缓存清理；
- ``high_level._prefetch_predict`` 预取线程目标（成功/异常封装）；
- ``high_level`` 写回参数 ``garbage=4, clean=True`` 合法且产出可打开文档；
- 并行 ``_translate_parallel`` 走 Warm Pool 的调用路径（env 开关语义）。
"""

import os
from pathlib import Path

import pytest

import pdf2zh.translator as translator_mod

from pdf2zh.parallel import pool as pool_mod

_HERE = Path(__file__).parent
_SAMPLE_PDF = _HERE / "file" / "TestPDF.pdf"


# ── 8.2.2 OpenAI / Azure client 缓存 ───────────────────────────────────
def test_openai_client_cache_reuses_instance(monkeypatch):
    calls = []

    class FakeOpenAI:
        def __init__(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(translator_mod.openai, "OpenAI", FakeOpenAI)
    try:
        c1 = translator_mod._get_openai_client("http://x", "k1")
        c2 = translator_mod._get_openai_client("http://x", "k1")
        assert c1 is c2
        assert len(calls) == 1  # 同参只构造一次

        c3 = translator_mod._get_openai_client("http://y", "k2")
        assert c3 is not c1
        assert len(calls) == 2  # 异参重建
    finally:
        translator_mod.clear_openai_client_cache()


def test_openai_client_cache_after_clear(monkeypatch):
    class FakeOpenAI:
        def __init__(self, **kwargs):
            pass

    monkeypatch.setattr(translator_mod.openai, "OpenAI", FakeOpenAI)
    try:
        c1 = translator_mod._get_openai_client("http://x", "k1")
        translator_mod.clear_openai_client_cache()
        c2 = translator_mod._get_openai_client("http://x", "k1")
        assert c2 is not c1  # 清缓存后重建
    finally:
        translator_mod.clear_openai_client_cache()


def test_azure_openai_client_cache(monkeypatch):
    calls = []

    class FakeAzureOpenAI:
        def __init__(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(translator_mod.openai, "AzureOpenAI", FakeAzureOpenAI)
    try:
        a1 = translator_mod._get_azure_openai_client(
            "http://az", "gpt-4", "2024-01", "k"
        )
        a2 = translator_mod._get_azure_openai_client(
            "http://az", "gpt-4", "2024-01", "k"
        )
        assert a1 is a2
        assert len(calls) == 1
        assert calls[0]["azure_deployment"] == "gpt-4"
        assert calls[0]["api_version"] == "2024-01"

        a3 = translator_mod._get_azure_openai_client(
            "http://az2", "gpt-4o", "2024-02", "k"
        )
        assert a3 is not a1
    finally:
        translator_mod.clear_openai_client_cache()


# ── 8.3.2 预测预取线程目标 ─────────────────────────────────────────────
def test_prefetch_predict_success():
    import pdf2zh.high_level as hl

    class FakeModel:
        def predict(self, image, imgsz=None):
            return ["layout-result"]

    layout, elapsed = hl._prefetch_predict(FakeModel(), None, 512)
    assert layout == "layout-result"
    assert isinstance(elapsed, float)


def test_prefetch_predict_error_wrapped():
    import pdf2zh.high_level as hl

    class BoomModel:
        def predict(self, image, imgsz=None):
            raise RuntimeError("inference exploded")

    res = hl._prefetch_predict(BoomModel(), None, 512)
    assert isinstance(res, RuntimeError)  # 异常封装为返回值，主线程同步兜底


def test_prefetch_switch_env_parsed():
    import pdf2zh.high_level as hl

    os.environ["PDF2ZH_LAYOUT_PREFETCH"] = "1"
    try:
        assert hl._int_env("PDF2ZH_LAYOUT_PREFETCH", 0) >= 1
    finally:
        os.environ.pop("PDF2ZH_LAYOUT_PREFETCH", None)
    assert hl._int_env("PDF2ZH_LAYOUT_PREFETCH", 0) == 0


# ── 8.4.1 写回参数合法性 ───────────────────────────────────────────────
@pytest.mark.skipif(not _SAMPLE_PDF.exists(), reason="sample pdf missing")
def test_write_params_garbage4_clean_produce_openable_pdf():
    import pymupdf

    doc = pymupdf.open(str(_SAMPLE_PDF))
    try:
        data = doc.write(deflate=True, garbage=4, clean=True, use_objstms=1)
    finally:
        doc.close()
    out = pymupdf.open(stream=data, filetype="pdf")
    try:
        assert out.page_count >= 1
    finally:
        out.close()


# ── 8.2.1 Warm Pool 开关接入路径 ───────────────────────────────────────
def test_translate_parallel_warm_pool_switch():
    # 未启用时 _translate_parallel 回落每次新建池路径（旧行为）。
    os.environ.pop("PDF2ZH_WARM_POOL", None)
    assert pool_mod.get_shared_pool(4, "cpu") is None

    os.environ["PDF2ZH_WARM_POOL"] = "1"
    try:
        sp = pool_mod.get_shared_pool(4, "cpu")
        assert sp is not None
    finally:
        pool_mod.shutdown_shared_pool()
