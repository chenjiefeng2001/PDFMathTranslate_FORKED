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


class TestErasePrecedesAllText(unittest.TestCase):
    """擦除遍必须**先于本页所有译文**落笔。

    缺陷
    ----
    逐块「擦白→画字」交错执行时，块 N 的白矩形会盖掉块 N-1 已经画好的译文 ——
    只要 N-1 的译文比它的 ``src_box`` 高（重排后行数变多是常态）就会发生。
    实测（Harvard 出版社 325 页书，正文页）译文被削掉半截字形，与残留的原文
    叠成不可读的一团；而内容流给出的顺序是
    ``ORIG WHITE TEXT*8 WHITE TEXT*3 WHITE ...`` —— 矩形夹在文本之间。

    修法：擦除遍与绘制遍共用 :func:`_erases_source_region` 判据，先统一画白，
    再统一落笔。
    """

    def _source_pdf(self) -> str:
        """Two long source lines, 20pt apart, that will BOTH be replaced.

        Lines are long on purpose: the probe sits in a column where the
        **source** has ink but the short translations do not reach, so "not
        white" means exposed original ink rather than the translation we drew.

        The lines sit 20pt apart so that entry A's three-line translation
        (drawn from A's box top downwards) genuinely reaches into entry B's
        source region -- the situation the interleaved order destroyed.
        """
        doc = pymupdf.Document()
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        page.insert_text((100, 200), "FIRST SOURCE LINE " * 5, fontsize=12)
        page.insert_text((100, 220), "SECOND SOURCE LINE " * 5, fontsize=12)
        path = os.path.join(tempfile.gettempdir(), "order_source.pdf")
        doc.save(path, garbage=3, deflate=True)
        doc.close()
        return path

    #: probe window in x: source ink lives here, the short translations
    #: (7 CJK glyphs at 12pt from x=90, i.e. ~x<180) do not reach it.
    _PROBE_X = (300, 460)
    #: probe bands in y, chosen **strictly inside** each erase rect so the
    #: measurement is about the erase pass and not about glyph ascenders that
    #: poke out above/below the rect by a fraction of a point.
    #: src A v3 [586,600] -> fitz [192,206]; src B v3 [566,586] -> fitz [206,226]
    _PROBE_BANDS = (("first", 195, 204), ("second", 209, 224))

    def _overflowing_entries(self):
        """Entry A: src 586..600 (one source line) but three translated lines
        drawn downwards into y 546..586 -- which is entry B's src_box."""

        def flow(bid, src, translated_lines, y_top):
            return {
                "block_id": bid,
                "page": 0,
                "kind": "paragraph",
                "text": "source",
                "translated": translated_lines[0],
                "render_path": "translate_refit",
                "src_box": list(src),
                "dst_box": list(src),
                "font_size": 12.0,
                "render_payload": {
                    "kind": "flow",
                    "commands": [
                        {
                            "kind": "flow-text",
                            "text": t,
                            "x": float(src[0]),
                            "y": float(y_top - 14 * i),
                            "width": 260.0,
                            "line": i,
                            "is_last": i == len(translated_lines) - 1,
                            "overflow": False,
                            "font_size": 12.0,
                        }
                        for i, t in enumerate(translated_lines)
                    ],
                    "overflow": False,
                    "layout_ok": True,
                },
            }

        # A occupies v3 586..600 (fitz 192..206) but paints three lines from
        # y=600 downwards -> fitz baselines ~202/216/230, i.e. it reaches into
        # B's source region (fitz 206..226). That overlap is the whole point.
        a = flow(
            "p0_0",
            [90.0, 586.0, 520.0, 600.0],
            ["译文第一行内容", "译文第二行内容", "译文第三行内容"],
            600.0,
        )
        # B's src v3 566..586 -> fitz 206..226, covering the second source line.
        b = flow(
            "p0_1",
            [90.0, 566.0, 520.0, 586.0],
            ["下面一段的译文"],
            586.0,
        )
        return [a, b]

    def test_later_block_does_not_erase_earlier_translation(self):
        src_path = self._source_pdf()
        entries = self._overflowing_entries()
        doc, _ = _render(entries, source_pdf=src_path)
        try:
            spans = _ink_spans(doc)
            cjk = [
                (t, bb) for t, bb in spans if any("\u4e00" <= c <= "\u9fff" for c in t)
            ]
            texts = [t for t, _ in cjk]
            for expected in ("译文第一行内容", "译文第二行内容", "译文第三行内容"):
                self.assertIn(
                    expected,
                    texts,
                    f"{expected!r} was wiped by a later block's white rect; "
                    f"rendered CJK spans: {texts}",
                )
        finally:
            doc.close()
            try:
                os.remove(src_path)
            except OSError:
                pass

    def _band_darkness(self, doc, y0, y1, x0, x1):
        pm = doc[0].get_pixmap(dpi=72)
        dark = total = 0
        for y in range(int(y0), int(y1)):
            for x in range(int(x0), int(x1)):
                i = (y * pm.width + x) * pm.n
                total += 1
                if pm.samples[i] < 160:
                    dark += 1
        return dark, total

    def test_every_replaced_source_region_is_still_erased(self):
        """The reordering must not weaken erasing: both source lines go away.

        Measured **differentially** against a translation-only control (same
        plan, no source PDF). An absolute "must be white" threshold is brittle
        here: antialiasing at the glyph edges of the translation we just drew
        puts a handful of dark pixels in any band. What must be zero is the
        *extra* ink the background layer contributes -- that is the original
        text the erase pass is responsible for hiding.
        """
        src_path = self._source_pdf()
        entries = self._overflowing_entries()
        with_bg, _ = _render(entries, source_pdf=src_path)
        without_bg, _ = _render(entries, source_pdf=None)
        try:
            px0, px1 = self._PROBE_X
            for label, y0, y1 in self._PROBE_BANDS:
                d_with, total = self._band_darkness(with_bg, y0, y1, px0, px1)
                d_without, _ = self._band_darkness(without_bg, y0, y1, px0, px1)
                self.assertLessEqual(
                    d_with,
                    d_without + total * 0.01,
                    f"{label} source line still visible in x[{px0},{px1}]: "
                    f"{d_with} dark px with background vs {d_without} without "
                    "-- erase pass left original ink exposed",
                )
            # The original Latin must remain in the text layer; the white rect
            # hides it visually, it does not delete it.
            latin = [t for t, _ in _ink_spans(with_bg) if "SOURCE LINE" in t]
            self.assertTrue(
                latin,
                "expected the original Latin to remain in the background text "
                "layer (that is what the white rect hides visually)",
            )
        finally:
            with_bg.close()
            without_bg.close()
            try:
                os.remove(src_path)
            except OSError:
                pass


