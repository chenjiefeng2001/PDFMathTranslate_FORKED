# -*- coding: utf-8 -*-
"""验证新构建产物：排除项已消失、体积、onnxruntime/capi 构成。"""
import os

root = r"c:\Users\14977\source\repos\PDFMathTranslate_FORKED\deploy\_build_sidecar\dist\pdf2zh-api-sidecar"


def dsize(p):
    t = 0
    for r, ds, fs in os.walk(p):
        for f in fs:
            try:
                t += os.path.getsize(os.path.join(r, f))
            except OSError:
                pass
    return t


total = dsize(root)
print("== 新产物总大小: {:.1f} MB ==".format(total / 1048576))
internal = os.path.join(root, "_internal")

print("== 应排除项检查（不应存在） ==")
bad = [
    "_polars_runtime_32", "transformers", "tokenizers", "hf_xet",
    "botocore", "boto3", "s3transfer", "safetensors", "huggingface_hub",
]
for b in bad:
    hit = os.path.isdir(os.path.join(internal, b))
    print("  {:22s}: {}".format(b, "!!! 仍在包内 !!!" if hit else "OK 已排除"))

capi = os.path.join(internal, "onnxruntime", "capi")
print("== onnxruntime/capi 内容 ==")
for f in sorted(os.listdir(capi)):
    p = os.path.join(capi, f)
    print("  {}: {:.1f} MB".format(f, os.path.getsize(p) / 1048576))

print("== 新产物 top 12 ==")
items = []
for name in os.listdir(internal):
    p = os.path.join(internal, name)
    items.append((dsize(p) if os.path.isdir(p) else os.path.getsize(p), name))
items.sort(reverse=True)
for s, n in items[:12]:
    print("  {:9.1f} MB  {}".format(s / 1048576, n))
