"""动态 Batch 并行（V3 iteration）单测。

覆盖 OnnxModel.predict_batch / supports_batch 与 high_level._LayoutBatchPredictor：
- 动态/固定 batch 轴检测
- predict_batch 空列表、公共 canvas 合并、混合尺寸、不支持时逐页降级
- _LayoutBatchPredictor 分批/余量/降级/统计
- _int_env 环境变量解析
- 真模型（已下载时）批量与逐页结果的结构一致性
"""

import os
import unittest

import numpy as np

from pdf2zh.doclayout import OnnxModel, YoloResult
from pdf2zh.high_level import _LayoutBatchPredictor, _int_env

MODEL_PATH = os.path.expanduser(
    "~/.cache/babeldoc/models/doclayout_yolo_docstructbench_imgsz1024.onnx"
)


class _FakeSession:
    """确定性假 session：batch 轴可动态/固定，输出 [N,300,6]。"""

    def __init__(self, dynamic_batch=True):
        self.dynamic = dynamic_batch
        self.runs = []

    def get_inputs(self):
        shape0 = "batch" if self.dynamic else 1
        inp = type(
            "Input", (), {"name": "images", "shape": [shape0, 3, "height", "width"]}
        )
        return [inp()]

    def run(self, outputs, feed):
        imgs = feed["images"]
        n, _, h, w = imgs.shape
        self.runs.append(imgs.shape)
        out = np.zeros((n, 300, 6), dtype=np.float32)
        for i in range(n):
            out[i, 0] = [10, 20, 300, 400, 0.9, 0.0]
            out[i, 1] = [w - 10, h - 20, w - 5, h - 5, 0.95, 1.0]
        return [out]


def _make_model(session, names=None):
    m = OnnxModel.__new__(OnnxModel)
    m.model_path = "fake.onnx"
    m._names = names if names is not None else {0: "text", 1: "figure"}
    m._stride = 32
    m._supports_batch = None
    m.model = session
    return m


def _img(h, w):
    rng = np.random.default_rng(7)
    return rng.integers(0, 255, (h, w, 3), dtype=np.uint8)


class TestSupportsBatch(unittest.TestCase):
    def test_dynamic_axis_true(self):
        m = _make_model(_FakeSession(dynamic_batch=True))
        self.assertTrue(m.supports_batch)
        self.assertTrue(m.supports_batch)  # 二次访问命中缓存

    def test_fixed_axis_false(self):
        m = _make_model(_FakeSession(dynamic_batch=False))
        self.assertFalse(m.supports_batch)

    def test_input_absent_false(self):
        class NoInput:
            def get_inputs(self):
                raise RuntimeError("no inputs")

        m = _make_model(NoInput())
        self.assertFalse(m.supports_batch)


class TestPredictBatch(unittest.TestCase):
    def test_empty(self):
        m = _make_model(_FakeSession(dynamic_batch=True))
        self.assertEqual(m.predict_batch([]), [])

    def test_batch_stack_single_call(self):
        sess = _FakeSession(dynamic_batch=True)
        m = _make_model(sess)
        imgs = [_img(800, 600), _img(800, 600)]
        results = m.predict_batch(imgs)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(isinstance(r, YoloResult) for r in results))
        # 同尺寸页：一次 ONNX 调度，batch=2
        self.assertEqual(len(sess.runs), 1)
        self.assertEqual(sess.runs[0][0], 2)
        # 每页 2 个检测（conf 0.9 / 0.95）
        self.assertEqual(len(results[0].boxes), 2)
        self.assertEqual(len(results[1].boxes), 2)

    def test_mixed_sizes_common_canvas(self):
        sess = _FakeSession(dynamic_batch=True)
        m = _make_model(sess)
        imgs = [_img(800, 600), _img(850, 600)]
        results = m.predict_batch(imgs)
        self.assertEqual(len(results), 2)
        # 一次调度，canvas 取 batch 内最大 letterbox 尺寸
        self.assertEqual(len(sess.runs), 1)
        n, _, h, w = sess.runs[0]
        self.assertEqual(n, 2)
        self.assertGreaterEqual(h, 832)  # 850 → page_imgsz 832
        # 每页保持 cls 顺序（conf 降序）：[figure(0.95), text(0.9)]
        for r in results:
            self.assertEqual(r.boxes[0].cls, 1.0)  # conf 0.95
            self.assertEqual(r.boxes[1].cls, 0.0)  # conf 0.9

    def test_fallback_when_fixed_batch(self):
        sess = _FakeSession(dynamic_batch=False)
        m = _make_model(sess)
        imgs = [_img(800, 600), _img(400, 300)]
        results = m.predict_batch(imgs)
        self.assertEqual(len(results), 2)
        # 不支持动态 batch：降级为两次 batch=1 调度（等价逐页）
        self.assertEqual(sess.runs, [(1, 3, 800, 608), (1, 3, 384, 288)])