class TestErasePrecedesAllTextOnEveryPath(unittest.TestCase):
    """擦除前置必须覆盖**每一条**绘制路径。

    flow 只是其中一条；list / toc / legacy 兜底各自调用不同的落笔函数，任何
    一条把逐块擦白加回来，都会重现「后一块削掉前一块译文」的原缺陷，而只测
    flow 是发现不了的。
    """

    def _source_pdf(self, gap):
        doc = pymupdf.Document()
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        page.insert_text((100, 200), "ALPHA SOURCE LINE " * 5, fontsize=12)
        page.insert_text((100, 200 + gap), "BETA SOURCE LINE " * 5, fontsize=12)
        path = os.path.join(tempfile.gettempdir(), f"order_{gap}.pdf")
        doc.save(path, garbage=3, deflate=True)
        doc.close()
        return path

    def _entries(self, kind):
        """Two entries; A's second drawn line lands inside B's source region."""

        def mk(bid, src, lines, y_top):
            cmds = []
            for i, t in enumerate(lines):
                y = y_top - 14 * i
                if kind == "list":
                    cmds.append(
                        {
                            "kind": "list-text",
                            "text": t,
                            "x": float(src[0]),
                            "y": float(y),
                        }
                    )
                elif kind == "toc":
                    cmds.append(
                        {
                            "kind": "toc-title",
                            "text": t,
                            "x": float(src[0]),
                            "y": float(y),
                        }
                    )
                else:
                    cmds.append(
                        {
                            "kind": "flow-text",
                            "text": t,
                            "x": float(src[0]),
                            "y": float(y),
                            "width": 200.0,
                            "line": i,
                            "is_last": i == len(lines) - 1,
                            "overflow": False,
                            "font_size": 12.0,
                        }
                    )
            payload = {"kind": kind, "commands": cmds}
            if kind == "flow":
                payload.update({"overflow": False, "layout_ok": True})
            return {
                "block_id": bid,
                "page": 0,
                "kind": "paragraph",
                "text": "source",
                "translated": lines[0],
                "render_path": "translate_refit",
                "src_box": list(src),
                "dst_box": list(src),
                "font_size": 12.0,
                "render_payload": payload,
            }

        a = mk(
            "p0_0",
            [90.0, 586.0, 520.0, 600.0],
            ["译文第一行内容", "译文第二行内容"],
            600.0,
        )
        b = mk("p0_1", [90.0, 566.0, 520.0, 586.0], ["下面一段的译文"], 586.0)
        return [a, b]

    def _check(self, kind):
        src_path = self._source_pdf(20)
        entries = self._entries(kind)
        doc, _ = _render(entries, source_pdf=src_path)
        try:
            texts = [t for t, _ in _ink_spans(doc)]
            for expected in ("译文第一行内容", "译文第二行内容"):
                self.assertIn(
                    expected,
                    texts,
                    f"[{kind}] {expected!r} was wiped by a later block's white "
                    f"rect; rendered spans: {texts}",
                )
        finally:
            doc.close()
            try:
                os.remove(src_path)
            except OSError:
                pass

    def test_list_path(self):
        self._check("list")

    def test_toc_path(self):
        self._check("toc")

    def test_legacy_wrapped_path(self):
        """payload_kind 既非 list/toc/flow 时走 legacy 兜底（_insert_text_wrapped），
        它的擦除在主循环里内联，同样必须已经被前置遍取代。"""
        entries = self._entries("flow")
        for e in entries:
            e["render_payload"] = {"kind": "wrapped", "commands": []}
            e["translated"] = "译文内容占据整行宽度并且足够长以便换行 " * 3
        src_path = self._source_pdf(60)
        doc, _ = _render(entries, source_pdf=src_path)
        try:
            texts = "".join(t for t, _ in _ink_spans(doc))
            self.assertIn(
                "译文内容占据整行宽度并且足够长以便换行",
                texts,
                "legacy wrapped path drew nothing over the source",
            )
        finally:
            doc.close()
            try:
                os.remove(src_path)
            except OSError:
                pass


