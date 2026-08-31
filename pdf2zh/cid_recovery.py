"""cid_recovery — font-aware ``(font, cid) → Unicode`` recovery (7I-3B).

pdfminer emits ``(cid:N)`` placeholders when a font has no usable
ToUnicode/encoding entry for a character code (PDFUnicodeNotDefined).  Those
placeholders are **not** a defect of this pipeline — they faithfully record a
gap in the *source* PDF's encoding metadata (missing ToUnicode, out-of-codespace
CID, or an encoding that does not cover a used code).

Before falling back to the placeholder we attempt to recover the character from
the font's own glyph evidence::

    (font resource, char code)
        → glyph name   (font's own CFF charset / declared encoding)
        → Unicode      (Adobe Glyph List, incl. math-variant digit suffixes)

Recovery policy — **only restore when the glyph evidence is reliable; never
guess** (7I-2 §5, 7I-3 decision):

- ``recover_unicode`` returns ``None`` for anything it cannot *prove*, and the
  caller keeps the ``(cid:N)`` artifact and marks it as a parser anomaly
  (source-PDF encoding gap) for later diagnosis (FDS → parser).

Supported font sources:

- **CFF (FontFile3)**: charset (GID → glyph name) + the font's own encoding
  (predefined Standard/Expert, custom array, or the renderer's de-facto
  identity fallback for codes the declared encoding cannot name).
- **Type1 PFB (FontFile)**: the PFB's ``/Encoding`` (via pdfminer's
  ``Type1FontHeaderParser``).

Explicitly *not* guessed: codes whose glyph name resolves to nothing, and
codes the declared encodings leave unnameable without identity evidence.
"""

from __future__ import annotations

import io
import logging
import os
import re
import unicodedata
import weakref
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)

from pdfminer.converter import PDFPageAggregator
from pdfminer.layout import LAParams
from pdfminer.pdfinterp import PDFPageInterpreter, PDFResourceManager
from pdfminer.pdfpage import PDFPage
from pdfminer.pdffont import Type1FontHeaderParser
from pdfminer.pdftypes import resolve1
from pdfminer.utils import open_filename

try:  # fontTools is a hard dependency of the project (text_metrics.py)
    from fontTools.agl import toUnicode as _agl_to_unicode
    from fontTools.cffLib import CFFFontSet
    from fontTools.encodings.StandardEncoding import StandardEncoding as _STD_ENC
except Exception:  # pragma: no cover - defensive; project requires fontTools
    _agl_to_unicode = None
    CFFFontSet = None
    _STD_ENC = None

__all__ = [
    "recover_unicode",
    "recovery_enabled",
    "glyph_name_to_unicode",
    "CIDRecoveringPageAggregator",
    "extract_pages_recovering",
]

#: Predefined CFF encodings (CFF spec §5.6.1).  ``fontTools`` decompiles the
#: CFF's encoding offset into these names; the tables are 256-entry lists
#: indexed by char code → glyph name.
_CFF_PREDEFINED: Dict[str, Any] = {}
if _STD_ENC is not None:
    _CFF_PREDEFINED["StandardEncoding"] = _STD_ENC
try:
    from fontTools.encodings.ExpertEncoding import ExpertEncoding as _EXP_ENC  # type: ignore[attr-defined]

    _CFF_PREDEFINED["ExpertEncoding"] = _EXP_ENC
except Exception:  # pragma: no cover - table absent in some fontTools builds
    pass

#: Math-font variant idiom: names like ``Theta1`` / ``Omega1`` are digit-suffixed
#: variants of the base AGL name (MathTime, TeX, cmmi…).  Stripping the suffix is
#: only allowed when the base resolves to a Greek letter or a math symbol.
_RE_DIGIT_SUFFIX = re.compile(r"^(.*?)(\d+)$")
_MATH_BASE_CACHE: Dict[str, Optional[str]] = {}


def recovery_enabled() -> bool:
    """``PDF2ZH_CID_RECOVERY=0`` disables recovery (operator escape hatch).

    Defaults to enabled: recovery only replaces ``(cid:N)`` placeholders that
    were never meaningful text, so it can only improve the character stream.
    """
    return os.environ.get("PDF2ZH_CID_RECOVERY", "1") != "0"


