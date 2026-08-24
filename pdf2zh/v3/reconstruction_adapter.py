"""P5–P10 主链路接管适配器（阶段 3 接线）。

把 P5–P10 重建管线的输出（``LogicalParagraph``/``SolvedUnit``）真正接回
legacy ``receive_layout`` 渲染主链路：

    legacy sstk/pstk ──► 文本集一致性配对 ──► SolvedUnit 几何接管 ──►
        翻译 + gen_op_txt 渲染（legacy 引擎，公式走旧 {vN} 机制）

设计原则（对齐 ``adopt_geometry_cluster`` 契约 + 阶段 3 路线图）：

1. **文本集完全一致才接管**：legacy 段落文本（含 ``{vN}`` 公式占位符）与
   重建段语义文本（含 ``<formula_N>`` 锚点）经公式占位符归一化 + 空白折叠
   后一致，才能接管。任何分歧回退 legacy，保证翻译/渲染文本路径零变化。
2. **sstk 文本保持 legacy**：只替换 ``pstk`` 几何（鸭子类型，converter 排版
   只读 ``y/x/x0/x1/y0/y1/size/brk``）。公式占位符 ``{vN}`` 原样保留 →
   公式渲染走旧 ``{vN}`` 逐字形还原机制，公式位置零漂移。
3. **几何来自 ``SolvedUnit.render_bbox``**：P9 LayoutSolver 的三阶段坐标
   （source→translated→render）已做页面边界防御夹紧，直接替换段落容器。
4. **Level 2 合并接管（修复多字体段落语义割裂）**：当重建段 j 的语义文本
   恰好是 legacy 段 i..k 的拼接（归一化后），把 i..k 合并为一个渲染段落，
   ``sstk/toc_track/pfkstk`` 同步压缩 —— LLM 获得完整自然段上下文。
5. **TOC 页永不接管**：``toc_track`` 非空的段保持 legacy（目录行逐字符
   几何保护不能被段落合并破坏）。

返回接管报告 dict（``adopted``/``reason``/``level``/``merged_paragraphs``）；
``run_reconstruction_channel`` 依据它把 ``render_source`` 标注为
``reconstructed``。
"""

from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional, Sequence, Tuple

from pdf2zh.v3.geometry_merge import AdoptedParagraph

log = logging.getLogger(__name__)

_LEGACY_FORMULA_RE = re.compile(r"\{v(\d+)\}")
_ANCHOR_FORMULA_RE = re.compile(r"<formula_(\d+)>")
_LEGACY_FORMULA_KEY_RE = re.compile(r"\{v\d+\}")
_ANCHOR_FORMULA_KEY_RE = re.compile(r"<formula_\d+>")


def normalize_formula_tokens(text: Optional[str]) -> str:
    """公式占位符归一化 + 空白折叠（比较键）。

    legacy ``{v0}`` 与重建锚点 ``<formula_0>`` 均折叠为 ``{formula}``；
    ``\\n``/连续空白折叠为单空格，消除\"行连接符\"差异。
    """
    if text is None:
        return ""
    t = _LEGACY_FORMULA_KEY_RE.sub("{formula}", str(text))
    t = _ANCHOR_FORMULA_KEY_RE.sub("{formula}", t)
    return re.sub(r"\s+", " ", t).strip()


def legacy_to_anchor_text(text: str) -> str:
    """legacy 译文 ``{vN}`` → P6 锚点 ``<formula_N>``（供 solver 用真实译文求解）。

    legacy 渲染引擎用 ``{vN}`` 占位公式，P6/P9 的 ``LayoutSolver`` 只认
    ``<formula_N>`` 锚点；两者按页内公式出现顺序共享同一编号。
    """
    return _LEGACY_FORMULA_RE.sub(
        lambda m: f"<formula_{m.group(1)}>", str(text) if text is not None else ""
    )


def _pair_key(
    text: Optional[str],
    legacy_formula_texts: Optional[Dict[int, str]] = None,
    recon_formula_texts: Optional[Dict[int, str]] = None,
) -> str:
    """字符序列比较键：去空白 + 公式占位符展开为**实际字形字符**。

    legacy 的 ``{vN}`` 与重建锚点 ``<formula_N>`` 都替换为该公式的实际字符
    序列（legacy 取 ``var[vid]`` 字形拼接，重建取 ``FormulaObject.text``）。
    这统一了 legacy ``vflag`` 与 P6 公式提取的识别边界差异（如斜体书名 _T_：
    legacy 判为公式 ``{v0}``，P6 判为普通文本 "T" —— 展开后两侧字符序列一致）。
    公式文本缺失时回退 ``{f}`` 标记（保持占位可见，避免误匹配）。
    """
    if text is None:
        return ""
    t = str(text)
    if legacy_formula_texts:
        t = _LEGACY_FORMULA_RE.sub(
            lambda m: legacy_formula_texts.get(int(m.group(1)), "{f}"), t
        )
    else:
        t = _LEGACY_FORMULA_KEY_RE.sub("{f}", t)
    if recon_formula_texts:
        t = _ANCHOR_FORMULA_RE.sub(
            lambda m: recon_formula_texts.get(int(m.group(1)), "{f}"), t
        )
    else:
        t = _ANCHOR_FORMULA_KEY_RE.sub("{f}", t)
    return re.sub(r"\s+", "", t)


