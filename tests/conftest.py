"""pytest conftest — 共享 fixture。

目前注册两类：

1. golden regression 的 fixtures（见 :mod:`tests.golden_helpers`）。
2. PDF 样本构造器 —— 「pikepdf 能开、PDFium 拒绝」的混合引用样本原先是
   :mod:`tests.test_pdf_validity_gate` 里的私有函数。该样本如今被两处需要
   （闸门自身 + MinerU 摄入路径），而它是一段容易写错的字节手术，所以收敛到
   这里做**唯一实现**，两边都用它。
"""

import zlib

import pytest
import pymupdf

from tests.golden_helpers import (  # noqa: F401
    golden_artifact,
    golden_expected,
    golden_fixture_name,
    golden_input_pdf,
)


def plain_pdf(path, pages=1, text="hello"):
    """一份常规、可被 PDFium 打开的 PDF（用于「健康文件」一侧的用例）。"""
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page(width=300, height=300)
        page.insert_text((30, 60 + i * 20), f"{text} {i}")
    doc.save(path)
    doc.close()
    return path


def hybrid_encrypt_pdf(path):
    """pikepdf 能开、PDFium 拒绝的样本。

    复现已实证的失效形态：``/Encrypt`` **只挂在 XRef 流上**、传统 trailer 里
    没有 ``/Encrypt``（真实来源是 325 页混合引用文件）。按规范这是无效结构 ——
    严格阅读器读到 ``/Encrypt`` 却建不起 security handler，于是在
    ``FPDF_LoadDocument`` 阶段就失败；宽松的 MuPDF/pikepdf 把它当未加密文档
    放行。所以「能翻译」与「引擎打得开」在这里是两件事。

    做法：给一份正常 PDF 追加 ``/Encrypt`` 字典，再在 trailer 里加
    ``/XRefStm`` 指向一个携带 ``/Encrypt`` 的 /XRef 流。
    """
    plain_pdf(path)
    data = open(path, "rb").read()

    enc_obj = (
        b"1023 0 obj\n<</Filter/Standard/V 4/R 4/Length 128/P -3904"
        b"/O <" + b"0" * 32 + b">/U <" + b"0" * 32 + b">>>\nendobj\n"
    )
    rows = b"\x00" + (0).to_bytes(4, "big") + (65535).to_bytes(2, "big")
    comp = zlib.compress(rows)
    xref_stream_obj = (
        b"1024 0 obj\n<</Type/XRef/Size 1025/W [1 4 2]/Root 1 0 R"
        b"/Encrypt 1023 0 R/Filter/FlateDecode/Length "
        + str(len(comp)).encode()
        + b">>\nstream\n"
        + comp
        + b"\nendstream\nendobj\n"
    )

    ti = data.rfind(b"trailer")
    body, trailer = data[:ti], data[ti:]
    sx = trailer.find(b"startxref")
    if sx >= 0:
        trailer = trailer[:sx]
    new_trailer = trailer.replace(b">>", b"/XRefStm 999999>>", 1)
    open(path, "wb").write(
        body + enc_obj + xref_stream_obj + new_trailer + b"startxref\n999999\n%%EOF\n"
    )
    return path


def _pdfium_can_open(path):
    """PDFium 能否载入（不依赖 pytest 断言，供 fixture 做前提校验）。"""
    from pdf2zh import pdf_validity

    pdfium = pdf_validity._pdfium()
    if pdfium is None:
        return None
    try:
        doc = pdfium.PdfDocument(path)
        doc.close()
        return True
    except Exception:  # noqa: BLE001 -- PDFium 的错误类型随版本变化
        return False


def _require_pdfium():
    from pdf2zh import pdf_validity

    if pdf_validity._pdfium() is None:
        pytest.skip("pypdfium2 unavailable; PDFium-based assertions degrade")


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------
@pytest.fixture()
def hybrid_xref_pdf(tmp_path):
    """混合引用样本（pikepdf 能开、PDFium 拒绝）。"""
    _require_pdfium()
    path = hybrid_encrypt_pdf(str(tmp_path / "paper.pdf"))
    if _pdfium_can_open(path):
        pytest.fail(
            "合成样本竟能被 PDFium 打开 —— PDFium 版本或判据变了，"
            "依赖该样本的用例会失去意义"
        )
    return path


