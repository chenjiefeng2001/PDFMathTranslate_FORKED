"""Commit 7E-3a — TOC layout contract tests.

Covers ``pdf2zh.semantic.layout.toc_layout``:

- geometry passthrough (title_x / page_x / indent / bbox verbatim from the
  entry, never recomputed from level / index);
- numbering prefix PRESERVE (never translated / renumbered);
- page number PRESERVE at original page_x (title growth never moves it);
- dot leader regenerates to page_x from the translated title's actual width;
- no-leader entries never get dots forced;
- long translated title → leader shrinks, page_x unchanged, overflow flagged;
- CJK titles measured by the unified measurer (no char-count heuristic);
- multi-line entries: continuation pinned to title_x (+size), stepping down;
- nested title_x strictly increasing, = node.title_x (not level * const);
- translator decoupling: the adapter takes pre-translated text, never a
  translator; commands are JSON-safe; failure degrades, never raises.
"""

import json

from pdf2zh.semantic.layout.overflow import OverflowPolicy
from pdf2zh.semantic.layout.toc_layout import (
    layout_toc_entry,
    toc_layout_commands,
)

_SIZE = 10.0  # latin 5pt, CJK 10pt, dot 3pt


def _measure(text, size=_SIZE):
    w = 0.0
    for ch in text or "":
        if ch == ".":
            w += size * 0.3
        elif ord(ch) >= 0x2E80:
            w += size * 1.0
        else:
            w += size * 0.5
    return w


def _entry(
    number="",
    title_only="Introduction",
    level=0,
    page_number="1",
    title_x=72.0,
    page_x=540.0,
    indent=72.0,
    dot_leader="...................",
    leader_present=True,
    continuation=None,
    bbox=None,
):
    return {
        "title": (f"{number} {title_only}").strip(),
        "number": number,
        "title_only": title_only,
        "level": level,
        "page_number": page_number,
        "title_x": title_x,
        "page_x": page_x,
        "indent": indent,
        "dot_leader": dot_leader,
        "leader_present": leader_present,
        "continuation": list(continuation or []),
        "bbox": list(bbox or (title_x, 0.0, page_x, 16.0)),
    }


def _layout(entry, translated=None, **kw):
    return layout_toc_entry(
        entry, measure=_measure, size=10.0,
        translated_title=translated,
        **kw,
    )


# ── 1. geometry passthrough ──────────────────────────────────────────────

def test_geometry_passthrough_verbatim():
    e = _entry(number="2.1", title_only="Dataset", title_x=96.0, page_x=500.0, level=2,
               bbox=(96.0, 0.0, 500.0, 16.0))
    r = _layout(e, translated="译_Dataset")
    assert r.title_x == 96.0
    assert r.page_x == 500.0
    assert r.level == 2
    assert r.bbox == (96.0, 0.0, 500.0, 16.0)


# ── 2. numbering PRESERVE ────────────────────────────────────────────────

def test_number_verbatim_and_position():
    r = _layout(_entry(number="3.1.2", title_only="Background", title_x=90.0),
                translated="译_Background")
    assert r.number is not None
    assert r.number.lines == ["3.1.2"]
    assert r.number.bbox[0] == 90.0
    assert r.number.policy is OverflowPolicy.SHRINK  # FixedAnchor: single line


def test_title_after_number_plus_gap():
    r = _layout(_entry(number="2.1", title_only="Dataset", title_x=96.0),
                translated="译_Dataset")
    # title 起点 = title_x + measure(number) + leader_gap
    expected = 96.0 + _measure("2.1") + 4.0
    assert r.title.bbox[0] == round(expected, 2)


# ── 3. page number PRESERVE ──────────────────────────────────────────────

def test_page_number_verbatim_at_page_x():
    r = _layout(_entry(title_only="Intro", page_number="42", page_x=520.0),
                translated="译_Intro")
    assert r.page is not None
    assert r.page.lines == ["42"]
    assert r.page.bbox[0] == 520.0
    assert r.page.policy is OverflowPolicy.PRESERVE  # FixedColumn: never moved


def test_page_x_unchanged_when_title_grows():
    short = _layout(_entry(title_only="Intro", page_x=500.0), translated="译_Intro")
    long = _layout(
        _entry(title_only="A", page_x=500.0),
        translated="一个非常非常长的中文标题说明这是一个很长的条目" * 2,
    )
    assert short.page.bbox[0] == long.page.bbox[0] == 500.0


# ── 4. dot leader ────────────────────────────────────────────────────────

def test_leader_fills_to_page_x():
    r = _layout(_entry(title_x=72.0, page_x=540.0), translated="Introduction")
    assert r.leader is not None
    # leader 从 title 右缘开始，终点不超过 page_x
    assert r.leader.bbox[0] == round(r.title_end, 2)
    assert r.leader.bbox[0] + r.leader.line_widths[0] <= 540.0
    assert r.title_end < 540.0


