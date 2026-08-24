import glob
import time
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
        patcher = patch(
            "pdf2zh.doclayout._ort_available_providers", return_value=list(providers)
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_dml_uses_new_azure_name_when_available(self):
        self._patch_available(["AzureExecutionProvider", "CPUExecutionProvider"])
        with patch("pdf2zh.doclayout._exec_gpu_providers",
                   return_value={"AzureExecutionProvider"}):
            providers = resolve_providers("dml")
        self.assertEqual(providers, ["AzureExecutionProvider", "CPUExecutionProvider"])

    def test_dml_falls_back_to_legacy_name(self):
        self._patch_available(["DmlExecutionProvider", "CPUExecutionProvider"])
        with patch("pdf2zh.doclayout._exec_gpu_providers",
                   return_value={"DmlExecutionProvider"}):
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
        with patch("pdf2zh.doclayout._exec_gpu_providers", return_value=set()):
            providers = resolve_providers(None)
        self.assertEqual(providers, ["AzureExecutionProvider", "CPUExecutionProvider"])

    def test_auto_filters_degraded_compiled_provider(self):
        # TensorRT 已注册但缺运行库（执行级探测不可用）：auto 必须过滤，
        # 否则 ORT 每次创建会话都尝试加载缺失库并打印 EP Error 噪音。
        self._patch_available([
            "TensorrtExecutionProvider",
            "CUDAExecutionProvider",
            "CPUExecutionProvider",
        ])
        with patch("pdf2zh.doclayout._exec_gpu_providers",
                   return_value={"CUDAExecutionProvider"}):
            providers = resolve_providers(None)
        self.assertEqual(
            providers,
            ["CUDAExecutionProvider", "CPUExecutionProvider"],
        )

    def test_auto_keeps_exec_usable_compiled_provider(self):
        # TensorRT 执行级可用（运行库就绪）时 auto 原样保留，不误伤。
        self._patch_available([
            "TensorrtExecutionProvider",
            "CUDAExecutionProvider",
            "CPUExecutionProvider",
        ])
        with patch("pdf2zh.doclayout._exec_gpu_providers",
                   return_value={"TensorrtExecutionProvider",
                                 "CUDAExecutionProvider"}):
            providers = resolve_providers(None)
        self.assertEqual(
            providers,
            ["TensorrtExecutionProvider", "CUDAExecutionProvider",
             "CPUExecutionProvider"],
        )

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

    def test_cuda_missing_warns_and_returns_cpu_only(self):
        # 请求 cuda 但环境无 CUDA：CPU 兜底项使交集非空，必须记录明确警告，
        # 而不是“选 GPU 却静默跑 CPU”。（此前该场景完全静默。）
        self._patch_available(["AzureExecutionProvider", "CPUExecutionProvider"])
        with patch("pdf2zh.doclayout.logger.warning") as mock_warn:
            providers = resolve_providers("cuda")
        self.assertEqual(providers, ["CPUExecutionProvider"])
        self.assertTrue(mock_warn.called, "cuda missing must log a warning")
        fmt, args = mock_warn.call_args.args[0], mock_warn.call_args.args[1:]
        self.assertIn("no GPU provider is available", fmt)
        self.assertTrue(any("onnxruntime-gpu" in str(p) for p in args))
        self.assertTrue(any("cuda" in str(p) for p in args))

    def test_get_runtime_provider_status(self):
        from pdf2zh.doclayout import get_runtime_provider_status

        self._patch_available(["AzureExecutionProvider", "CPUExecutionProvider"])
        with patch("pdf2zh.doclayout._exec_gpu_providers",
                   return_value={"AzureExecutionProvider"}), \
             patch("pdf2zh.doclayout._runtime_provider_status_cache", None):
            status = get_runtime_provider_status()
        self.assertIn("onnxruntime", status)
        self.assertEqual(status["available"], ["AzureExecutionProvider", "CPUExecutionProvider"])
        self.assertFalse(status["cuda"])
        self.assertTrue(status["dml"])
        self.assertEqual(status["effective"], ["AzureExecutionProvider", "CPUExecutionProvider"])

    def test_get_runtime_provider_status_cuda_true(self):
        from pdf2zh.doclayout import get_runtime_provider_status

        self._patch_available(["CUDAExecutionProvider", "CPUExecutionProvider"])
        with patch("pdf2zh.doclayout._exec_gpu_providers",
                   return_value={"CUDAExecutionProvider"}), \
             patch("pdf2zh.doclayout._runtime_provider_status_cache", None):
            status = get_runtime_provider_status()
        self.assertTrue(status["cuda"])
        self.assertFalse(status["dml"])

    def test_warn_gpu_unavailable_logs_hint(self):
        from pdf2zh.doclayout import warn_gpu_unavailable

        with patch("pdf2zh.doclayout.logger.warning") as mock_warn:
            warn_gpu_unavailable("cuda", ["CUDAExecutionProvider", "CPUExecutionProvider"], ["CPUExecutionProvider"])
        fmt, args = mock_warn.call_args.args[0], mock_warn.call_args.args[1:]
        self.assertIn("requested but no GPU provider", fmt)
        self.assertTrue(any("cuda" in str(p) for p in args))
        self.assertTrue(any("onnxruntime-gpu" in str(p) for p in args))

    def test_warn_gpu_unavailable_hint_distinguishes_installed_runtime(self):
        from pdf2zh.doclayout import warn_gpu_unavailable

        with patch("pdf2zh.doclayout.logger.warning") as mock_warn:
            warn_gpu_unavailable("cuda", ["CUDAExecutionProvider", "CPUExecutionProvider"], ["CUDAExecutionProvider", "CPUExecutionProvider"])
        args = mock_warn.call_args.args[1:]
        self.assertTrue(any("cublasLt" in str(p) for p in args))

    def test_warn_gpu_session_fallback_logs_hint(self):
        from pdf2zh.doclayout import warn_gpu_session_fallback

        with patch("pdf2zh.doclayout.logger.warning") as mock_warn:
            warn_gpu_session_fallback("cuda", ["CUDAExecutionProvider", "CPUExecutionProvider"], ["CPUExecutionProvider"])
        fmt, args = mock_warn.call_args.args[0], mock_warn.call_args.args[1:]
        self.assertIn("fell back to CPU", fmt)
        self.assertTrue(any("onnxruntime-gpu" in str(p) for p in args))

    def test_has_gpu_provider(self):
        from pdf2zh.doclayout import has_gpu_provider

        with patch("pdf2zh.doclayout._exec_gpu_providers",
                   return_value={"CUDAExecutionProvider", "AzureExecutionProvider"}):
            self.assertTrue(has_gpu_provider("cuda", ["CUDAExecutionProvider", "CPUExecutionProvider"]))
            self.assertFalse(has_gpu_provider("cuda", ["CPUExecutionProvider"]))
            self.assertTrue(has_gpu_provider("dml", ["AzureExecutionProvider"]))
            self.assertTrue(has_gpu_provider("auto", ["AzureExecutionProvider", "CPUExecutionProvider"]))
            self.assertFalse(has_gpu_provider("cpu", ["AzureExecutionProvider"]))
            self.assertFalse(has_gpu_provider("cuda", []))

    def test_has_gpu_provider_false_when_registered_but_ineffective(self):
        # DML 已注册但执行级探测判定无效（ORT 静默回退 CPU）：必须判无 GPU。
        from pdf2zh.doclayout import has_gpu_provider

        with patch("pdf2zh.doclayout._exec_gpu_providers", return_value=set()):
            self.assertFalse(has_gpu_provider("dml", ["AzureExecutionProvider"]))
            self.assertFalse(has_gpu_provider("auto", ["AzureExecutionProvider"]))

    def test_check_session_fallback_warns_on_silent_cpu(self):
        from pdf2zh.doclayout import _check_session_fallback

        with patch("pdf2zh.doclayout._exec_gpu_providers", return_value=set()), \
             patch("pdf2zh.doclayout.warn_gpu_session_fallback") as mock_warn:
            _check_session_fallback("cuda", ["CUDAExecutionProvider", "CPUExecutionProvider"], ["CPUExecutionProvider"])
        self.assertTrue(mock_warn.called)

        # effective 含 CUDA 且执行级探测有效 → 不警告
        with patch("pdf2zh.doclayout._exec_gpu_providers",
                   return_value={"CUDAExecutionProvider"}), \
             patch("pdf2zh.doclayout.warn_gpu_session_fallback") as mock_warn2:
            _check_session_fallback("cuda", ["CUDAExecutionProvider", "CPUExecutionProvider"], ["CUDAExecutionProvider", "CPUExecutionProvider"])
        self.assertFalse(mock_warn2.called)

        # effective 含 CUDA 但执行级探测判定无效（静默回退）→ 仍必须警告
        with patch("pdf2zh.doclayout._exec_gpu_providers", return_value=set()), \
             patch("pdf2zh.doclayout.warn_gpu_session_fallback") as mock_warn3:
            _check_session_fallback("cuda", ["CUDAExecutionProvider", "CPUExecutionProvider"], ["CUDAExecutionProvider", "CPUExecutionProvider"])
        self.assertTrue(mock_warn3.called, "get_providers() 掩盖静默回退时必须警告")

    def test_provider_status_detects_silent_cpu_fallback(self):
        """注册表有 CUDAExecutionProvider 但实际创建会话回退 CPU（缺
        cublasLt/cuDNN DLL 的典型场景）：诊断必须显示 CUDA 不可用。"""
        from pdf2zh.doclayout import get_runtime_provider_status

        self._patch_available(["TensorrtExecutionProvider", "CUDAExecutionProvider", "CPUExecutionProvider"])
        with patch("pdf2zh.doclayout._exec_gpu_providers", return_value=set()), \
             patch("pdf2zh.doclayout._runtime_provider_status_cache", None):
            status = get_runtime_provider_status()
        self.assertIn("CUDAExecutionProvider", status["available"])
        self.assertFalse(status["cuda"])
        self.assertFalse(status["dml"])

    def test_dml_registered_but_ineffective_falls_back_to_cpu(self):
        # provider 已注册（available 含 Azure）但执行级探测判定 DML 无效：必须
        # 回退 CPU-only 并记录明确警告（此前 get_providers() 掩盖了静默回退）。
        self._patch_available(["AzureExecutionProvider", "CPUExecutionProvider"])
        with patch("pdf2zh.doclayout._exec_gpu_providers", return_value=set()), \
             patch("pdf2zh.doclayout.warn_gpu_session_fallback") as mock_warn:
            providers = resolve_providers("dml")
        self.assertEqual(providers, ["CPUExecutionProvider"])
        self.assertTrue(mock_warn.called, "DML 无效必须给出会话级回退警告")



class TestExecutionLevelProbe(unittest.TestCase):
    """执行级探测：ORT 静默回退时 get_providers() 失真 → 用 profiling 判定。"""

    def test_parse_profile_providers_extracts_node_providers(self):
        import json
        from pdf2zh.doclayout import _parse_profile_providers

        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "probe.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({
                    "traceEvents": [
                        {"cat": "Node", "name": "Conv",
                         "args": {"provider": "CPUExecutionProvider"}},
                        {"cat": "Node", "name": "Add",
                         "args": {"provider": "AzureExecutionProvider"}},
                        {"cat": "Kernel", "name": "x", "args": {}},
                    ]
                }, fh)
            result = _parse_profile_providers(path)
        self.assertEqual(result, {"CPUExecutionProvider", "AzureExecutionProvider"})
        self.assertFalse(os.path.exists(path), "profile 临时文件应被清理")

    def test_parse_profile_providers_missing_file_returns_empty(self):
        from pdf2zh.doclayout import _parse_profile_providers

        self.assertEqual(
            _parse_profile_providers(r"C:\nonexistent\probe.json"), set()
        )

    def test_probe_providers_returns_only_executing_providers(self):
        # 请求 [Azure, CPU] 但 profile 显示只有 CPU 执行 → 结果仅含 CPU。
        import json
        from pdf2zh.doclayout import _probe_providers

        sess = MagicMock()
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "probe.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({
                    "traceEvents": [
                        {"cat": "Node", "args": {"provider": "CPUExecutionProvider"}},
                    ]
                }, fh)
            sess.end_profiling.return_value = path
            with patch("onnxruntime.InferenceSession", return_value=sess):
                result = _probe_providers(
                    ["AzureExecutionProvider", "CPUExecutionProvider"]
                )
        self.assertEqual(result, ["CPUExecutionProvider"])

    def test_probe_gpu_provider_false_when_cpu_only(self):
        import onnxruntime
        from pdf2zh.doclayout import _probe_gpu_provider

        with patch("pdf2zh.doclayout._ort_available_providers",
                   return_value=["AzureExecutionProvider", "CPUExecutionProvider"]), \
             patch("pdf2zh.doclayout._probe_providers",
                   return_value=["CPUExecutionProvider"]):
            self.assertFalse(_probe_gpu_provider("AzureExecutionProvider"))

    def test_probe_gpu_provider_true_when_executing(self):
        import onnxruntime
        from pdf2zh.doclayout import _probe_gpu_provider

        with patch("pdf2zh.doclayout._ort_available_providers",
                   return_value=["AzureExecutionProvider", "CPUExecutionProvider"]), \
             patch("pdf2zh.doclayout._probe_providers",
                   return_value=["AzureExecutionProvider", "CPUExecutionProvider"]):
            self.assertTrue(_probe_gpu_provider("AzureExecutionProvider"))

    def test_exec_gpu_providers_caches_result(self):
        import onnxruntime
        import pdf2zh.doclayout as dl
        from pdf2zh.doclayout import _exec_gpu_providers

        saved = dl._EXEC_GPU_PROVIDERS
        try:
            # 自包含：忽略前序（OnnxModel 初始化）可能已写入的真实探测缓存。
            # 注意必须写模块全局（dl._EXEC_GPU_PROVIDERS），仅改局部绑定无效。
            dl._EXEC_GPU_PROVIDERS = None
            with patch("pdf2zh.doclayout._ort_available_providers",
                       return_value=["CUDAExecutionProvider", "CPUExecutionProvider"]), \
                 patch("pdf2zh.doclayout._probe_gpu_provider", return_value=True):
                first = _exec_gpu_providers()
                second = _exec_gpu_providers()
            self.assertEqual(first, {"CUDAExecutionProvider"})
            self.assertIs(first, second)  # 进程级缓存命中（同一对象）
        finally:
            dl._EXEC_GPU_PROVIDERS = saved

    def test_exec_gpu_providers_never_probes_tensorrt(self):
        # TRT 不在任何后端开关（auto/cpu/cuda/dml）里，永远不会被选用；
        # 探测它只会创建 TRT 测试会话，在缺 TensorRT 运行库的机器上触发
        # ORT C++ 层向 stderr 打印整段 "EP Error ... Falling back" 刷屏。
        import pdf2zh.doclayout as dl
        from pdf2zh.doclayout import _exec_gpu_providers

        saved = dl._EXEC_GPU_PROVIDERS
        probed: list[str] = []

        def _fake_probe(name: str) -> bool:
            probed.append(name)
            return name == "CUDAExecutionProvider"

        try:
            dl._EXEC_GPU_PROVIDERS = None
            with patch(
                "pdf2zh.doclayout._ort_available_providers",
                return_value=[
                    "TensorrtExecutionProvider",
                    "CUDAExecutionProvider",
                    "CPUExecutionProvider",
                ],
            ), patch(
                "pdf2zh.doclayout._probe_gpu_provider",
                side_effect=_fake_probe,
            ):
                result = _exec_gpu_providers()
            self.assertEqual(probed, ["CUDAExecutionProvider"])
            self.assertEqual(result, {"CUDAExecutionProvider"})
        finally:
            dl._EXEC_GPU_PROVIDERS = saved

    def test_probe_model_pads_match_declared_output_shape(self):
        # 探针 Conv 必须带 pads=[1,1,1,1]：输入 64×64 → 输出 64×64，与图
        # 声明的 'y' 形状一致。无 padding 时实际输出 62×62，ORT 每次会话
        # 创建都打 "Error merging shape info ... lenient merge" 警告。
        import pdf2zh.doclayout as dl

        data = dl._build_probe_model_bytes()
        self.assertIsNotNone(data)
        import onnx

        model = onnx.ModelProto.FromString(data)
        conv = next(n for n in model.graph.node if n.op_type == "Conv")
        pads = next(
            a for a in conv.attribute if a.name == "pads"
        ).ints
        self.assertEqual(list(pads), [1, 1, 1, 1])
        declared = model.graph.output[0].type.tensor_type.shape.dim
        self.assertEqual([d.dim_value for d in declared], [1, 3, 64, 64])

    def test_ort_log_severity_default_and_env_override(self):
        import pdf2zh.doclayout as dl
        from pdf2zh.doclayout import ort_log_severity

        old = os.environ.get("PDF2ZH_ORT_LOG_SEVERITY")
        try:
            os.environ.pop("PDF2ZH_ORT_LOG_SEVERITY", None)
            self.assertEqual(ort_log_severity(), 3)  # 默认 ERROR-only
            os.environ["PDF2ZH_ORT_LOG_SEVERITY"] = "0"
            self.assertEqual(ort_log_severity(), 0)
            os.environ["PDF2ZH_ORT_LOG_SEVERITY"] = "9"
            self.assertEqual(ort_log_severity(), 4)  # 上限钳制
            os.environ["PDF2ZH_ORT_LOG_SEVERITY"] = "bogus"
            self.assertEqual(ort_log_severity(), 3)  # 非法值回退默认
        finally:
            if old is None:
                os.environ.pop("PDF2ZH_ORT_LOG_SEVERITY", None)
            else:
                os.environ["PDF2ZH_ORT_LOG_SEVERITY"] = old

    def test_session_options_carry_log_severity(self):
        import onnxruntime

        from pdf2zh.doclayout import _configure_session_options

        opts = _configure_session_options()
        self.assertEqual(opts.log_severity_level, 3)



