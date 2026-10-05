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
