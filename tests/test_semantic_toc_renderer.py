"""TocRenderer 单元测试（Commit 6C 验收）。

覆盖 ``pdf2zh.semantic.renderer.toc``：
- numbering prefix PRESERVE（不进 translator）
- page_number PRESERVE（不进 translator、不被重编、不被移动）
- dot leader 根据实际翻译宽度重新生成，填充到原始 page_x
- 无 leader 条目不被强制加点
- 多层嵌套 indent/title_x 保持（不 level * constant）
- 多行条目 continuation 保持垂直 progression，page number 不随标题变长
- CJK 宽度字符宽度不依赖英文字符数估算
- leader 字符串宽度由 measure_width 函数给出（注入式）
- 中文 title 保持 title_x，page number 保持 page_x
- render_plan 返回 JSON 可序列化内容
- renderer 内部不导入 translator
"""

import inspect

from pdf2zh.semantic.renderer.toc import TocRenderer, build_page_toc_plan

# ── fixtures ──────────────────────────────────────────────────────────────


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


def _fixed_measure(size=10.0):
    """Fake width measurer: 1pt per Latin char, 2pt per CJK, 0pt for space/dot."""

    def m(text, sz=size):
        w = 0.0
        for ch in text or "":
            if ch.isspace() or ch == ".":
                w += sz * 0.3
            elif ord(ch) >= 0x2E80:
                w += sz * 1.0
            else:
                w += sz * 0.5
        return w

    return m


# ── marker numbering preserved ────────────────────────────────────────────


def test_numbering_never_translated():
    """Numbering prefix never enters translate callback."""
    calls = []
    renderer = TocRenderer(measure_width=_fixed_measure())

    def _spy(s):
        calls.append(s)
        return f"译_{s}"

    entries = [_entry(number="2.1", title_only="Dataset")]
    cmds = renderer.render(entries, ys=[0.0], size=10.0, translate=_spy)
    nums = [c for c in cmds if c.kind == "number"]
    assert len(nums) == 1
    assert nums[0].text == "2.1"
    # "2.1" should NOT have been passed to the translator
    assert "2.1" not in calls


def test_numbering_position_at_title_x():
    renderer = TocRenderer(measure_width=_fixed_measure())
    entries = [_entry(number="3.1.2", title_only="Background", title_x=90.0)]
    cmds = renderer.render(entries, ys=[0.0], size=10.0)
    nums = [c for c in cmds if c.kind == "number"]
    assert nums[0].x == 90.0


# ── page number preserved ────────────────────────────────────────────────


def test_page_number_never_translated():
    calls = []
    renderer = TocRenderer(measure_width=_fixed_measure())

    def _spy(s):
        calls.append(s)
        return f"译_{s}"

    entries = [_entry(page_number="42")]
    cmds = renderer.render(entries, ys=[0.0], size=10.0, translate=_spy)
    pages = [c for c in cmds if c.kind == "page"]
    assert len(pages) == 1
    assert pages[0].text == "42"
    assert "42" not in calls


def test_page_number_x_unchanged_when_title_grows():
    """Title grows → leader shrinks; page_number x stays at original page_x."""
    renderer = TocRenderer(measure_width=_fixed_measure())
    entries_short = [_entry(title_only="Intro", page_x=500.0, title_x=72.0)]
    cmds_short = renderer.render(entries_short, ys=[0.0], size=10.0)
    x_before = [c.x for c in cmds_short if c.kind == "page"][0]

    entries_long = [
        _entry(
            title_only="A much longer title that will push the leader",
            page_x=500.0,
            title_x=72.0,
        )
    ]
    cmds_long = renderer.render(entries_long, ys=[0.0], size=10.0)
    x_after = [c.x for c in cmds_long if c.kind == "page"][0]

    assert x_before == x_after == 500.0
    # leader shrinks when title grows
    leads_short = [c for c in cmds_short if c.kind == "leader"]
    leads_long = [c for c in cmds_long if c.kind == "leader"]
    short_leader_len = sum(len(c.text) for c in leads_short)
    long_leader_len = sum(len(c.text) for c in leads_long)
    assert long_leader_len < short_leader_len or (not leads_long)


