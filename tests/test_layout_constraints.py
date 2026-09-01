# -*- coding: utf-8 -*-
"""Commit 7B — geometry constraint vocabulary tests.

Covers ``pdf2zh.semantic.layout.constraints``: FixedX / FixedY / FixedWidth /
MaxWidth / MaxHeight / PreserveBBox and ``resolve_geometry``.  Constraints are
renderer-independent — this module only checks the folding behaviour, never
any PDF/renderer coupling.
"""

from pdf2zh.semantic.layout.constraints import (
    FixedWidth,
    FixedX,
    FixedY,
    LayoutGeometry,
    MaxHeight,
    MaxWidth,
    PreserveBBox,
    resolve_geometry,
)


def _base(x=40.0, y=50.0, w=400.0, h=24.0):
    return LayoutGeometry(x=x, y=y, width=w, height=h)


def test_fixed_x_pins_horizontal_origin():
    g = resolve_geometry(_base(), (FixedX(72.0),))
    assert g.x == 72.0
    assert g.y == 50.0


def test_fixed_y_pins_vertical_origin():
    g = resolve_geometry(_base(), (FixedY(120.0),))
    assert g.y == 120.0
    assert g.x == 40.0


def test_fixed_width_pins_width():
    g = resolve_geometry(_base(w=400.0), (FixedWidth(300.0),))
    assert g.width == 300.0


def test_max_width_caps_large_width():
    g = resolve_geometry(_base(w=400.0), (MaxWidth(200.0),))
    assert g.width == 200.0


def test_max_width_keeps_smaller_width():
    g = resolve_geometry(_base(w=150.0), (MaxWidth(400.0),))
    assert g.width == 150.0


def test_max_height_caps_height():
    g = resolve_geometry(_base(h=80.0), (MaxHeight(50.0),))
    assert g.height == 50.0


def test_preserve_bbox_sets_all_four():
    g = resolve_geometry(_base(), (PreserveBBox((10.0, 20.0, 210.0, 60.0)),))
    assert g.x == 10.0
    assert g.y == 20.0
    assert g.width == 200.0
    assert g.height == 40.0


def test_multiple_constraints_left_to_right_override():
    g = resolve_geometry(
        _base(),
        (FixedX(50.0), MaxWidth(150.0), FixedWidth(220.0)),
    )
    assert g.x == 50.0
    # FixedWidth applied after MaxWidth overrides the cap
    assert g.width == 220.0


def test_no_constraints_returns_base():
    g = resolve_geometry(_base())
    assert g == _base()


def test_code_region_uses_preserve_bbox():
    """Preserved (code) regions get a PreserveBBox so geometry can't drift."""
    src = (20.0, 30.0, 300.0, 60.0)
    g = resolve_geometry(_base(), (PreserveBBox(src),))
    assert g.x == src[0]
    assert g.y == src[1]
    assert g.width == src[2] - src[0]
    assert g.height == src[3] - src[1]
