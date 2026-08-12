import unittest
from unittest.mock import patch, MagicMock
import numpy as np
import os
import tempfile
from pdf2zh.doclayout import (
    OnnxModel,
    YoloResult,
    YoloBox,
    _OptimizedCache,
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


class TestOptimizedCacheLock(unittest.TestCase):
    """.optimized 缓存跨进程写锁：并发安全 + 损坏兜底。

    背景：并行 worker 同一时刻同时缺失缓存时会把同一路径并发写入，
    互相截断后 ORT 读损坏文件直接原生崩溃（worker 瞬死 → BrokenProcessPool）。
    """

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="opt_cache_")
        self.final = os.path.join(self._tmp, "model_opt_bce.onnx.optimized")

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _write_cache(self, content=b"valid-graph"):
        with open(self.final, "wb") as f:
            f.write(content)

    @patch("onnx.load")
    def test_single_generator_publishes_atomically(self, mock_load):
        mock_load.return_value = MagicMock()
        cache = _OptimizedCache(self.final)
        self.assertIsNone(cache.acquire())       # 获得锁 → 生成
        self.assertEqual(cache.state, "busy")
        self.assertTrue(os.path.exists(cache.lock_path))
        with open(cache.tmp_path, "wb") as f:
            f.write(b"optimized")
        cache.publish()                          # 原子发布
        self.assertTrue(os.path.exists(self.final))
        self.assertFalse(os.path.exists(cache.lock_path))
        self.assertFalse(os.path.exists(cache.tmp_path))

    @patch("onnx.load")
    def test_locked_waiter_reuses_published_cache(self, mock):
        mock.return_value = MagicMock()
        first = _OptimizedCache(self.final)
        self.assertIsNone(first.acquire())       # 持锁者
        # 模拟另一进程已发布成品缓存且锁尚在（>=1KB 且可被 onnx 解析）
        self._write_cache(b"x" * 2048)
        second = _OptimizedCache(self.final)
        resolved = second.acquire()
        self.assertEqual(resolved, self.final)
        self.assertEqual(second.state, "cached")

    @patch("onnx.load")
    def test_waiter_never_touches_owner_lock(self, mock):
        mock.return_value = MagicMock()
        owner = _OptimizedCache(self.final)
        self.assertIsNone(owner.acquire())
        self._write_cache(b"x" * 2048)
        waiter = _OptimizedCache(self.final)
        self.assertEqual(waiter.acquire(), self.final)  # state == "cached"
        waiter.publish()                                 # 非持有者：必须是无操作
        waiter.abort()
        self.assertTrue(os.path.exists(self.final + ".lock"))  # 持有者锁还在

    def test_corrupt_cache_is_detected(self):
        self._write_cache(b"garbage-not-a-protobuf" * 100)  # >1024B but invalid
        mock = MagicMock()
        mock.side_effect = Exception("corrupt")
        with patch("onnx.load", mock):
            cache = _OptimizedCache(self.final)
            self.assertIsNone(cache.acquire())   # 损坏 → 不作为缓存返回

    def test_stale_lock_is_reclaimed(self):
        # 死进程残留的锁文件：内容指向不存在的 pid
        with open(self.final + ".lock", "wb") as f:
            f.write(b"999999999")
        cache = _OptimizedCache(self.final)
        self.assertIsNone(cache.acquire())       # 成功拿到锁（残锁被清除）
        self.assertEqual(cache.state, "busy")

    def test_abort_cleans_tmp_and_lock(self):
        cache = _OptimizedCache(self.final)
        cache.acquire()
        with open(cache.tmp_path, "wb") as f:
            f.write(b"partial")
        cache.abort()
        self.assertFalse(os.path.exists(cache.tmp_path))
        self.assertFalse(os.path.exists(cache.lock_path))
        self.assertFalse(os.path.exists(self.final))


if __name__ == "__main__":
    unittest.main()
