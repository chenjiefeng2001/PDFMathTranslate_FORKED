# -*- coding: utf-8 -*-
"""7J-3C — minimal reproducer for Case B special code-point loss.

Builds a tiny one-page PDF containing the three historical victims
(ï U+00EF, ► U+25BA, → U+2192, — U+2014) plus normal text, then runs the
current babeldoc 0.6.4 pipeline with a stub translator that CHANGES the
text (ASCII->CJK) but PRESERVES non-ASCII characters. Inspect the fresh
mono output: do the specials survive typesetting, or become NUL?

Layer lock:
  - preserved      -> loss is upstream of typesetting (translation string /
                      old stack), not the current emitter
  - NUL reproduced -> isolate typesetting next (feed the IL string directly)
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

SPECIALS = "Ana\u00efs Wheeler \u2014\u25ba Rn \u2192 2 test"


FONTFILE = r"C:/Windows/Fonts/seguisym.ttf"  # Segoe UI Symbol: has ï — ► → glyphs


def make_source_pdf(path: Path) -> None:
    # Base-14 fonts (helv) only cover WinAnsi, so — ► → would be silently
    # replaced at PDF creation. Embed a real font with the target glyphs.
    doc = pymupdf.open()
    page = doc.new_page(width=360, height=140)
    page.insert_font(fontname="seg", fontfile=FONTFILE)
    page.insert_text((40, 55), SPECIALS, fontname="seg", fontsize=14)
    page.insert_text((40, 85), "plain ascii line one two three", fontname="seg", fontsize=14)
    doc.save(str(path))
    doc.close()


def verify_source(path: Path) -> bool:
    doc = pymupdf.open(str(path))
    text = doc[0].get_text()
    doc.close()
    ok = all(ch in text for ch in "\u00ef\u2014\u25ba\u2192")
    print(f"source PDF text: {text!r}  specials_encoded={ok}")
    return ok


def make_stub_preserve_translator():
    """ASCII->CJK mapping that leaves non-ASCII code points untouched."""
    from babeldoc.translator.translator import BaseTranslator as YadtBaseTranslator

    class _Stub(YadtBaseTranslator):
        name = "stub7j3c"

        def __init__(self, lang_in: str, lang_out: str, log_path: Path | None = None) -> None:
            self.lang_in = lang_in
            self.lang_out = lang_out
            self.ignore_cache = True
            self.translate_call_count = 0
            self.translate_cache_call_count = 0
            self._log_path = log_path

        def _log(self, tag: str, text: str) -> None:
            if self._log_path is None:
                return
            with open(self._log_path, "a", encoding="utf-8") as fh:
                fh.write(f"{tag}\t{text!r}\n")  # noqa: PLE1205

        def do_translate(self, text: str, rate_limit_params: dict = None) -> str:
            # KV: keep rich-text/formula placeholder tokens (<b1>, </b2> ...)
            # verbatim so BabelDOC's restore step can match them.
            import re as _re

            self._log("INPUT ", text)
            keep = _re.compile(r"</?b\d+>|\[{1,2}[^]]*?]{1,2}|%[sd]|\{\w+\}")
            out = []
            i = 0
            while i < len(text):
                m = keep.match(text, i)
                if m:
                    out.append(m.group(0))
                    i = m.end()
                    continue
                ch = text[i]
                if ch.isascii() and ch.isalpha():
                    out.append(chr(0x4E00 + (ord(ch) % 0x1F00)))
                elif ch == " ":
                    out.append("\u3000")
                else:
                    out.append(ch)  # preserve ► ï → — and friends
                i += 1
            result = "".join(out)
            self._log("OUTPUT", result)
            return result

        def do_llm_translate(self, text: str, rate_limit_params=None) -> str:
            raise NotImplementedError

        def llm_translate(self, text: str, rate_limit_params=None) -> str:
            return self.translate(text)

    return _Stub


async def run_translate(work: Path, out_dir: Path) -> None:
    from babeldoc.format.pdf.high_level import async_translate as yadt_async_translate
    from babeldoc.format.pdf.high_level import init as yadt_init
    from babeldoc.format.pdf.translation_config import TranslationConfig as YadtConfig
    from babeldoc.format.pdf.translation_config import WatermarkOutputMode

    from pdf2zh.babeldoc_xobj_shim import apply_babeldoc_xobj_shim

    apply_babeldoc_xobj_shim()
    yadt_init()
    translator = make_stub_preserve_translator()("en", "zh", log_path=out_dir / "translator.log")
    cfg = YadtConfig(
        translator=translator,
        input_file=str(work),
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


def inspect(out_dir: Path) -> int:
    monos = sorted(out_dir.glob("*mono.pdf"))
    if not monos:
        print("no mono output")
        return 1
    path = monos[0]
    doc = pymupdf.open(str(path))
    page = doc[0]
    text = page.get_text()
    print("=== fresh mono p1 text ===")
    print(repr(text))
    nul = text.count("\x00")
    print(f"NUL count: {nul}")
    kept = {ch for ch in "\u00ef\u25ba\u2192\u2014" if ch in text}
    print("specials still present:", {hex(ord(c)): c for c in kept})
    # per-char trace for specials
    tr = page.get_texttrace()
    items = tr if isinstance(tr, list) else tr.get("glyph_info", [])
    for span in items:
        for c in span["chars"]:
            u, gid = c[0], c[1]
            if u in (0xEF, 0x25BA, 0x2192, 0x2014, 0) or gid == 0:
                print(f"  trace unicode=U+{u:04X} gid={gid} font={span['font']}")
    doc.close()
    return 0


def main() -> int:
    work_dir = ROOT / "doc" / "7j3c" / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    inp = work_dir / "case_b_input.pdf"
    out = work_dir / "out"
    out.mkdir(parents=True, exist_ok=True)
    make_source_pdf(inp)
    if not verify_source(inp):
        print("source PDF lost specials at creation - aborting")
        return 2
    try:
        asyncio.run(run_translate(inp, out))
    except Exception as exc:  # noqa: BLE001
        print(f"translate error {type(exc).__name__}: {exc}")
    return inspect(out)


if __name__ == "__main__":
    raise SystemExit(main())
