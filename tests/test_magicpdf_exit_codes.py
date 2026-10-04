"""``run_magicpdf_main`` 的退出码契约回归。

背景缺陷：``rendered_pdf`` 只在 ``if render_mono:`` 分支里被赋值，但文件
收尾处（``if translation_failed and partial_translation and rendered_pdf``）
无条件读取它。于是 ``--no-magicpdf-render`` + 翻译失败 这条组合会抛
``UnboundLocalError``，调用方拿到的是 traceback 而不是退出码 —— 服务层无法
区分「翻译失败」和「CLI 崩了」。

修复：每轮循环开头 ``rendered_pdf: str | None = None``。

这里锁定三件事：
1. 上述组合返回 1 而不是抛异常；
2. 翻译器构造失败（更靠前的 except 分支）同样返回 1；
3. 开启渲染且「部分块翻译成功」时返回 EXIT_PARTIAL（不是硬失败）。
"""

import argparse
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from pdf2zh.magicpdf_adapter import MagicPdfAdapter
from pdf2zh.magicpdf_cli import EXIT_PARTIAL, run_magicpdf_main


def _middle_with_blocks(count: int) -> dict:
    """构造含 ``count`` 个可翻译块的 middle json（每块一次 translate 调用）。"""
    blocks = []
    for i in range(count):
        top = 24 + i * 30
        blocks.append(
            {
                "type": "text",
                "bbox": [0, top, 600, top + 24],
                "cls": "title",
                "lines": [
                    {
                        "bbox": [0, top, 600, top + 24],
                        "spans": [
                            {
                                "bbox": [0, top, 600, top + 24],
                                "content": f"Block number {i}",
                                "type": "text",
                            }
                        ],
                    }
                ],
            }
        )
    return {
        "pdf_info": [blocks],
        "page_info": [{"page_no": 0, "width": 612, "height": 792}],
    }


def _make_args(render: bool, **kw) -> argparse.Namespace:
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
        magicpdf_render=render,
    )
    for key, value in kw.items():
        setattr(ns, key, value)
    return ns


class TestMagicPdfExitCodes(unittest.TestCase):
    def _run(
        self, render, translate_side_effect, translator_factory=None, block_count=1
    ):
        results = MagicPdfAdapter.from_middle_json(_middle_with_blocks(block_count))
        translator = Mock()
        translator.translate = Mock(side_effect=translate_side_effect)

        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = os.path.join(tmp, "paper.pdf")
            with open(pdf_path, "w", encoding="utf-8") as fh:
                fh.write("%PDF-1.4 placeholder")
            factory = (
                translator_factory
                if translator_factory is not None
                else Mock(return_value=translator)
            )
            with (
                patch(
                    "pdf2zh.magicpdf_adapter.MagicPdfAdapter.is_available",
                    return_value=True,
                ),
                patch(
                    "pdf2zh.magicpdf_adapter.MagicPdfAdapter.parse",
                    return_value=results,
                ),
                patch("pdf2zh.translator.build_translator", factory),
            ):
                code = run_magicpdf_main(
                    _make_args(render=render, files=[pdf_path], output=tmp)
                )
            return code

    def test_no_render_with_total_translation_failure_returns_one(self):
        """回归点：这条组合过去抛 UnboundLocalError。"""
        code = self._run(
            render=False,
            translate_side_effect=RuntimeError("HTTP 429"),
        )
        self.assertEqual(code, 1)

    def test_render_with_total_translation_failure_returns_one(self):
        code = self._run(
            render=True,
            translate_side_effect=RuntimeError("HTTP 429"),
        )
        self.assertEqual(code, 1)

    def test_translator_construction_failure_returns_one(self):
        """更靠前的 except 分支：翻译器都构造不出来。"""

        def _boom(*_a, **_k):
            raise RuntimeError("no such service")

        code = self._run(
            render=False,
            translate_side_effect=lambda t: "T:" + t,
            translator_factory=_boom,
        )
        self.assertEqual(code, 1)

    def test_success_returns_zero(self):
        code = self._run(render=True, translate_side_effect=lambda t: "T:" + t)
        self.assertEqual(code, 0)

    def test_partial_translation_with_render_returns_exit_partial(self):
        """首个块失败、其余成功且产物已落盘 → EXIT_PARTIAL，不是硬失败。"""
        calls = {"n": 0}

        def _flaky(text):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("HTTP 429")
            return "T:" + text

        code = self._run(render=True, translate_side_effect=_flaky, block_count=3)
        self.assertEqual(
            code,
            EXIT_PARTIAL,
            "部分块翻译失败但产物已生成时应报 EXIT_PARTIAL",
        )

    def test_partial_translation_without_render_is_hard_failure(self):
        """没渲染产物就没有可交付文件，只能算硬失败（且不得抛异常）。"""
        calls = {"n": 0}

        def _flaky(text):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("HTTP 429")
            return "T:" + text

        code = self._run(render=False, translate_side_effect=_flaky, block_count=3)
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