@pytest.fixture()
def healthy_pdf(tmp_path):
    """PDFium 直接能开的正常 PDF。"""
    return plain_pdf(str(tmp_path / "plain.pdf"), pages=2, text="page")


@pytest.fixture(autouse=True)
def _isolated_cache(monkeypatch, tmp_path_factory):
    """测试期间把缓存库重定向到临时目录，别写进用户真实缓存。

    背景不是洁癖：本机那个真实库长到 754 MB / 811,917 行之后，
    ``tests/test_translator.py`` 里两条普通缓存用例在全量跑时从 0.06s 涨到
    36s / 27s —— 而单跑时库是干净的，所以单跑永远看不出来。这类"跑得越久越
    慢"的问题会被误判成机器性能波动。

    作用域用 session 级目录（而非 per-test）：``TranslationCache`` / ``db.init``
    在模块导入期就会绑定路径，per-test 目录来不及生效。session 内共用一个库还
    顺带保住了"同一 session 内缓存命中"的行为。
    """
    session_dir = tmp_path_factory.mktemp("pdf2zh-cache")
    monkeypatch.setenv("PDF2ZH_CACHE_DIR", str(session_dir))
    yield session_dir


# --------------------------------------------------------------------------
# 全局设置隔离：节流间隔绝不许漏到用户真实配置
# --------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _isolated_translation_settings(monkeypatch):
    """测试期间清掉节流间隔，并保证收尾时**不还原成测试值**。

    为什么必须在这里做，而不是让每个用例自己收尾
    --------------------------------------------
    实测踩过：``tests/test_services_api.py::test_config_beats_env`` 通过真实
    端点把 0.4s 写进 ConfigManager（那是**用户真实配置文件**），而
    ``tests/test_request_throttling.py`` 的 fixture 把当前值当 ``old_cfg``
    在收尾时**还原回去** —— 于是 0.4 被一轮轮固化下来。后果不是"脏"：
    同一个进程里后续任何 ``translate()`` 都被真的节流 0.4s，
    ``TestTranslator::test_cache`` 从 0.06s 涨到 27s，单跑却永远看不出来
    （单跑时进程刚启动，值还是干净的）。

    所以这里用 autouse + 显式置 None：**收尾一律清空，不还原**。测试本来就不该
    依赖"上一个用例留下的设置"，还原只会把污染固化。
    """
    from pdf2zh.config import ConfigManager
    from pdf2zh.translator import _MIN_REQUEST_INTERVAL_KEY

    monkeypatch.delenv("PDF2ZH_MIN_REQUEST_INTERVAL", raising=False)
    ConfigManager.set(_MIN_REQUEST_INTERVAL_KEY, None)
    yield
    # 不还原：留在 None 上
    ConfigManager.set(_MIN_REQUEST_INTERVAL_KEY, None)


# --------------------------------------------------------------------------
# 段落打包：测试环境默认关闭
# --------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def packed_translation_off(monkeypatch):
    """默认关掉段落打包，让"translate_fn 每块被调一次"这条契约恢复确定。

    ``translate_document`` 的并发路径上，打包会把多个块并成一次请求 —— 而**哪些
    块会并到一起取决于线程时序**。于是一份用例两次运行可能走进不同分支，不接受
    分隔符的假译者就会随机红：既掩盖真 bug，也让 suite 变得不可信。

    打包自己的用例（``tests/test_dynamic_batching.py``、
    ``test_translate_document_concurrency.py`` 的打包小节）用
    :func:`packed_translation` 显式重新打开。
    """
    monkeypatch.setenv("PDF2ZH_PARAGRAPH_BATCH", "0")


@pytest.fixture()
def packed_translation(monkeypatch):
    """打开段落打包：只给"打包真的接到了调用链上"这类用例用。"""
    monkeypatch.setenv("PDF2ZH_PARAGRAPH_BATCH", "1")