def glyph_name_to_unicode(name: str) -> Optional[str]:
    """Map an Adobe glyph name to Unicode (AGL), never fabricating.

    Resolution order:
    1. exact Adobe Glyph List (incl. ``uniXXXX`` / ``uXXXXX`` encodings);
    2. math-variant digit suffix: ``Theta1 → Theta → Θ`` — only when the base
       is a Greek letter or math symbol (Sk/Sm), so subset artifacts like
       ``g3`` or ``parenleft1`` never resolve;
    3. ``None`` otherwise.
    """
    if not name or _agl_to_unicode is None:
        return None
    exact = _agl_to_unicode(name)
    if exact:
        return exact
    m = _RE_DIGIT_SUFFIX.match(name)
    if not m:
        return None
    base = m.group(1)
    cached = _MATH_BASE_CACHE.get(base)
    if base in _MATH_BASE_CACHE:
        return cached
    bu = _agl_to_unicode(base)
    result = None
    if bu and _is_math_base(bu):
        # return the NFKC-normalized form (e.g. ``Omega → Ω`` not ``℧``/``Ω``)
        try:
            result = unicodedata.normalize("NFKC", bu)
        except Exception:  # pragma: no cover - defensive
            result = bu
    _MATH_BASE_CACHE[base] = result
    return result


def _is_math_base(u: str) -> bool:
    """True when ``u`` is a Greek letter or math symbol.

    AGL maps some Greek capitals to symbol codepoints (``Omega → U+2126``
    OHM); NFKC canonical equivalence (Unicode-defined, not a guess) brings
    them back into the Greek block before the check.
    """
    if len(u) != 1:
        return False
    try:
        u = unicodedata.normalize("NFKC", u)
    except Exception:  # pragma: no cover - defensive
        return False
    if len(u) != 1:
        return False
    cp = ord(u)
    if 0x0370 <= cp <= 0x03FF:  # Greek
        return True
    try:
        return unicodedata.category(u) in ("Sm", "Sk")
    except Exception:  # pragma: no cover - defensive
        return False


# ── per-font parsed evidence cache ─────────────────────────────────────────
# pdfminer caches font objects per document, so a weak cache is safe and keeps
# CFF decompilation (one per font) from being repeated per undefined char.

_FONT_CACHE: "weakref.WeakKeyDictionary[Any, Dict[str, Any]]" = (
    weakref.WeakKeyDictionary()
)


def _font_evidence(font: Any) -> Dict[str, Any]:
    """Parse (once) the recoverable glyph evidence carried by a pdfminer font."""
    ev = _FONT_CACHE.get(font)
    if ev is not None:
        return ev
    ev = {"cff": None, "pfb": None}
    try:
        descriptor = getattr(font, "descriptor", None) or {}
        if CFFFontSet is not None and descriptor.get("FontFile3") is not None:
            ev["cff"] = _parse_cff(resolve1(descriptor["FontFile3"]))
        if descriptor.get("FontFile") is not None:
            ev["pfb"] = _parse_pfb(resolve1(descriptor["FontFile"]))
    except (
        Exception
    ) as exc:  # noqa: BLE001 - a broken font must never break the pipeline
        log.debug("cid_recovery: font evidence parse failed: %s", exc)
    _FONT_CACHE[font] = ev
    return ev


def _parse_cff(stream: Any) -> Optional[Dict[str, Any]]:
    """charset (GID → name) + declared encoding of an embedded CFF font."""
    data = stream.get_data()
    cff = CFFFontSet()
    cff.decompile(io.BytesIO(data), None)
    f = cff[0]
    charset: Dict[int, str] = {}
    for gid, item in enumerate(f.charset):
        charset[gid] = item[0] if isinstance(item, (tuple, list)) else item
    return {"charset": charset, "encoding": getattr(f, "Encoding", None)}