class TestOptimizedCacheIsolation(unittest.TestCase):
    """.optimized 缓存按 backend/优化级别隔离（避免 CPU NCHWc 图污染 GPU 会话）。"""

    @staticmethod
    def _reset_probe_cache():
        import pdf2zh.doclayout as dl

        dl._EXEC_GPU_PROVIDERS = None

    def _restore_backend(self):
        old = get_backend()
        self.addCleanup(self._reset_probe_cache)
        self.addCleanup(set_backend, old if old else "auto")
        set_backend("auto")

    def test_cache_path_cpu_fingerprinted(self):
        from pdf2zh.doclayout import _optimized_cache_path

        self._restore_backend()
        set_backend("cpu")
        self.assertRegex(
            _optimized_cache_path("m.onnx"), r"^m\.onnx\.cpu-[0-9a-f]{12}\.optimized$"
        )

    def test_cache_path_cuda_fingerprinted(self):
        from pdf2zh.doclayout import _optimized_cache_path

        self._restore_backend()
        set_backend("cuda")
        with patch("pdf2zh.doclayout._exec_gpu_providers",
                   return_value={"CUDAExecutionProvider"}):
            self.assertRegex(
                _optimized_cache_path("m.onnx"), r"^m\.onnx\.cuda-[0-9a-f]{12}\.optimized$"
            )

    def test_cache_path_dml_effective_fingerprinted(self):
        from pdf2zh.doclayout import _optimized_cache_path

        self._restore_backend()
        set_backend("dml")
        with patch("pdf2zh.doclayout._exec_gpu_providers",
                   return_value={"AzureExecutionProvider"}):
            self.assertRegex(
                _optimized_cache_path("m.onnx"), r"^m\.onnx\.dml-basic-[0-9a-f]{12}\.optimized$"
            )

    def test_cache_path_dml_ineffective_uses_cpu_fingerprint(self):
        from pdf2zh.doclayout import _optimized_cache_path

        self._restore_backend()
        set_backend("dml")
        with patch("pdf2zh.doclayout._exec_gpu_providers", return_value=set()):
            self.assertRegex(
                _optimized_cache_path("m.onnx"), r"^m\.onnx\.cpu-[0-9a-f]{12}\.optimized$"
            )

    def test_cache_fingerprint_stable_within_environment(self):
        from pdf2zh.doclayout import _cache_fingerprint_key

        self._restore_backend()
        set_backend("cpu")
        self.assertEqual(_cache_fingerprint_key(), _cache_fingerprint_key())

    def test_cache_fingerprint_differs_across_backends(self):
        from pdf2zh.doclayout import _cache_fingerprint_key

        self._restore_backend()
        set_backend("cpu")
        cpu_fp = _cache_fingerprint_key()
        set_backend("cuda")
        with patch("pdf2zh.doclayout._exec_gpu_providers",
                   return_value={"CUDAExecutionProvider"}):
            cuda_fp = _cache_fingerprint_key()
        self.assertNotEqual(cpu_fp, cuda_fp)

    def test_should_generate_cache_false_for_explicit_gpu(self):
        from pdf2zh.doclayout import _should_generate_optimized_cache

        self._restore_backend()
        set_backend("cuda")
        self.assertFalse(_should_generate_optimized_cache())
        set_backend("dml")
        self.assertFalse(_should_generate_optimized_cache())
        set_backend("cpu")
        self.assertTrue(_should_generate_optimized_cache())
        set_backend("auto")
        self.assertTrue(_should_generate_optimized_cache())

    def test_session_options_dml_effective_uses_basic(self):
        import onnxruntime
        from pdf2zh.doclayout import _configure_session_options

        self._restore_backend()
        set_backend("dml")
        with patch("pdf2zh.doclayout._exec_gpu_providers",
                   return_value={"AzureExecutionProvider"}):
            opts = _configure_session_options()
        self.assertEqual(
            opts.graph_optimization_level,
            onnxruntime.GraphOptimizationLevel.ORT_ENABLE_BASIC,
        )

    def test_session_options_dml_ineffective_uses_all(self):
        import onnxruntime
        from pdf2zh.doclayout import _configure_session_options

        self._restore_backend()
        set_backend("dml")
        with patch("pdf2zh.doclayout._exec_gpu_providers", return_value=set()):
            opts = _configure_session_options()
        self.assertEqual(
            opts.graph_optimization_level,
            onnxruntime.GraphOptimizationLevel.ORT_ENABLE_ALL,
        )

    def test_session_options_cpu_uses_all(self):
        import onnxruntime
        from pdf2zh.doclayout import _configure_session_options

        self._restore_backend()
        set_backend("cpu")
        opts = _configure_session_options()
        self.assertEqual(
            opts.graph_optimization_level,
            onnxruntime.GraphOptimizationLevel.ORT_ENABLE_ALL,
        )



