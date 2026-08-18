"""单元测试：BabelDOC 伪代码保护融合布局模型（pdf2zh.doclayout_pseudocode）。

覆盖：文本类别判定、算法框覆盖提升逻辑、conf 保持 numpy 标量（BabelDOC
``layout_parser`` 依赖 ``.item()``）、缺 detector 时的 no-op。真实模型的端到端
对照见 ``tools/diag_fused_babeldoc.py``（需要本地 PP-DocLayoutV2.onnx）。
"""

from __future__ import annotations

import numpy as np

from babeldoc.docvision.base_doclayout import YoloResult

from pdf2zh.doclayout_pseudocode import (
    PseudoCodeProtectedLayoutModel,
    _is_text_layout_name,
)


class _FakeGeometry:
    """最小 ``RasterGeometry`` 替身：像素坐标与 pt 坐标 1:1。"""

    def __init__(self, image: np.ndarray) -> None:
        self.image = image

    def px_len_to_pt(self, value: float, axis: str) -> float:
        return float(value)


class _FakeDetector:
    def __init__(self, boxes) -> None:
        self.boxes = list(boxes)  # [(x1, y1, x2, y2), ...] 像素坐标

    def detect_algorithm_boxes(self, image_rgb, page_index=None):
        return list(self.boxes)


class _LegacyDetector:
    """旧签名检测器（无 page_index 参数），验证 _protect_page 的 TypeError 回退。"""

    def __init__(self, boxes) -> None:
        self.boxes = list(boxes)

    def detect_algorithm_boxes(self, image_rgb):
        return list(self.boxes)


def _make_result(rows, names):
    return YoloResult(
        names=names,
        boxes_data=np.array(rows, dtype=np.float32),
    )


class TestIsTextLayoutName:
    def test_known_text_classes(self) -> None:
        assert _is_text_layout_name("plain text")
        assert _is_text_layout_name("paragraph")
        assert _is_text_layout_name("title")
        assert _is_text_layout_name("abstract")

    def test_algorithm_is_not_text(self) -> None:
        # algorithm 不在 is_text_layout 白名单中，这是跳过翻译的前提
        assert not _is_text_layout_name("algorithm")

    def test_unknown_label_falls_back_false(self) -> None:
        assert not _is_text_layout_name("brand_new_label")


class TestProtectPage:
    def test_promotes_fully_covered_text_box(self) -> None:
        detector = _FakeDetector([(100.0, 100.0, 400.0, 300.0)])
        model = PseudoCodeProtectedLayoutModel(base_model=None, detector=detector)
        result = _make_result(
            [
                [100, 100, 400, 300, 0.5, 0],  # 完全被 algorithm 覆盖 → 提升
                [100, 400, 400, 500, 0.5, 0],  # 不重叠 → 保持
            ],
            {0: "plain text"},
        )
        model._protect_page(
            _FakeGeometry(np.zeros((600, 600, 3), dtype=np.uint8)), result
        )
        names_after = [result.names[int(b.cls)] for b in result.boxes]
        assert names_after[0] == "algorithm"
        assert names_after[1] == "plain text"
        # 新类别已登记进 names（原 10 类之外的新 id）
        assert "algorithm" in result.names.values()

    def test_algorithm_id_preserved_across_pages(self) -> None:
        detector = _FakeDetector([(100.0, 100.0, 400.0, 300.0)])
        model = PseudoCodeProtectedLayoutModel(base_model=None, detector=detector)
        result = _make_result([[100, 100, 400, 300, 0.5, 0]], {0: "plain text"})
        model._protect_page(
            _FakeGeometry(np.zeros((600, 600, 3), dtype=np.uint8)), result
        )
        algo_id = next(k for k, v in result.names.items() if v == "algorithm")
        assert int(result.boxes[0].cls) == algo_id

    def test_non_text_box_never_promoted(self) -> None:
        detector = _FakeDetector([(100.0, 100.0, 400.0, 300.0)])
        model = PseudoCodeProtectedLayoutModel(base_model=None, detector=detector)
        result = _make_result([[100, 100, 400, 300, 0.9, 3]], {3: "figure"})
        model._protect_page(
            _FakeGeometry(np.zeros((600, 600, 3), dtype=np.uint8)), result
        )
        assert result.names[int(result.boxes[0].cls)] == "figure"

    def test_partial_cover_below_threshold_kept(self) -> None:
        detector = _FakeDetector([(100.0, 100.0, 150.0, 110.0)])
        model = PseudoCodeProtectedLayoutModel(
            base_model=None, detector=detector, cover_threshold=0.35
        )
        result = _make_result([[100, 100, 600, 200, 0.5, 0]], {0: "plain text"})
        model._protect_page(
            _FakeGeometry(np.zeros((300, 700, 3), dtype=np.uint8)), result
        )
        assert result.names[int(result.boxes[0].cls)] == "plain text"

    def test_promoted_conf_keeps_numpy_scalar(self) -> None:
        # BabelDOC layout_parser 对 layout.conf 调用 .item()；
        # 提升后 conf 必须是 numpy 标量，否则翻译管线直接崩溃。
        detector = _FakeDetector([(100.0, 100.0, 400.0, 300.0)])
        model = PseudoCodeProtectedLayoutModel(base_model=None, detector=detector)
        result = _make_result([[100, 100, 400, 300, 0.5, 0]], {0: "plain text"})
        model._protect_page(
            _FakeGeometry(np.zeros((600, 600, 3), dtype=np.uint8)), result
        )
        assert hasattr(result.boxes[0].conf, "item")

    def test_no_detector_is_noop(self) -> None:
        model = PseudoCodeProtectedLayoutModel(base_model=None, detector=None)
        result = _make_result([[100, 100, 400, 300, 0.5, 0]], {0: "plain text"})
        model._protect_page(
            _FakeGeometry(np.zeros((600, 600, 3), dtype=np.uint8)), result
        )
        assert result.names[int(result.boxes[0].cls)] == "plain text"

    def test_legacy_detector_signature_fallback(self) -> None:
        # PP-DocLayoutV2 检测器无 page_index 参数：_protect_page 应回退到
        # 纯图像调用，保护逻辑仍然生效。
        detector = _LegacyDetector([(100.0, 100.0, 400.0, 300.0)])
        model = PseudoCodeProtectedLayoutModel(base_model=None, detector=detector)
        result = _make_result([[100, 100, 400, 300, 0.5, 0]], {0: "plain text"})
        model._protect_page(
            _FakeGeometry(np.zeros((600, 600, 3), dtype=np.uint8)), result
        )
        assert result.names[int(result.boxes[0].cls)] == "algorithm"

    def test_legacy_detector_page_number_none(self) -> None:
        # 未提供 page_number 时（page_index=None）主接口直接调用也成立
        detector = _FakeDetector([(100.0, 100.0, 400.0, 300.0)])
        model = PseudoCodeProtectedLayoutModel(base_model=None, detector=detector)
        result = _make_result([[100, 100, 400, 300, 0.5, 0]], {0: "plain text"})
        model._protect_page(_FakeGeometry(np.zeros((600, 600, 3), dtype=np.uint8)), result)
        assert result.names[int(result.boxes[0].cls)] == "algorithm"


class TestInterface:
    def test_stride_delegates_to_base(self) -> None:
        class _Base:
            stride = 32

        model = PseudoCodeProtectedLayoutModel(_Base(), detector=None)
        assert model.stride == 32