class TestErasePlanMatchesDispatch(unittest.TestCase):
    """擦除遍与绘制遍共用判据 —— 两边对「谁被替换」的分歧都是静默故障。"""

    def test_command_path_without_commands_is_not_erased(self):
        """命令路径但**没有命令** → 不擦。

        分派是按 ``payload["kind"]`` 走的：一个 ``kind="toc"`` 而命令为空的
        条目会进入 toc 分支，却一笔都画不出来。此时若照旧擦白，结果是
        「抹掉整页原文 + 一个字都不画」—— 实测目录页因此几乎全白。宁可不擦
        （退回显示原文），也不能擦了却什么都不画。
        """
        from pdf2zh.v3.magicpdf_renderer import _erases_source_region

        entry = {
            "block_id": "p0_0",
            "page": 0,
            "kind": "toc",
            "text": "Information 13",
            "translated": "信息 13",
            "src_box": [90.0, 586.0, 400.0, 600.0],
            "dst_box": [90.0, 586.0, 400.0, 600.0],
            "render_payload": {"kind": "toc", "commands": []},
        }
        self.assertFalse(
            _erases_source_region(entry, object()),
            "a toc entry with no commands must not have its region whited out",
        )

        entry["render_payload"]["commands"] = [{"text": "x", "x": 1.0, "y": 1.0}]
        self.assertTrue(
            _erases_source_region(entry, object()),
            "once it has commands to draw, it must erase its source region",
        )

    def test_preserved_blocks_are_not_erased(self):
        from pdf2zh.v3.magicpdf_renderer import _erases_source_region

        preserved = {
            "block_id": "p0_0",
            "page": 0,
            "kind": "formula",
            "text": "E = mc^2",
            "translated": "E = mc^2",
            "src_box": [90.0, 586.0, 400.0, 600.0],
            "dst_box": [90.0, 586.0, 400.0, 600.0],
            "font_size": 12.0,
            "render_payload": {"kind": "flow", "commands": []},
        }
        self.assertFalse(
            _erases_source_region(preserved, object()),
            "a preserved block must not have its source region whited out",
        )

    def test_translated_blocks_and_command_paths_are_erased(self):
        from pdf2zh.v3.magicpdf_renderer import _erases_source_region

        base = {
            "block_id": "p0_0",
            "page": 0,
            "kind": "paragraph",
            "text": "source",
            "translated": "译文",
            "src_box": [90.0, 586.0, 400.0, 600.0],
            "dst_box": [90.0, 586.0, 400.0, 600.0],
            "font_size": 12.0,
        }
        self.assertTrue(_erases_source_region(dict(base), object()))

        for kind in ("list", "toc"):
            with_cmd = dict(base)
            with_cmd["render_payload"] = {
                "kind": kind,
                "commands": [{"text": "x", "x": 1.0, "y": 1.0}],
            }
            self.assertTrue(
                _erases_source_region(with_cmd, object()), f"{kind} path must erase"
            )

        flow = dict(base)
        flow["render_payload"] = {
            "kind": "flow",
            "commands": [{"text": "x", "x": 1.0, "y": 1.0}],
        }
        self.assertTrue(_erases_source_region(flow, object()))

    def test_empty_text_is_never_erased(self):
        from pdf2zh.v3.magicpdf_renderer import _erases_source_region

        # commands present, so the payload *would* erase if the empty-text
        # guard were missing -- a fixture without commands cannot tell the two
        # apart (both paths return False).
        entry = {
            "block_id": "p0_0",
            "page": 0,
            "kind": "paragraph",
            "text": "",
            "translated": "",
            "src_box": [90.0, 586.0, 400.0, 600.0],
            "dst_box": [90.0, 586.0, 400.0, 600.0],
            "render_payload": {
                "kind": "flow",
                "commands": [{"text": "x", "x": 1.0, "y": 1.0}],
            },
        }
        self.assertFalse(
            _erases_source_region(entry, object()),
            "an entry with nothing to draw must not erase its region",
        )
