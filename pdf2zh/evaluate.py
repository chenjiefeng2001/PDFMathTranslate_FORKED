"""Document-Level Evaluation — 阶段九评测体系（真实 PDF 文档级指标）。

在**真实 PDF 输出**上计算四组可量化指标（对齐路线图阶段九）：

    Geometry    — 位置误差 / bbox 位移 / 行漂移 / 页漂移 / 重叠率
    Structure   — 标题保留率 / 题注保留率 / 目录识别 / 阅读顺序一致性
    Translation — 目标语覆盖率 / 原文残留估计 / 文本覆盖率
    Rendering   — 碰撞率 / 溢出率 / 空白得分 / 版面密度

与传统 BLEU 不同，这些指标全部在几何与结构层计算，不需要参考译文。
输入是一对真实 PDF（源文档 + 翻译输出），无头可测、可入 CI。

CLI::

    python -m pdf2zh.evaluate source.pdf output-mono.pdf [--json report.json]

Library::

    from pdf2zh.evaluate import evaluate_translation, EvaluationReport
    report = evaluate_translation("paper.pdf", "paper-mono.pdf")
    print(report.summary())
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from pdf2zh.v3.geometry import GeometryEngine
from pdf2zh.v3.migration_diff import snapshot_ir
from pdf2zh.v3.structure import BlockRole, StructureClassifier, to_document_ir

# ── 指标阈值 ──────────────────────────────────────────────────────────────

OVERLAP_IOU_THRESHOLD = 0.15
LATIN_DOMINATED_RATIO = 0.85

_MARGIN_RATIO = 0.02


@dataclass
class DocumentProfile:
    """从真实 PDF 提取的文档级画像（几何 + 结构，供评估/回归基线）。"""

    path: str = ""
    page_count: int = 0
    char_count: int = 0
    line_count: int = 0
    paragraph_count: int = 0
    pages: List = field(default_factory=list)  # List[PageGeometry]
    headings: int = 0
    captions: int = 0
    toc_entries: int = 0
    formulas: int = 0
    page_numbers: int = 0
    total_area: float = 0.0
    covered_area: float = 0.0
    cjk_count: int = 0
    latin_dominated_lines: int = 0
    duplicate_chars: int = 0

    @property
    def whitespace_ratio(self) -> float:
        return 1.0 - (self.covered_area / self.total_area) if self.total_area else 0.0

    @property
    def duplicate_rate(self) -> float:
        """同一位置被重复绘制的字符比例（原文重绘/重叠的廉价检测）。"""
        return (self.duplicate_chars / self.char_count) if self.char_count else 0.0

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "pages": self.page_count,
            "chars": self.char_count,
            "lines": self.line_count,
            "paragraphs": self.paragraph_count,
            "headings": self.headings,
            "captions": self.captions,
            "toc_entries": self.toc_entries,
            "formulas": self.formulas,
            "page_numbers": self.page_numbers,
            "whitespace_ratio": round(self.whitespace_ratio, 4),
            "cjk_count": self.cjk_count,
            "latin_dominated_lines": self.latin_dominated_lines,
            "duplicate_rate": round(self.duplicate_rate, 4),
        }


def _is_cjk_char(ch: str) -> bool:
    code = ord(ch)
    return (0x4E00 <= code <= 0x9FFF or 0x3400 <= code <= 0x4DBF
            or 0x3000 <= code <= 0x303F or 0xFF00 <= code <= 0xFFEF
            or 0x20000 <= code <= 0x2EBEF)


def build_profile(path: str, target_lang: str = "zh-CN",
                  max_pages: Optional[int] = None) -> DocumentProfile:
    """从 PDF 文件构建 DocumentProfile（几何 + 结构 + 统计）。"""
    import fitz
    engine = GeometryEngine()
    classifier = StructureClassifier()
    prof = DocumentProfile(path=path)
    doc = fitz.open(path)
    try:
        prof.page_count = doc.page_count
        pages = []
        seen_chars: Dict[tuple, str] = {}
        for i in range(prof.page_count):
            if max_pages is not None and i >= max_pages:
                break
            page_doc = doc.load_page(i)
            raw_chars = [c for c in _chars_of_page(page_doc, i) if c is not None]
            # 同一位置重复绘制检测（原文重绘/重叠）
            for c in raw_chars:
                key = (i, round(c.x0, 1), round(c.y0, 1), round(c.x1, 1),
                       round(c.y1, 1))
                if key in seen_chars and seen_chars[key] == c.text:
                    prof.duplicate_chars += 1
                else:
                    seen_chars[key] = c.text
            page = engine.build_page(raw_chars, page_num=i)
            pages.append(page)
            body = classifier.estimate_body_font_size([page])
            for para in page.reading_order():
                prof.paragraph_count += 1
                prof.char_count += len(para.text)
                prof.line_count += para.line_count
                block = classifier.classify_paragraph(para, page=page,
                                                      body_font_size=body)
                role = block.role
                if role is BlockRole.HEADING:
                    prof.headings += 1
                elif role is BlockRole.CAPTION:
                    prof.captions += 1
                elif role is BlockRole.TOC_ENTRY:
                    prof.toc_entries += 1
                elif role is BlockRole.FORMULA:
                    prof.formulas += 1
                elif role is BlockRole.PAGE_NUMBER:
                    prof.page_numbers += 1
                text = para.text
                prof.cjk_count += sum(1 for c in text if _is_cjk_char(c))
                latin = sum(1 for c in text if c.isascii() and c.isalpha())
                if len(text) >= 12 and latin / max(len(text), 1) >= LATIN_DOMINATED_RATIO:
                    prof.latin_dominated_lines += 1
                w = para.width
                h = para.height
                if w > 0 and h > 0:
                    prof.covered_area += w * h
            prof.total_area += _page_area(page_doc)
        prof.pages = pages
        # 实际评估页数（max_pages 截断时以评估到的页数为准）
        prof.page_count = len(pages)
    finally:
        doc.close()
    return prof


def _page_area(page) -> float:
    rect = page.rect
    return float(rect.width * rect.height)


def _chars_of_page(page, page_num: int):
    raw = page.get_text("rawdict")
    for block in raw.get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                span_bbox = span.get("bbox")
                size = float(span.get("size", 12.0))
                font = str(span.get("font", ""))
                for ch in span.get("chars", []):
                    cb = ch.get("bbox", span_bbox)
                    if not cb:
                        yield None
                        continue
                    text = ch.get("c", "")
                    if not text:
                        yield None
                        continue
                    from pdf2zh.v3.geometry import Char
                    yield Char(text=text, x0=float(cb[0]), y0=float(cb[1]),
                               x1=float(cb[2]), y1=float(cb[3]),
                               size=size, font=font, page_num=page_num)


# ── 几何指标 ──────────────────────────────────────────────────────────────


def _line_overlap_rate(pages: Sequence) -> float:
    """碰撞率：发生重叠（IoU ≥ 阈值）的行数占比。"""
    if not pages:
        return 0.0
    overlapping = 0
    total = 0
    for page in pages:
        lines = list(page.lines)
        total += len(lines)
        for i in range(len(lines)):
            a = lines[i]
            for j in range(i + 1, len(lines)):
                b = lines[j]
                ox = min(a.x1, b.x1) - max(a.x0, b.x0)
                oy = min(a.y1, b.y1) - max(a.y0, b.y0)
                if ox > 0 and oy > 0:
                    inter = ox * oy
                    union = a.width * a.height + b.width * b.height - inter
                    if union > 0 and inter / union >= OVERLAP_IOU_THRESHOLD:
                        overlapping += 1
                        break
    return overlapping / total if total else 0.0


def _overflow_rate(pages: Sequence, page_w: float = 612.0,
                   page_h: float = 792.0) -> float:
    """溢出率：越过页面边界的行数占比。"""
    total = 0
    overflow = 0
    margin = _MARGIN_RATIO * max(page_w, page_h)
    for page in pages:
        for line in page.lines:
            total += 1
            if line.x0 < -margin or line.x1 > page_w + margin or \
                    line.y1 > page_h + margin or line.y0 < -margin:
                overflow += 1
    return overflow / total if total else 0.0


def _positional_drift(src_pages, tgt_pages) -> Dict[str, float]:
    """位置漂移：按阅读顺序一一对应的段落 bbox 位移（无匹配返回 0）。"""
    pairs = 0
    dx_total = 0.0
    dy_total = 0.0
    for sp, tp in zip(src_pages, tgt_pages):
        for sa, ta in zip(sp.reading_order(), tp.reading_order()):
            pairs += 1
            dx_total += abs(ta.x0 - sa.x0)
            dy_total += abs(ta.y0 - sa.y0)
    return {
        "pairs": pairs,
        "mean_dx": dx_total / pairs if pairs else 0.0,
        "mean_dy": dy_total / pairs if pairs else 0.0,
        "mean_drift": (math.hypot(dx_total, dy_total) / pairs) if pairs else 0.0,
    }


def _page_drift(src_prof: DocumentProfile, tgt_prof: DocumentProfile) -> float:
    """页漂移：翻译前后有内容的页数差 / 源页数（分页一致则 0）。"""
    src_pages = max(1, src_prof.page_count)
    return abs(tgt_prof.page_count - src_pages) / src_pages


# ── 结构指标 ──────────────────────────────────────────────────────────────


def _preservation(src: int, tgt: int) -> float:
    if src <= 0:
        return 1.0 if tgt <= 0 else 0.5
    return min(1.0, tgt / src)


def _reading_order_consistency(src_pages, tgt_pages) -> float:
    """阅读顺序一致性：两文档同页段落数的接近程度。"""
    if not src_pages:
        return 1.0
    scores = []
    for sp, tp in zip(src_pages, tgt_pages):
        sn = len(sp.reading_order())
        tn = len(tp.reading_order())
        scores.append(min(sn, tn) / max(sn, tn) if max(sn, tn) else 1.0)
    return sum(scores) / len(scores) if scores else 1.0


# ── 翻译指标 ──────────────────────────────────────────────────────────────


def _target_coverage(tgt_prof: DocumentProfile, target_lang: str) -> float:
    """目标语覆盖率：CJK 目标语中 CJK 字符占比（公式/保留术语天然拉低）。"""
    if tgt_prof.char_count <= 0:
        return 0.0
    if target_lang.lower().startswith(("zh", "ja", "ko")):
        return tgt_prof.cjk_count / tgt_prof.char_count
    return 1.0 - tgt_prof.cjk_count / tgt_prof.char_count


def _residue_estimate(tgt_prof: DocumentProfile,
                      src_prof: DocumentProfile) -> float:
    """原文残留估计：目标语主导的行中，拉丁字符占比超过阈值的长行比例。"""
    if src_prof.line_count <= 0:
        return 0.0
    return min(1.0, tgt_prof.latin_dominated_lines / src_prof.line_count)


def _text_coverage(src_prof: DocumentProfile, tgt_prof: DocumentProfile) -> float:
    """文本覆盖率：译文字符数 / 原文字符数（膨胀 >1，损失 <1）。"""
    if src_prof.char_count <= 0:
        return 1.0
    return min(1.5, tgt_prof.char_count / src_prof.char_count)


# ── 渲染指标 ──────────────────────────────────────────────────────────────


def _whitespace_score(prof: DocumentProfile) -> float:
    """空白得分：1 - 空白率映射为 0~1（空白越多版面越稀疏，得分越低）。"""
    return max(0.0, min(1.0, 1.0 - prof.whitespace_ratio * 2.0))


def _density_score(prof: DocumentProfile) -> float:
    """版面密度得分：每页行数适中为佳（稀疏/过密都扣分）。"""
    if prof.page_count <= 0:
        return 1.0
    per_page = prof.line_count / prof.page_count
    if per_page <= 0:
        return 0.0
    ideal = 40.0
    return max(0.0, min(1.0, 1.0 - abs(per_page - ideal) / ideal))


# ── 报告 ──────────────────────────────────────────────────────────────────


@dataclass
class EvaluationReport:
    """文档级评测报告（四组指标 + 综合得分 + IR 快照）。"""

    geometry: Dict[str, float] = field(default_factory=dict)
    structure: Dict[str, float] = field(default_factory=dict)
    translation: Dict[str, float] = field(default_factory=dict)
    rendering: Dict[str, float] = field(default_factory=dict)
    source_profile: Dict[str, Any] = field(default_factory=dict)
    target_profile: Dict[str, Any] = field(default_factory=dict)
    overall_score: float = 0.0
    ir_snapshot: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "overall_score": round(self.overall_score, 1),
            "geometry": {k: round(v, 4) for k, v in self.geometry.items()},
            "structure": {k: round(v, 4) for k, v in self.structure.items()},
            "translation": {k: round(v, 4) for k, v in self.translation.items()},
            "rendering": {k: round(v, 4) for k, v in self.rendering.items()},
            "source": self.source_profile,
            "target": self.target_profile,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    def summary(self) -> str:
        return (
            f"overall={self.overall_score:.1f}/100 "
            f"geometry={self.geometry.get('geometry_score', 0):.1f} "
            f"structure={self.structure.get('structure_score', 0):.1f} "
            f"translation={self.translation.get('translation_score', 0):.1f} "
            f"rendering={self.rendering.get('rendering_score', 0):.1f}"
        )


def evaluate_translation(
    source_path: str,
    translated_path: str,
    target_lang: str = "zh-CN",
    max_pages: Optional[int] = None,
    include_ir: bool = True,
    report_dir: Optional[str] = None,
    report_threshold: float = 90.0,
) -> EvaluationReport:
    """评估真实翻译输出：源 PDF vs 译文 PDF。

    ``report_dir`` 非空时，得分低于 ``report_threshold``（默认 90 分）自动
    留存差分快照（P1）：report.json（完整报告）+ source-ir.json /
    target-ir.json（IR 快照）+ diff.json（按语义桶的计数对比）。这为
    「<90 分自动差分快照留存」验收提供可查证的落盘产物。
    """
    src = build_profile(source_path, target_lang=target_lang, max_pages=max_pages)
    tgt = build_profile(translated_path, target_lang=target_lang, max_pages=max_pages)

    overlap = _line_overlap_rate(tgt.pages)
    # 碰撞率 = max(不同基线行重叠率, 同位置重复绘制率)
    collision = max(overlap, tgt.duplicate_rate)
    overflow = _overflow_rate(tgt.pages)
    drift = _positional_drift(src.pages, tgt.pages)
    page_drift = _page_drift(src, tgt)

    geometry_score = 100.0 * max(0.0, 1.0 - collision - overflow - page_drift)
    geometry = {
        "overlap_rate": overlap,
        "duplicate_rate": tgt.duplicate_rate,
        "collision_rate": collision,
        "overflow_rate": overflow,
        "page_drift": page_drift,
        "mean_drift_pt": drift["mean_drift"],
        "mean_dx_pt": drift["mean_dx"],
        "mean_dy_pt": drift["mean_dy"],
        "geometry_score": geometry_score,
    }

    structure = {
        "heading_preservation": _preservation(src.headings, tgt.headings),
        "caption_preservation": _preservation(src.captions, tgt.captions),
        "toc_preservation": _preservation(src.toc_entries, tgt.toc_entries),
        "formula_preservation": _preservation(src.formulas, tgt.formulas),
        "reading_order_consistency": _reading_order_consistency(src.pages, tgt.pages),
        "structure_score": 100.0 * (
            0.35 * _preservation(src.headings, tgt.headings)
            + 0.2 * _preservation(src.captions, tgt.captions)
            + 0.15 * _preservation(src.toc_entries, tgt.toc_entries)
            + 0.1 * _preservation(src.formulas, tgt.formulas)
            + 0.2 * _reading_order_consistency(src.pages, tgt.pages)
        ),
    }

    coverage = _target_coverage(tgt, target_lang)
    residue = _residue_estimate(tgt, src)
    text_cov = _text_coverage(src, tgt)
    translation = {
        "target_coverage": coverage,
        "residue_estimate": residue,
        "text_coverage": text_cov,
        "translation_score": 100.0 * max(0.0,
            0.5 * coverage + 0.3 * (1.0 - residue) + 0.2 * text_cov),
    }

    whitespace = _whitespace_score(tgt)
    density = _density_score(tgt)
    rendering = {
        "collision_rate": collision,
        "overflow_rate": overflow,
        "whitespace_score": whitespace,
        "density_score": density,
        "rendering_score": 100.0 * (
            0.4 * (1.0 - collision) + 0.25 * (1.0 - overflow)
            + 0.2 * whitespace + 0.15 * density
        ),
    }

    overall = (
        0.3 * geometry_score + 0.2 * structure["structure_score"]
        + 0.25 * translation["translation_score"]
        + 0.25 * rendering["rendering_score"]
    )

    report = EvaluationReport(
        geometry=geometry,
        structure=structure,
        translation=translation,
        rendering=rendering,
        source_profile=src.to_dict(),
        target_profile=tgt.to_dict(),
        overall_score=overall,
    )
    if include_ir:
        try:
            ir = to_document_ir(tgt.pages, title=translated_path,
                                target_lang=target_lang)
            report.ir_snapshot = snapshot_ir(ir, title=translated_path)
        except Exception:
            report.ir_snapshot = {}
    if report_dir:
        _retain_report(report, report_dir, source_path,
                       threshold=report_threshold)
    return report


# ── <90 分自动差分快照留存（P1） ────────────────────────────────────────


_IR_BUCKET_KEYS = ("paragraphs", "captions", "tables", "headings",
                   "formulas", "references", "others")


def _retain_report(report: EvaluationReport, report_dir: str,
                   source_path: str, threshold: float = 90.0) -> Optional[str]:
    """得分 < threshold 时留存差分快照到 report_dir/<basename>/。

    产物：
      report.json      — 完整报告（含指标与画像）
      source-ir.json   — 源文档 IR 快照（首次评估时由源画像快照生成）
      target-ir.json   — 译文 IR 快照（report.ir_snapshot）
      diff.json        — 按语义桶的计数对比（节点数/每桶条目数差值）

    返回报告目录路径；得分达标且未强制时不落盘，返回 None。
    """
    if report.overall_score >= threshold:
        return None
    if not report_dir:
        return None
    import os
    basename = os.path.splitext(os.path.basename(source_path))[0]
    out_dir = os.path.join(report_dir, basename)
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "report.json"), "w",
              encoding="utf-8") as f:
        f.write(report.to_json())
    src_ir = report.ir_snapshot
    with open(os.path.join(out_dir, "target-ir.json"), "w",
              encoding="utf-8") as f:
        json.dump(src_ir, f, ensure_ascii=False, indent=2)
    source_snapshot = _source_ir_snapshot(report)
    with open(os.path.join(out_dir, "source-ir.json"), "w",
              encoding="utf-8") as f:
        json.dump(source_snapshot, f, ensure_ascii=False, indent=2)
    diff = _ir_diff(source_snapshot, src_ir)
    with open(os.path.join(out_dir, "diff.json"), "w",
              encoding="utf-8") as f:
        json.dump(diff, f, ensure_ascii=False, indent=2)
    return out_dir


def _source_ir_snapshot(report: EvaluationReport) -> dict:
    """从报告 source 画像重建源文档 IR 快照（bucket 计数）。

    画像缺少逐段 bbox，因此仅保留可复核的统计视图：字符/行/段计数与
    各角色计数，作为与译文 IR 快照对比的粗粒度基线。
    """
    src = report.source_profile or {}
    counts = {
        "paragraphs": src.get("paragraphs", 0),
        "headings": src.get("headings", 0),
        "captions": src.get("captions", 0),
        "toc_entries": src.get("toc_entries", 0),
        "formulas": src.get("formulas", 0),
    }
    return {
        "schema": "pdf2zh.v3.ir-snapshot",
        "version": 1,
        "title": src.get("path", ""),
        "node_count": counts["paragraphs"],
        "source_of": "profile",
        **counts,
    }


def _ir_diff(src: dict, tgt: dict) -> dict:
    """按语义桶对比两个 IR 快照的条目计数（无参考依赖、纯几何结构级）。"""
    buckets: Dict[str, dict] = {}
    keys = set(_IR_BUCKET_KEYS) | {"node_count"}
    for k in sorted(keys):
        s = len(src.get(k, [])) if isinstance(src.get(k, []), list) else \
            int(src.get(k, 0) or 0)
        t = len(tgt.get(k, [])) if isinstance(tgt.get(k, []), list) else \
            int(tgt.get(k, 0) or 0)
        buckets[k] = {"source": s, "target": t, "delta": t - s,
                      "preservation": round(min(1.0, t / s), 4) if s else 1.0}
    changed = {k: v for k, v in buckets.items() if v["delta"] != 0}
    return {
        "schema": "pdf2zh.v3.ir-diff",
        "version": 1,
        "changed_buckets": list(changed),
        "buckets": buckets,
    }


# ── CLI ───────────────────────────────────────────────────────────────────


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pdf2zh.evaluate",
        description="Document-level PDF translation evaluation (geometry / "
                    "structure / translation / rendering metrics).",
    )
    parser.add_argument("source", help="source PDF path")
    parser.add_argument("translated", help="translated (mono) PDF path")
    parser.add_argument("--target-lang", default="zh-CN",
                        help="target language (default: zh-CN)")
    parser.add_argument("--max-pages", type=int, default=None,
                        help="limit evaluation to the first N pages")
    parser.add_argument("--json", metavar="PATH", default=None,
                        help="write the full report as JSON to PATH")
    parser.add_argument("--no-ir", action="store_true",
                        help="skip the IR snapshot section")
    parser.add_argument("--report-dir", metavar="DIR", default=None,
                        help="retain failure snapshots (<90) into DIR/<basename>/")
    parser.add_argument("--report-threshold", type=float, default=90.0,
                        help="score below which snapshots are retained (default 90)")
    args = parser.parse_args(argv)

    report = evaluate_translation(
        args.source, args.translated,
        target_lang=args.target_lang,
        max_pages=args.max_pages,
        include_ir=not args.no_ir,
        report_dir=args.report_dir,
        report_threshold=args.report_threshold,
    )
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            f.write(report.to_json())
        print(f"report written to {args.json}")
    print(report.summary())
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
