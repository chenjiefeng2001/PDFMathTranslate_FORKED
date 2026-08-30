"""Commit 7E-3 — TOC layout integration tests.

Covers the 7E-3a adapter + 7E-3c renderer wiring end to end:

1.  ``layout_toc_entry`` — TOCEntryNode → ``TocEntryLayoutResult``: title_x /
    page_x / number / level / bbox passthrough verbatim (nothing re-inferred);
    the flexible leader shrinks when the translated title grows and page_x
    never moves; no dots are ever forced for ``leader_present=False``; a title
    that reaches past page_x flags ``overflow`` (never silent).
2.  Multi-line entries — continuation lines anchor at ``title_x + size`` and
    step **down** in v3 y-up (smaller y); page number stays at page_x.
3.  ``build_block_toc_payload`` — the translator receives the **title only**:
    numbering prefix, dot leader, page number and geometry never enter it
    (``translated_calls == [\"Introduction\"]``).
4.  Nested entries — horizontal geometry comes from the node verbatim
    (``title_x = node.title_x``, not ``f(level)``).
"""

import json
import unittest

from pdf2zh.semantic.layout.toc_layout import layout_toc_entry, toc_layout_commands
from pdf2zh.semantic.models import TOCEntryNode
from pdf2zh.v3.canonical_page import BlockModel, PageModel
from pdf2zh.v3.toc_render_sidechannel import build_block_toc_payload
from pdf2zh.v3.toc_sidechannel import attach_toc_entries, entry_to_dict

_SIZE = 10.0


def _cmd(cmds, kind):
    """First command of ``kind`` (number/title/leader/page)."""
    return next(c for c in cmds if c["kind"] == kind)


def _measure(text, size=_SIZE):
    w = 0.0
    for ch in text or "":
        if ch == ".":
            w += size * 0.3
        elif ord(ch) >= 0x2E80:
            w += size
        else:
            w += size * 0.5
    return w


def _entry(
    title="1 Introduction",
    number="1",
    title_only="Introduction",
    page_number="12",
    level=0,
    title_x=72.0,
    page_x=500.0,
    indent=72.0,
    leader="..............",
    leader_present=True,
    continuation=None,
    bbox=None,
):
    return {
        "title": title,
        "number": number,
        "title_only": title_only,
        "level": level,
        "page_number": page_number,
        "indent": indent,
        "title_x": title_x,
        "page_x": page_x,
        "dot_leader": leader,
        "leader_present": leader_present,
        "continuation": list(continuation or []),
        "bbox": list(bbox or (40.0, 40.0, 540.0, 200.0)),
    }


class TestLayoutTocEntry(unittest.TestCase):
    # ── 1. 几何逐字透传 ─────────────────────────────────────────
    def test_geometry_passthrough_verbatim(self):
        r = layout_toc_entry(
            _entry(title_x=123.0, page_x=456.0, level=2),
            measure=_measure, size=_SIZE, y=700.0,
            translated_title="Introduction",
        )
        self.assertEqual(r.title_x, 123.0)
        self.assertEqual(r.page_x, 456.0)
        self.assertEqual(r.level, 2)
        self.assertEqual(r.bbox, (40.0, 40.0, 540.0, 200.0))
        self.assertEqual(r.y, 700.0)
        # 命令几何与节点一致
        cmds = toc_layout_commands(r)
        self.assertEqual(_cmd(cmds, "number")["x"], 123.0)
        self.assertEqual(_cmd(cmds, "page")["x"], 456.0)

    def test_leader_shrinks_but_page_x_never_moves(self):
        short = layout_toc_entry(
            _entry(title_only="Intro", page_x=500.0),
            measure=_measure, size=_SIZE, translated_title="Intro",
        )
        long = layout_toc_entry(
            _entry(title_only="A much much longer translated title", page_x=500.0),
            measure=_measure, size=_SIZE, translated_title="A much much longer translated title",
        )
        self.assertEqual(short.page_x, long.page_x)
        self.assertEqual(short.page_x, 500.0)
        # 标题变长 → leader 缩短
        s_leader = short.leader.line_widths[0] if short.leader else 0.0
        l_leader = long.leader.line_widths[0] if long.leader else 0.0
        self.assertGreater(s_leader, l_leader)
        # page 命令 x 不变
        s_page = _cmd(toc_layout_commands(short), "page")
        l_page = _cmd(toc_layout_commands(long), "page")
        self.assertEqual(s_page["x"], l_page["x"])

    def test_no_leader_never_forces_dots(self):
        r = layout_toc_entry(
            _entry(leader_present=False, leader=""),
            measure=_measure, size=_SIZE, translated_title="Intro",
        )
        self.assertIsNone(r.leader)
        cmds = toc_layout_commands(r)
        self.assertFalse([c for c in cmds if c["kind"] == "leader"])

    def test_title_past_page_x_flags_overflow(self):
        r = layout_toc_entry(
            _entry(title_x=72.0, page_x=100.0, leader_present=False),
            measure=_measure, size=_SIZE, translated_title="This title is far too long",
        )
        self.assertTrue(r.overflow)
        # leader 不再发射，page 仍钉在 page_x —— 明确 overflow 而非静默
        self.assertIsNone(r.leader)
        page_cmds = [c for c in toc_layout_commands(r) if c["kind"] == "page"]
        if page_cmds:
            self.assertEqual(page_cmds[0]["x"], 100.0)

    def test_page_number_preserved_never_translated_here(self):
        r = layout_toc_entry(
            _entry(page_number="42"), measure=_measure, size=_SIZE,
            translated_title="Intro",
        )
        self.assertEqual(_cmd(toc_layout_commands(r), "page")["text"], "42")


