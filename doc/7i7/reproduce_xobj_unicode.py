# -*- coding: utf-8 -*-
"""7I-7A — Reproduce ``Xobj id must be provided when unicode is provided``.

Drives the real BabelDOC engine (``babeldoc.format.pdf.high_level.async_translate``)
over the two reported books with a stub (identity) translator so the failure
reproduces offline without any network translation service.

The assertion lives in BabelDOC's ``TypesettingUnit.__init__``:
    if unicode:
        assert xobj_id is not None, "Xobj id must be provided when unicode is provided"

``xobj_id`` flows from ``paragraph.xobj_id`` — assigned by ``on_xobj_begin`` only
while the PDF interpreter is inside a Form XObject.  A ``None`` means unicode
typesetting units were built for text that the frontend never attributed to an
XObject container.

Usage:  python doc/7i7/reproduce_xobj_unicode.py [--page N]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

BOOKS = [
    "tests/file/Matrix Algebra (Abadir K.M., Magnus J.R.) "
    "(z-library.sk, 1lib.sk, z-lib.sk).pdf",
    "tests/file/Groups and Symmetries From Finite Groups to Lie Groups, "
    "Second Edition (Yvette Kosmann-Schwarzbach) "
    "(z-library.sk, 1lib.sk, z-lib.sk).pdf",
]


def make_stub_babeldoc_translator():
    """A BabelDOC-contract translator that returns input unchanged.

    Identity passthrough still exercises the full parse → IL → typeset →
    render pipeline, which is where the XObject/unicode assertion lives.
    """
    from babeldoc.translator.translator import BaseTranslator as YadtBaseTranslator

    class _Stub(YadtBaseTranslator):
        name = "stub7i7"

        def __init__(self, lang_in: str, lang_out: str) -> None:
            self.lang_in = lang_in
            self.lang_out = lang_out
            self.ignore_cache = True
            # BabelDOC 0.6.x BaseTranslator.translate touches these counters.
            self.translate_call_count = 0
            self.translate_cache_call_count = 0

        # NOTE: do NOT override ``translate`` — BaseTranslator.translate
        # routes through ``do_translate`` (with rate_limit_params), which is
        # where the CJK mapping lives. Overriding translate to return input
        # would make translated == input and BabelDOC would skip the unicode
        # composition entirely (no XObject path exercised).

        def do_translate(self, text: str, rate_limit_params: dict = None) -> str:
            # Emulate a REAL translation: map ASCII to CJK so the pipeline must
            # emit unicode typesetting units (the XObject/unicode path).
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
            # Matches pdf2zh's babeldoc bridge: LLM prompt path disabled,
            # BabelDOC falls back to the plain translate path.
            raise NotImplementedError

        def llm_translate(self, text: str, rate_limit_params=None) -> str:
            return self.translate(text)

    return _Stub


async def run_book(path: str, page: int | None, out_dir: Path) -> None:
    from babeldoc.format.pdf.high_level import async_translate as yadt_async_translate
    from babeldoc.format.pdf.high_level import init as yadt_init
    from babeldoc.format.pdf.translation_config import TranslationConfig as YadtConfig
    from babeldoc.format.pdf.translation_config import WatermarkOutputMode

    # 7I-7C: install the xobj_id normalization shim before the run (same
    # code path the production adapter uses).
    from pdf2zh.babeldoc_xobj_shim import apply_babeldoc_xobj_shim

    apply_babeldoc_xobj_shim()
    yadt_init()
    translator = make_stub_babeldoc_translator()("en", "zh")

    cfg = YadtConfig(
        translator=translator,
        input_file=str(ROOT / path),
        font="",
        pages=str(page) if page is not None else None,
        output_dir=str(out_dir),
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
    async for _ev in yadt_async_translate(cfg):
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--page", type=int, default=None)
    ap.add_argument("--book", type=int, default=None, help="0 or 1; default both")
    args = ap.parse_args()

    books = [BOOKS[args.book]] if args.book is not None else BOOKS
    failed = 0
    for b in books:
        out = ROOT / "doc" / "7i7" / "out"
        out.mkdir(parents=True, exist_ok=True)
        print(f"\n=== {Path(b).name} page={args.page} ===", flush=True)
        try:
            asyncio.run(run_book(b, args.page, out))
            print("  OK (no XObject/unicode assertion)", flush=True)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAILED {type(exc).__name__}: {exc}", flush=True)
            traceback.print_exc()
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
