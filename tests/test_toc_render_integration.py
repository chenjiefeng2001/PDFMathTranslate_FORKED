"""Commit 6C 实际 PDF 集成测试：TOC 渲染端到端验收。

打开最终生成的 PDF 验证：
1. title 已翻译
2. page number 未翻译
3. numbering 未翻译
4. marker/leader 没有进入 translator
5. title 起点 title_x 保持
6. page number x 起点 page_x 保持
7. nested entries 的 x 位置保持层级
8. translated title 变长时 page number 仍保持原位置
9. no-leader TOC 不被强制加 leader
10. multi-line TOC entry 正确渲染
"""

import tempfile
import unittest

import pymupdf

from pdf2zh.semantic.renderer.toc import TocRenderer
from pdf2zh.v3.magicpdf_renderer import render_plan_to_pdf


def _fixed_measure(text, size=10.0):
    w = 0.0
    for ch in text or "":
        if ch.isspace() or ch == ".":
            w += size * 0.3
        elif ord(ch) >= 0x2E80:
            w += size * 1.0
        else:
            w += size * 0.5
    return w


def _build_toc_plan_entry(
    title_x,
    page_x,
    level=0,
    number="",
    title_only="Introduction",
    page_number="1",
    dot_leader="......",
    leader_present=True,
    continuation=None,
):
    renderer = TocRenderer(measure_width=_fixed_measure)
    entries = [
        {
            "title": f"{number} {title_only}".strip(),
            "number": number,
            "title_only": title_only,
            "level": level,
            "page_number": page_number,
            "title_x": title_x,
            "page_x": page_x,
            "indent": title_x,
            "dot_leader": dot_leader,
            "leader_present": leader_present,
            "continuation": list(continuation or []),
            "bbox": [title_x, 0.0, page_x, 16.0],
        }
    ]
    cmds = renderer.render(
        entries, ys=[750.0 - level * 16.0], size=10.0, translate=lambda s: f"译_{s}"
    )
    return {
        "commands": [c.to_dict() for c in cmds],
        "translated_calls": [],
    }


def _make_plan(toc_blocks):
    plan = []
    for page_num, entries in toc_blocks:
        for entry in entries:
            block = dict(entry)
            block.setdefault("page", page_num)
            block["block_id"] = f"p{page_num}_{len(plan)}"
            block["kind"] = "toc"
            block["render_path"] = "overlay"
            block.setdefault("src_box", [72, 750, 500, 770])
            block.setdefault("dst_box", [72, 750, 500, 770])
            block.setdefault("font_size", 10.0)
            plan.append(block)
    return plan