class TestStaleOptimizedTmpCleanup(unittest.TestCase):
    """孤儿 <model>.*optimized.*.tmp 清理：过期且生成进程已亡才删除。"""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="opt_tmp_clean_")
        self.model = os.path.join(self._tmp, "m.onnx")

    def tearDown(self):
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)

    def _make_tmp(self, name, mtime_age):
        p = os.path.join(self._tmp, name)
        with open(p, "wb") as fh:
            fh.write(b"x" * 1024)
        os.utime(p, (time.time() - mtime_age, time.time() - mtime_age))
        return p

    @patch("pdf2zh.doclayout._pid_alive", return_value=False)
    def test_cleans_orphan_tmp_older_than_60s(self, _):
        from pdf2zh.doclayout import cleanup_stale_optimized_cache_tmp

        self._make_tmp("m.onnx.optimized.12345.tmp", mtime_age=3600)
        self._make_tmp(
            "m.onnx.cpu-abc123def456.optimized.99999.tmp", mtime_age=7200
        )
        n = cleanup_stale_optimized_cache_tmp(self.model)
        self.assertEqual(n, 2)
        self.assertEqual(glob.glob(os.path.join(self._tmp, "*.tmp")), [])

    def test_keeps_fresh_tmp_and_live_owner(self):
        from pdf2zh.doclayout import cleanup_stale_optimized_cache_tmp

        fresh = self._make_tmp("m.onnx.optimized.11111.tmp", mtime_age=10)
        with patch("pdf2zh.doclayout._pid_alive", return_value=True):
            live = self._make_tmp("m.onnx.optimized.22222.tmp", mtime_age=3600)
            n = cleanup_stale_optimized_cache_tmp(self.model)
        self.assertEqual(n, 0)
        self.assertTrue(os.path.exists(fresh))
        self.assertTrue(os.path.exists(live))

    def test_ignores_cached_and_lock_files(self):
        from pdf2zh.doclayout import cleanup_stale_optimized_cache_tmp

        with open(self.model + ".optimized", "wb") as fh:
            fh.write(b"x" * 2048)
        with open(self.model + ".optimized.lock", "wb") as fh:
            fh.write(b"12345")
        n = cleanup_stale_optimized_cache_tmp(self.model)
        self.assertEqual(n, 0)
        self.assertTrue(os.path.exists(self.model + ".optimized"))