def _char_key(text: Optional[str]) -> str:
    """无公式展开的字符序列键（兼容旧调用/诊断）。"""
    return _pair_key(text)


def pair_legacy_to_reconstructed(
    sstk: Sequence[str],
    recon_texts: Sequence[str],
    legacy_formula_texts: Optional[Dict[int, str]] = None,
    recon_formula_texts: Optional[Dict[int, str]] = None,
) -> Optional[List[Tuple[int, int, int]]]:
    """贪心配对 legacy 段 → 重建段（返回 (legacy_start, legacy_end, recon_idx)）。

    - 比较键为**字符序列**（去空白 + 公式占位符展开为实际字形字符）：legacy
      推断空格、公式识别边界（vflag vs P6）与 P5 字形直拼视为同一文本 ——
      修复真实 PDF 上「测试全过、实际 100% text_mismatch」的结构性鸿沟。
    - 单段字符序列一致 → Level 1（start == end）。
    - legacy 段 i..k 拼接与重建段 j 一致 → Level 2 合并。
    - 重建段 j..k 拼接与 legacy 段 i 一致（**反向合并**：P5 按视觉行拆段、
      legacy 按收字符合并）→ Level 2 合并。
    - 全局字符序列不一致或无法完全配对 → None（回退 legacy）。
    """
    n, m = len(sstk), len(recon_texts)
    if n == 0 or m == 0:
        return None
    if _pair_key(
        "".join(str(s) for s in sstk), legacy_formula_texts, recon_formula_texts
    ) != _pair_key(
        "".join(str(r) for r in recon_texts), legacy_formula_texts, recon_formula_texts
    ):
        return None
    pairs: List[Tuple[int, int, int]] = []
    i = j = 0
    while i < n and j < m:
        if _pair_key(sstk[i], legacy_formula_texts, recon_formula_texts) == _pair_key(
            recon_texts[j], legacy_formula_texts, recon_formula_texts
        ):
            pairs.append((i, i, j))
            i += 1
            j += 1
            continue
        # 尝试把 legacy i..k 拼接匹配重建段 j
        joined = str(sstk[i])
        target = _pair_key(recon_texts[j], legacy_formula_texts, recon_formula_texts)
        k = i + 1
        found = False
        while k < n:
            joined += str(sstk[k])
            norm_joined = _pair_key(joined, legacy_formula_texts, recon_formula_texts)
            if norm_joined == target:
                pairs.append((i, k, j))
                i = k + 1
                j += 1
                found = True
                break
            if target and not target.startswith(norm_joined):
                break  # 拼接已超出目标前缀，不再继续
            k += 1
        if found:
            continue
        # 尝试把重建段 j..k 拼接匹配 legacy 段 i（反向合并）
        rjoined = str(recon_texts[j])
        ltarget = _pair_key(sstk[i], legacy_formula_texts, recon_formula_texts)
        k = j + 1
        while k < m:
            rjoined += str(recon_texts[k])
            norm_rjoined = _pair_key(rjoined, legacy_formula_texts, recon_formula_texts)
            if norm_rjoined == ltarget:
                pairs.append((i, i, j))
                i += 1
                j = k + 1
                found = True
                break
            if ltarget and not ltarget.startswith(norm_rjoined):
                break
            k += 1
        if not found:
            return None
    if i == n and j == m:
        return pairs
    return None


