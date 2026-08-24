"""Tests for V3 Layout Optimizer (Module: optimizer.py)."""

import pytest
from pdf2zh.v3.optimizer import LayoutElement, OptimizationResult, LayoutOptimizer
from pdf2zh.v3.layout import LayoutConstraint, ConstraintType


class TestLayoutElement:
    def test_defaults(self):
        e = LayoutElement(node_id="n1", width=100, height=50)
        assert e.node_id == "n1"
        assert e.width == 100
        assert e.height == 50
        assert e.min_y == 0.0
        assert e.max_y == 10000.0
        assert e.preferred_y is None

    def test_with_preferred(self):
        e = LayoutElement(node_id="n1", width=100, height=50, preferred_y=200)
        assert e.preferred_y == 200


class TestOptimizationResult:
    def test_defaults(self):
        r = OptimizationResult(positions={"n1": 50.0}, total_cost=10.0)
        assert r.positions["n1"] == 50.0
        assert r.total_cost == 10.0
        assert r.feasible is True

    def test_with_feasibility(self):
        r = OptimizationResult(positions={}, total_cost=100.0, feasible=False)
        assert r.feasible is False


class TestLayoutOptimizer:
    def test_init(self):
        opt = LayoutOptimizer()
        assert opt.page_width == 612.0
        assert opt.page_height == 792.0

    def test_add_element(self):
        opt = LayoutOptimizer()
        e = LayoutElement(node_id="n1", width=100, height=50)
        opt.add_element(e)
        assert "n1" in opt._elements

    def test_set_elements(self):
        opt = LayoutOptimizer()
        elems = [
            LayoutElement(node_id="a", width=100, height=50),
            LayoutElement(node_id="b", width=200, height=30),
        ]
        opt.set_elements(elems)
        assert len(opt._elements) == 2

    def test_optimize_empty(self):
        opt = LayoutOptimizer()
        result = opt.optimize()
        assert result.positions == {}
        assert result.feasible is True

    def test_optimize_single(self):
        opt = LayoutOptimizer()
        opt.add_element(LayoutElement(node_id="n1", width=100, height=50))
        result = opt.optimize()
        assert "n1" in result.positions
        assert result.positions["n1"] >= opt.margin_top

    def test_optimize_multiple(self):
        opt = LayoutOptimizer()
        opt.add_element(LayoutElement(node_id="a", width=100, height=50))
        opt.add_element(LayoutElement(node_id="b", width=100, height=30))
        result = opt.optimize()
        assert result.positions["b"] > result.positions["a"]

    def test_must_below_constraint(self):
        opt = LayoutOptimizer()
        opt.add_element(LayoutElement(node_id="a", width=100, height=50))
        opt.add_element(LayoutElement(node_id="b", width=100, height=30))
        opt.add_constraint(
            LayoutConstraint(
                constraint_type=ConstraintType.HARD,
                source_id="b",
                target_id="a",
                relationship="must_below",
                gap=20.0,
            )
        )
        result = opt.optimize()
        assert result.positions["b"] > result.positions["a"] + 20

    def test_cannot_overlap(self):
        opt = LayoutOptimizer()
        opt.add_element(LayoutElement(node_id="a", width=100, height=50))
        opt.add_element(LayoutElement(node_id="b", width=100, height=30))
        opt.add_constraint(
            LayoutConstraint(
                constraint_type=ConstraintType.HARD,
                source_id="b",
                target_id="a",
                relationship="cannot_overlap",
                gap=5.0,
            )
        )
        result = opt.optimize()
        diff = result.positions["b"] - result.positions["a"]
        assert diff >= 0

    def test_clear(self):
        opt = LayoutOptimizer()
        opt.add_element(LayoutElement(node_id="n1", width=50, height=20))
        opt.clear()
        assert len(opt._elements) == 0

    def test_estimate_cost(self):
        opt = LayoutOptimizer()
        opt.add_element(LayoutElement(node_id="n1", width=100, height=50))
        opt.add_element(LayoutElement(node_id="n2", width=100, height=30))
        opt.add_constraint(
            LayoutConstraint(
                constraint_type=ConstraintType.SOFT,
                source_id="n2",
                target_id="n1",
                relationship="cannot_overlap",
                gap=5.0,
            )
        )
        result = opt.optimize()
        cost = opt.estimate_cost(result.positions)
        assert isinstance(cost, float)