def test_long_title_shrinks_leader():
    short = _layout(_entry(page_x=500.0), translated="Intro")
    long = _layout(_entry(page_x=500.0), translated="A much longer translated title here")
    s_len = len(short.leader.lines[0]) if short.leader else 0
    l_len = len(long.leader.lines[0]) if long.leader else 0
    assert l_len < s_len


def test_no_leader_never_forces_dots():
    r = _layout(_entry(leader_present=False, dot_leader="", page_number="5"),
                translated="Intro")
    assert r.leader is None


def test_overlong_title_flags_overflow_no_leader():
    # 7F-5b：长标题先 WRAP（≤ 1+max_extra_lines 行不算溢出）；只有真正
    # 无法容纳的标题（SHRINK 到底仍超出行预算）才显式 overflow。
    r = _layout(
        _entry(title_x=72.0, page_x=200.0, page_number="5"),
        translated=("This translated title is far too long for the available gap " * 4).strip(),
    )
    assert r.overflow is True
    assert r.leader is None
    # page number 仍留在 page_x（overflow 显式、不静默、不移动页码）
    assert r.page.bbox[0] == 200.0


def test_long_title_wraps_within_extra_line_budget():
    # 7F-5b：长标题在 extra-line 预算内 → WRAP 成多行，不是 overflow。
    r = _layout(
        _entry(title_x=72.0, page_x=500.0, page_number="5"),
        translated=("This translated title wraps into several lines but stays inside the budget " * 2).strip(),
    )
    assert r.overflow is False
    assert r.line_count >= 2
    assert r.recovery is not None
    assert "WRAP" in r.recovery["steps"]


# ── 5. CJK ───────────────────────────────────────────────────────────────

def test_cjk_title_measured_not_char_count():
    r = _layout(_entry(title_only="引言", page_x=500.0, leader_present=False),
                translated="第一章 引言")
    assert r.title.bbox[0] == 72.0
    # 宽度来自统一 measure（CJK 1em）—— 不是 len * constant
    assert r.title.line_widths[0] == _measure("第一章 引言")


# ── 6. multi-line continuation ───────────────────────────────────────────

def test_continuation_pinned_under_title_stepping_down():
    r = _layout(
        _entry(title_only="Title", page_x=500.0, continuation=["cont one", "cont two"]),
        translated="译_Title",
        translated_continuation=["译_cont one", "译_cont two"],
    )
    assert len(r.continuation) == 2
    # 延续行锚定 title_x + size（不落回页边）
    for c in r.continuation:
        assert c.bbox[0] == 72.0 + 10.0
    # v3 y-up：延续行向下 → y 递减
    assert r.continuation[0].bbox[1] == round(0.0 - 14.0, 2)
    assert r.continuation[1].bbox[1] == round(0.0 - 28.0, 2)


# ── 7. nested title_x ────────────────────────────────────────────────────

def test_nested_title_x_increasing_and_from_node():
    levels = [
        _entry(number="1", title_only="Intro", level=0, title_x=72.0),
        _entry(number="1.1", title_only="Background", level=1, title_x=108.0),
        _entry(number="1.1.1", title_only="Dataset", level=2, title_x=138.0),
    ]
    rs = [_layout(e, translated="T") for e in levels]
    xs = [r.title.bbox[0] for r in rs]
    assert xs[2] > xs[1] > xs[0]
    # title_x 就是节点值（不是 level * constant）：
    # 每个 title 起点 = 该层 title_x + measure(number) + leader_gap
    assert xs == [tx + _measure(n) + 4.0 for tx, n in zip((72.0, 108.0, 138.0), ("1", "1.1", "1.1.1"))]


# ── 8. commands + JSON-safety + degrade ──────────────────────────────────

def test_commands_json_safe_and_ordered():
    r = _layout(_entry(number="2.1", title_only="Dataset", page_number="12"),
                translated="译_Dataset")
    cmds = toc_layout_commands(r)
    json.dumps(cmds)
    assert [c["kind"] for c in cmds] == ["number", "title", "leader", "page"]
    assert cmds[0]["text"] == "2.1"
    assert cmds[1]["text"] == "译_Dataset"
    assert cmds[-1]["text"] == "12"
    assert cmds[-1]["x"] == 540.0


def test_measure_failure_degrades_never_raises():
    def bad(s, size):
        raise RuntimeError("boom")

    r = layout_toc_entry(
        _entry(title_only="Intro", page_number="1"), measure=bad, size=10.0,
        translated_title="译_Intro",
    )
    assert r.title is not None
    assert r.page is not None
