"""toc_render_sidechannel 适配层直接测试（Commit 6C 接线）。

``build_block_toc_payload`` / ``build_page_toc_payload`` 已通过
``block_translation_unit``（test_architecture_7a）与 TocRenderer
（test_semantic_toc_renderer / test_toc_render_integration）间接覆盖；
本文件直接覆盖薄适配层的自身契约：

1.  有 toc_entries 的块 → JSON 安全命令载荷；
2.  无条目的块 → 空载荷（上层回退普通块渲染），不抛异常；
3.  条目几何（title_x/page_x）透传、基线取块行中位 y；
4.  build_page_toc_payload：非 TOC 页 → 空命令；translate 只收到 title_only；
5.  渲染失败 → 空载荷回退，绝不抛出。
"""

import json
import unittest

from pdf2zh.v3.canonical_page import BlockModel, LineModel
from pdf2zh.v3.toc_render_sidechannel import (
    build_block_toc_payload,
    build_page_toc_payload,
)


def _entry(number="1.", title_only="Introduction", title_x=72.0, page_x=540.0, page_number="1"):
    return {
        "title": f"{number} {title_only}".strip(),
        "number": number,
        "title_only": title_only,
        "level": 0,
        "page_number": page_number,
        "destination_page": 3,
        "title_x": title_x,
        "page_x": page_x,
        "indent": title_x,
        "dot_leader": "................",
        "leader_present": True,
        "continuation": [],
        "bbox": [title_x, 40.0, page_x, 60.0],
    }


def _toc_block(entries):
    block = BlockModel(
        text="TOC placeholder",
        kind="toc",
        x0=40.0,
        y0=40.0,
        x1=540.0,
        y1=200.0,
        lines=[
            LineModel(text="1. Introduction", y0=700.0, y1=712.0),
            LineModel(text="2. Method", y0=714.0, y1=726.0),
        ],
    )
    block.metadata["toc_entries"] = entries
    return block


class TestBuildBlockTocPayload(unittest.TestCase):
    def test_payload_json_safe_with_commands(self):
        payload = build_block_toc_payload(
            _toc_block([_entry(), _entry("2.", "Method", title_x=96.0, page_number="5")]),
            size=10.0,
        )
        json.dumps(payload)
        self.assertTrue(payload["commands"])
        c0 = payload["commands"][0]
        self.assertIn("text", c0)
        self.assertIn("x", c0)
        self.assertIn("y", c0)

    def test_baseline_from_block_line_mid_y(self):
        payload = build_block_toc_payload(_toc_block([_entry()]), size=10.0)
        # 第一行 bbox 中位 y = (700+712)/2 = 706（v3 左下原点，原样透传）
        self.assertEqual(payload["commands"][0]["y"], 706.0)

    def test_empty_entries_returns_empty_payload(self):
        payload = build_block_toc_payload(_toc_block([]), size=10.0)
        self.assertEqual(payload["commands"], [])
        self.assertEqual(payload["translated_calls"], [])

    def test_render_failure_returns_empty_never_raises(self):
        from unittest.mock import patch

        with patch(
            "pdf2zh.v3.toc_render_sidechannel.TocRenderer",
            side_effect=RuntimeError("renderer broke"),
        ):
            payload = build_block_toc_payload(_toc_block([_entry()]), size=10.0)
        self.assertEqual(payload["commands"], [])


class TestBuildPageTocPayload(unittest.TestCase):
    def test_non_toc_page_returns_empty_commands(self):
        payload = build_page_toc_payload(
            [{"text": "Just a normal paragraph", "x0": 72.0, "x1": 400.0, "size": 10.0}],
            page_width=612.0,
            translate=lambda s: s,
        )
        self.assertEqual(payload["commands"], [])

    def test_translate_only_receives_title_only(self):
        lines = [
            {"text": "1. Introduction ............ 3", "x0": 72.0, "x1": 540.0, "size": 10.0},
            {"text": "2. Method ................. 15", "x0": 72.0, "x1": 540.0, "size": 10.0},
        ]
        calls: list = []

        def tr(s):
            calls.append(s)
            return "译_" + s

        payload = build_page_toc_payload(
            lines, page_width=612.0, translate=tr, size=10.0
        )
        # 两条都是 TOC 行 → 有命令；翻译回调只收到 title 余量
        self.assertTrue(payload["commands"])
        self.assertTrue(calls)
        for c in calls:
            self.assertNotIn(".....", c)
            self.assertNotIn(" 3", c)
            self.assertNotIn(" 15", c)


if __name__ == "__main__":
    unittest.main()