def _parse_pfb(stream: Any) -> Dict[int, str]:
    """code → unicode from a Type1 PFB's own ``/Encoding``.

    Reuses pdfminer's Type1FontHeaderParser; entries bound to custom
    CharStrings (subset glyphs) resolve to ``''`` and are dropped, which is
    exactly the conservative behavior we want.
    """
    data = stream.get_data()
    try:
        length1 = int(stream.get("Length1") or 0) or len(data)
    except Exception:  # noqa: BLE001
        length1 = len(data)
    parser = Type1FontHeaderParser(io.BytesIO(data[:length1]))
    return {cid: u for cid, u in parser.get_encoding().items() if u}


# ── the recovery ───────────────────────────────────────────────────────────


def recover_unicode(font: Any, cid: int) -> Optional[str]:
    """Recover the Unicode for ``(font, cid)`` or return ``None``.

    ``None`` means "no reliable glyph evidence" — the caller keeps the
    ``(cid:N)`` placeholder and marks a parser anomaly.  Never guesses.
    """
    if not recovery_enabled():
        return None
    if not isinstance(cid, int) or cid < 0:
        return None
    ev = _font_evidence(font)
    recovered = _cff_unicode(ev.get("cff"), cid)
    if recovered is not None:
        return recovered
    pfb = ev.get("pfb")
    if pfb:
        return pfb.get(cid)
    return None


def _cff_unicode(cff: Optional[Dict[str, Any]], cid: int) -> Optional[str]:
    if not cff:
        return None
    charset: Dict[int, str] = cff["charset"]
    enc = cff["encoding"]

    def _usable(name: Optional[str]) -> Optional[str]:
        if not name or name == ".notdef":
            return None
        if name not in charset.values():
            return None  # the font does not actually carry that glyph
        return glyph_name_to_unicode(name)

    # 1. font's own declared encoding (predefined name or custom table)
    name: Optional[str] = None
    if isinstance(enc, str):
        table = _CFF_PREDEFINED.get(enc)
        if table and 0 <= cid < len(table):
            name = table[cid]
    elif isinstance(enc, (list, tuple)):
        if 0 <= cid < len(enc):
            name = enc[cid]
    elif isinstance(enc, dict):
        name = enc.get(cid)
    u = _usable(name)
    if u is not None:
        return u

    # 2. identity fallback — the de-facto renderer behavior for CFF fonts whose
    #    declared encoding cannot name the code (e.g. out-of-codespace CID).
    #    Only when the subset charset itself names a real glyph for GID == code.
    if 0 <= cid < len(charset):
        u = _usable(charset[cid])
        if u is not None:
            return u
    return None


# ── forensic snapshot support ──────────────────────────────────────────────


class CIDRecoveringPageAggregator(PDFPageAggregator):
    """``PDFPageAggregator`` that applies CID recovery at character decode.

    Used by the forensic snapshot chain (``dual_forensics``) so the parser
    evidence reflects the same character normalization as the production
    pipeline.  Unknown glyphs keep pdfminer's ``(cid:N)`` placeholder.
    """

    def handle_undefined_char(self, font, cid: int) -> str:
        recovered = recover_unicode(font, cid)
        if recovered is not None:
            return recovered
        return super().handle_undefined_char(font, cid)


def extract_pages_recovering(
    pdf_file,
    password: str = "",
    page_numbers=None,
    maxpages: int = 0,
    caching: bool = True,
    laparams: Optional[LAParams] = None,
):
    """``pdfminer.high_level.extract_pages`` + CID recovery (7I-3B).

    Same signature/behavior as ``extract_pages`` — only the character-decode
    fallback is upgraded, so the parser-side evidence carries recovered Unicode
    instead of ``(cid:N)`` placeholders whenever the font proves the glyph.
    """
    if laparams is None:
        laparams = LAParams()
    with open_filename(pdf_file, "rb") as fp:
        rsrcmgr = PDFResourceManager(caching=caching)
        device = CIDRecoveringPageAggregator(rsrcmgr, laparams=laparams)
        interpreter = PDFPageInterpreter(rsrcmgr, device)
        for page in PDFPage.get_pages(
            fp,
            page_numbers,
            maxpages=maxpages,
            password=password,
            caching=caching,
        ):
            interpreter.process_page(page)
            yield device.get_result()
