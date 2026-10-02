"""前端步骤条边界必须与后端 _STAGE_WEIGHTS 对齐。

背景：ProgressPanel 的步骤条曾把 layouting 收在 92%，而��端累计区间是
parsing 0-10 / analyzing 10-30 / planning 30-40 / translating 40-70 /
layouting 70-85 / rendering 85-95 / evaluating 95-100 —— 92 不对应任何阶段，
也与该文件自己注释里写的 85 矛盾。后果是排版还没结束步骤条就跳到「渲染」，
进度看起来比实际领先。

本测试直接读取前端常量（不复制数值）并与后端 ``_STAGE_BOUNDS`` 比对，
任何一侧改动而另一侧忘记同步都会失败。
"""

import json
import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PROGRESS_PANEL = REPO_ROOT / "frontend" / "src" / "pages" / "ProgressPanel.tsx"


def _read_storyboard_ends() -> dict:
    """从 ProgressPanel.tsx 里解析 STAGE_PCT_END，不在本测试里复写数值。"""
    source = PROGRESS_PANEL.read_text(encoding="utf-8")
    match = re.search(
        r"STAGE_PCT_END[^=]*=\s*Object\.freeze\(\{(.*?)\}\)",
        source,
        re.DOTALL,
    )
    assert match, "STAGE_PCT_END constant not found in ProgressPanel.tsx"
    body = match.group(1)
    # Object.freeze 里的键是裸标识符，不是合法 JSON，改用正则逐项取
    ends = {
        key: int(value) for key, value in re.findall(r"(\w+)\s*:\s*(\d+)\s*,", body)
    }
    assert ends, f"could not parse any stage bounds from: {body!r}"
    return ends


def test_step_bounds_match_backend_stage_weights():
    from pdf2zh.services.runtime_service import _STAGE_BOUNDS

    backend_cumulative = {stage: bounds[1] for stage, bounds in _STAGE_BOUNDS.items()}
    ends = _read_storyboard_ends()

    # 前端 5 步是后端 7 阶段的合并视图：
    #   parsing      <- parsing + analyzing + planning
    #   translating  <- translating
    #   layouting    <- layouting
    #   rendering    <- rendering + evaluating
    # pending 在后端没有对应阶段（纯排队余量），单独断言。
    merged = {
        "parsing": backend_cumulative["planning"],
        "translating": backend_cumulative["translating"],
        "layouting": backend_cumulative["layouting"],
        "rendering": backend_cumulative["evaluating"],
    }

    for key, expected in merged.items():
        assert ends[key] == expected, (
            f"frontend step {key!r} ends at {ends[key]} but backend "
            f"_STAGE_BOUNDS implies {expected} (cumulative={backend_cumulative})"
        )
    assert set(ends) == set(merged) | {"pending"}, ends
    assert 0 < ends["pending"] < ends["parsing"], (
        "pending is the pre-stage queue margin: it must be > 0 and below the "
        f"first real boundary, got {ends['pending']}"
    )


def test_layouting_ends_at_85():
    """排版阶段的结束点必须是后端的 85，而不是历史上那个无依据的 92。"""
    from pdf2zh.services.runtime_service import _STAGE_BOUNDS

    ends = _read_storyboard_ends()
    assert ends["layouting"] == _STAGE_BOUNDS["layouting"][1] == 85


def test_step_bounds_are_strictly_increasing():
    """边界必须单调递增，否则 stepIndexForPercent 会跳过步骤。"""
    ends = _read_storyboard_ends()
    values = [
        ends[key]
        for key in ("pending", "parsing", "translating", "layouting", "rendering")
    ]
    assert values == sorted(values), f"not monotonic: {values}"
    assert values[-1] == 100, values


def test_every_step_key_has_a_stage_label():
    """每个步骤都必须能查到 stage.* 标签，否则会渲染出原始 key。"""
    source = PROGRESS_PANEL.read_text(encoding="utf-8")
    labels = re.findall(r'label:\s*"(stage\.[a-z_]+)"', source)
    assert labels, "no stage.* labels found"
    for loc in ("zh-CN", "en"):
        table = json.loads(
            (
                REPO_ROOT
                / "pdf2zh"
                / "gui"
                / "assets"
                / "generated"
                / "locales"
                / f"{loc}.json"
            ).read_text(encoding="utf-8")
        )["stage"]
        for label in labels:
            key = label.split(".", 1)[1]
            assert key in table, f"{loc}: missing stage key {label}"