class TestOnnxModelGpuNoDiskCache(unittest.TestCase):
    """GPU 显式后端在无同指纹缓存时不落盘，直接在线优化。"""

    def setUp(self):
        import pdf2zh.doclayout as dl

        self._dl = dl
        fake_model = MagicMock()
        fake_model.metadata_props = [
            MagicMock(key="stride", value="32"),
            MagicMock(key="names", value="['a']"),
        ]
        self._onnx_load = patch.object(dl.onnx, "load", return_value=fake_model)
        self._sess = patch.object(
            dl.onnxruntime,
            "InferenceSession",
            return_value=MagicMock(
                get_providers=lambda: ["CUDAExecutionProvider", "CPUExecutionProvider"]
            ),
        )
        self._fallback = patch.object(dl, "_check_session_fallback")
        self._onnx_load.start()
        self._sess.start()
        self._fallback.start()
        self._probe_cache = dl._EXEC_GPU_PROVIDERS
        dl._EXEC_GPU_PROVIDERS = None

    def tearDown(self):
        self._fallback.stop()
        self._sess.stop()
        self._onnx_load.stop()
        self._dl._EXEC_GPU_PROVIDERS = self._probe_cache
        old = get_backend()
        set_backend(old if old else "auto")

    def _busy_cache(self):
        holder = MagicMock()
        holder.acquire.return_value = None
        holder.state = "busy"
        holder.tmp_path = os.path.join(tempfile.gettempdir(), "x.tmp")
        return holder

    def _patch_opts(self):
        captured = {}
        real = self._dl._configure_session_options

        def fake_opts():
            o = real()
            captured["opts"] = o
            return o

        return (
            patch.object(self._dl, "_configure_session_options", side_effect=fake_opts),
            captured,
        )

    def test_cuda_busy_aborts_and_skips_disk_cache(self):
        set_backend("cuda")
        holder = self._busy_cache()
        opts_patch, captured = self._patch_opts()
        with opts_patch, patch.object(self._dl, "_OptimizedCache", return_value=holder):
            model = self._dl.OnnxModel("fake_model_path.onnx")
        self.assertEqual(captured["opts"].optimized_model_filepath, "")
        holder.abort.assert_called_once()
        holder.publish.assert_not_called()
        self.assertEqual(model.model_path, "fake_model_path.onnx")

    def test_dml_busy_aborts_and_skips_disk_cache(self):
        set_backend("dml")
        holder = self._busy_cache()
        opts_patch, captured = self._patch_opts()
        with patch.object(
            self._dl,
            "_exec_gpu_providers",
            return_value={"AzureExecutionProvider"},
        ), opts_patch, patch.object(self._dl, "_OptimizedCache", return_value=holder):
            self._dl.OnnxModel("fake_model_path.onnx")
        self.assertEqual(captured["opts"].optimized_model_filepath, "")
        holder.abort.assert_called_once()
        holder.publish.assert_not_called()

    def test_cpu_busy_still_writes_disk_cache(self):
        set_backend("cpu")
        holder = self._busy_cache()
        opts_patch, captured = self._patch_opts()
        with opts_patch, patch.object(self._dl, "_OptimizedCache", return_value=holder):
            self._dl.OnnxModel("fake_model_path.onnx")
        self.assertEqual(captured["opts"].optimized_model_filepath, holder.tmp_path)
        holder.publish.assert_called_once()


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
