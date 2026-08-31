"""dual_forensics — 7H-1 Dual Fidelity Forensics.

A differential diagnostic toolchain that answers, for a visual defect observed
in a generated Dual PDF, *at which stage it first appears* (the **First
Divergence Stage**, or FDS).  For each analysed source page it snapshots the
whole evidence chain

    source PDF page
      ├── parser evidence        (LTChar stream → canonical page tree)
      ├── document-model evidence (``p{page}_{i}`` blocks: kind/role/style)
      ├── translation evidence   (source → translated, per unit)
      ├── layout-plan evidence   (src_box / dst_box / font_size / render_path)
      └── rendered-PDF evidence  (actual text/draw objects read back)

then **matches** rendered objects back to source blocks and, for each candidate
defect (F1–F10), walks source→parser→model→translation→layout→render→PDF to
record the first stage whose evidence already shows the divergence.

Pure analysis: never re-lays-out, never mutates a plan, never re-renders; it
only *reads*.  Run from the repo root::

    python -m dual_forensics \\
        --source tests/file/xxx.pdf \\
        --dual   pdf2zh_files/xxx-dual.pdf \\
        --page 77 \\
        --out   forensic-report/
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
