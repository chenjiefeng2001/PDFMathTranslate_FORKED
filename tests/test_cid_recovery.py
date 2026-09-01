"""7I-3 — PDF character decoding / CID recovery.

- 7I-3A: FDS attribution — a ``(cid:N)`` / ``�`` artifact that already exists
  in the *parser* text is a source-PDF encoding gap (parser FAIL), never a
  renderer defect; one introduced at translation → translation FAIL; one that
  first appears in rendered text → render FAIL.
- 7I-3B: font-aware ``(font, cid) → Unicode`` recovery on the real
  "Art of Multiprocessor Programming, 2e" page 300:
    - MTMI ``(cid:3)`` → ``Θ`` (out-of-codespace CID; CFF charset GID 3 =
      Theta1 → AGL math-variant) — recovery *proves* the glyph;
    - Times-Roman ``(cid:129)`` stays a placeholder — there is **no** reliable
      code→glyph evidence (StandardEncoding maps 0x81 → .notdef; bullet is at
      0xB7), so the don't-guess policy keeps the artifact + parser anomaly;
    - unknown glyph names (``g1``, ``.notdef``) never fabricate Unicode.
"""

import pytest

from pdfminer.pdfdocument import PDFDocument
from pdfminer.pdfinterp import PDFPageInterpreter, PDFResourceManager
from pdfminer.pdfpage import PDFPage
from pdfminer.pdfparser import PDFParser as PDFMinerParser

from pdf2zh.cid_recovery import (
    extract_pages_recovering,
    glyph_name_to_unicode,
    recover_unicode,
)
from pdf2zh.converter import PDFConverterEx

from pathlib import Path

BOOK = "tests/file/The Art of Multiprocessor Programming, 2e.pdf"


# 大 PDF fixture 不入库（.gitignore 排除 *.pdf）—— CI clean checkout 没有该
# 文件时必须优雅 skip，而不是 FileNotFoundError 使整个 release gate 变红。
# 本地有 fixture 时这些测试照常执行（corpus 取证依赖）。
requires_book = pytest.mark.skipif(
    not (Path(__file__).resolve().parents[1] / BOOK).exists(),
    reason=f"fixture not present in this checkout: {BOOK}",
)


# ── 7I-3B: glyph-name → Unicode (AGL + math-variant, never fabricates) ─────


def test_glyph_name_to_unicode_known_and_math_variants():
    assert glyph_name_to_unicode("bullet") == "•"
    assert glyph_name_to_unicode("Theta1") == "Θ"  # math variant digit suffix
    assert glyph_name_to_unicode("Omega1") == "Ω"  # AGL gives U+2126; NFKC → Ω
    assert glyph_name_to_unicode("phi") == "φ"
    assert glyph_name_to_unicode("uni2022") == "•"


def test_glyph_name_to_unicode_never_fabricates():
    # subset artifacts / unknown names / notdef must NOT resolve
    assert glyph_name_to_unicode("g1") is None
    assert glyph_name_to_unicode("g123") is None
    assert glyph_name_to_unicode(".notdef") is None
    assert glyph_name_to_unicode("parenleft1") is None  # digit suffix on non-math
    assert glyph_name_to_unicode("") is None
    assert glyph_name_to_unicode(None) is None


class _FakeStream:
    """Minimal stand-in for a pdfminer PDFStream (get_data / get)."""

    def __init__(self, data: bytes, length1: int = 0):
        self._data = data
        self._length1 = length1

    def get_data(self) -> bytes:
        return self._data

    def get(self, key, default=None):
        return self._length1 if key == "Length1" else default


class _FakeFont:
    """Weakref-able pdfminer-font stand-in (the recovery cache keys on it)."""

    def __init__(self, descriptor: dict):
        self.descriptor = descriptor
        self.basefont = "Fake"


def _fake_font(descriptor: dict):
    return _FakeFont(descriptor)


def test_recover_unknown_returns_none_not_fabrication():
    # no embedded font at all
    assert recover_unicode(_fake_font({}), 3) is None
    # garbage FontFile3 — unparseable CFF must never crash nor guess
    assert (
        recover_unicode(_fake_font({"FontFile3": _FakeStream(b"not a cff")}), 3) is None
    )


