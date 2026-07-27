# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for pdf2zh stand-alone EXE.
Build:  pyinstaller build_exe.spec
"""
from PyInstaller.utils.hooks import collect_all, copy_metadata

a = Analysis(
    ['pdf2zh/pdf2zh.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        'pdf2zh', 'pdf2zh.high_level', 'pdf2zh.converter', 'pdf2zh.converter_docx',
        'pdf2zh.translator', 'pdf2zh.font_resolver', 'pdf2zh.font_cache',
        'pdf2zh.text_metrics', 'pdf2zh.paragraph_layout', 'pdf2zh.layout_graph',
        'pdf2zh.scan_pdf_processor', 'pdf2zh.collision_resolver', 'pdf2zh.pdf_op_builder',
        'pdf2zh.translation_cache', 'pdf2zh.overlay_renderer', 'pdf2zh.cache',
        'pdf2zh.config', 'pdf2zh.doclayout', 'pdf2zh.backend', 'pdf2zh.gui',
        'pdf2zh.mcp_server', 'pdf2zh.pdfinterp',
        'pdf2zh.kernel', 'pdf2zh.kernel.legacy', 'pdf2zh.kernel.precise',
        'pdf2zh.kernel.protocol', 'pdf2zh.kernel.registry', 'pdf2zh.kernel.v2_bridge',
        'pdf2zh.kernel.v2_worker',
        'networkx', 'rtree',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['matplotlib', 'scipy.spatial'],
    noarchive=False,
)

for pkg in ['pdf2zh', 'gradio', 'gradio_pdf', 'fontTools', 'peewee']:
    try:
        d, b, h = collect_all(pkg, include_py_files=True)
        a.datas += d; a.binaries += b; a.hiddenimports.extend(h)
    except Exception:
        pass

for pkg in ['pdf2zh', 'peewee', 'gradio', 'pymupdf', 'pikepdf']:
    try:
        a.datas += copy_metadata(pkg)
    except Exception:
        pass

pyz = PYZ(a.pure, a.zipped_data)
exe = EXE(pyz, a.scripts, a.binaries, a.zipfiles, a.datas, [],
          name='pdf2zh', debug=False, bootloader_ignore_signals=False,
          strip=False, upx=False, console=True, disable_windowed_traceback=False,
          argv_emulation=False, target_arch=None, icon=None)