def _try_match(
    sstk,
    recon_texts,
    li: int,
    ri: int,
    legacy_formula_texts=None,
    recon_formula_texts=None,
) -> Optional[Tuple[int, int, int, int]]:
    """在 (li, ri) 处尝试三种匹配；成功返回 (ls, le, rs, re)，失败返回 None。"""
    if _pair_key(sstk[li], legacy_formula_texts, recon_formula_texts) == _pair_key(
        recon_texts[ri], legacy_formula_texts, recon_formula_texts
    ):
        return (li, li, ri, ri)
    # legacy li..le 拼接 == recon ri
    l_acc = str(sstk[li])
    r_target = _pair_key(recon_texts[ri], legacy_formula_texts, recon_formula_texts)
    le = li + 1
    while le < len(sstk):
        l_acc += str(sstk[le])
        if _pair_key(l_acc, legacy_formula_texts, recon_formula_texts) == r_target:
            return (li, le, ri, ri)
        if r_target and not r_target.startswith(
            _pair_key(l_acc, legacy_formula_texts, recon_formula_texts)
        ):
            break
        le += 1
    # recon ri..re 拼接 == legacy li（反向合并）
    r_acc = str(recon_texts[ri])
    l_target = _pair_key(sstk[li], legacy_formula_texts, recon_formula_texts)
    re_ = ri + 1
    while re_ < len(recon_texts):
        r_acc += str(recon_texts[re_])
        if _pair_key(r_acc, legacy_formula_texts, recon_formula_texts) == l_target:
            return (li, li, ri, re_)
        if l_target and not l_target.startswith(
            _pair_key(r_acc, legacy_formula_texts, recon_formula_texts)
        ):
            break
        re_ += 1
    return None


def pair_legacy_to_reconstructed_partial(
    sstk: Sequence[str],
    recon_texts: Sequence[str],
    legacy_formula_texts: Optional[Dict[int, str]] = None,
    recon_formula_texts: Optional[Dict[int, str]] = None,
) -> Tuple[List[Tuple[int, int, int, int]], List[int]]:
    """部分接管配对：返回 ``(pairs, skipped)``。

    - ``pairs`` 每项 ``(ls, le, rs, re)``：可安全接管的段落（字符序列一致）。
    - ``skipped``：无法配对的 legacy 段索引 —— 保持 legacy 渲染（零回归）。

    真实 PDF 一页内常混有「文本一致的正文段」与「无法对齐的图表/公式段」；
    整页 all-or-nothing 会因个别段失败而放弃整页（实测 100% 回退）。部分
    接管让能配的段接管、配不上的段原样渲染。
    """
    n, m = len(sstk), len(recon_texts)
    if n == 0 or m == 0:
        return [], list(range(n))
    pairs: List[Tuple[int, int, int, int]] = []
    skipped: List[int] = []
    li = ri = 0
    while li < n and ri < m:
        match = _try_match(
            sstk, recon_texts, li, ri, legacy_formula_texts, recon_formula_texts
        )
        if match is not None:
            ls, le, rs, re = match
            pairs.append((ls, le, rs, re))
            li = le + 1
            ri = re + 1
        else:
            # 当前 legacy 段无法与当前 recon 段配对：跳过 legacy 段（保持原样），
            # recon 段可能与后续 legacy 段配对。
            skipped.append(li)
            li += 1
    for x in range(li, n):
        skipped.append(x)
    return pairs, skipped


def _apply_adoption(
    sstk, pstk, toc_track, pfkstk, pairs, built: List[AdoptedParagraph]
) -> int:
    """原地压缩 sstk/pstk/toc_track/pfkstk；返回合并段数（Level 2 计数）。"""
    new_sstk: List[str] = []
    new_pstk: List[AdoptedParagraph] = []
    new_toc: List[list] = []
    new_pfk: List[set] = []
    merged = 0
    has_pfk = pfkstk is not None
    for start, end, ridx in pairs:
        if start < end:
            merged += 1
            new_sstk.append("".join(str(s) for s in sstk[start : end + 1]))
            track: list = []
            for seg in toc_track[start : end + 1]:
                track.extend(list(seg or []))
            new_toc.append(track)
            if has_pfk:
                pk = set()
                for f in pfkstk[start : end + 1]:
                    pk |= set(f or [])
                new_pfk.append(pk)
            else:
                new_pfk.append(set())
        else:
            new_sstk.append(str(sstk[start]))
            new_toc.append(list(toc_track[start] or []))
            new_pfk.append(set(pfkstk[start] or []) if has_pfk else set())
        new_pstk.append(built[ridx])
    sstk[:] = new_sstk
    pstk[:] = new_pstk
    toc_track[:] = new_toc
    if has_pfk:
        pfkstk[:] = new_pfk
    return merged