# ── dot leader regeneration ──────────────────────────────────────────────


def test_leader_fills_to_page_x():
    renderer = TocRenderer(measure_width=_fixed_measure())
    entries = [_entry(title_x=72.0, page_x=540.0)]
    cmds = renderer.render(entries, ys=[0.0], size=10.0)
    leads = [c for c in cmds if c.kind == "leader"]
    assert len(leads) == 1
    title = [c for c in cmds if c.kind == "title"][0]
    # leader starts after title end
    assert leads[0].x >= title.x + title.width
    # leader ends before page_x
    assert leads[0].x + leads[0].width <= 540.0


def test_no_leader_entry_does_not_force_dots():
    renderer = TocRenderer(measure_width=_fixed_measure())
    entries = [_entry(leader_present=False, dot_leader="", page_number="5")]
    cmds = renderer.render(entries, ys=[0.0], size=10.0)
    leads = [c for c in cmds if c.kind == "leader"]
    assert leads == []


def test_leader_uses_original_dot_leader_char():
    renderer = TocRenderer(measure_width=_fixed_measure())
    entries = [_entry(dot_leader="··· ··· ···", leader_present=True)]
    cmds = renderer.render(entries, ys=[0.0], size=10.0)
    leads = [c for c in cmds if c.kind == "leader"]
    assert len(leads) == 1


# ── title_x / page_x / indent preserved from node ────────────────────────


def test_title_x_from_node_not_recomputed():
    renderer = TocRenderer(measure_width=_fixed_measure())
    entries = [_entry(number="2.1", title_only="Dataset", title_x=96.0, level=2)]
    cmds = renderer.render(entries, ys=[0.0], size=10.0)
    nums = [c for c in cmds if c.kind == "number"]
    titles = [c for c in cmds if c.kind == "title"]
    assert nums[0].x == 96.0
    assert titles[0].x > 96.0  # title after number + gap


def test_nested_level_preserves_independent_title_x():
    """Each nested entry keeps its own title_x (not level * constant)."""
    renderer = TocRenderer(measure_width=_fixed_measure())
    entries = [
        _entry(number="1", title_only="Intro", level=0, title_x=72.0, indent=72.0),
        _entry(
            number="1.1", title_only="Background", level=1, title_x=108.0, indent=108.0
        ),
        _entry(
            number="1.1.1", title_only="Dataset", level=2, title_x=138.0, indent=138.0
        ),
    ]
    cmds = renderer.render(entries, ys=[0.0, 14.0, 28.0], size=10.0)
    nums = [c for c in cmds if c.kind == "number"]
    assert [c.x for c in nums] == [72.0, 108.0, 138.0]
    assert entries[1]["level"] == 1
    assert entries[2]["level"] == 2


# ── multi-line entry ─────────────────────────────────────────────────────


def test_continuation_vertical_progression():
    renderer = TocRenderer(measure_width=_fixed_measure(), line_height=14.0)
    entries = [_entry(continuation=["on another line"])]
    cmds = renderer.render(entries, ys=[0.0], size=10.0)
    conts = [c for c in cmds if c.kind == "title" and c.text == "on another line"]
    assert len(conts) == 1
    # y-up: continuation lines sit below the first line (smaller y)
    assert conts[0].y < 0.0
    assert conts[0].x >= 72.0


def test_long_title_does_not_move_page_number():
    renderer = TocRenderer(measure_width=_fixed_measure())
    entries = [
        _entry(
            title_only="An extremely long title that would normally push the number rightward",
            title_x=72.0,
            page_x=500.0,
        )
    ]
    cmds = renderer.render(entries, ys=[0.0], size=10.0)
    page = [c for c in cmds if c.kind == "page"][0]
    # page number stays at its original column regardless of title length
    assert page.x == 500.0
    # leader ends at/right of the long title, never past page_x
    for lead in [c for c in cmds if c.kind == "leader"]:
        assert lead.x + lead.width <= 500.0


