"""7K-1C — E2E translation of the synthetic annotation corpus.

Runs the same offline stub-CJK translator as 7J-3B/7J-3C, then inspects
the mono/dual outputs for any surviving annotations or links.  Evidence
for the 7K-1 first-divergence matrix (source -> output).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

CORPUS = ROOT / "doc" / "7k1" / "annotation_corpus.pdf"


def make_stub_babeldoc_translator():
    """BabelDOC-contract translator that maps ASCII->CJK (real text change)."""
    from babeldoc.translator.translator import BaseTranslator as YadtBaseTranslator

    class _Stub(YadtBaseTranslator):
        name = "stub7k1"

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
                    cjk += "\u3000"
                else:
                    cjk += ch
            return cjk

        def do_llm_translate(self, text: str, rate_limit_params=None) -> str:
            raise NotImplementedError

        def llm_translate(self, text: str, rate_limit_params=None) -> str:
            return self.translate(text)

    return _Stub


async def run(out_dir: Path) -> None:
    from babeldoc.format.pdf.high_level import async_translate as yadt_async_translate
    from babeldoc.format.pdf.high_level import init as yadt_init
    from babeldoc.format.pdf.translation_config import TranslationConfig as YadtConfig
    from babeldoc.format.pdf.translation_config import WatermarkOutputMode

    from pdf2zh.babeldoc_xobj_shim import apply_babeldoc_xobj_shim

    apply_babeldoc_xobj_shim()
    yadt_init()
    translator = make_stub_babeldoc_translator()("en", "zh")

    cfg = YadtConfig(
        translator=translator,
        input_file=str(CORPUS),
        font="",
        pages=None,
        output_dir=str(out_dir),
        doc_layout_model=None,
        debug=False,
        lang_in="en",
        lang_out="zh",
        no_dual=False,
        no_mono=False,
        use_rich_pbar=False,
        watermark_output_mode=WatermarkOutputMode.NoWatermark,
        use_alternating_pages_dual=True,
        auto_extract_glossary=False,
        skip_scanned_detection=True,
    )
    async for _ev in yadt_async_translate(cfg):
        pass


def main() -> int:
    out = ROOT / "doc" / "7k1" / "out_e2e"
    out.mkdir(parents=True, exist_ok=True)
    try:
        asyncio.run(run(out))
        print("E2E done", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"E2E error {type(exc).__name__}: {exc}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
