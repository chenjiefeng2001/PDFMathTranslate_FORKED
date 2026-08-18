"""Step 2.1 — magic-pdf / MinerU 独立解析适配器（MagicPdfAdapter）单元测试。

覆盖：
- 归一化：middle.json（pdf_info/page_info）→ MagicPdfParseResult 列表；
- 文本合并：line→spans content 拼接、扁平文本块（无 lines）兼容；
- 错误路径：文件缺失（MagicPdfParseError）/ 后端未安装（NotInstalled）；
- middle.json 磁盘加载（load_middle_json）round-trip。
"""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from pdf2zh.magicpdf_adapter import (
    MagicPdfAdapter,
    MagicPdfNotInstalledError,
    MagicPdfParseError,
    load_middle_json,
)

SAMPLE_MIDDLE = {
    "pdf_info": [
        [
            {
                "type": "text",
                "bbox": [0, 0, 300, 24],
                "cls": "title",
                "lines": [
                    {
                        "bbox": [0, 0, 300, 24],
                        "spans": [
                            {"bbox": [0, 0, 180, 24], "content": "Hello", "type": "text"},
                            {"bbox": [180, 0, 300, 24], "content": " World", "type": "text"},
                        ],
                    }
                ],
            },
            {
                "type": "text",
                "bbox": [0, 30, 200, 50],
                "cls": "code",
                "content": "if x: pass",  # 扁平文本块（无 lines）
            },
        ]
    ],
    "page_info": [{"page_no": 0, "width": 612, "height": 792}],
}


class TestNormalization(unittest.TestCase):
    def test_from_middle_json(self):
        results = MagicPdfAdapter.from_middle_json(SAMPLE_MIDDLE)
        self.assertEqual(len(results), 1)
        res = results[0]
        self.assertEqual(res.page_num, 0)
        self.assertEqual(res.width, 612.0)
        self.assertEqual(res.height, 792.0)
        self.assertEqual(res.backend, "offline")
        self.assertEqual(len(res.blocks), 2)

    def test_span_content_merge(self):
        res = MagicPdfAdapter.from_middle_json(SAMPLE_MIDDLE)[0]
        self.assertEqual(res.blocks[0]["text"], "Hello World")
        spans = res.blocks[0]["lines"][0]["spans"]
        self.assertEqual([s["content"] for s in spans], ["Hello", " World"])

    def test_flat_content_block(self):
        res = MagicPdfAdapter.from_middle_json(SAMPLE_MIDDLE)[0]
        block = res.blocks[1]
        self.assertEqual(block["text"], "if x: pass")
        self.assertEqual(block["cls"], "code")

    def test_pages_filter(self):
        middle = dict(SAMPLE_MIDDLE)
        middle["pdf_info"] = middle["pdf_info"] + middle["pdf_info"]
        results = MagicPdfAdapter.from_middle_json(middle, pages=[1])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].page_num, 1)

    def test_missing_file(self):
        with self.assertRaises(MagicPdfParseError):
            MagicPdfAdapter().parse("definitely-not-exist.pdf")

    @unittest.skipIf(
        MagicPdfAdapter().is_available(), "backend installed; skip missing-engine path"
    )
    def test_missing_backend(self):
        with self.assertRaises(MagicPdfNotInstalledError):
            MagicPdfAdapter().parse(__file__)

    def test_bbox_helpers(self):
        from pdf2zh.magicpdf_adapter import _as_bbox, _as_float

        self.assertEqual(_as_bbox([1, 2, 3, 4]), [1.0, 2.0, 3.0, 4.0])
        self.assertEqual(_as_bbox([5, 6]), [0.0, 0.0, 5.0, 6.0])
        self.assertEqual(_as_bbox(None), [0.0, 0.0, 0.0, 0.0])
        self.assertEqual(_as_float("3.5"), 3.5)
        self.assertEqual(_as_float("bad", 1.0), 1.0)


