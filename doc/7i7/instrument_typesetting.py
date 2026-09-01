# -*- coding: utf-8 -*-
"""7I-7B — Instrument BabelDOC's typesetting to observe xobj_id.

Patches ``TypesettingUnit.__init__`` to log every unicode unit's ``xobj_id``
before the assertion, so we can see whether page-level text yields ``None``.

Usage: python doc/7i7/instrument_typesetting.py --book N --page M
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "doc" / "7i7"))

from reproduce_xobj_unicode import BOOKS, make_stub_babeldoc_translator  # noqa: E402

_observed = {"none": 0, "zero": 0, "other": 0, "samples_none": []}
_saved_init = None


def _patched_init(self, **kw):
    if kw.get("unicode") is not None:
        x = kw.get("xobj_id")
        if x is None:
            _observed["none"] += 1
            if len(_observed["samples_none"]) < 5:
                _observed["samples_none"].append(
                    (kw.get("unicode", "?")[:1], kw.get("font_size"))
                )
        elif x == 0:
            _observed["zero"] += 1
        else:
            _observed["other"] += 1
    return _saved_init(self, **kw)


def install_patch():
    global _saved_init
    import babeldoc.format.pdf.document_il.midend.typesetting as ts

    _saved_init = ts.TypesettingUnit.__init__
    ts.TypesettingUnit.__init__ = _patched_init


async def run_book(path: str, page: int | None, out_dir: Path) -> None:
    from babeldoc.format.pdf.high_level import async_translate as yadt_async_translate
    from babeldoc.format.pdf.high_level import init as yadt_init
    from babeldoc.format.pdf.translation_config import TranslationConfig as YadtConfig
    from babeldoc.format.pdf.translation_config import WatermarkOutputMode

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
    import json

    async for ev in yadt_async_translate(cfg):
        if isinstance(ev, dict) and ev.get("type") in ("error", "finish"):
            print(f"  EVENT {ev.get('type')}: {str(ev)[:300]}", flush=True)
        if isinstance(ev, dict) and ev.get("type") == "error":
            print(
                "  OBSERVED " + json.dumps(_observed, ensure_ascii=False),
                flush=True,
            )


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--book", type=int, default=0)
    ap.add_argument("--page", type=int, default=None)
    args = ap.parse_args()

    install_patch()
    out = ROOT / "doc" / "7i7" / "out"
    out.mkdir(parents=True, exist_ok=True)
    b = BOOKS[args.book]
    print(f"=== {Path(b).name} page={args.page} ===", flush=True)
    try:
        asyncio.run(run_book(b, args.page, out))
        print("  completed; observed:", _observed, flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"  FAILED {type(exc).__name__}: {exc}", flush=True)
        import traceback

        traceback.print_exc()
        print("  observed so far:", _observed, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
