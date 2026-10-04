"""D1 的真实落点验证：identity 翻译必须让 run_magicpdf_main 返回硬失败。

用 stub 翻译器精确复刻三种结局，不碰 MinerU/OCR：

- 翻译器抛异常 → 硬失败（这是修复前就有的行为，必须不回归）
- 翻译器返回原文 → **修复前 rc=0（把原文当译文交付），修复后必须硬失败**
- 翻译器返回真译文 → rc=0

为此把 ``run_magicpdf_main`` 的解析阶段替换成假的 document model，
只测「翻译 → 退出码」这一段。
"""

import os
import tempfile
import unittest
from unittest.mock import patch

from pdf2zh import magicpdf_cli
from pdf2zh.pdf2zh import parse_args
from pdf2zh.v3.canonical_page import BlockModel, LineModel, PageModel, SpanModel
from pdf2zh.v3.document_model import DocumentModel


def _model(text="Structural holes are absent relationships."):
    span = SpanModel(text=text, x0=30, y0=40, x1=200, y1=52, size=12.0)
    line = LineModel(spans=[span])
    block = BlockModel(
        kind="paragraph",
        text=text,
        x0=30,
        y0=40,
        x1=200,
        y1=80,
        lines=[line],
    )
    page = PageModel(page_num=0, width=300, height=300, blocks=[block])
    return DocumentModel(pages=[page])


class _IdentityTranslator:
    """返回与输入相同的内容 —— 缓存命中同源文本 / passthrough 时的真实形态。"""

    def translate(self, value, **_kw):
        return value


class _RealTranslator:
    def translate(self, value, **_kw):
        return f"【译】{value}"


class _BrokenTranslator:
    def translate(self, value, **_kw):
        raise ConnectionError("translate service unreachable")


class TestIdentityTranslationExitCode(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = os.path.join(self.tmp.name, "s.pdf")
        import pymupdf

        doc = pymupdf.open()
        doc.new_page(width=300, height=300)
        doc.save(self.src)
        doc.close()

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, translator_cls):
        out = os.path.join(self.tmp.name, "out")
        os.makedirs(out, exist_ok=True)
        ns = parse_args([self.src])
        ns.output = out
        ns.service = "google"
        ns.lang_in = "en"
        ns.lang_out = "zh-CN"
        ns.thread = 1
        ns.magicpdf_render = False  # 只测退出码，不测渲染
        translator = translator_cls()

        def fake_build(service, lang_in, lang_out, envs=None, **kw):
            return translator

        model = _model()

        def fake_convert_all(self, results):
            return []

        def fake_to_document_model(self, pages):
            return model

        with (
            # 解析层：跳过 MinerU/Marker（adapter.parse / bridge 转换），
            # 直接给一份单页模型，聚焦「翻译 → 退出码」这一段。
            # results 必须是空列表 —— _write_dumps 会对每个元素调 to_dict()。
            patch.object(magicpdf_cli, "_adapter_parse", return_value=[]),
            patch(
                "pdf2zh.v3.magicpdf_bridge.MagicPdfBridge.convert_all",
                fake_convert_all,
            ),
            patch(
                "pdf2zh.v3.magicpdf_bridge.MagicPdfBridge.to_document_model",
                fake_to_document_model,
            ),
            patch("pdf2zh.translator.build_translator", fake_build),
        ):
            return magicpdf_cli.run_magicpdf_main(ns, None)

    def test_identity_translation_is_hard_failure(self):
        rc = self._run(_IdentityTranslator)
        self.assertNotEqual(
            rc,
            0,
            "译文与原文完全相同时不得判成功 —— 那会把原文当译文交付",
        )

    def test_real_translation_succeeds(self):
        self.assertEqual(self._run(_RealTranslator), 0)

    def test_broken_translator_still_hard_fails(self):
        self.assertNotEqual(self._run(_BrokenTranslator), 0)


if __name__ == "__main__":
    unittest.main()