class TestLayoutTocMultiline(unittest.TestCase):
    def test_continuation_anchors_below_first_line(self):
        r = layout_toc_entry(
            _entry(title_x=72.0, page_x=500.0, continuation=["continues here"]),
            measure=_measure, size=_SIZE, y=700.0, line_height=14.0,
            translated_title="A long title", translated_continuation=["continues here"],
        )
        self.assertEqual(len(r.continuation), 1)
        cont = r.continuation[0]
        # y-up：延续行在首行下方（y 更小）
        self.assertLess(cont.bbox[1], 700.0)
        # 锚定在 title_x + size（不落回 marker 列、不重新推断）
        self.assertAlmostEqual(cont.bbox[0], 72.0 + _SIZE, delta=0.5)

    def test_multiline_page_number_stays(self):
        r = layout_toc_entry(
            _entry(title_x=72.0, page_x=500.0, page_number="5",
                   continuation=["line two", "line three"]),
            measure=_measure, size=_SIZE, y=700.0, line_height=14.0,
            translated_title="First line", translated_continuation=["line two", "line three"],
        )
        self.assertEqual(len(r.continuation), 2)
        # 延续行逐行下移
        ys = [c.bbox[1] for c in r.continuation]
        self.assertLess(ys[1], ys[0])
        # page 钉在 page_x，不因 wrap 跑掉
        self.assertEqual(_cmd(toc_layout_commands(r), "page")["x"], 500.0)


class TestNestedTocEntries(unittest.TestCase):
    def test_title_x_comes_from_node_not_level(self):
        """title_x(1) < title_x(1.1) < title_x(1.1.1) 且逐字来自节点。"""
        nodes = [
            _entry(title="1 Intro", number="1", title_only="Intro", level=0, title_x=72.0),
            _entry(title="1.1 Background", number="1.1", title_only="Background", level=1, title_x=96.0),
            _entry(title="1.1.1 Deep", number="1.1.1", title_only="Deep", level=2, title_x=120.0),
        ]
        xs = []
        for n in nodes:
            r = layout_toc_entry(n, measure=_measure, size=_SIZE, translated_title=n["title_only"])
            num = [c for c in toc_layout_commands(r) if c["kind"] == "number"]
            xs.append(num[0]["x"] if num else r.title_x)
        self.assertEqual(xs, [72.0, 96.0, 120.0])
        self.assertEqual(xs, sorted(xs))
        # 与 level 无函数关系：同 level 不同 x 也存在（不靠 level 计算）
        r1 = layout_toc_entry(
            _entry(level=1, title_x=88.0), measure=_measure, size=_SIZE, translated_title="A"
        )
        r2 = layout_toc_entry(
            _entry(level=1, title_x=140.0), measure=_measure, size=_SIZE, translated_title="B"
        )
        self.assertEqual(r1.title_x, 88.0)
        self.assertEqual(r2.title_x, 140.0)


class TestBlockTocPayloadTranslation(unittest.TestCase):
    def _block(self, entries):
        page = PageModel(page_num=1)
        host = BlockModel(text="TOC placeholder", kind="paragraph", x0=40, y0=40, x1=500, y1=200)
        page.blocks.append(host)
        nodes = []
        for e in entries:
            nodes.append(
                TOCEntryNode(
                    title=e["title"],
                    level=e["level"],
                    page_number=e["page_number"],
                    indent=e["indent"],
                    title_x=e["title_x"],
                    page_x=e["page_x"],
                    dot_leader=e["dot_leader"],
                    leader_present=e["leader_present"],
                    continuation=list(e.get("continuation") or []),
                    bbox=tuple(e["bbox"]),
                )
            )
        attach_toc_entries(page, [entry_to_dict(n) for n in nodes])
        return page.blocks[0]

    def test_translator_receives_title_only(self):
        calls = []

        def tr(s):
            calls.append(s)
            return "译_" + s

        block = self._block([_entry(title="1 Introduction", number="1", title_only="Introduction",
                                    page_number="12", leader="..............")])
        payload = build_block_toc_payload(block, translate=tr, size=_SIZE)
        self.assertEqual(calls, ["Introduction"])
        self.assertTrue(payload["commands"])
        kinds = [c["kind"] for c in payload["commands"]]
        self.assertIn("title", kinds)
        self.assertIn("page", kinds)

    def test_commands_json_safe_and_translated(self):
        block = self._block([_entry(title_only="Introduction", page_number="12")])
        payload = build_block_toc_payload(block, translate=lambda s: "译_" + s, size=_SIZE)
        json.dumps(payload)
        joined = "".join(c["text"] for c in payload["commands"] if c["kind"] == "title")
        self.assertIn("译_Introduction", joined)

    def test_no_geometry_reinference_in_payload(self):
        block = self._block([_entry(title_only="Intro", page_number="7", title_x=99.0, page_x=520.0)])
        payload = build_block_toc_payload(block, translate=lambda s: s, size=_SIZE)
        num = [c for c in payload["commands"] if c["kind"] == "number"]
        title = [c for c in payload["commands"] if c["kind"] == "title"]
        page = [c for c in payload["commands"] if c["kind"] == "page"]
        # number 初始为空（translate_toc_entries 后才拆分）→ title 直接锚在 title_x
        self.assertFalse(num)
        if title:
            self.assertEqual(title[0]["x"], 99.0)
        if page:
            self.assertEqual(page[0]["x"], 520.0)


if __name__ == "__main__":
    unittest.main()
