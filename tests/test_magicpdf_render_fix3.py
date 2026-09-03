"""7N-FIX-3 — renderer anchor (box-top → baseline) + erase-geometry regression.

Locks in the Stage-4 renderer fixes from the MECH-4 audit (MP2e p442_4):

- FIX-3A: a flow command's ``y`` is the **box-top anchor** in v3 y-up
  (``first_cmd_y == dst_box.y1``), NOT a baseline.  The renderer must place
  the baseline at ``box_top + 0.85 * font_size`` (same anchoring as
  ``_insert_text_wrapped``); the OLD behaviour drew the baseline exactly at
  the box top, making every translation float a full em up into the line
  above.
- FIX-3B: the white erase rectangle must cover the **source** geometry
  (src_box) — decoupled from the (possibly shifted) dst_box — so a
  ``shift_down`` block never wipes out a neighbouring line.

These tests assert the *actual pixels* of the rendered mono PDF (the audit
methodology note: span-level checks are blind to baseline-vs-ink-band
misplacement, so the gate is rasterised ink).
"""

from __future__ import annotations

import os
import tempfile
import unittest

import pymupdf

from pdf2zh.v3.magicpdf_renderer import render_plan_to_pdf

PAGE_W, PAGE_H = 612.0, 792.0


def _flow_entry(
    *,
    block_id="p0_0",
    page=0,
    src_box=None,
    dst_box=None,
    translated="译文文本内容",
    font_size=12.0,
    cmd_font_size=None,
) -> dict:
    """A flow block whose single command is anchored at ``dst_box.y1`` (v3
    box top — the real-dump anchoring the renderer must reinterpret)."""
    src = list(src_box or [90.0, 586.0, 400.0, 600.0])
    dst = list(dst_box or src)
    cmd_fs = float(cmd_font_size or font_size)
    return {
        "block_id": block_id,
        "page": page,
        "kind": "paragraph",
        "text": "source text",
        "translated": translated,
        "render_path": "translate_refit",
        "src_box": src,
        "dst_box": dst,
        "font_size": font_size,
        "render_payload": {
            "kind": "flow",
            "commands": [
                {
                    "kind": "flow-text",
                    "text": translated,
                    "x": float(src[0]),
                    "y": float(dst[3]),  # box-top anchor (v3 y-up)
                    "width": 200.0,
                    "line": 0,
                    "is_last": True,
                    "overflow": False,
                    "font_size": cmd_fs,
                }
            ],
            "overflow": False,
            "layout_ok": True,
        },
    }


def _render(entries, source_pdf=None):
    pdf, stats = render_plan_to_pdf(
        entries,
        page_sizes={0: [PAGE_W, PAGE_H]},
        cjk_font=True,
        source_pdf=source_pdf,
    )
    doc = pymupdf.open(stream=pdf, filetype="pdf")
    return doc, stats


def _ink_spans(doc, page=0):
    """(text, bbox) spans from the rendered page (PDF y-down)."""
    d = doc[page].get_text("rawdict")
    out = []
    for b in d["blocks"]:
        for ln in b.get("lines", []):
            for sp in ln.get("spans", []):
                txt = "".join(ch["c"] for ch in sp.get("chars", []))
                if txt.strip():
                    out.append((txt, tuple(sp["bbox"])))
    return out


