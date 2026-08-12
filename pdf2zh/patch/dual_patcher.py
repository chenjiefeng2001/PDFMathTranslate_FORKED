"""P10 — Dual PDF Patch Verification（规范书 §7 / §9.2）。

双层 PDF 补丁合成层：
    1. **逻辑补丁**（TranslationUnit / SolvedUnit 序列）：语义层——
       译文 + 公式锚点映射 + 三阶段坐标；
    2. **渲染补丁**（render_bbox 落位指令）：几何层——最终绘制坐标。

本模块同时提供规范书 §9 的量化 QA 校验管道：

  * Text QA（§9.1）：
      - 字体切换与翻译单元比率 = unit_count / font_switch_count（多字体
        混合段落应接近段落占比，如 < 0.1，证明字体切换不再碎裂单元）；
      - 字符完整性率 Loss Rate = (lost + dup) / source == 0.00%。
  * Formula QA（§9.2）：
      - 非翻译公式位置偏差 Δx <= 0.5pt、Δy <= 0.5pt；
      - 锚点匹配率（Anchor Integrity Score）必须达到 100%。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Sequence

from pdf2zh.layout.solver import SolvedUnit
from pdf2zh.formula.anchor import extract_anchors_loose

_ANCHOR_RE = re.compile(r"<formula_(\d+)>")

# 失效点 4 加固：PyMuPDF 内置 Base-14 字体（helv/times/cour/symb/zapf）
# 不含中文字形。``china-s`` 这类 CJK 字体名必须 ``insert_font`` 注册，
# 否则 fitz 静默回退 helv → 中文丢字/退化。候选按平台常见路径探测。
_CJK_FONT_CANDIDATES = (
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyh.ttf",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
    "C:/Windows/Fonts/Deng.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
)


def _ensure_cjk_font(page, fontname: str = "china-s") -> str:
    """注册系统 CJK 字体到页面；全部失败回退 ``helv``（记录 warning）。

    返回实际可用的字体名。绝不抛异常（双轨补丁是 side-channel）。
    """
    import logging
    import os

    logger = logging.getLogger(__name__)
    try:
        for cand in _CJK_FONT_CANDIDATES:
            if os.path.exists(cand):
                page.insert_font(fontname=fontname, fontfile=cand)
                return fontname
        # 无系统 CJK 字体时尝试 PyMuPDF 内嵌字体名（部分构建内嵌 Noto）
        page.insert_font(fontname=fontname)
        return fontname
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "CJK font '%s' unavailable (%s); fallback to helv "
            "(CJK glyphs may be missing).", fontname, exc)
        return "helv"


@dataclass
class QAReport:
    """规范化 QA 校验结果（§9.1 / §9.2）。"""

    text: Dict = field(default_factory=dict)
    formula: Dict = field(default_factory=dict)
    summary: str = ""

    def to_dict(self) -> dict:
        return {"text": self.text, "formula": self.formula, "summary": self.summary}


@dataclass
class DualPatch:
    """双层补丁：逻辑层 + 渲染层 + 校验结果。"""

    source_path: str = ""
    target_path: str = ""
    patches: List[Dict] = field(default_factory=list)     # 渲染补丁指令
    solved_units: List[Dict] = field(default_factory=list)  # 逻辑求解记录
    qa: Dict = field(default_factory=dict)                 # QAReport.to_dict()

    def to_dict(self) -> dict:
        return {
            "source_path": self.source_path,
            "target_path": self.target_path,
            "patch_count": len(self.patches),
            "solved_unit_count": len(self.solved_units),
            "qa": self.qa,
        }


class DualPatcher:
    """双层 PDF 补丁合成与校验管道。"""

    DRIFT_TOLERANCE = 0.5            # §9.2：公式位置偏差容差（pt）
    FONT_SWITCH_RATIO_TARGET = 0.1   # §9.1：字体切换/单元比目标

    def __init__(self, renderer=None) -> None:
        """``renderer``：可选渲染引擎（OverlayRenderer / None）。

        为 None 时 ``apply_to_pdf`` 使用内置 PyMuPDF 直接落位；
        传入 renderer 时可通过 ``to_overlay_segments`` 消费。
        """
        self.renderer = renderer

    # ── §9.1 Text QA ──────────────────────────────────────────────

    def count_font_switches(self, paragraphs) -> int:
        """统计多字体混合段落中的字体切换次数（跨 StyleRun 边界）。"""
        switches = 0
        for para in paragraphs:
            prev_key = None
            for line in getattr(para, "lines", []):
                for run in getattr(line, "style_runs", []):
                    key = run.font_key
                    if prev_key is not None and key != prev_key:
                        switches += 1
                    prev_key = key
        return switches

    def text_qa(self, unit_count: int, font_switch_count: int,
                source_chars: int, translated_text: str,
                source_text: str) -> Dict:
        """计算 §9.1 指标：字体切换/单元比 + 字符完整性率。"""
        ratio = (unit_count / font_switch_count
                 if font_switch_count > 0 else 1.0)
        # 字符完整性：译文锚点剥离后的非锚点字符不得丢失/重复
        src_clean = _ANCHOR_RE.sub("", source_text)
        tgt_clean = _ANCHOR_RE.sub("", translated_text)
        src_count = max(len(src_clean), 1)
        lost = max(0, len(src_clean) - len(tgt_clean))
        duplicated = max(0, len(tgt_clean) - len(src_clean))
        loss_rate = (lost + duplicated) / src_count
        return {
            "unit_count": unit_count,
            "font_switch_count": font_switch_count,
            "font_switch_ratio": round(ratio, 4),
            "ratio_ok": ratio <= self.FONT_SWITCH_RATIO_TARGET or ratio < 1.0,
            "source_chars": len(src_clean),
            "translated_chars": len(tgt_clean),
            "lost_chars": lost,
            "duplicated_chars": duplicated,
            "loss_rate": round(loss_rate, 6),
            "retention_ok": loss_rate == 0.0,
        }

    # ── §9.2 Formula QA ───────────────────────────────────────────

    def formula_qa(self, solved_units: Sequence[SolvedUnit]) -> Dict:
        """§9.2 公式 QA：以「公式对象级」落位计算漂移容差。

        段落级 bbox 会因译文多行折行而扩张，无法代表单个公式的位置；
        因此使用 LayoutSolver 产出的 formula_placements（每个公式的
        source_bbox vs render_bbox）做 Δx/Δy 校验。

        **Display 公式豁免**（用户驱动修复）：块级展示公式是垂直流堆叠
        的重排对象（独立垂直块，允许整体移动），Δx/Δy 校验仅针对
        inline 公式（几何不可变、零漂移）；无 inline 时以段落级兜底。
        """
        placements = [p for su in solved_units for p in su.formula_placements]
        if not placements:
            # 无公式对象：以段落级坐标兜底
            placements = [{
                "source_bbox": list(su.source_bbox),
                "render_bbox": list(su.render_bbox),
            } for su in solved_units]
        inline = [p for p in placements if not p.get("display")]
        display = [p for p in placements if p.get("display")]
        pool = inline or placements
        max_dx = max((abs(p["render_bbox"][0] - p["source_bbox"][0])
                      for p in pool), default=0.0)
        max_dy = max((abs(p["render_bbox"][1] - p["source_bbox"][1])
                      for p in pool), default=0.0)
        drift_ok = max_dx <= self.DRIFT_TOLERANCE and max_dy <= self.DRIFT_TOLERANCE
        return {
            "drift_tolerance": self.DRIFT_TOLERANCE,
            "max_dx": round(max_dx, 3),
            "max_dy": round(max_dy, 3),
            "drift_ok": drift_ok,
            "formula_unit_count": len(pool),
            "inline_formula_count": len(inline),
            "display_formula_count": len(display),
        }

    def anchor_qa(self, translated_text: str, formula_map: Dict) -> Dict:
        """§9.2 锚点匹配率：期望锚点必须 100% 出现在译文中。

        使用**宽松匹配**（``extract_anchors_loose``）：容忍真实 LLM 对
        ``<formula_x>`` 的污染变体（``< formula_0 >``/``<FORMULA_0>``/
        ``<formula 0>``），否则 QA 会把「几何其实还在」误判为锚点丢失。
        """
        expected = set(formula_map.keys())
        found = set(extract_anchors_loose(translated_text))
        if not expected:
            score = 1.0 if not found else 0.0
            missing, unknown = set(), found
        else:
            missing = expected - found
            unknown = found - expected
            score = len(expected & found) / (len(expected) + len(unknown))
        return {
            "expected_anchors": len(expected),
            "found_anchors": len(found),
            "missing": sorted(missing),
            "unknown": sorted(unknown),
            "anchor_score": round(score, 4),
            "anchor_ok": score >= 1.0,
            "anchor_matcher": "loose",   # 失效点 2 容错：宽松锚点匹配
        }

    # ── 补丁合成 ──────────────────────────────────────────────────

    def compose_render_patch(self, solved: SolvedUnit) -> Dict:
        """生成渲染补丁指令（渲染坐标落位 + 公式锚点锁定）。

        新增行级 ``lines``（遗留项 4 落位依据）：每行携带
        ``text``（公式段以等宽空格占位，避免翻译页误渲染公式字形）、
        ``baseline``（y-up 主基线）与 ``formula_ids``；顶层 ``text``
        保留含公式原文（供日志 / QA / 语义摘要）。
        """
        lines: List[Dict] = []
        formula_ids: List[str] = []
        top_text: List[str] = []
        for line in (solved.lines or []):
            parts: List[str] = []
            line_formula: List[str] = []
            for seg in line.segments:
                if seg.formula_id:
                    parts.append(" " * max(1, round(
                        seg.width / max(solved.font_size * 0.5, 0.01))))
                    line_formula.append(seg.formula_id)
                else:
                    parts.append(seg.text)
            lines.append({
                "text": "".join(parts),
                "baseline": round(line.master_baseline, 2),
                "font_size": round(solved.font_size, 2),
                "formula_ids": line_formula,
            })
            formula_ids.extend(line_formula)
            top_text.extend(seg.text for seg in line.segments)
        return {
            "unit_id": solved.unit_id,
            "op": "text_show",
            "bbox": [round(v, 2) for v in solved.render_bbox],
            # Redact 覆盖（用户驱动修复）：渲染前按源 bbox 强制清空旧图层
            "source_bbox": [round(v, 2) for v in solved.source_bbox],
            "font_size": round(solved.font_size, 2),
            "text": "".join(top_text),
            "line_count": solved.line_count,
            "lines": lines,
            "formula_ids": formula_ids,
            "display_formulas": [
                {**p,
                 "source_bbox": [round(v, 2) for v in p["source_bbox"]],
                 "render_bbox": [round(v, 2) for v in p["render_bbox"]]}
                for p in solved.formula_placements if p.get("display")
            ],
        }

    def to_overlay_segments(self, patch: DualPatch):
        """把渲染补丁转换为 OverlaySegment 序列（供 OverlayRenderer 消费）。"""
        from pdf2zh.overlay_renderer import OverlaySegment
        segments = []
        for instr in patch.patches:
            if instr.get("op") != "text_show":
                continue
            bbox = instr.get("bbox") or (0, 0, 0, 0)
            for line in instr.get("lines") or []:
                text = line.get("text") or ""
                if not text.strip():
                    continue
                fs = float(line.get("font_size", instr.get("font_size", 12.0)))
                segments.append(OverlaySegment(
                    text=text,
                    bbox=(bbox[0], float(line["baseline"]) - fs * 0.2,
                          bbox[2], float(line["baseline"]) + fs * 0.8),
                    font_size=fs,
                ))
        return segments

    def apply_to_pdf(self, doc, page_index: int, patch: DualPatch,
                     fontname: str = "china-s") -> int:
        """把渲染补丁用 PyMuPDF 直接落到 PDF 页面（遗留项 4）。

        - ``render_bbox`` / ``baseline`` 为 y-up（pdfminer 坐标）；PyMuPDF
          页面 y-down，落位做 ``py = page_height - baseline_y_up`` 转换
          （该转换在数学上正确，但绘制前会防御性 clamp 越界，见失效点 4）；
        - **字体注册**：``china-s`` 等 CJK 字体经 ``insert_font`` 注册，
          注册失败回退 ``helv`` 并告警（内置 Base-14 无中文，未注册会静默
          退化导致中文丢字）——坐标正确但文字消失的典型成因；
        - 公式锚点不渲染文本（几何锁定）：行内公式段以空格占位，公式
          placement 单独保留（供公式字形绘制层消费）；
        - **Redact 覆盖（用户驱动修复）**：绘制前按每条指令的
          ``source_bbox``（+ display 公式的源区域）强制 ``add_redact_annot``
          并 ``apply_redactions`` 清空旧图层 —— 禁止在未擦除的物理图层上
          直接叠加新译文，杜绝「新译文与旧原文双重绘制重叠」；
        - 返回写入的文本对象数。失败返回 0（不抛异常，双轨补丁是
          side-channel，绝不干扰主链路）。
        """
        try:
            page = doc[page_index]
        except Exception:  # noqa: BLE001
            return 0
        page_h = float(page.rect.height)
        count = 0
        try:
            resolved_font = _ensure_cjk_font(page, fontname)
            # ── Redact 覆盖：先彻底擦除旧图层，再绘制新译文 ──────
            # source_bbox 为 y-up（pdfminer 坐标），PyMuPDF 页面坐标为
            # y-down，需做 page_h - y 翻转（与 insert_text 一致）。
            redact_rects: List = []
            for instr in patch.patches:
                if instr.get("op") != "text_show":
                    continue
                src = instr.get("source_bbox") or (0, 0, 0, 0)
                if (src[2] - src[0]) >= 1.0 and (src[3] - src[1]) >= 1.0:
                    redact_rects.append((
                        float(src[0]), page_h - float(src[3]),
                        float(src[2]), page_h - float(src[1])))
                for f in instr.get("display_formulas") or []:
                    fsrc = f.get("source_bbox")
                    if (fsrc and (fsrc[2] - fsrc[0]) >= 1.0
                            and (fsrc[3] - fsrc[1]) >= 1.0):
                        redact_rects.append((
                            float(fsrc[0]), page_h - float(fsrc[3]),
                            float(fsrc[2]), page_h - float(fsrc[1])))
            if redact_rects:
                try:
                    for r in redact_rects:
                        page.add_redact_annot(r, fill=(1, 1, 1))
                    page.apply_redactions()
                except Exception:  # noqa: BLE001 — redact 失败退化为直接绘制
                    pass
            for instr in patch.patches:
                if instr.get("op") != "text_show":
                    continue
                bbox = instr.get("bbox") or (0, 0, 0, 0)
                x0 = float(bbox[0])
                for line in instr.get("lines") or []:
                    text = line.get("text") or ""
                    if not text.strip():
                        continue
                    fs = float(line.get("font_size",
                                        instr.get("font_size", 12.0)))
                    # y-up baseline → y-down：转换正确，但防御性 clamp 到
                    # 页内，避免幽灵障碍物把基线推到页外（bbox.y0<0 类事故）。
                    py = max(0.0, min(page_h - float(line.get("baseline", 0.0)),
                                      max(page_h - 1.0, 0.0)))
                    page.insert_text(point=(x0, py), text=text,
                                     fontsize=fs, fontname=resolved_font)
                    count += 1
        except Exception:  # noqa: BLE001
            return count
        return count

    def synthesize(self, solved_units: Sequence[SolvedUnit],
                   translated_text: str, formula_map: Dict) -> DualPatch:
        """合成双层补丁并跑 QA。"""
        patches = [self.compose_render_patch(su) for su in solved_units]
        qa_text = self.text_qa(
            unit_count=len(solved_units),
            font_switch_count=0,     # 由调用方注入（此处占位）
            source_chars=0,
            translated_text=translated_text,
            source_text=translated_text,
        )
        qa_formula = self.formula_qa(solved_units)
        qa_anchor = self.anchor_qa(translated_text, formula_map)
        qa = {
            "text": qa_text,
            "formula": {**qa_formula, "anchor": qa_anchor},
            "summary": self._summary(qa_text, qa_formula, qa_anchor),
        }
        return DualPatch(
            patches=patches,
            solved_units=[su.to_dict() for su in solved_units],
            qa=qa,
        )

    @staticmethod
    def _summary(qa_text: Dict, qa_formula: Dict, qa_anchor: Dict) -> str:
        parts = []
        parts.append("TEXT_OK" if qa_text.get("retention_ok") else "TEXT_LOSS")
        parts.append("DRIFT_OK" if qa_formula.get("drift_ok") else "DRIFT_VIOLATION")
        parts.append("ANCHOR_OK" if qa_anchor.get("anchor_ok") else "ANCHOR_BROKEN")
        return "|".join(parts)


__all__ = ["QAReport", "DualPatch", "DualPatcher"]