def _apply_partial_adoption(
    sstk, pstk, toc_track, pfkstk, pairs, skipped, built: List[AdoptedParagraph]
) -> int:
    """部分接管：只压缩配对的段；``skipped`` 段保持 legacy 原样（零回归）。

    返回合并段数（Level 2 计数）。``pairs`` 每项 ``(ls, le, rs, re)``，
    ``built`` 与之平行（压缩后按序消费）。
    """
    new_sstk: List[str] = []
    new_pstk: List[object] = []
    new_toc: List[list] = []
    new_pfk: List[set] = []
    merged = 0
    has_pfk = pfkstk is not None
    skipped_set = set(skipped)
    bi = 0  # built 消费游标
    i = 0
    while i < len(sstk):
        if i in skipped_set:
            new_sstk.append(str(sstk[i]))
            new_pstk.append(pstk[i])
            new_toc.append(list(toc_track[i] or []))
            if has_pfk:
                new_pfk.append(set(pfkstk[i] or []))
            i += 1
            continue
        # i 是某个 pair 的起点
        ls = i
        le = None
        for p_ls, p_le, _rs, _re in pairs:
            if p_ls == ls:
                le = p_le
                break
        if le is None:
            # 防御：pair 表中不存在（理论不可达），保持 legacy
            new_sstk.append(str(sstk[i]))
            new_pstk.append(pstk[i])
            new_toc.append(list(toc_track[i] or []))
            if has_pfk:
                new_pfk.append(set(pfkstk[i] or []))
            i += 1
            continue
        if le > ls:
            merged += 1
            new_sstk.append("".join(str(s) for s in sstk[ls : le + 1]))
            track: list = []
            for seg in toc_track[ls : le + 1]:
                track.extend(list(seg or []))
            new_toc.append(track)
            if has_pfk:
                pk = set()
                for f in pfkstk[ls : le + 1]:
                    pk |= set(f or [])
                new_pfk.append(pk)
        else:
            new_sstk.append(str(sstk[ls]))
            new_toc.append(list(toc_track[ls] or []))
            if has_pfk:
                new_pfk.append(set(pfkstk[ls] or []))
        new_pstk.append(built[bi])
        bi += 1
        i = le + 1
    sstk[:] = new_sstk
    pstk[:] = new_pstk
    toc_track[:] = new_toc
    if has_pfk:
        pfkstk[:] = new_pfk
    return merged


