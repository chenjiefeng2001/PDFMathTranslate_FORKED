import unittest
from unittest.mock import patch, MagicMock
import numpy as np
from pdf2zh.doclayout import (
    OnnxModel,
    YoloResult,
    YoloBox,
    get_backend,
    resolve_providers,
    set_backend,
)


class TestOnnxModel(unittest.TestCase):
    @patch("onnx.load")
    @patch("onnxruntime.InferenceSession")
    def setUp(self, mock_inference_session, mock_onnx_load):
        # Mock ONNX model metadata
        mock_model = MagicMock()
        mock_model.metadata_props = [
            MagicMock(key="stride", value="32"),
            MagicMock(key="names", value="['class1', 'class2']"),
        ]
        mock_onnx_load.return_value = mock_model

        # Initialize OnnxModel with a fake path
        self.model_path = "fake_model_path.onnx"
        self.model = OnnxModel(self.model_path)

    def test_stride_property(self):
        # Test that stride is correctly set from model metadata
        self.assertEqual(self.model.stride, 32)

    def test_resize_and_pad_image(self):
        # Create a dummy image (100x200)
        image = np.ones((100, 200, 3), dtype=np.uint8)
        resized_image = self.model.resize_and_pad_image(image, 1024)

        # Validate the output shape
        self.assertEqual(resized_image.shape[0], 512)
        self.assertEqual(resized_image.shape[1], 1024)

        # Check that padding has been added
        padded_height = resized_image.shape[0] - image.shape[0]
        padded_width = resized_image.shape[1] - image.shape[1]
        self.assertGreater(padded_height, 0)
        self.assertGreater(padded_width, 0)

    def test_scale_boxes(self):
        img1_shape = (1024, 1024)  # Model input shape
        img0_shape = (500, 300)  # Original image shape
        boxes = np.array([[512, 512, 768, 768]])  # Example bounding box

        scaled_boxes = self.model.scale_boxes(img1_shape, boxes, img0_shape)

        # Verify the output is scaled correctly
        self.assertEqual(scaled_boxes.shape, boxes.shape)
        self.assertTrue(np.all(scaled_boxes <= max(img0_shape)))

    def test_predict(self):
        # Mock model inference output
        mock_output = np.random.random((1, 300, 6))
        self.model.model.run.return_value = [mock_output]

        # Create a dummy image
        image = np.ones((500, 300, 3), dtype=np.uint8)

        results = self.model.predict(image)

        # Validate predictions
        self.assertEqual(len(results), 1)
        self.assertIsInstance(results[0], YoloResult)
        self.assertGreater(len(results[0].boxes), 0)
        self.assertIsInstance(results[0].boxes[0], YoloBox)


class TestYoloResult(unittest.TestCase):
    def test_yolo_result(self):
        # Example prediction data
        boxes = [
            [100, 200, 300, 400, 0.9, 0],
            [50, 100, 150, 200, 0.8, 1],
        ]
        names = ["class1", "class2"]

        result = YoloResult(boxes, names)

        # Validate the number of boxes and their order by confidence
        self.assertEqual(len(result.boxes), 2)
        self.assertGreater(result.boxes[0].conf, result.boxes[1].conf)
        self.assertEqual(result.names, names)


class TestYoloBox(unittest.TestCase):
    def test_yolo_box(self):
        # Example box data
        box_data = [100, 200, 300, 400, 0.9, 0]

        box = YoloBox(box_data)

        # Validate box properties
        self.assertEqual(box.xyxy, box_data[:4])
        self.assertEqual(box.conf, box_data[4])
        self.assertEqual(box.cls, box_data[5])

class TestBackendResolution(unittest.TestCase):
    """GPU 后端解析：DML provider 更名兼容 + 请求后端不可用时的回退。

    背景：onnxruntime >= 1.20 将 DirectML provider 更名为
    ``AzureExecutionProvider``。若解析逻辑仍只认旧名 ``DmlExecutionProvider``，
    ``--backend dml`` 会在主进程静默退化为 CPU，而 spawn 出的 worker 进程
    却自动探测到 GPU —— 这种主/worker 不一致正是并行翻译 BrokenProcessPool
    的高发来源。
    """

    def _patch_available(self, providers):
        import onnxruntime

        patcher = patch.object(onnxruntime, "get_available_providers", return_value=providers)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_dml_uses_new_azure_name_when_available(self):
        self._patch_available(["AzureExecutionProvider", "CPUExecutionProvider"])
        providers = resolve_providers("dml")
        self.assertEqual(providers, ["AzureExecutionProvider", "CPUExecutionProvider"])

    def test_dml_falls_back_to_legacy_name(self):
        self._patch_available(["DmlExecutionProvider", "CPUExecutionProvider"])
        providers = resolve_providers("dml")
        self.assertEqual(providers, ["DmlExecutionProvider", "CPUExecutionProvider"])

    def test_cpu_backend_is_strictly_cpu(self):
        self._patch_available(["AzureExecutionProvider", "CPUExecutionProvider"])
        providers = resolve_providers("cpu")
        self.assertEqual(providers, ["CPUExecutionProvider"])

    def test_unavailable_gpu_backend_falls_back_to_auto(self):
        # 请求 cuda 但环境只有 CPU/DML：不要静默产出“以为在用 GPU 实际跑 CPU”
        # 的不一致状态，而是带警告回退到自动探测（可能含其他 GPU）。
        self._patch_available(["AzureExecutionProvider", "CPUExecutionProvider"])
        providers = resolve_providers("cuda")
        self.assertNotIn("CUDAExecutionProvider", providers)
        self.assertIn("CPUExecutionProvider", providers)

    def test_auto_returns_all_available(self):
        self._patch_available(["AzureExecutionProvider", "CPUExecutionProvider"])
        providers = resolve_providers(None)
        self.assertEqual(providers, ["AzureExecutionProvider", "CPUExecutionProvider"])

    def test_set_get_backend_roundtrip(self):
        old = get_backend()
        try:
            set_backend("dml")
            self.assertEqual(get_backend(), "dml")
            set_backend("auto")
            self.assertIsNone(get_backend())
            set_backend("cpu")
            self.assertEqual(get_backend(), "cpu")
        finally:
            set_backend(old if old else "auto")


if __name__ == "__main__":
    unittest.main()
