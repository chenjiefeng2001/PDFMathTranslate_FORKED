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
                            {
                                "bbox": [0, 0, 300, 24],
                                "content": "Hello MagicPDF",
                                "type": "text",
                            },
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
        magicpdf_ocr_mode="auto",
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

        self.assertEqual(
            resolve_parse_engine(make_args(parse_engine="magicpdf")), "magicpdf"
        )
        self.assertEqual(
            resolve_parse_engine(make_args(parse_engine="legacy")), "legacy"
        )
        self.assertEqual(
            resolve_parse_engine(make_args(parse_engine="babeldoc")), "babeldoc"
        )


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

    @patch(
        "pdf2zh.pdf2zh._run_legacy_kernel",
        return_value=7,
    )
    def test_fallback_raises_degrade_to_babeldoc(self, _mock_kernel):
        """矛盾配置（magicpdf 引擎 + BabelDOC 模式）：MinerU 不可用时不静默
        降级 legacy，而是抛 MagicPdfDegradeError 由服务层改走 BabelDOC。"""
        import pytest as _pytest

        from pdf2zh.magicpdf_cli import (
            MagicPdfDegradeError,
            run_magicpdf_main,
        )

        with _pytest.raises(MagicPdfDegradeError):
            run_magicpdf_main(
                make_args(),
                progress_cb=lambda *a, **k: None,
                degrade_to="babeldoc",
            )
        _mock_kernel.assert_not_called()  # 不得跑 legacy 内核

    @patch(
        "pdf2zh.pdf2zh._run_legacy_kernel",
        return_value=7,
    )
    def test_fallback_emits_degrade_event(self, _mock_kernel):
        """修复 #2：熔断降级必须经 progress_cb 显式上报降级事件。

        旧版降级后 legacy 在服务进程内默默翻译，UI 永远停在解析期最后一个
        百分比（任务「假死」在 ~38%）——降级事实必须作为进度事件透传。
        """
        from pdf2zh.magicpdf_cli import _fallback_legacy

        events: list = []
        ns = make_args()
        code = _fallback_legacy(
            ns, "paper.pdf 解析失败", progress_cb=lambda *a, **k: events.append(a)
        )
        self.assertEqual(code, 7)
        self.assertTrue(getattr(ns, "_magicpdf_fallback", False))
        self.assertEqual(len(events), 1)
        stage, pct, msg = events[0][:3]
        self.assertEqual(stage, "analyzing")
        self.assertEqual(pct, 10.0)
        self.assertIn("[降级]", msg)
        self.assertIn("paper.pdf 解析失败", msg)

    def test_full_flow_with_dumps(self):
        from pdf2zh.magicpdf_cli import run_magicpdf_main

        fake_translator = Mock()
        fake_translator.translate = Mock(side_effect=lambda t: "T:" + t)

        results = MagicPdfAdapter.from_middle_json(SAMPLE_MIDDLE)
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = os.path.join(tmp, "paper.pdf")
            with open(pdf_path, "w", encoding="utf-8") as fh:
                fh.write("%PDF-1.4 placeholder")

            with (
                patch(
                    "pdf2zh.magicpdf_adapter.MagicPdfAdapter.is_available",
                    return_value=True,
                ),
                patch(
                    "pdf2zh.magicpdf_adapter.MagicPdfAdapter.parse",
                    return_value=results,
                ),
                patch(
                    "pdf2zh.translator.build_translator",
                    return_value=fake_translator,
                ) as bt,
            ):
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
            translated = [
                b["metadata"].get("translated") for b in doc_json["pages"][0]["blocks"]
            ]
            self.assertTrue(translated[0].startswith("T:"))
            bt.assert_called_once()


class TestTorchPreload(unittest.TestCase):
    """torch 预载（Windows DLL 加载顺序防御）——双轨收尾修复的定向回归。"""

    def test_preload_torch_returns_bool(self):
        import sys

        from pdf2zh.magicpdf_cli import _preload_torch

        if "torch" in sys.modules:
            # torch 已可导入的正常环境：必须返回 True
            self.assertTrue(_preload_torch())
        else:
            # torch 未加载时预载成功与否都不得抛异常
            self.assertIsInstance(_preload_torch(), bool)

    def test_preload_torch_swallows_import_error(self):
        import sys

        from pdf2zh.magicpdf_cli import _preload_torch

        saved = sys.modules.get("torch")
        try:
            # sys.modules 中值为 None 会使 import 抛 ImportError，
            # 用于模拟 torch 缺失/损坏环境。
            sys.modules["torch"] = None  # type: ignore[assignment]
            self.assertFalse(_preload_torch())
        finally:
            if saved is None:
                sys.modules.pop("torch", None)
            else:
                sys.modules["torch"] = saved

    def test_run_magicpdf_main_preloads_torch_first(self):
        """run_magicpdf_main 入口先预载 torch，再创建适配器。"""
        order = []
        with (
            patch(
                "pdf2zh.magicpdf_cli._preload_torch",
                side_effect=lambda: order.append("preload") or True,
            ),
            patch(
                "pdf2zh.magicpdf_adapter.MagicPdfAdapter.is_available",
                side_effect=lambda: order.append("is_available") or False,
            ),
            patch(
                "pdf2zh.magicpdf_cli._fallback_legacy",
                return_value=0,
            ) as fb,
            patch(
                "pdf2zh.magicpdf_adapter.MagicPdfAdapter.__init__",
                return_value=None,
            ),
        ):
            from pdf2zh.magicpdf_cli import run_magicpdf_main

            run_magicpdf_main(make_args())
        self.assertEqual(order[:2], ["preload", "is_available"])
        fb.assert_called_once()


class TestFallbackAntiPingPong(unittest.TestCase):
    """熔断降级防乒乓：_fallback_legacy 打标记后不再自动切回 magicpdf。"""

    def test_fallback_sets_reentry_flag(self):
        import argparse

        from pdf2zh.magicpdf_cli import _fallback_legacy

        ns = argparse.Namespace(files=["x.pdf"], _magicpdf_fallback=False)
        recorded = {}

        def fake_legacy(parsed_args):
            recorded["flag"] = getattr(parsed_args, "_magicpdf_fallback", False)
            return 0

        with patch("pdf2zh.pdf2zh._run_legacy_kernel", side_effect=fake_legacy):
            code = _fallback_legacy(ns, "engine broken")
        self.assertEqual(code, 0)
        self.assertTrue(recorded["flag"])

    def test_auto_switch_skips_after_fallback(self):
        import argparse

        from pdf2zh.pdf2zh import _try_auto_switch_magicpdf

        ns = argparse.Namespace(
            files=["scanned.pdf"],
            parse_engine="auto",
            magicpdf_ocr=False,
            magicpdf_ocr_mode="auto",
            _magicpdf_fallback=True,
        )
        # 刚从 magicpdf 熔断降级回来：即使文件命中扫描信号也不得切回。
        self.assertFalse(_try_auto_switch_magicpdf(ns))


if __name__ == "__main__":
    unittest.main()
