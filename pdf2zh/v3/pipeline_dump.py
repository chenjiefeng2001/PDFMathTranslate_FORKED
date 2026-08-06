"""Module: PipelineDump — 逐阶段可观测层（Glyph / Line / Block / TOC / Translation / Layout）。

针对「字符流损坏（� / (cid:N) / 标题丢失）」的排查：不依赖任何修复，
只把每个阶段的中间状态导出为 JSON，回答「损坏发生在哪一层」：

    PDF
     ├─ GlyphDump      字符层：char/cid/font/bbox/is_replacement（ToUnicode 失败信号）
     ├─ LineDump       行恢复：geometry 行文本 + 几何
     ├─ BlockDump      块恢复：geometry 段落 + 几何
     ├─ TOCDump        目录解析：raw/kind/number/title/leader/page/confidence
     │                 + title_has_replacement（标题在解析前是否已损坏）
     ├─ TranslationDump 翻译前后：source/translated/same + replacement 标记
     └─ LayoutDump     布局结果：gate 记录 + 门控裁决

判定口诀：
- GlyphDump 已见 ``�`` / ``(cid:N)`` → 问题在 PDF 字体解析（ToUnicode/CMap），
  与 TOC/翻译无关；
- Glyph 正常、TOCDump.title 为空/乱序 → reading order / tokenizer；
- 前三层正常、翻译后才损坏 → 编码转换/字体回写/子集化。

纯逻辑 + 可选 fitz/pdfminer（CLI 路径 guarded），不触碰主链路。
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence
from unittest.mock import Mock, patch

log = logging.getLogger(__name__)

_RE_CID_NOTDEF = re.compile(r"\(cid:\d+\)")

# 疑似「多目录行被合并成一行」信号：行内出现 ≥2 个点号编号（5.1 / 5.2.1 …）
_RE_MERGED_ENTRY_HINT = re.compile(r"\b\d+(?:\.\d+){1,3}\b")


def has_replacement(text: str) -> bool:
    """是否含替换字符 / 未定义 CID 标记（ToUnicode 解码失败信号）。"""
    t = text or ""
    return "\ufffd" in t or bool(_RE_CID_NOTDEF.search(t))


# ── 字符层 ────────────────────────────────────────────────────────────────


def glyph_dump(ltpage, max_chars: int = 1000) -> List[dict]:
    """遍历页面 LTChar → 逐字符记录（解码结果 + 字体 + cid + 几何）。

    额外给出 Font Decode 信号：``font_type``（cid/simple）与
    ``has_to_unicode``（字体是否带 ToUnicode CMap）—— 直接回答
    「� 是 Extract 层字体解码失败还是 Render 层回退」。
    """
    out: List[dict] = []
    seen = 0
    for child in getattr(ltpage, "_objs", []) or []:
        cls = child.__class__.__name__
        if cls == "LTChar":
            if seen >= max_chars:
                continue
            seen += 1
            char = child.get_text() or ""
            font_obj = getattr(child, "font", None)
            font_type = "n/a"
            has_to_unicode = None
            if font_obj is not None:
                try:
                    font_type = ("cid" if getattr(font_obj, "is_multibyte", False)
                                 else "simple")
                except Exception:  # noqa: BLE001
                    font_type = "n/a"
                try:
                    tu = font_obj.get_toUnicode()
                    has_to_unicode = tu is not None
                except Exception:  # noqa: BLE001
                    has_to_unicode = None
            out.append({
                "char": char,
                "cid": int(getattr(child, "cid", 0) or 0),
                "font": getattr(child, "fontname", "") or "",
                "font_type": font_type,
                "has_to_unicode": has_to_unicode,
                "size": round(float(getattr(child, "size", 0.0) or 0.0), 2),
                "x0": round(float(child.x0), 1),
                "y0": round(float(child.y0), 1),
                "x1": round(float(child.x1), 1),
                "y1": round(float(child.y1), 1),
                "is_replacement": has_replacement(char),
                "decode": "notdef" if _RE_CID_NOTDEF.search(char) else
                          ("fffd" if "\ufffd" in char else "ok"),
            })
        elif cls in ("LTLine", "LTFigure"):
            out.append({"kind": cls, "char": "", "cid": -1,
                        "font": "", "font_type": "n/a",
                        "has_to_unicode": None, "size": 0.0,
                        "x0": round(float(child.x0), 1),
                        "y0": round(float(child.y0), 1),
                        "x1": round(float(child.x1), 1),
                        "y1": round(float(child.y1), 1),
                        "is_replacement": False, "decode": "ok"})
    return out


# ── Style Run（字形级 → 样式连续段） ──────────────────────────────────────


def run_dump(chars, page_num: int = 0, max_runs: int = 300) -> List[dict]:
    """把字符聚成 Style Run（同字体 + 同字号 + 水平连续的文本段）。

    回答「TOC 行/标题的样式是否因多字体错乱被拆散」：每条 run 记录
    font/size/文本跨度/是否含替换字符。
    """
    if not chars:
        return []
    ordered = sorted(chars, key=lambda c: (-round(c.y0, 1), c.x0))
    out: List[dict] = []
    line_idx = -1
    last_baseline = None
    run: List = []
    gap = 2.0

    def flush():
        nonlocal run
        if not run:
            return
        text = "".join(c.text for c in run)
        out.append({
            "line": line_idx,
            "font": run[0].font,
            "size": round(float(run[0].size), 2),
            "text": text,
            "x0": round(min(c.x0 for c in run), 1),
            "y0": round(min(c.y0 for c in run), 1),
            "x1": round(max(c.x1 for c in run), 1),
            "y1": round(max(c.y1 for c in run), 1),
            "char_count": len(run),
            "has_replacement": has_replacement(text),
        })
        run = []

    for c in ordered:
        if last_baseline is None or abs(c.y0 - last_baseline) > 3.0:
            flush()
            line_idx += 1
            run = [c]
        elif run and (c.font == run[-1].font
                      and abs(c.size - run[-1].size) < 0.5
                      and c.x0 - run[-1].x1 <= gap):
            run.append(c)
        else:
            flush()
            run = [c]
        last_baseline = c.y0
        if len(out) >= max_runs:
            break
    flush()
    return out[:max_runs]


# ── 行 / 块恢复（Geometry Engine 视角） ──────────────────────────────────


def line_dump(chars, page_num: int = 0, max_lines: int = 500) -> List[dict]:
    from pdf2zh.v3.geometry import GeometryEngine
    if not chars:
        return []
    page = GeometryEngine().build_page(chars, page_num=page_num)
    out: List[dict] = []
    for pi, para in enumerate(page.reading_order()):
        for li, line in enumerate(getattr(para, "lines", []) or []):
            if len(out) >= max_lines:
                return out
            text = line.text or ""
            out.append({
                "paragraph": pi,
                "line": li,
                "text": text,
                "x0": round(line.x0, 1), "y0": round(line.y0, 1),
                "x1": round(line.x1, 1), "y1": round(line.y1, 1),
                "size": round(float(getattr(line, "size", 0.0) or 0.0), 2),
                "has_replacement": has_replacement(text),
                # 疑似「多目录行被合并成一行」（Line Builder 阈值过大）：
                # 行内 ≥2 个点号编号（5.1 / 5.2.1 …）
                "suspected_merged_entries":
                    len(_RE_MERGED_ENTRY_HINT.findall(text)) >= 2,
            })
    return out


def block_dump(chars, page_num: int = 0) -> List[dict]:
    from pdf2zh.v3.geometry import GeometryEngine
    if not chars:
        return []
    page = GeometryEngine().build_page(chars, page_num=page_num)
    out: List[dict] = []
    for pi, para in enumerate(page.reading_order()):
        out.append({
            "index": pi,
            "text": para.text,
            "x0": round(para.x0, 1), "y0": round(para.y0, 1),
            "x1": round(para.x1, 1), "y1": round(para.y1, 1),
            "line_count": para.line_count,
            "has_replacement": has_replacement(para.text),
        })
    return out


# ── TOC 解析层（调试视图，不改 TOC 引擎） ───────────────────────────────


def toc_confidence(entry, raw: str) -> float:
    """轻量启发式置信度（只服务调试视图，不参与决策）。"""
    if entry is None or not getattr(entry, "matched", False):
        return 0.3
    score = 0.5
    if (entry.title or "").strip():
        score += 0.2
    if (entry.page or "").strip():
        score += 0.15
    if (entry.leader or "").strip():
        score += 0.1
    if has_replacement(raw):
        score -= 0.25
    return round(max(0.0, min(score, 0.98)), 4)


def toc_dump(conv, pageid: int) -> List[dict]:
    """gate 记录里的目录行 → 调试视图（解析字段 + 置信度 + 损坏信号）。

    gate 记录只保留标题余量（converter 剥离了号段），与 ``run_toc_channel``
    一致：PLAIN 时回退解析组合译文头（``第7.13节 …``）复原 kind/number。
    """
    from pdf2zh.v3.toc_semantics import parse_toc_entry
    out: List[dict] = []
    for i, rec in enumerate(getattr(conv, "_gate_records", []) or []):
        if rec.get("node_type") != "toc":
            continue
        raw = rec.get("text") or ""
        entry = parse_toc_entry(raw)
        if not entry.matched:
            composed = rec.get("translated") or ""
            fallback = parse_toc_entry(composed)
            if fallback.matched:
                entry = fallback
        out.append({
            "line": i,
            "raw": raw,
            "raw_has_replacement": has_replacement(raw),
            "composed": rec.get("translated", ""),
            "kind": entry.kind.value,
            "level": entry.level,
            "number": entry.number,
            "title": entry.title,
            "title_has_replacement": has_replacement(entry.title),
            "leader": entry.leader,
            "page": entry.page,
            "translated": rec.get("translated", ""),
            "confidence": toc_confidence(entry, raw),
        })
    return out


# ── 翻译前后 ─────────────────────────────────────────────────────────────


def translation_dump(conv, pageid: int) -> List[dict]:
    """gate 记录 → (source, translated) 对 + 损坏信号对比。"""
    out: List[dict] = []
    for i, rec in enumerate(getattr(conv, "_gate_records", []) or []):
        src = rec.get("text") or ""
        dst = rec.get("translated") or src
        out.append({
            "node_id": f"p{pageid}_{i}",
            "source": src,
            "translated": dst,
            "same": src == dst,
            "source_has_replacement": has_replacement(src),
            "translated_has_replacement": has_replacement(dst),
            "node_type": rec.get("node_type", "paragraph"),
        })
    return out


# ── 布局层 ───────────────────────────────────────────────────────────────


def layout_dump(conv, pageid: int) -> dict:
    recs = []
    for i, rec in enumerate(getattr(conv, "_gate_records", []) or []):
        recs.append({
            "node_id": f"p{pageid}_{i}",
            "text": (rec.get("text") or "")[:120],
            "x": round(float(rec.get("x", 0.0)), 1),
            "y": round(float(rec.get("y", 0.0)), 1),
            "width": round(float(rec.get("width", 0.0)), 1),
            "height": round(float(rec.get("height", 0.0)), 1),
            "node_type": rec.get("node_type", "paragraph"),
        })
    return {
        "page": pageid,
        "blocks": recs,
        "gate_verdict": (getattr(conv, "gate_verdicts", {}) or {}).get(pageid),
    }


# ── 页面全量 dump ─────────────────────────────────────────────────────────


def dump_page(conv, ltpage) -> dict:
    """组装单页全阶段 dump（供 run_pipeline_dump 侧通道使用）。"""
    pageid = getattr(ltpage, "pageid", 0)
    try:
        from pdf2zh.v3.geometry import chars_from_ltpage
        chars = chars_from_ltpage(ltpage, page_num=pageid)
    except Exception as e:  # noqa: BLE001
        log.debug("pipeline_dump: chars failed p%s: %s", pageid, e)
        chars = []
    toc_entries = toc_dump(conv, pageid)
    try:
        from pdf2zh.v3.toc_tree import build_toc_tree
        tree = build_toc_tree(toc_entries)
    except Exception as e:  # noqa: BLE001
        log.debug("pipeline_dump: toc tree failed p%s: %s", pageid, e)
        tree = {"roots": [], "nodes": [], "max_depth": 0}
    # V11: 规范页面模型（唯一数据树）+ 标注 Pass（TOC/公式/样式只写 metadata）
    page_model = None
    try:
        from pdf2zh.v3.canonical_page import (
            annotate_formulas, annotate_style, annotate_toc,
            annotate_toc_scan, build_page_model,
        )
        pm = build_page_model(ltpage, page_num=pageid)
        from pdf2zh.v3.document_model import annotate_roles
        annotate_roles(pm)
        pm.metadata["toc_annotated_blocks"] = annotate_toc(pm, toc_entries)
        # legacy 检测失败时（段落合并等）：从树内块文本自扫描目录行
        pm.metadata["toc_scan_blocks"] = annotate_toc_scan(pm)
        pm.metadata["math_spans"] = annotate_formulas(pm)
        annotate_style(pm)
        page_model = pm.to_dict()
    except Exception as e:  # noqa: BLE001
        log.debug("pipeline_dump: canonical model failed p%s: %s", pageid, e)
    return {
        "page": pageid,
        "glyphs": glyph_dump(ltpage),
        "runs": run_dump(chars, page_num=pageid),
        "lines": line_dump(chars, page_num=pageid),
        "blocks": block_dump(chars, page_num=pageid),
        "toc": toc_entries,
        "toc_tree": tree,
        "page_model": page_model,
        "translations": translation_dump(conv, pageid),
        "layout": layout_dump(conv, pageid),
    }


# ── CLI：不翻译的纯提取 dump（回答「提取层是否已坏」） ───────────────────


class _IdentityTranslator:
    lang_in = "en"
    lang_out = "zh-CN"

    def translate(self, text: str) -> str:
        return text


def dump_pdf_pipeline(path: str, out_dir: str = "",
                      max_pages: Optional[int] = None) -> List[dict]:
    """对真实 PDF 跑完整提取管线（恒等翻译器），逐页导出 JSON dump。

    返回 manifest（每页 dump 文件路径 + 该页 replacement 计数）。
    失败页记 error 不中断；pdfminer 不可用/解析失败返回空列表。
    """
    import io
    from unittest.mock import Mock

    from pdfminer.pdfdocument import PDFDocument
    from pdfminer.pdfinterp import PDFResourceManager
    from pdfminer.pdfpage import PDFPage
    from pdfminer.pdfparser import PDFParser

    from pdf2zh.converter import TranslateConverter
    from pdf2zh.pdfinterp import PDFPageInterpreterEx

    manifest: List[dict] = []
    try:
        with open(path, "rb") as f:
            stream = io.BytesIO(f.read())
        doc = PDFDocument(PDFParser(stream))
    except Exception as e:  # noqa: BLE001
        log.error("pipeline_dump: cannot parse %s: %s", path, e)
        return manifest

    rsrcmgr = PDFResourceManager()
    with patch("pdf2zh.converter.build_translator") as bt:
        bt.return_value = _IdentityTranslator()
        conv = TranslateConverter(
            rsrcmgr,
            layout={},
            lang_in="en", lang_out="zh-CN", service="stub",
        )
    conv.thread = 1
    conv.noto_name = "noto"
    noto = Mock()
    noto.char_lengths.return_value = [8.0]
    noto.has_glyph.return_value = True
    conv.noto = noto
    conv.fontmap, conv.fontid = {}, {}
    conv.text_metrics = {}
    from pdf2zh.collision_resolver import CollisionResolver
    conv.collision_resolver = CollisionResolver()
    conv.translator = _IdentityTranslator()
    conv.emit_ir = False
    conv.relayout_gate = None
    conv.pipeline_dump = True
    conv.pipeline_dumps = {}
    conv.document_model_enabled = True
    conv.document_model = None

    try:
        interp = PDFPageInterpreterEx(rsrcmgr, conv, {})
        for pageno, page in enumerate(PDFPage.create_pages(doc)):
            if max_pages is not None and pageno >= max_pages:
                break
            page.pageno = pageno
            page.page_xref = pageno
            interp.process_page(page)
    except Exception as e:  # noqa: BLE001
        log.error("pipeline_dump: interpreter failed: %s", e)

    for pageid, dump in (getattr(conv, "pipeline_dumps", {}) or {}).items():
        replacement = sum(
            1 for g in dump.get("glyphs", []) if g.get("is_replacement"))
        entry = {"page": pageid,
                 "replacement_glyphs": replacement,
                 "blocks": len(dump.get("blocks", [])),
                 "toc_entries": len(dump.get("toc", []))}
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
            out_path = os.path.join(out_dir, f"page_{pageid}.json")
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(dump, f, ensure_ascii=False, indent=2)
            entry["dump"] = out_path
        manifest.append(entry)
    # 文档统一模型（跨页累积）落盘
    dm = getattr(conv, "document_model", None)
    if dm is not None and out_dir:
        try:
            dm_path = os.path.join(out_dir, "document_model.json")
            with open(dm_path, "w", encoding="utf-8") as f:
                json.dump(dm.to_dict(), f, ensure_ascii=False, indent=2)
            manifest.append({"page": "all", "document_model": dm_path,
                             "stats": dm.stats()})
        except Exception as e:  # noqa: BLE001
            log.debug("pipeline_dump: document model save failed: %s", e)
    return manifest


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(
        prog="pdf2zh.pipeline_dump",
        description="逐阶段 dump：定位乱码发生在 提取/行恢复/TOC/翻译/渲染 哪一层",
    )
    parser.add_argument("pdf")
    parser.add_argument("--out", default="",
                        help="输出目录（缺省只打印 manifest，不落盘）")
    parser.add_argument("--max-pages", type=int, default=None)
    args = parser.parse_args(argv)
    manifest = dump_pdf_pipeline(args.pdf, out_dir=args.out,
                                 max_pages=args.max_pages)
    for m in manifest:
        tag = "CORRUPT" if m["replacement_glyphs"] else "clean"
        print(f"page {m['page']}: {tag} "
              f"replacement={m['replacement_glyphs']} "
              f"blocks={m['blocks']} toc_entries={m['toc_entries']} "
              f"dump={m.get('dump', '-')}")
    if not manifest:
        print("no pages dumped (parse failed?)")
        return 1
    return 0


__all__ = [
    "has_replacement", "glyph_dump", "run_dump", "line_dump", "block_dump",
    "toc_confidence", "toc_dump", "translation_dump", "layout_dump",
    "dump_page", "dump_pdf_pipeline", "main",
]