class _RecordingModel:
    """记录 predict/predict_batch 调用的假模型。"""

    def __init__(self):
        self.batch_sizes = []
        self.predict_calls = 0
        self.supports_batch = True

    def predict(self, image, imgsz=1024, **kw):
        self.predict_calls += 1
        return [YoloResult(boxes=np.zeros((0, 6), np.float32), names={})]

    def predict_batch(self, images):
        self.batch_sizes.append(len(images))
        return [
            YoloResult(boxes=np.zeros((0, 6), np.float32), names={})
            for _ in images
        ]


class TestLayoutBatchPredictor(unittest.TestCase):
    def test_batches_and_remainder(self):
        model = _RecordingModel()
        p = _LayoutBatchPredictor(model, batch_size=2)
        imgs = [_img(100, 100) for _ in range(5)]
        self.assertEqual(len(p.predict_images(imgs[:2])), 2)
        self.assertEqual(model.batch_sizes, [2])
        self.assertEqual(len(p.predict_images(imgs[2:4])), 2)
        self.assertEqual(model.batch_sizes, [2, 2])
        self.assertEqual(len(p.predict_images(imgs[4:])), 1)
        self.assertEqual(model.batch_sizes, [2, 2, 1])
        flush, pages, secs = p.stats()
        self.assertEqual((flush, pages), (3, 5))
        self.assertGreaterEqual(secs, 0.0)

    def test_empty(self):
        model = _RecordingModel()
        p = _LayoutBatchPredictor(model, batch_size=2)
        self.assertEqual(p.predict_images([]), [])
        self.assertEqual(p.stats()[0], 0)

    def test_fallback_when_no_predict_batch(self):
        class OnlyPredict:
            def __init__(self):
                self.calls = 0

            def predict(self, image, imgsz=1024, **kw):
                self.calls += 1
                return [YoloResult(boxes=np.zeros((0, 6), np.float32), names={})]

        model = OnlyPredict()
        p = _LayoutBatchPredictor(model, batch_size=4)
        p.predict_images([_img(50, 50)] * 3)
        self.assertEqual(model.calls, 3)
        self.assertEqual(p.stats()[1], 3)

    def test_min_batch_size_clamped(self):
        p = _LayoutBatchPredictor(_RecordingModel(), batch_size=1)
        self.assertEqual(p.batch_size, 2)


class TestIntEnv(unittest.TestCase):
    def test_parse(self):
        os.environ["_PDF2ZH_TEST_INT"] = "8"
        try:
            self.assertEqual(_int_env("_PDF2ZH_TEST_INT", 0), 8)
            self.assertEqual(_int_env("_PDF2ZH_TEST_INT_MISSING", 0), 0)
            os.environ["_PDF2ZH_TEST_INT"] = "abc"
            self.assertEqual(_int_env("_PDF2ZH_TEST_INT", 4), 4)
        finally:
            os.environ.pop("_PDF2ZH_TEST_INT", None)


@unittest.skipUnless(os.path.exists(MODEL_PATH), "DocLayout ONNX 模型未下载")
class TestRealModelBatch(unittest.TestCase):
    """真模型冒烟：动态轴声明、批量与逐页结构一致性。"""

    @classmethod
    def setUpClass(cls):
        import ast

        import onnx
        import onnxruntime as ort

        from pdf2zh.doclayout import _configure_session_options

        opts = _configure_session_options()
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_DISABLE_ALL
        m = OnnxModel.__new__(OnnxModel)
        m.model_path = MODEL_PATH
        m._supports_batch = None
        mdl = onnx.load(MODEL_PATH, load_external_data=False)
        metadata = {d.key: d.value for d in mdl.metadata_props}
        m._stride = ast.literal_eval(metadata["stride"])
        m._names = ast.literal_eval(metadata["names"])
        del mdl
        m.model = ort.InferenceSession(
            MODEL_PATH, opts, providers=["CPUExecutionProvider"]
        )
        cls.model = m

    def test_supports_batch_real(self):
        self.assertTrue(self.model.supports_batch)

    def test_batch_vs_single_structure(self):
        rng = np.random.default_rng(42)
        imgs = [
            rng.integers(0, 255, (800, 600, 3), dtype=np.uint8) for _ in range(3)
        ]
        single = [
            self.model.predict(im, imgsz=int(im.shape[0] / 32) * 32)[0]
            for im in imgs
        ]
        batch = self.model.predict_batch(imgs)
        self.assertEqual(len(batch), 3)
        for s, b in zip(single, batch):
            self.assertEqual(len(s.boxes), len(b.boxes))
            # top-1 检测置信度接近（batch 与逐页在 ORT 下数值略有差异）
            if s.boxes and b.boxes:
                self.assertAlmostEqual(s.boxes[0].conf, b.boxes[0].conf, delta=0.05)


if __name__ == "__main__":
    unittest.main()