def test_recover_type1_pfb_encoding():
    # minimal Type1 /Encoding: custom CharString name g1 (unresolvable) + /A
    pfb = (
        b"%!PS-AdobeFont-1.0: Fake 1.0\n"
        b"/Encoding 256 array\n"
        b"0 1 255 {1 index exch /.notdef put} for\n"
        b"dup 65 /g1 put\n"
        b"dup 66 /A put\n"
        b"readonly def\n"
        b"end\n"
    )
    font = _fake_font({"FontFile": _FakeStream(pfb)})
    assert recover_unicode(font, 65) is None  # /g1 has no Unicode — no guess
    assert recover_unicode(font, 66) == "A"


def test_recovery_disabled_by_env(monkeypatch):
    monkeypatch.setenv("PDF2ZH_CID_RECOVERY", "0")
    pfb = (
        b"%!PS-AdobeFont-1.0: Fake 1.0\n"
        b"/Encoding 256 array\n"
        b"0 1 255 {1 index exch /.notdef put} for\n"
        b"dup 65 /g1 put\n"
        b"dup 66 /A put\n"
        b"readonly def\n"
        b"end\n"
    )
    font = _fake_font({"FontFile": _FakeStream(pfb)})
    # even a provable mapping is suppressed when recovery is disabled
    assert recover_unicode(font, 66) is None


# ── 7I-3B: production converter recovers page 300 ──────────────────────────


def _run_page_300_converter() -> PDFConverterEx:
    with open(BOOK, "rb") as fp:
        parser = PDFMinerParser(fp)
        doc = PDFDocument(parser)
        rsrcmgr = PDFResourceManager()
        conv = PDFConverterEx(rsrcmgr)
        interp = PDFPageInterpreter(rsrcmgr, conv)
        page = list(PDFPage.create_pages(doc))[300]
        page.pageno = 300
        interp.process_page(page)
    return conv


def _chars_of(conv: PDFConverterEx):
    chars = []

    def walk(obj):
        for child in obj:
            if hasattr(child, "get_text") and hasattr(child, "cid"):
                chars.append(child)
            try:
                walk(child)
            except TypeError:
                pass

    walk(conv.cur_item)
    return chars


@requires_book
def test_converter_recovers_theta_keeps_unresolvable_bullet():
    conv = _run_page_300_converter()
    chars = _chars_of(conv)
    texts = [c.get_text() for c in chars]
    assert texts.count("Θ") == 1  # MTMI (cid:3) → Theta1 → Θ
    assert texts.count("(cid:129)") == 2  # Times-Roman bullet: no reliable evidence
    assert not any("(cid:" in t for t in texts if t != "(cid:129)")
    # per-char anomaly markers
    theta = [c for c in chars if c.get_text() == "Θ"][0]
    assert getattr(theta, "cid", None) == 3
    assert theta.cid_placeholder is False
    bullets = [c for c in chars if c.get_text() == "(cid:129)"]
    assert len(bullets) == 2
    assert all(b.cid_placeholder for b in bullets)
    assert conv.cid_recovery_stats == {"recovered": 1, "unresolved": 2}


# ── 7I-3B: forensic snapshot reflects the same character normalization ─────


@requires_book
def test_capture_source_chain_recovers_page_300():
    from dual_forensics.diff import Trace
    from dual_forensics.defect import F4, run_defect_detectors
    from dual_forensics.snapshot import capture_source_chain

    r = capture_source_chain(BOOK, page_ids=[300])
    assert not r["errors"]
    rows = r["pages"]["300"]
    parser_texts = "".join(ev.get("parser", {}).get("text") or "" for ev in rows)
    # the out-of-codespace Theta is restored …
    assert "Θ(log w)" in parser_texts
    assert "(cid:3)" not in parser_texts
    # … while the bullet stays an explicit placeholder (no reliable evidence)
    assert "(cid:129)" in parser_texts

    traces = [
        Trace(
            node_id=ev["node_id"],
            page=300,
            kind=ev.get("kind"),
            source_text=ev.get("parser", {}).get("text"),
            translated_text=ev.get("translation", {}).get("translated_text"),
            translation_status=ev.get("translation", {}).get("translation_status"),
            render_rows=[],
        )
        for ev in rows
    ]
    f4 = [f for f in run_defect_detectors(traces) if f.defect_id == F4]
    # only the bullet remains, and it is attributed to the parser stage
    assert len(f4) == 1
    assert f4[0].first_divergence == "parser"
    assert f4[0].stage_verdicts["parser"] == "FAIL"
    assert f4[0].stage_verdicts["render"] == "PASS"
    assert "(cid:129)" in (f4[0].evidence or {}).get("text", "")


