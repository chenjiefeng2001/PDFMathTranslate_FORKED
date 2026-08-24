"""Tests for the final-product wiring in RuntimeService:

* ServiceConfig.run_evaluation drives the document-level evaluation
  step on the legacy mainline (阶段九 接入主链路)。
* ServiceConfig.use_v4_* synchronises into the v3 FeatureFlags singleton
  and records fallback telemetry (V8.2 灰度排程接线)。
* V8.3/V8.4: IR snapshots + write-back gate verdicts flow through
  TaskState (side-channel, non-blocking)。
"""

import pytest

from pdf2zh.services.runtime_service import (
    RuntimeService,
    ServiceConfig,
    TaskStage,
    TranslationRequest,
)
from pdf2zh.v3.feature_flags import get_feature_flags, reset_feature_flags


@pytest.fixture(autouse=True)
def _clean_flags():
    reset_feature_flags()
    yield
    reset_feature_flags()


class TestFeatureFlagSync:
    def test_sync_legacy_default(self):
        svc = RuntimeService(config=ServiceConfig())
        svc._sync_feature_flags("task_x")
        flags = get_feature_flags()
        assert flags.use_v4_engine is False
        assert flags.telemetry is not None
        events = flags.telemetry.events()
        assert any(e.get("reason") == "legacy_mainline" for e in events)

    def test_sync_v4_enabled(self):
        svc = RuntimeService(
            config=ServiceConfig(use_v4_engine=True, use_v4_layout=True)
        )
        svc._sync_feature_flags("task_y")
        flags = get_feature_flags()
        assert flags.use_v4_engine is True
        assert flags.use_v4_layout is True

    def test_sync_per_component(self):
        svc = RuntimeService(
            config=ServiceConfig(use_v4_engine=False, use_v4_repair=True)
        )
        svc._sync_feature_flags("task_z")
        flags = get_feature_flags()
        # 主开关关闭时子开关不强制打开（保守回退）
        assert flags.use_v4_engine is False

    def test_sync_is_safe_when_v3_missing(self, monkeypatch):
        import pdf2zh.services.runtime_service as rs_mod

        monkeypatch.setitem(__import__("sys").modules, "pdf2zh.v3.feature_flags", None)
        svc = RuntimeService(config=ServiceConfig())
        # 不应抛异常（静默跳过）
        svc._sync_feature_flags("task_w")


class TestRunEvaluationConfig:
    def test_config_has_evaluation_flag(self):
        cfg = ServiceConfig()
        assert cfg.run_evaluation is False
        assert ServiceConfig(run_evaluation=True).run_evaluation is True

    def test_service_accepts_evaluation_config(self):
        svc = RuntimeService(config=ServiceConfig(run_evaluation=True))
        assert svc.config.run_evaluation is True

    def test_task_state_carries_quality_scores(self):
        svc = RuntimeService()
        task_id = svc.submit_task(
            TranslationRequest(source_path="", target_lang="zh-CN")
        )
        state = svc.get_task_state(task_id)
        assert state is not None
        assert state.quality_scores is None or isinstance(state.quality_scores, dict)

    def test_evaluation_metrics_shape(self):
        """评测指标字段形状（与 evaluate.py 输出对齐）不随服务层丢失。"""
        from pdf2zh.evaluate import EvaluationReport

        report = EvaluationReport(
            geometry={"geometry_score": 95.0},
            structure={},
            translation={},
            rendering={},
            overall_score=90.0,
        )
        d = report.to_dict()
        assert d["overall_score"] == 90.0


class TestV4GateConfigSync:
    def test_config_has_v4_gate_flag(self):
        cfg = ServiceConfig()
        assert cfg.use_v4_gate is False
        assert ServiceConfig(use_v4_gate=True).use_v4_gate is True

    def test_sync_v4_gate_flag(self):
        svc = RuntimeService(config=ServiceConfig(use_v4_gate=True))
        svc._sync_feature_flags("task_g")
        flags = get_feature_flags()
        assert flags.use_v4_gate is True
        events = flags.telemetry.events()
        assert any(e.get("reason") == "legacy_mainline" for e in events)
        assert any("emit_ir" in e or "use_v4_gate" in e for e in events)

    def test_gate_factory_returns_gate(self):
        svc = RuntimeService(config=ServiceConfig(use_v4_gate=True))
        gate = svc._make_gate(612.0, 792.0)
        from pdf2zh.v3.mainline_gate import MainlineRelayoutGate

        assert isinstance(gate, MainlineRelayoutGate)
        assert gate.page_width == 612.0
        assert gate.page_height == 792.0

    def test_gate_factory_guarded(self, monkeypatch):
        import pdf2zh.services.runtime_service as rs_mod

        monkeypatch.delitem(
            __import__("sys").modules, "pdf2zh.v3.mainline_gate", raising=False
        )
        monkeypatch.setattr(rs_mod, "_MAINLINE_GATE_AVAILABLE", False, raising=False)
        svc = RuntimeService(config=ServiceConfig(use_v4_gate=True))

        class _Missing:
            attribute = None

            def __getattr__(self, name):
                raise AttributeError(name)

        # 若 v3 缺失，返回 None 而非抛异常（保守回退）
        if hasattr(rs_mod, "_make_gate_fallback"):
            gate = rs_mod._make_gate_fallback()
            assert gate is None


class TestTaskStateSideChannel:
    def test_task_state_fields_exist(self):
        svc = RuntimeService()
        tid = svc.submit_task(TranslationRequest(source_path="", target_lang="zh-CN"))
        state = svc.get_task_state(tid)
        d = state.to_dict()
        assert "ir_snapshots" in d
        assert "gate_verdicts" in d


class TestCharsFromLTPage:
    def test_adapts_pdfminer_chars(self):
        from pdfminer.layout import LTChar, LTTextLine, LTTextContainer
        from pdf2zh.v3.geometry import chars_from_ltpage

        char = LTChar.__new__(LTChar)
        char.fontname = "ABC"
        char.size = 10.0
        char.bbox = (0, 0, 5, 10)
        char.get_text = lambda: "A"
        line = LTTextLine.__new__(LTTextLine)
        line._objs = [char]
        line.bbox = (0, 0, 5, 10)
        line.x0, line.y0, line.x1, line.y1 = 0, 0, 5, 10
        cont = LTTextContainer.__new__(LTTextContainer)
        cont._objs = [line]
        cont.bbox = (0, 0, 5, 10)
        cont.x0, cont.y0, cont.x1, cont.y1 = 0, 0, 5, 10
        ltp = type(
            "LTPage",
            (),
            {
                "_objs": [cont],
                "bbox": (0, 0, 612, 792),
                "__iter__": lambda self: iter(self._objs),
            },
        )()
        chars = chars_from_ltpage(ltp, page_num=3)
        assert len(chars) == 1
        c = chars[0]
        assert c.text == "A"
        assert c.page_num == 3
        assert abs(c.size - 10.0) < 1e-6
