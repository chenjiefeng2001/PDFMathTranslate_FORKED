# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec：pdf2zh REST/SSE sidecar（onedir 目录形态）。
#
# 构建（仓库根目录执行）：
#   python -m PyInstaller deploy/pdf2zh-api-sidecar.spec \
#       --workpath deploy/_build_sidecar/_work --distpath deploy/_build_sidecar/dist --noconfirm
# 产物：
#   deploy/_build_sidecar/dist/pdf2zh-api-sidecar/   （onedir 目录）
#
# 为什么是 onedir 而不是 onefile：
# - NSIS/makensis 对 >2GB 的单文件无法 mmap，onefile 产物（~2.3GB）会直接
#   导致 Tauri NSIS 打包失败；onedir 把体积摊到大量小文件上，规避该限制，
#   同时免去 onefile 每次启动的自解压开销。
#
# 说明：
# - pdf2zh.translator 在 API 的 /api/engines 处理器内延迟导入，modulegraph 可
#   静态追踪；babeldoc/magicpdf 等可选链路按需 hiddenimports。
# - onnxruntime/pymupdf 由各自的 PyInstaller hook 自动收集二进制。
#
# BabelDOC 链路补收（否则 frozen 环境运行时 ModuleNotFoundError，被
# babeldoc_adapter 包装成误导性的 "BabelDOC engine not available"）：
# - collect_submodules('babeldoc')：document_il/new_parser 等子包大量使用
#   模块内延迟导入，modulegraph 只能追到部分；
# - xsdata：pikepdf 的 XMP 元数据链路依赖，其内部动态导入使 modulegraph
#   只收走零散子模块（实测 _internal/xsdata 整体缺失）；
# - skimage.metrics：skimage 用 __getattr__ 懒加载子模块，
#   detect_scanned_file 顶层 `from skimage.metrics import
#   structural_similarity 在 frozen 环境解析失败；
# - pyzstd：babeldoc.progress_monitor 顶层导入的 C 扩展包；
# - copy_metadata('babeldoc)：任何 importlib.metadata.version("babeldoc")
#   调用都需要 dist-info。

from PyInstaller.utils.hooks import collect_submodules, copy_metadata

import sysconfig
from pathlib import Path

# hyperscan 为 delvewheel 修补 wheel：_hs_ext.pyd 依赖同级 `hyperscan.libs`
# 目录内哈希后缀的 msvcp140 DLL；PyInstaller 只收 pyd 不收该兄弟目录，
# 导致 frozen 环境 `import hyperscan`（babeldoc.glossary 顶层导入）报
# "DLL load failed while importing _hs_ext"。按原始相对布局补收。
_SITE = Path(sysconfig.get_paths()["purelib"])

a = Analysis(
    ['pdf2zh_sidecar.py'],
    pathex=['..'],
    binaries=[
        (str(_SITE / "hyperscan.libs"), "hyperscan.libs"),
    ],
    datas=[
        *copy_metadata('babeldoc'),
        # tiktoken 经 importlib.metadata entry_points 加载编码插件
        # （tiktoken_ext.openai_public），缺 dist-info 会报
        # "Unknown encoding o200k_base. Plugins found: []"
        *copy_metadata('tiktoken'),
    ],
    hiddenimports=[
        'pdf2zh.services.api',
        'pdf2zh.services.runtime_service',
        'pdf2zh.services.runtime_singleton',
        'pdf2zh.translator',
        'pdf2zh.converter',
        'pdf2zh.high_level',
        'uvicorn.logging',
        'uvicorn.loops.auto',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.lifespan.on',
        *collect_submodules('babeldoc'),
        *collect_submodules('xsdata'),
        *collect_submodules('skimage.metrics'),
        *collect_submodules('bitstring'),   # babeldoc 链路懒加载 bitstore_*
        *collect_submodules('tiktoken_ext'),
        'tiktoken',
        'pyzstd',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        'tkinter', 'matplotlib', 'IPython', 'pytest',
        'gradio', 'gradio_pdf',           # GUI 栈不进 sidecar
        'magicpdf', 'magic_pdf',          # OCR 解析链路按需另行打包
        'torch',
    ],
    noarchive=False,
)

# excludes 只挡住 torch 的纯 Python 模块，hook 仍会把 ~2GB 的 torch/torchvision
# 二进制（CUDA/cuDNN DLL）与 dist-info 元数据收进产物。sidecar 链路（REST/SSE +
# babeldoc + doclayout ONNX）不经过这些库：babeldoc 无 torch 导入；magicpdf OCR
# 与 GUI 已整体排除；doclayout 在 CUDA 缺失时由 ORT 自动回退 CPU。此处显式剔除，
# 否则安装器体积会突破 NSIS/makensis 的 2GB 硬限制，且其 dist-info 深层许可证
# 路径会触发 makensis 的 MAX_PATH 限制。
def _torch_blocked(dest_name: str) -> bool:
    head = dest_name.replace('\\', '/').lower().split('/')[0]
    return head.split('-', 1)[0] in ('torch', 'torchvision', 'torchaudio')


a.binaries = [b for b in a.binaries if not _torch_blocked(b[0])]
a.datas = [d for d in a.datas if not _torch_blocked(d[0])]
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='pdf2zh-api-sidecar',
    debug=False,
    strip=False,
    upx=False,
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='pdf2zh-api-sidecar',
)
