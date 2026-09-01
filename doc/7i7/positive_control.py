# -*- coding: utf-8 -*-
"""7I-7C — Positive control (SHIM OFF) on the real failing book.

Two things are proven here that the unit-level reproducer alone cannot:

1. The REAL book (Matrix Algebra) actually produces unicode typesetting units
   whose ``xobj_id`` is ``None`` (i.e. the failure path is genuinely reached,
   not just theoretically possible).
2. With the shim disabled (``PDF2ZH_BABELDOC_XOBJ_SHIM=0``) native BabelDOC
   raises the exact ``Xobj id must be provided...`` assertion — the positive
   control for the 7I-7C fix.

``TypesettingUnit.__init__`` is wrapped to *record* every call (unicode?,
xobj_id) to a JSON log **before** invoking the original, so even if BabelDOC
swallows the assertion internally, the evidence is on disk.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# SHIM OFF — native BabelDOC behavior.  Must be set before any pdf2zh import.
os.environ["PDF2ZH_BABELDOC_XOBJ_SHIM"] = "0"

BOOK = (
    "tests/file/Matrix Algebra (Abadir K.M., Magnus J.R.) "
    "(z-library.sk, 1lib.sk, z-lib.sk).pdf"
)
RECORD = Path("/tmp/7i7_posctl_record.json")
LOG = Path("/tmp/7i7_posctl.log")

PAGE = int(os.environ.get("POSCTL_PAGE", "4"))
RUN_ID = os.environ.get("POSCTL_RUN", "m4")


def _log(msg: str) -> None:
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(msg + "\n")


def _record(row: dict) -> None:
    with RECORD.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def install_recorder() -> None:
    from babeldoc.format.pdf.document_il.midend.typesetting import (
        TypesettingUnit,
    )

    original = TypesettingUnit.__init__

    def _recording(self, *args, **kwargs) -> None:
        _record(
            {
                "run": RUN_ID,
                "unicode": kwargs.get("unicode"),
                "xobj_id": kwargs.get("xobj_id"),
            }
        )
        original(self, *args, **kwargs)

    TypesettingUnit.__init__ = _recording
    _log(f"[{RUN_ID}] recorder installed on TypesettingUnit.__init__")


def main() -> int:
    _log(f"[{RUN_ID}] start page={PAGE} book={Path(BOOK).name}")
    install_recorder()

    from babeldoc.format.pdf.high_level import async_translate as yadt_at
    from babeldoc.format.pdf.high_level import init as yadt_init
    from babeldoc.format.pdf.translation_config import TranslationConfig as YadtCfg
    from babeldoc.format.pdf.translation_config import WatermarkOutputMode

    yadt_init()

    from babeldoc.translator.translator import BaseTranslator as YadtBT

    class _Stub(YadtBT):
        name = "posctl7i7"

        def __init__(self, lang_in: str, lang_out: str) -> None:
            self.lang_in = lang_in
            self.lang_out = lang_out
            self.ignore_cache = True
            self.translate_call_count = 0
            self.translate_cache_call_count = 0

        def do_translate(self, text: str, rate_limit_params: dict = None) -> str:
            cjk = ""
            for ch in text:
                if ch.isascii() and ch.isalpha():
                    cjk += chr(0x4E00 + (ord(ch) % 0x1F00))
                elif ch == " ":
                    cjk += "　"
                else:
                    cjk += ch
            return cjk

        def do_llm_translate(self, text: str, rate_limit_params=None) -> str:
            raise NotImplementedError

        def llm_translate(self, text: str, rate_limit_params=None) -> str:
            return self.translate(text)

    translator = _Stub("en", "zh")
    out = ROOT / "doc" / "7i7" / "out_posctl"
    cfg = YadtCfg(
        translator=translator,
        input_file=str(ROOT / BOOK),
        font="",
        pages=str(PAGE),
        output_dir=str(out),
        doc_layout_model=None,
        debug=True,
        lang_in="en",
        lang_out="zh",
        no_dual=False,
        no_mono=False,
        use_rich_pbar=False,
        watermark_output_mode=WatermarkOutputMode.NoWatermark,
        use_alternating_pages_dual=True,
        auto_extract_glossary=False,
    )
    try:
        import asyncio

        async def _run() -> None:
            async for _ev in yadt_at(cfg):
                pass

        asyncio.run(_run())
        _log(f"[{RUN_ID}] finished WITHOUT assertion (unexpected for SHIM OFF)")
        return 0
    except BaseException as exc:  # noqa: BLE001 -- capture every kind
        _log(f"[{RUN_ID}] raised {type(exc).__name__}: {exc}")
        _log(traceback.format_exc())
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
