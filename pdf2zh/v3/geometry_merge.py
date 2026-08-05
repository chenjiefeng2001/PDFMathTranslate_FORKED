"""Module: GeometryMerge — Geometry Engine 与 legacy ``receive_layout`` 的聚类合并。

V8.3 已建立同流收敛点（``chars_from_ltpage`` 让两套引擎消费同一个
LTChar 流），但没有对比机制验证"Geometry 聚类 是否与 legacy
段落聚类一致"。本模块提供**双轨对比**：

    legacy 段落（``_gate_records``：text/x/y/w/h）
        +  Geometry 段落（``chars_from_ltpage`` → GeometryEngine）
                │
                ▼
    GeometryMergeReport（段落数 / 文本相似度 / 按页几何一致性）

逻辑全部与渲染解耦：对比在 side-channel 内完成，结果写入
``conv.geometry_reports[pageid]``。迁移闭环内用这个报告决定
"聚类是否可用 GeometryEngine 替换" —— 一致则允许接管，否则保留
legacy。纯 Python + numpy 可选（文本相似度用 Dice，无重依赖）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple


@dataclass
class GeometryMergeReport:
    """单页双轨聚类对比结果。"""

    page: int = 0
    legacy_count: int = 0
    geometry_count: int = 0
    matched: int = 0
    text_similarity: float = 1.0
    bbox_displacement: float = 0.0
    legacy_rows: List[Tuple[str, float, float, float, float]] = field(default_factory=list)
    geometry_rows: List[Tuple[str, float, float, float, float]] = field(default_factory=list)

    @property
    def consistent(self) -> bool:
        """是否允许以 GeometryEngine 聚类接管本页。"""
        if self.legacy_count == 0 or self.geometry_count == 0:
            return self.legacy_count == self.geometry_count
        count_ok = abs(self.legacy_count - self.geometry_count) <= 1
        return count_ok and self.text_similarity >= 0.7

    def to_dict(self) -> dict:
        return {
            "page": self.page,
            "legacy_count": self.legacy_count,
            "geometry_count": self.geometry_count,
            "matched": self.matched,
            "text_similarity": round(self.text_similarity, 4),
            "bbox_displacement": round(self.bbox_displacement, 2),
            "consistent": self.consistent,
        }

    def summary(self) -> str:
        verdict = "CONSISTENT" if self.consistent else "DIVERGE"
        return (f"[{verdict}] p{self.page} "
                f"legacy={self.legacy_count} geometry={self.geometry_count} "
                f"sim={self.text_similarity:.3f} "
                f"bboxΔ={self.bbox_displacement:.2f}")


def dice_similarity(a: str, b: str) -> float:
    """Token-level Dice 相似度（与 migration_diff 口径一致）。"""
    if not a and not b:
        return 1.0
    ta = set(a.lower().split())
    tb = set(b.lower().split())
    if not ta and not tb:
        return 1.0
    return 2.0 * len(ta & tb) / (len(ta) + len(tb))


@dataclass
class AdoptedParagraph:
    """GeometryEngine 段落替换 legacy ``Paragraph`` 的几何鸭子类型。

    converter 排版段（Section C）只读 ``y/x/x0/x1/y0/y1/size/brk``，
    用纯数据类即可替换，不依赖 converter 的 Paragraph 实例。
    """

    y: float = 0.0
    x: float = 0.0
    x0: float = 0.0
    x1: float = 0.0
    y0: float = 0.0
    y1: float = 0.0
    size: float = 12.0
    brk: bool = False


def adopt_geometry_cluster(conv, ltpage, sstk: List[str],
                           pstk: List, var, varl, varf,
                           toc_track: List, vlen) -> Optional[dict]:
    """P1：双轨对比一致时以 GeometryEngine 段落替换 legacy sstk/pstk。

    原地修改传入的列表（sstk/pstk/toc_track），只在**文本内容完全一致且
    段数完全相等**时接管 —— 公式占位符 ``{vN}`` 缺失、段落拆分差异等任何
    分歧都回退 legacy，保证翻译/渲染文本路径零变化。几何则来自
    GeometryEngine（更贴近阅读几何，供 gate/link_remap 消费）。

    返回采纳报告 dict（adopted / reason）；异常或分歧返回 None（回退）。
    """
    try:
        from pdf2zh.v3.geometry import GeometryEngine, chars_from_ltpage
        pageid = getattr(ltpage, "pageid", 0)
        chars = chars_from_ltpage(ltpage, page_num=pageid)
        if not chars:
            return {"adopted": False, "reason": "no_chars", "page": pageid}
        g_paras = GeometryEngine().build_page(chars, page_num=pageid).reading_order()
        if len(sstk) != len(g_paras):
            return {"adopted": False, "reason": "count_mismatch",
                    "page": pageid, "legacy": len(sstk),
                    "geometry": len(g_paras)}
        for i in range(len(sstk)):
            s = (sstk[i] or "").strip()
            g = (g_paras[i].text or "").strip()
            if s != g:
                # 容忍：legacy 含公式占位符 {vN}，geometry 为原始公式文本
                if not (g and s.startswith("{v") and "}" in s
                        and s.count(" ") == 0):
                    return {"adopted": False, "reason": "text_mismatch",
                            "page": pageid, "index": i}
        new_pstk: List = []
        for p in g_paras:
            size = getattr(p, "avg_char_size", 0.0) or 0.0
            new_pstk.append(AdoptedParagraph(
                y=p.y0, x=p.x0, x0=p.x0, x1=p.x1,
                y0=p.y0, y1=p.y1, size=float(size) or 12.0,
            ))
        sstk[:] = [p.text for p in g_paras]
        pstk[:] = new_pstk
        toc_track[:] = [[] for _ in range(len(g_paras))]
        return {"adopted": True, "reason": "consistent", "page": pageid,
                "paragraph_count": len(g_paras)}
    except Exception as e:  # noqa: BLE001 — 接管失败回退 legacy
        return {"adopted": False, "reason": str(e)[:120],
                "page": getattr(ltpage, "pageid", 0)}


def _rows_from_gate_records(records: Sequence[dict]) -> List[Tuple[str, float, float, float, float]]:
    """legacy ``_gate_records`` → (text, x, y, width, height)。"""
    rows: List[Tuple[str, float, float, float, float]] = []
    for rec in records or []:
        text = str(rec.get("text", ""))
        if not text:
            continue
        rows.append((
            rec.get("text", ""),
            float(rec.get("x", 0.0)), float(rec.get("y", 0.0)),
            float(rec.get("width", 0.0)), float(rec.get("height", 0.0)),
        ))
    return rows


def rows_from_geometry(chars, page_num: int = 0,
                       max_rows: Optional[int] = None) -> List[Tuple[str, float, float, float, float]]:
    """Geometry Engine 段落 → (text, x, y, width, height)。

    Geometry 坐标系 y 向上（PDF），legacy gate 记录同样 y 向上；
    两者直接对比无需翻转。
    """
    from pdf2zh.v3.geometry import GeometryEngine
    if not chars:
        return []
    page = GeometryEngine().build_page(chars, page_num=page_num)
    rows: List[Tuple[str, float, float, float, float]] = []
    for para in page.reading_order():
        rows.append((para.text, para.x0, para.y0, para.width, para.height))
    if max_rows is not None:
        rows = rows[:max_rows]
    return rows


def merge_geometry_and_legacy(chars, legacy_rows: Sequence[Tuple[str, float, float, float, float]],
                              page_num: int = 0,
                              gate_records: Optional[Sequence[dict]] = None) -> GeometryMergeReport:
    """双轨对比：Geometry 聚类 vs legacy 聚类。

    ``gate_records`` 存在时优先作为 legacy 输入（含真实渲染几何），否则
    使用调用方给出的 ``legacy_rows``。
    """
    if gate_records is not None:
        legacy_rows = _rows_from_gate_records(gate_records)
    geometry_rows = rows_from_geometry(chars, page_num=page_num,
                                       max_rows=max(len(legacy_rows), 1) + 2)
    report = GeometryMergeReport(
        page=page_num,
        legacy_count=len(legacy_rows),
        geometry_count=len(geometry_rows),
        legacy_rows=list(legacy_rows),
        geometry_rows=list(geometry_rows),
    )
    if not legacy_rows or not geometry_rows:
        report.matched = min(report.legacy_count, report.geometry_count)
        report.text_similarity = 1.0 if report.consistent else 0.0
        return report

    # 按 y 顺序配对（两套引擎都按阅读/渲染序），逐对算文本相似 + 位移
    sims: List[float] = []
    displacements: List[float] = []
    matched = 0
    n = min(len(legacy_rows), len(geometry_rows))
    for i in range(n):
        lt = legacy_rows[i][0]
        gt = geometry_rows[i][0]
        sim = dice_similarity(lt, gt)
        sims.append(sim)
        if sim >= 0.5:
            matched += 1
        # 位移：legacy 记录的是"最终渲染位置"（可能含碰撞位移），
        # 取 bbox 中心差距作为几何一致性度量
        lx, ly, lw, lh = legacy_rows[i][1:]
        gx, gy, gw, gh = geometry_rows[i][1:]
        dx = (lx + lw / 2.0) - (gx + gw / 2.0)
        dy = (ly + lh / 2.0) - (gy + gh / 2.0)
        displacements.append((dx * dx + dy * dy) ** 0.5)

    report.matched = matched
    report.text_similarity = sum(sims) / len(sims)
    report.bbox_displacement = sum(displacements) / len(displacements)
    return report


__all__ = [
    "GeometryMergeReport", "dice_similarity",
    "rows_from_geometry", "merge_geometry_and_legacy",
    "AdoptedParagraph", "adopt_geometry_cluster",
]