class TestMiddleJsonIO(unittest.TestCase):
    def test_load_roundtrip(self):
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        ) as fh:
            json.dump(SAMPLE_MIDDLE, fh)
            path = fh.name
        try:
            loaded = load_middle_json(path)
            self.assertEqual(loaded["page_info"][0]["width"], 612)
        finally:
            os.unlink(path)

    def test_load_missing(self):
        with self.assertRaises(MagicPdfParseError):
            load_middle_json("no-such-middle.json")


class TestMagicPdfConfig(unittest.TestCase):
    """magic-pdf 配置文件自动生成（~/magic-pdf.json 缺失兜底）。"""

    def test_ensure_config_generates_when_missing(self):
        from pdf2zh.magicpdf_adapter import _ensure_magicpdf_config

        with tempfile.TemporaryDirectory() as td:
            cfg_path = os.path.join(td, "magic-pdf.json")
            with patch.dict(
                os.environ, {"MINERU_TOOLS_CONFIG_JSON": cfg_path}, clear=False
            ):
                result = _ensure_magicpdf_config(device="cuda")
            self.assertEqual(result, cfg_path)
            self.assertTrue(os.path.exists(cfg_path))
            with open(cfg_path, encoding="utf-8") as fh:
                cfg = json.load(fh)
            # torch 无 CUDA 时 device-mode 必须回退 cpu（避免 torch 模型崩溃）
            self.assertIn(cfg["device-mode"], ("cuda", "cpu"))
            self.assertTrue(cfg["layout-config"])
            self.assertIn("models-dir", cfg)
            self.assertTrue(cfg["formula-config"]["enable"])
            # 模型名必须与 magic-pdf 1.3.12 resources/model_config/model_configs.yaml
            # 的 weights 表键一致，否则 CustomPEKModel.__init__ 抛
            # KeyError: 'YOLO_v8_MFD'（解析在 DocAnalysis init 阶段必然失败）
            self.assertEqual(cfg["formula-config"]["mfd_model"], "yolo_v8_mfd")
            self.assertEqual(cfg["formula-config"]["mfr_model"], "unimernet_small")

    def test_ensure_config_models_match_magicpdf_weights(self):
        """真实环境一致性：生成的模型名必须是 magic-pdf 已知的 weights 键。

        magic-pdf 未安装时跳过（离线/CI 环境）。
        """
        try:
            import magic_pdf
            import yaml  # noqa: F401

            from magic_pdf.config.constants import MODEL_NAME
        except Exception:
            self.skipTest("magic-pdf 未安装，跳过一致性校验")
        from pdf2zh.magicpdf_adapter import (
            _MAGICPDF_MFD_MODEL,
            _MAGICPDF_MFR_MODEL,
            _ensure_magicpdf_config,
        )

        with tempfile.TemporaryDirectory() as td:
            cfg_path = os.path.join(td, "magic-pdf.json")
            with patch.dict(
                os.environ, {"MINERU_TOOLS_CONFIG_JSON": cfg_path}, clear=False
            ):
                _ensure_magicpdf_config(device="auto")
            with open(cfg_path, encoding="utf-8") as fh:
                cfg = json.load(fh)
        # 枚举值即合法字符串键（MODEL_NAME 是普通类，成员即字符串）
        self.assertEqual(MODEL_NAME.YOLO_V8_MFD, "yolo_v8_mfd")
        self.assertEqual(MODEL_NAME.UniMerNet_v2_Small, "unimernet_small")
        # 与生成配置一致
        self.assertEqual(cfg["formula-config"]["mfd_model"], MODEL_NAME.YOLO_V8_MFD)
        self.assertEqual(cfg["formula-config"]["mfr_model"], MODEL_NAME.UniMerNet_v2_Small)
        # 能从 model_configs.yaml 的 weights 表解析出相对路径（无 KeyError）
        weights_path = os.path.join(
            os.path.dirname(magic_pdf.__file__),
            "resources", "model_config", "model_configs.yaml",
        )
        with open(weights_path, encoding="utf-8") as fh:
            weights = yaml.safe_load(fh)["weights"]
        self.assertIn(_MAGICPDF_MFD_MODEL, weights)
        self.assertIn(_MAGICPDF_MFR_MODEL, weights)

    def test_ensure_models_reports_missing(self):
        """模型缺失预检：空 models_dir 应返回全部必需模型相对路径。"""
        try:
            import magic_pdf  # noqa: F401
        except Exception:
            self.skipTest("magic-pdf 未安装，跳过模型预检测试")
        from pdf2zh.magicpdf_adapter import _ensure_magicpdf_models

        with tempfile.TemporaryDirectory() as td:
            missing = _ensure_magicpdf_models(td)
        self.assertEqual(len(missing), 3)
        self.assertIn("Layout/YOLO/doclayout_yolo_docstructbench_imgsz1280_2501.pt", missing)
        self.assertIn("MFD/YOLO/yolo_v8_ft.pt", missing)
        self.assertIn("MFR/unimernet_hf_small_2503", missing)

    def test_ensure_models_defaults_to_home_dir(self):
        """models_dir 为空时预检基准 = ~/.cache/magic-pdf/models（与配置默认一致）。

        回归保护：此前 ``os.path.expanduser(\"\")`` 会把空串解析为当前工作目录，
        导致预检基准漂移、永远报模型缺失。
        """
        try:
            import magic_pdf  # noqa: F401
        except Exception:
            self.skipTest("magic-pdf 未安装，跳过模型预检基准测试")
        import pdf2zh.magicpdf_adapter as mpa

        home_base = os.path.join(
            os.path.expanduser("~"), ".cache", "magic-pdf", "models"
        )
        norm = lambda p: p.replace("\\", "/")
        prefix = norm(home_base) + "/"
        probed: list[str] = []
        real_exists = os.path.exists

        def fake_exists(p):
            probed.append(p)
            if isinstance(p, str) and norm(p).startswith(prefix):
                return True  # 模拟模型已下载到默认家目录
            return real_exists(p)

        with patch.object(mpa.os.path, "exists", side_effect=fake_exists):
            missing = mpa._ensure_magicpdf_models("")
        self.assertEqual(missing, [])  # 全部命中 → 无缺失
        self.assertTrue(any(norm(p).startswith(prefix) for p in probed))

    def test_parse_magicpdf_model_precheck_fails_fast(self):
        """模型缺失时 parse() 秒级抛带下载指引的 MagicPdfParseError。

        验证修复 5 的熔断路径：预检命中后不再进入 doc_analyze 空跑数十秒。
        """
        try:
            import magic_pdf  # noqa: F401
        except Exception:
            self.skipTest("magic-pdf 未安装，跳过模型预检熔断测试")
        import pdf2zh.magicpdf_adapter as mpa

        tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        tmp.write(b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n")
        tmp.close()
        try:
            with tempfile.TemporaryDirectory() as td:
                cfg_path = os.path.join(td, "magic-pdf.json")
                with patch.dict(
                    os.environ,
                    {"MINERU_TOOLS_CONFIG_JSON": cfg_path},
                    clear=False,
                ):
                    with patch.object(
                        mpa, "_ensure_magicpdf_models", return_value=["MFD/YOLO/yolo_v8_ft.pt"]
                    ):
                        adapter = MagicPdfAdapter(device="cpu", models_dir="")
                        with self.assertRaises(MagicPdfParseError) as ctx:
                            adapter.parse(tmp.name)
            self.assertIn("PDF-Extract-Kit", str(ctx.exception))
        finally:
            os.unlink(tmp.name)

    def test_ensure_config_keeps_existing(self):
        from pdf2zh.magicpdf_adapter import _ensure_magicpdf_config

        with tempfile.TemporaryDirectory() as td:
            cfg_path = os.path.join(td, "magic-pdf.json")
            with open(cfg_path, "w", encoding="utf-8") as fh:
                json.dump({"device-mode": "cpu", "custom": True}, fh)
            before = os.path.getmtime(cfg_path)
            with patch.dict(
                os.environ, {"MINERU_TOOLS_CONFIG_JSON": cfg_path}, clear=False
            ):
                _ensure_magicpdf_config(device="cuda")
            with open(cfg_path, encoding="utf-8") as fh:
                cfg = json.load(fh)
            self.assertEqual(cfg.get("custom"), True)
            self.assertEqual(os.path.getmtime(cfg_path), before)


if __name__ == "__main__":
    unittest.main()