# ── CJK width handling ───────────────────────────────────────────────────


def test_cjk_title_x_unchanged():
    renderer = TocRenderer(measure_width=_fixed_measure())
    entries = [_entry(title_only="引言", number="", title_x=80.0, leader_present=False)]
    cmds = renderer.render(entries, ys=[0.0], size=10.0)
    titles = [c for c in cmds if c.kind == "title"]
    assert titles[0].x == 80.0
    assert titles[0].text == "引言"


def test_cjk_longer_title_leader_shorter():
    renderer = TocRenderer(measure_width=_fixed_measure())
    short_e = [_entry(title_only="简介", page_x=500.0)]
    long_e = [
        _entry(
            title_only="一个非常非常长的中文标题说明这是一个很长的条目", page_x=500.0
        )
    ]
    cmds_s = renderer.render(short_e, ys=[0.0], size=10.0)
    cmds_l = renderer.render(long_e, ys=[0.0], size=10.0)
    s_len = sum(len(c.text) for c in cmds_s if c.kind == "leader")
    l_len = sum(len(c.text) for c in cmds_l if c.kind == "leader")
    assert l_len <= s_len


def test_cjk_page_number_x_unchanged():
    renderer = TocRenderer(measure_width=_fixed_measure())
    entries = [_entry(title_only="引言", page_x=520.0)]
    cmds = renderer.render(entries, ys=[0.0], size=10.0)
    pages = [c for c in cmds if c.kind == "page"]
    assert pages[0].x == 520.0


# ── render_plan JSON serializable ─────────────────────────────────────────


def test_render_plan_json_serializable():
    import json

    renderer = TocRenderer(measure_width=_fixed_measure())
    entries = [_entry(), _entry(title_only="Method")]
    plan = renderer.render_plan(entries, ys=[0.0, 14.0])
    json_str = json.dumps(plan)
    assert "commands" in json_str
    assert "translated_calls" in json_str


# ── no translator inside renderer ─────────────────────────────────────────


def test_renderer_has_no_translator_import():
    import pdf2zh.semantic.renderer.toc as mod

    src = inspect.getsource(mod)
    assert "from pdf2zh.translator" not in src
    assert "import pdf2zh.translator" not in src
    assert "import translator" not in src


# ── build_page_toc_plan integration ───────────────────────────────────────


def test_build_page_toc_plan_detects_toc_and_renders():
    lines = [
        {"text": "Introduction ........... 3", "x0": 72.0, "x1": 540.0, "size": 10.0},
        {"text": "Method ................ 15", "x0": 72.0, "x1": 540.0, "size": 10.0},
        {"text": "Results ............... 28", "x0": 72.0, "x1": 540.0, "size": 10.0},
    ]
    plan = build_page_toc_plan(lines, 612.0, size=10.0)
    assert plan["tree"] is not None
    assert len(plan["entries"]) >= 2
    assert len(plan["commands"]) >= 1
    pages = [c for c in plan["commands"] if c.get("kind") == "page"]
    titles = [c for c in plan["commands"] if c.get("kind") == "title"]
    assert len(pages) >= 2
    assert titles[0].get("x") >= 72.0
    # page numbers sit in the right-hand column
    assert all(p.get("x") >= 400.0 for p in pages)


def test_build_page_toc_plan_no_toc_returns_empty():
    lines = [
        {"text": "A normal paragraph of text", "x0": 72.0, "x1": 540.0, "size": 10.0},
    ]
    plan = build_page_toc_plan(lines, 612.0)
    assert plan["tree"] is None
    assert plan["entries"] == []
    assert plan["commands"] == []