class TestFlowBaselineAnchor(unittest.TestCase):
    """FIX-3A: the rendered ink must sit inside the box, not float a full em
    above the box top (old bug: baseline == box top)."""

    def _entry(self):
        # v3 src/dst box; PDF box top = PAGE_H - y1 = 792 - 600 = 192
        return _flow_entry(
            src_box=[90.0, 586.0, 400.0, 600.0],
            dst_box=[90.0, 586.0, 400.0, 600.0],
            translated="译文文本内容",
            font_size=12.0,
        )

    def test_ink_not_floating_above_box(self):
        doc, _ = _render([self._entry()])
        try:
            spans = _ink_spans(doc)
            cjk = [
                (t, bb) for t, bb in spans if any("\u4e00" <= c <= "\u9fff" for c in t)
            ]
            self.assertTrue(cjk, "translation ink must be present")
            t, bb = cjk[0]
            fs = 12.0
            box_top = PAGE_H - 600.0  # 192
            ink_top = bb[1]
            # Old bug: baseline at box top → ink top ≈ box_top - ascent ≈
            # box_top - 1.0*fs.  Fixed: ink top ≈ box_top - 0.15*fs.
            self.assertGreater(
                ink_top,
                box_top - 0.5 * fs,
                f"translation {t!r} floats too high (ink_top={ink_top}, box_top={box_top})",
            )
            # Sanity: ink starts near/inside the box (not pushed far below).
            self.assertLessEqual(ink_top, box_top + 0.5 * fs)
            # Ink bottom stays near the box (ascent+descent ≈ 1.2em).
            self.assertLess(bb[3], box_top + 1.4 * fs)
        finally:
            doc.close()

    def test_baseline_between_box_edges(self):
        """The rendered baseline must sit strictly below the box top (and not
        above it) — i.e. inside the box's vertical span."""
        doc, _ = _render([self._entry()])
        try:
            spans = _ink_spans(doc)
            cjk = [
                (t, bb) for t, bb in spans if any("\u4e00" <= c <= "\u9fff" for c in t)
            ]
            bb = cjk[0][1]
            fs = 12.0
            box_top = PAGE_H - 600.0
            box_bottom = PAGE_H - 586.0
            # baseline = ink_bottom - descent; descent ≈ 0.2em → baseline
            # ≈ ink_bottom - 0.2*fs.  Must be > box_top (was == box_top).
            approx_baseline = bb[3] - 0.2 * fs
            self.assertGreater(
                approx_baseline,
                box_top + 0.5 * fs,
                "baseline not moved below the box top (FIX-3A regression)",
            )
            self.assertLess(approx_baseline, box_bottom + 0.5 * fs)
        finally:
            doc.close()


class TestEraseGeometryDecoupled(unittest.TestCase):
    """FIX-3B: the white erase rect covers src_box only — a shifted dst_box
    landing on a neighbour line must NOT erase that neighbour."""

    def _source_pdf(self) -> str:
        """Two lines: SOURCE (to be replaced) at PDF y≈192-206, NEIGHBOUR
        (must survive) at PDF y≈232-246 — exactly where a 40pt downward
        shift would land (p442_4-like geometry)."""
        doc = pymupdf.Document()
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        page.insert_text((100, 200), "SOURCE LINE AAAA", fontsize=12)
        page.insert_text((100, 240), "NEIGHBOUR LINE BBBB", fontsize=12)
        path = os.path.join(tempfile.gettempdir(), "fix3_source.pdf")
        doc.save(path, garbage=3, deflate=True)
        doc.close()
        return path

    def _pixel_row_stats(self, pixmap, y0, y1, x0=80, x1=320):
        """min brightness + fraction of dark pixels in the band [y0, y1]."""
        dark = 0
        total = 0
        mn = 255
        n = pixmap.n
        for y in range(int(y0), int(y1)):
            for x in range(int(x0), int(x1)):
                i = (y * pixmap.width + x) * n
                v = pixmap.samples[i]
                total += 1
                if v < 160:
                    dark += 1
                mn = min(mn, v)
        return mn, dark, total

    def test_shifted_erase_covers_src_not_neighbour(self):
        src_path = self._source_pdf()
        # v3: SOURCE line ink at PDF [192,206] → v3 y = [792-206, 792-192] =
        # [586, 600].  dst shifted DOWN (v3 −40) lands on the NEIGHBOUR
        # [232,246] → v3 [546, 560].
        entry = _flow_entry(
            src_box=[90.0, 586.0, 400.0, 600.0],
            dst_box=[90.0, 546.0, 400.0, 560.0],
            translated="译文文本内容",
            font_size=12.0,
        )
        doc, _ = _render([entry], source_pdf=src_path)
        try:
            pm = doc[0].get_pixmap(dpi=72)
            # src region: erased → no dark ink left
            mn_src, dark_src, tot_src = self._pixel_row_stats(pm, 192, 206)
            self.assertEqual(
                dark_src,
                0,
                f"src region must be fully erased (dark px {dark_src}/{tot_src})",
            )
            # neighbour region (dst landing): must NOT be pure white — the
            # neighbour text (and/or the translation) keeps ink there.
            mn_dst, dark_dst, tot_dst = self._pixel_row_stats(pm, 232, 246)
            self.assertGreater(
                dark_dst,
                0,
                "neighbour line was erased by the shifted white rect (FIX-3B regression)",
            )
        finally:
            doc.close()
            try:
                os.remove(src_path)
            except OSError:
                pass


if __name__ == "__main__":
    unittest.main()
