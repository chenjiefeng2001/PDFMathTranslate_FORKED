# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec：pdf2zh REST/SSE sidecar 单文件可执行。
#
# 构建（仓库根目录执行）：
#   pyinstaller deploy/pdf2zh-api-sidecar.spec
# 产物：
#   dist/pdf2zh-api-sidecar.exe
#
# 说明：
# - pdf2zh.translator 在 API 的 /api/engines 处理器内延迟导入，modulegraph 可
#   静态追踪；babeldoc/magicpdf 等可选链路按需 hiddenimports。
# - onnxruntime/pymupdf 由各自的 PyInstaller hook 自动收集二进制。

a = Analysis(
    ['pdf2zh_sidecar.py'],
    pathex=['..'],
    binaries=[],
    datas=[],
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
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='pdf2zh-api-sidecar',
    debug=False,
    strip=False,
    upx=False,
    console=True,
)