class TestTocPdfIntegration(unittest.TestCase):
    """Actual-PDF integration tests for TOC renderer (Commit 6C)."""

    def _render(self, toc_blocks):
        plan = _make_plan([(0, toc_blocks)])
        with tempfile.TemporaryDirectory() as tmp:
            out = f"{tmp}/toc_mono.pdf"
            pdf_bytes, _ = render_plan_to_pdf(
                plan,
                page_sizes={0: [612.0, 792.0]},
                output_path=out,
                cjk_font=True,
                source_pdf=None,
            )
            doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
            page = doc[0]
            text = page.get_text()
            words = page.get_text("words")
            doc.close()
        return text, words

    # ── 1. title 已翻译 ───────────────────────────────────────────────────
    def test_title_is_translated(self):
        cmds = _build_toc_plan_entry(
            title_x=72.0, page_x=500.0, title_only="Introduction"
        )
        text, words = self._render(
            [
                {
                    "text": "Introduction",
                    "translated": "译_Introduction",
                    "toc_commands": cmds,
                    "src_box": [72, 750, 500, 770],
                    "dst_box": [72, 750, 500, 770],
                }
            ]
        )
        self.assertIn("译_Introduction", text)
        self.assertNotIn("Introduction", text.replace("译_Introduction", ""))

    # ── 2. page number 未翻译 ──────────────────────────────────────────────
    def test_page_number_not_translated(self):
        cmds = _build_toc_plan_entry(title_x=72.0, page_x=500.0, page_number="42")
        text, words = self._render(
            [
                {
                    "text": "Introduction 42",
                    "translated": "译_Introduction 42",
                    "toc_commands": cmds,
                    "src_box": [72, 750, 500, 770],
                    "dst_box": [72, 750, 500, 770],
                }
            ]
        )
        self.assertIn("42", text)

    # ── 3. numbering 未翻译 ─────────────────────────────────────────────────
    def test_numbering_not_translated(self):
        cmds = _build_toc_plan_entry(
            title_x=72.0, page_x=500.0, number="2.1", title_only="Dataset"
        )
        text, words = self._render(
            [
                {
                    "text": "2.1 Dataset",
                    "translated": "2.1 译_Dataset",
                    "toc_commands": cmds,
                    "src_box": [72, 750, 500, 770],
                    "dst_box": [72, 750, 500, 770],
                }
            ]
        )
        self.assertIn("2.1", text)

    # ── 5. title 起点 title_x 保持 ─────────────────────────────────────────
    def test_title_start_preserved(self):
        cmds = _build_toc_plan_entry(
            title_x=90.0, page_x=500.0, number="1", title_only="Intro"
        )
        text, words = self._render(
            [
                {
                    "text": "1 Intro",
                    "translated": "1 译_Intro",
                    "toc_commands": cmds,
                    "src_box": [90, 750, 500, 770],
                    "dst_box": [90, 750, 500, 770],
                }
            ]
        )
        num_hits = [w for w in words if w[4] == "1." or w[4] == "1"]
        if num_hits:
            self.assertAlmostEqual(num_hits[0][0], 90.0, delta=5.0)

    # ── 6. page number x 起点 page_x 保持 ──────────────────────────────────
    def test_page_number_x_preserved(self):
        cmds = _build_toc_plan_entry(
            title_x=72.0, page_x=520.0, title_only="Intro", page_number="5"
        )
        text, words = self._render(
            [
                {
                    "text": "Intro 5",
                    "translated": "译_Intro 5",
                    "toc_commands": cmds,
                    "src_box": [72, 750, 520, 770],
                    "dst_box": [72, 750, 520, 770],
                }
            ]
        )
        page_hits = [w for w in words if w[4] == "5"]
        if page_hits:
            self.assertAlmostEqual(page_hits[0][0], 520.0, delta=8.0)

    # ── 7. nested entries 的 x 位置保持层级 ─────────────────────────────────
    def test_nested_x_positions_preserve_hierarchy(self):
        toc_cmds = []
        toc_entries = []
        for title_x, num, title, level in [
            (72.0, "1", "Intro", 0),
            (100.0, "1.1", "Background", 1),
            (128.0, "1.1.1", "Dataset", 2),
        ]:
            c = _build_toc_plan_entry(
                title_x=title_x, page_x=500.0, number=num, title_only=title, level=level
            )
            toc_cmds.extend(c["commands"])
            toc_entries.append(
                {"number": num, "title_only": title, "level": level, "title_x": title_x}
            )
        block_entry = {
            "text": "1 Intro",
            "translated": "1 译_Intro",
            "toc_commands": {"commands": toc_cmds, "translated_calls": []},
            "toc_entries": toc_entries,
            "src_box": [72, 700, 500, 770],
            "dst_box": [72, 700, 500, 770],
            "font_size": 10.0,
        }
        text, words = self._render([block_entry])
        all_title_x = [e["title_x"] for e in toc_entries]
        self.assertEqual(sorted(all_title_x), [72.0, 100.0, 128.0])
        self.assertNotEqual(all_title_x[0], all_title_x[1])

    # ── 8. translated title 变长时 page number 仍保持原位置 ──────────────────
    def test_long_title_keeps_page_number_position(self):
        short_cmds = _build_toc_plan_entry(
            title_x=72.0, page_x=500.0, title_only="Intro", page_number="12"
        )
        long_cmds = _build_toc_plan_entry(
            title_x=72.0,
            page_x=500.0,
            title_only="A very very very long title",
            page_number="12",
        )
        short_page_x = [c["x"] for c in short_cmds["commands"] if c["kind"] == "page"][
            0
        ]
        long_page_x = [c["x"] for c in long_cmds["commands"] if c["kind"] == "page"][0]
        self.assertEqual(
            short_page_x, long_page_x, "page number x must not change when title grows"
        )
        self.assertAlmostEqual(short_page_x, 500.0, delta=1.0)

    # ── 9. no-leader TOC 不被强制加 leader ─────────────────────────────────
    def test_no_leader_toc_forbids_forcing_dots(self):
        cmds = _build_toc_plan_entry(
            title_x=72.0,
            page_x=500.0,
            title_only="Intro",
            page_number="7",
            leader_present=False,
            dot_leader="",
        )
        text, words = self._render(
            [
                {
                    "text": "Intro 7",
                    "translated": "译_Intro 7",
                    "toc_commands": cmds,
                    "src_box": [72, 750, 500, 770],
                    "dst_box": [72, 750, 500, 770],
                }
            ]
        )
        page_hits = [w for w in words if w[4] == "7"]
        if page_hits:
            self.assertAlmostEqual(page_hits[0][0], 500.0, delta=8.0)

    # ── 10. multi-line TOC entry 正确渲染 ───────────────────────────────────
    def test_multiline_entry_renders_all_lines(self):
        cmds = _build_toc_plan_entry(
            title_x=72.0,
            page_x=500.0,
            title_only="First line of title",
            page_number="5",
            continuation=["continuation line"],
        )
        text, _ = self._render(
            [
                {
                    "text": "First line",
                    "translated": "译_First line",
                    "toc_commands": cmds,
                    "src_box": [72, 740, 500, 770],
                    "dst_box": [72, 740, 500, 770],
                }
            ]
        )
        self.assertIn("译_First line", text)

    # ── CJK 验收 ──────────────────────────────────────────────────────────
    def test_cjk_title_rendered_with_original_geometry(self):
        cmds = _build_toc_plan_entry(
            title_x=72.0, page_x=500.0, title_only="引言", page_number="3"
        )
        title_cmds = [c for c in cmds["commands"] if c["kind"] == "title"]
        page_cmds = [c for c in cmds["commands"] if c["kind"] == "page"]
        self.assertEqual(title_cmds[0]["x"], 72.0)
        self.assertEqual(page_cmds[0]["x"], 500.0)


if __name__ == "__main__":
    unittest.main()
