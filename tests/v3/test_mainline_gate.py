"""V8.4 Mainline Relayout Gate — legacy write-back safety tests.

Run with:
    python -m pytest tests/v3/test_mainline_gate.py -v
"""

from __future__ import annotations
import json

import pytest

from pdf2zh.v3.mainline_gate import (
    GateBlock,
    GatedResult,
    MainlineRelayoutGate,
    _blocks_overlap_rate,
)


def _blocks():
    return [
        GateBlock(
            node_id="a", text="First block", x=72, y=100, width=400, height=20, page=1
        ),
        GateBlock(
            node_id="b", text="Second block", x=72, y=130, width=400, height=20, page=1
        ),
    ]


def test_gate_pass_without_translations():
    gate = MainlineRelayoutGate()
    result = gate.run(_blocks())
    assert isinstance(result, GatedResult)
    assert result.writeback_allowed
    assert result.overlap_rate == 0.0
    assert not result.relayout_needed
    assert result.passes == 0
    json.dumps(result.to_dict())


def test_gate_relayouts_growing_translation():
    gate = MainlineRelayoutGate()
    blocks = _blocks()
    result = gate.run(
        blocks,
        translations={
            "a": "这段译文比源文本长很多需要两行才能放下而且非常长还要再多一点直到超过单行宽度",
        },
    )
    assert result.relayout_needed
    assert result.writeback_allowed
    assert result.overlap_rate <= gate.threshold
    assert result.passes >= 1
    # b must now sit strictly below the grown block a
    a = next(b for b in result.blocks if b.node_id == "a")
    b = next(b for b in result.blocks if b.node_id == "b")
    assert b.y >= a.y + a.height - 1e-6


def test_gate_blocks_writeback_when_overflow_persists():
    blocks = [
        GateBlock(node_id="x", text="X", x=72, y=780, width=400, height=100, page=1)
    ]
    result = MainlineRelayoutGate().run(blocks)
    assert not result.writeback_allowed
    assert result.issues
    assert any("overflow" in i for i in result.issues)


def test_gate_max_passes_zero_never_relayouts():
    blocks = [
        GateBlock(node_id="a", text="A", x=72, y=100, width=400, height=20, page=1),
        GateBlock(node_id="b", text="B", x=72, y=120, width=400, height=20, page=1),
    ]
    gate = MainlineRelayoutGate(max_passes=0)
    result = gate.run(
        blocks,
        translations={
            "a": "这段译文比源文本长很多需要两行才能放下而且非常长还要再多一点直到超过单行宽度"
        },
    )
    assert result.passes == 0
    assert not result.writeback_allowed
    assert any("overlap rate" in i for i in result.issues)


def test_gate_kept_roles_keep_geometry():
    formula = GateBlock(
        node_id="f",
        text="E = mc^2",
        x=72,
        y=200,
        width=300,
        height=20,
        page=1,
        node_type="formula",
    )
    result = MainlineRelayoutGate().run([formula], translations={"f": "E = mc^2"})
    assert result.writeback_allowed
    out = result.blocks[0]
    assert out.text == "E = mc^2"


def test_overlap_rate_helper():
    assert _blocks_overlap_rate([]) == 0.0
    a = GateBlock(node_id="a", text="", x=0, y=0, width=100, height=50)
    b = GateBlock(node_id="b", text="", x=50, y=25, width=100, height=50)
    c = GateBlock(node_id="c", text="", x=0, y=200, width=100, height=50)
    assert _blocks_overlap_rate([a, b, c]) == pytest.approx(1.0 / 3.0)


def test_gate_result_jsonable():
    result = MainlineRelayoutGate().run(_blocks())
    json.dumps(result.to_dict())
