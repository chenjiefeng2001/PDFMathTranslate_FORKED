# -*- coding: utf-8 -*-
"""模拟 PyInstaller excludes 效果，验证排除候选模块后运行时链路完好。

机制：在 sys.path 上放一个 shim 目录，为每个候选 exclude 模块生成一个
抛 ImportError 的假模块文件。这等价于 PyInstaller 的 excludes（导入即失败），
比 monkeypatch sys.modules 更接近真实冻结环境。
"""
import os
import shutil
import subprocess
import sys
import tempfile

CANDIDATES = [
    "polars",
    "transformers",
    "tokenizers",
    "hf_xet",
    "huggingface_hub",
    "boto3",
    "botocore",
    "s3transfer",
    "tensorrt",
    "safetensors",
    "matplotlib",
]

SMOKE_SNIPPETS = {
    "sklearn_cluster_dbscan": (
        "from sklearn.cluster import DBSCAN;"
        "import numpy as np;"
        "X = np.array([[0,0],[1,1],[9,9]]);"
        "print('DBSCAN labels:', DBSCAN(eps=2).fit_predict(X).tolist())"
    ),
    "pandas_io": (
        "import pandas as pd; print('pandas', pd.__version__)"
    ),
    "babeldoc_import": (
        "import babeldoc; print('babeldoc', babeldoc.__version__)"
    ),
    "babeldoc_highlevel": (
        "from babeldoc.format.pdf.high_level import async_translate, translate;"
        "print('babeldoc high_level OK')"
    ),
    "babeldoc_doclayout": (
        "from babeldoc.docvision.doclayout import OnnxModel;"
        "import onnxruntime as ort;"
        "print('doclayout OK, providers:', ort.get_available_providers())"
    ),
    "pdf2zh_core": (
        "import pdf2zh; print('pdf2zh', pdf2zh.__version__ if hasattr(pdf2zh,'__version__') else 'OK')"
    ),
    "pdf2zh_high_level": (
        "from pdf2zh.high_level import translate; print('pdf2zh.high_level OK')"
    ),
    "pdf2zh_translator": (
        "import pdf2zh.translator; print('pdf2zh.translator OK')"
    ),
    "pdf2zh_sidecar_modules": (
        "import pdf2zh.services.api, pdf2zh.services.runtime_service, "
        "pdf2zh.services.runtime_singleton; print('sidecar services OK')"
    ),
    "pdf2zh_babeldoc_adapter": (
        "import pdf2zh.babeldoc_adapter, pdf2zh.babeldoc_next_adapter, "
        "pdf2zh.babeldoc_onnx_backend; print('babeldoc adapters OK')"
    ),
}


def build_shim(shim_dir):
    for mod in CANDIDATES:
        p = os.path.join(shim_dir, mod + ".py")
        with open(p, "w", encoding="utf-8") as f:
            f.write(
                "# generated exclude shim\n"
                "raise ImportError('simulated exclude of %s')\n" % mod
            )
        sub = os.path.join(shim_dir, mod)
        if os.path.isdir(sub):
            shutil.rmtree(sub)


def main():
    shim_dir = tempfile.mkdtemp(prefix="excl-shim-")
    try:
        build_shim(shim_dir)
        env = dict(os.environ)
        env["PYTHONPATH"] = shim_dir + os.pathsep + env.get("PYTHONPATH", "")
        env["PYTHONUTF8"] = "1"
        failed = 0
        for name, code in SMOKE_SNIPPETS.items():
            print("==== %s ====" % name)
            proc = subprocess.run(
                [sys.executable, "-c", code],
                env=env,
                capture_output=True,
                text=True,
            )
            if proc.returncode == 0:
                print("  OK: %s" % proc.stdout.strip().splitlines()[-1])
            else:
                failed += 1
                tail = (proc.stderr or "").strip().splitlines()
                print("  FAIL (%s)" % proc.returncode)
                for ln in tail[-8:]:
                    print("   | %s" % ln)
        print("=" * 40)
        print("summary: %d/%d passed" % (len(SMOKE_SNIPPETS) - failed, len(SMOKE_SNIPPETS)))
        return 1 if failed else 0
    finally:
        shutil.rmtree(shim_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
