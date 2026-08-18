"""Step 2.3 — ``--parse-engine magicpdf`` CLI 路由与执行器单元测试。

覆盖：
- 参数解析：--parse-engine {auto,legacy,babeldoc,magicpdf} + --magicpdf-ocr；
- resolve_parse_engine：auto 维持历史语义（--babeldoc → babeldoc），
  显式值直接生效；
- run_magicpdf_main：引擎不可用 → 熔断降级 _run_legacy_kernel；引擎可用
  → parse→bridge→translate→dump→render plan 全链路 + 退出码 0。
"""
import argparse
import json
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from pdf2zh.magicpdf_adapter import MagicPdfAdapter

SAMPLE_MIDDLE = {
    "pdf_info": [
        [
            {
                "type": "text",
                "bbox": [0, 0, 300, 24],
                "cls": "title",
                "lines": [
                    {
                        "bbox": [0, 0, 300, 24],
                        "spans": [
                            {"bbox": [0, 0, 300, 24], "content": "Hello MagicPDF", "type": "text"},
                        ],
                    }
                ],
            },
            {
                "type": "text",
                "bbox": [0, 40, 520, 70],
                "cls": "body",
                "lines": [
                    {
                        "bbox": [0, 40, 520, 70],
                        "spans": [
                            {
                                "bbox": [0, 40, 520, 70],
                                "content": "A bridge test sentence.",
                                "type": "text",
                            }
                        ],
                    }
                ],
            },
        ]
    ],
    "page_info": [{"page_no": 0, "width": 612, "height": 792}],
}


def make_args(**kw):
    ns = argparse.Namespace(
        files=["paper.pdf"],
        output="",
        pages=None,
        lang_in="en",
        lang_out="zh",
        service="google",
        thread=4,
        no_parallel=False,
        parallel_workers=None,
        vfont="",
        vchar="",
        envs={},
        prompt=None,
        ignore_cache=False,
        compatible=False,
        debug=False,
        dir=False,
        backend="auto",
        mode="fast",
        parse_engine="magicpdf",
        magicpdf_ocr=False,
    )
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


class TestCliArgs(unittest.TestCase):
    def test_parse_engine_arg(self):
        from pdf2zh.pdf2zh import parse_args

        args = parse_args(["--parse-engine", "magicpdf", "--magicpdf-ocr", "x.pdf"])
        self.assertEqual(args.parse_engine, "magicpdf")
        self.assertTrue(args.magicpdf_ocr)
        self.assertEqual(args.files, ["x.pdf"])

    def test_default_is_auto(self):
        from pdf2zh.pdf2zh import parse_args

        args = parse_args(["x.pdf"])
        self.assertEqual(args.parse_engine, "auto")


class TestResolveParseEngine(unittest.TestCase):
    def test_auto_babeldoc(self):
        from pdf2zh.pdf2zh import resolve_parse_engine

        self.assertEqual(
            resolve_parse_engine(make_args(parse_engine="auto", babeldoc=True)),
            "babeldoc",
        )
        self.assertEqual(
            resolve_parse_engine(make_args(parse_engine="auto", babeldoc=False)),
            "auto",
        )

    def test_explicit_values(self):
        from pdf2zh.pdf2zh import resolve_parse_engine

        self.assertEqual(resolve_parse_engine(make_args(parse_engine="magicpdf")), "magicpdf")
        self.assertEqual(resolve_parse_engine(make_args(parse_engine="legacy")), "legacy")
        self.assertEqual(resolve_parse_engine(make_args(parse_engine="babeldoc")), "babeldoc")


class TestRunMagicPdfMain(unittest.TestCase):
    @patch("pdf2zh.magicpdf_cli._fallback_legacy", return_value=7)
    @patch(
        "pdf2zh.magicpdf_adapter.MagicPdfAdapter.is_available",
        return_value=False,
    )
    def test_fallback_when_engine_unavailable(self, *_):
        from pdf2zh.magicpdf_cli import run_magicpdf_main

        code = run_magicpdf_main(make_args())
        self.assertEqual(code, 7)

    def test_full_flow_with_dumps(self):
        from pdf2zh.magicpdf_cli import run_magicpdf_main

        fake_translator = Mock()
        fake_translator.translate = Mock(side_effect=lambda t: "T:" + t)

        results = MagicPdfAdapter.from_middle_json(SAMPLE_MIDDLE)
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = os.path.join(tmp, "paper.pdf")
            with open(pdf_path, "w", encoding="utf-8") as fh:
                fh.write("%PDF-1.4 placeholder")

            with patch(
                "pdf2zh.magicpdf_adapter.MagicPdfAdapter.is_available",
                return_value=True,
            ), patch(
                "pdf2zh.magicpdf_adapter.MagicPdfAdapter.parse",
                return_value=results,
            ), patch(
                "pdf2zh.translator.build_translator",
                return_value=fake_translator,
            ) as bt:
                code = run_magicpdf_main(make_args(files=[pdf_path], output=tmp))

            self.assertEqual(code, 0)
            parse_dump = os.path.join(tmp, "magicpdf", "paper_magicpdf.json")
            doc_dump = os.path.join(tmp, "magicpdf", "paper_document.json")
            self.assertTrue(os.path.exists(parse_dump))
            self.assertTrue(os.path.exists(doc_dump))
            with open(doc_dump, "r", encoding="utf-8") as fh:
                doc_json = json.load(fh)
            self.assertEqual(doc_json["stats"]["blocks"], 2)
            # 标题与正文被翻译，译文回填 metadata
            translated = [b["metadata"].get("translated") for b in doc_json["pages"][0]["blocks"]]
            self.assertTrue(translated[0].startswith("T:"))
            bt.assert_called_once()


if __name__ == "__main__":
    unittest.main()