def adopt_reconstruction_cluster(
    conv, ltpage, sstk, pstk, var, varl, varf, vlen, toc_track, pfkstk=None
) -> dict:
    """P1：文本集一致时以 P5–P10 重建段落几何接管 legacy sstk/pstk。

    ``var/varl/varf/vlen`` 保持不动（公式 ``{vN}`` 占位符索引全局不变）；
    合并段落时 ``toc_track/pfkstk`` 同步压缩。返回接管报告 dict；异常或
    分歧回退 legacy（返回 adopted=False）。
    """
    pageid = getattr(ltpage, "pageid", 0)
    report = {"adopted": False, "reason": "adopt_disabled", "page": pageid}
    if not getattr(conv, "reconstruction_channel", False):
        return report
    if not getattr(conv, "reconstruction_adopt", False):
        return {**report, "reason": "reconstruction_adopt_disabled"}
    try:
        results = getattr(conv, "reconstruction_results", None)
        result = (results or {}).get(pageid)
        units = getattr(result, "translation_units", None)
        if result is None or not units:
            return {**report, "reason": "no_reconstruction_result"}
        recon_texts = [u.text for u in units]
        # 公式占位符 → 实际字形字符（统一 legacy vflag 与 P6 提取的识别边界）
        legacy_formula_texts: Dict[int, str] = {}
        for vid in range(len(var)):
            if var[vid]:
                try:
                    legacy_formula_texts[vid] = "".join(
                        ch.get_text() for ch in var[vid]
                    )
                except Exception:
                    pass
        recon_formula_texts: Dict[int, str] = {}
        for _u in units:
            for _token, _fobj in (getattr(_u, "formula_map", {}) or {}).items():
                _m = _ANCHOR_FORMULA_RE.match(str(_token))
                _t = getattr(_fobj, "text", None)
                if _m is not None and _t:
                    recon_formula_texts[int(_m.group(1))] = _t
        pairs = pair_legacy_to_reconstructed(
            sstk, recon_texts, legacy_formula_texts, recon_formula_texts
        )
        partial = False
        skipped: List[int] = []
        if pairs is None:
            # 整页 all-or-nothing 失败 → 部分接管：能配的段接管，配不上的段
            # 保持 legacy（真实 PDF 一页常混有图/表/公式段，实测整页回退率 100%）
            partial_pairs, skipped = pair_legacy_to_reconstructed_partial(
                sstk, recon_texts, legacy_formula_texts, recon_formula_texts
            )
            if not partial_pairs:
                return {
                    **report,
                    "reason": "text_mismatch",
                    "legacy": len(sstk),
                    "recon": len(recon_texts),
                }
            pairs = [(ls, le, rs) for (ls, le, rs, _re) in partial_pairs]
            partial = True
        # TOC 段永不接管（目录行逐字符几何保护）：partial 模式下从 pairs 剔除
        # P1 精判修复：原判据 ``any(toc_track[t])`` 把「正文页含页码/年份数字」
        # 也误判为目录行（track 只要有数字字符就非空），导致真实 PDF 的普通
        # 正文页 100% 因 toc_present 回退。改为 ``detect_toc_line`` 的目录行
        # 结构识别（标题+点线+页码 或 章节编号+空列页码），正文数字不触发。
        from pdf2zh.toc import detect_toc_line as _detect_toc

        _page_w = float(getattr(ltpage, "width", 0.0) or 0.0)
        kept: List[Tuple[int, int, int]] = []
        dropped: List[int] = []
        for start, end, ridx in pairs:
            _is_toc = False
            for _t in range(start, end + 1):
                try:
                    _spec = _detect_toc(
                        str(sstk[_t]),
                        bool(getattr(pstk[_t], "brk", False)),
                        toc_track[_t],
                        float(getattr(pstk[_t], "x1", 0.0) or 0.0),
                        page_width=_page_w,
                    )
                    if _spec is not None:
                        _is_toc = True
                        break
                except Exception:  # noqa: BLE001 — 精判失败回退旧判据
                    if any(toc_track[_t]):
                        _is_toc = True
                        break
            if _is_toc:
                dropped.extend(range(start, end + 1))
            else:
                kept.append((start, end, ridx))
        if not kept:
            return {**report, "reason": "toc_present"}
        if dropped:
            if not partial:
                return {**report, "reason": "toc_present"}
            # 已部分接管 → 剔除 TOC 段（保持 legacy；_apply_partial_adoption
            # 的防御分支会把不在 pairs 中的段原样保留）
            pairs = kept
        # 构造几何（优先 SolvedUnit.render_bbox，回退 LogicalParagraph.bbox）
        built: List[AdoptedParagraph] = []
        solved_units = getattr(result, "solved_units", []) or []
        paragraphs = getattr(result, "paragraphs", []) or []
        for start, end, ridx in pairs:
            brk = bool(getattr(pstk[end], "brk", False))
            solved = solved_units[ridx] if ridx < len(solved_units) else None
            para = paragraphs[ridx] if ridx < len(paragraphs) else None
            if solved is not None:
                built.append(_adopted_from_solved(solved, brk))
            elif para is not None:
                built.append(_adopted_from_paragraph(para, brk))
            else:
                return {**report, "reason": "no_geometry", "index": ridx}
        if partial:
            merged = _apply_partial_adoption(
                sstk, pstk, toc_track, pfkstk, pairs, skipped, built
            )
        else:
            merged = _apply_adoption(sstk, pstk, toc_track, pfkstk, pairs, built)
        return {
            "adopted": True,
            "reason": "consistent",
            "level": 2 if merged else 1,
            "merged_paragraphs": merged,
            "partial": partial,
            "skipped_paragraphs": len(skipped),
            "pairs": list(pairs),  # (压缩后 legacy_idx, legacy_end, recon_idx)
            "page": pageid,
            "paragraph_count": len(pairs),
        }
    except Exception as e:  # noqa: BLE001 — 接管失败回退 legacy
        return {**report, "reason": str(e)[:120]}


def _adopted_from_solved(solved, brk: bool) -> AdoptedParagraph:
    """SolvedUnit.render_bbox → legacy 段落几何鸭子类型（y-up 坐标系）。"""
    x0, y0, x1, y1 = solved.render_bbox
    return AdoptedParagraph(
        y=y0,
        x=x0,
        x0=x0,
        x1=x1,
        y0=y0,
        y1=y1,
        size=float(getattr(solved, "font_size", 0.0) or 12.0),
        brk=brk,
    )


def _adopted_from_paragraph(para, brk: bool) -> AdoptedParagraph:
    """LogicalParagraph.bbox 兜底（solved_units 缺失时）。"""
    x0, y0, x1, y1 = para.bbox
    return AdoptedParagraph(
        y=y0,
        x=x0,
        x0=x0,
        x1=x1,
        y0=y0,
        y1=y1,
        size=float(getattr(para, "font_size", 0.0) or 12.0),
        brk=brk,
    )


__all__ = [
    "normalize_formula_tokens",
    "legacy_to_anchor_text",
    "pair_legacy_to_reconstructed",
    "pair_legacy_to_reconstructed_partial",
    "adopt_reconstruction_cluster",
]