# ── 7I-3A: FDS attribution from stage snapshots ────────────────────────────


def _trace(source=None, translated=None, rendered_rows=None, **kw):
    from dual_forensics.diff import Trace

    return Trace(
        node_id=kw.get("node_id", "p0_0"),
        page=kw.get("page", 0),
        kind=kw.get("kind", "paragraph"),
        source_text=source,
        translated_text=translated,
        translation_status=kw.get("translation_status", "translated"),
        render_rows=rendered_rows or [],
        matched_present=bool(rendered_rows),
    )


def test_f4_attrs_parser_when_artifact_exists_at_parser():
    from dual_forensics.defect import F4, run_defect_detectors

    trace = _trace(
        source="...has depth (cid:3)(log w). Can we...",
        translated="...has depth (cid:3)(log w). Can we...",
        rendered_rows=[{"text": "...has depth (cid:3)(log w). Can we..."}],
    )
    findings = [f for f in run_defect_detectors([trace]) if f.defect_id == F4]
    assert len(findings) == 1
    f = findings[0]
    assert f.first_divergence == "parser"
    assert f.stage_verdicts["parser"] == "FAIL"
    assert f.stage_verdicts["render"] == "PASS"
    assert f.evidence.get("parser_originated") is True


def test_f4_attrs_parser_when_source_has_fffd():
    from dual_forensics.defect import F4, run_defect_detectors

    trace = _trace(
        source="bad \ufffd glyph",
        translated="bad \ufffd glyph",
        rendered_rows=[{"text": "bad \ufffd glyph"}],
    )
    f = [f for f in run_defect_detectors([trace]) if f.defect_id == F4][0]
    assert f.first_divergence == "parser"


def test_f4_attrs_translation_when_translation_introduces():
    from dual_forensics.defect import F4, run_defect_detectors

    trace = _trace(
        source="clean source text",
        translated="clean source text (cid:7) garbage",
        rendered_rows=[{"text": "clean source text (cid:7) garbage"}],
    )
    f = [f for f in run_defect_detectors([trace]) if f.defect_id == F4][0]
    assert f.first_divergence == "translation"
    assert f.stage_verdicts["translation"] == "FAIL"
    assert f.stage_verdicts["parser"] == "PASS"
    assert f.evidence.get("parser_originated") is False


def test_f4_attrs_render_when_only_rendered_has_artifact():
    from dual_forensics.defect import F4, run_defect_detectors

    trace = _trace(
        source="int f(int x) { return x+1; }",
        translated="int f(int x) { return x+1; }",
        rendered_rows=[{"text": "\ufffd 损坏字形 (cid:5xxx)"}],
    )
    f = [f for f in run_defect_detectors([trace]) if f.defect_id == F4][0]
    assert f.first_divergence == "render"
    assert f.stage_verdicts["render"] == "FAIL"
    assert f.stage_verdicts["parser"] == "PASS"


def test_f4_no_finding_when_clean():
    from dual_forensics.defect import F4, run_defect_detectors

    trace = _trace(
        source="clean", translated="clean", rendered_rows=[{"text": "clean"}]
    )
    assert not [f for f in run_defect_detectors([trace]) if f.defect_id == F4]


@requires_book
def test_extract_pages_recovering_matches_extract_pages_semantics():
    """The snapshot helper must still parse a plain PDF (no (cid:) impact)."""
    from pdfminer.high_level import extract_pages

    plain = list(extract_pages(BOOK, page_numbers=[0]))
    recovered = list(extract_pages_recovering(BOOK, page_numbers=[0]))
    assert len(plain) == len(recovered) == 1
    assert len(recovered[0]) > 0  # text boxes present
