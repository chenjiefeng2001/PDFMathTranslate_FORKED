"""Module: TOCAnalyzer — 目录块边界恢复 + 条目结构化（Semantic 层重切，不改 Parser）。

现状问题（Block Boundary 恢复失败）：
    TOC 后半段多条目录被压成一个 Paragraph：

        2.3 连续随机变量 31 2.3.1 均匀随机变量 32 ...

    原因：目录行的页码列没有「点线引导」时，geometry 段落合并保护
    （``_TOC_LINE_END_RE``）不命中，多条目录行被并入一个块。

本模块在**语义层**（不修改 Geometry/Parser）解决：
    - 重新扫描 Block：按「编号模式 ``\\d+(\\.\\d+)+`` + 页码」重切合并块，
      恢复逐行目录条目（Block Boundary 重建）；
    - 页码独立检测：``x > 0.8 × 页宽`` 且纯数字（Geometric Page Column），
      与标题/字号解耦，译文/渲染永不触碰页码；
    - 输出目录树：逐条目 {number, title, page} → 章节层级树；
    - 专用渲染：``render_toc_entry`` —— 目录条目按行渲染
      ``title ---- page``，不再走 Paragraph→Translate→Layout 整段路径。

正文目录行每行都是独立对象 —— 不适用普通段落合并规则。
设计约束：纯逻辑、无 I/O、无 fitz/pdfminer；输入 Block[]，输出 TOC 树，
不修改任何 Parser 代码；与 image_engine / toc_semantics 同风格。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

log = logging.getLogger(__name__)

# 编号模式：加权编号（2.3 / 2.3.1）或纯数字章节（2 / 21 在后半段失效）
_RE_ENTRY_HEAD = re.compile(r"^\s*(\d+(?:\.\d+)+)")  # 至少含一点
_RE_ENTRY_HEAD_FLAT = re.compile(r"^\s*(\d+)\s+")
# 行尾点线引导 + 页码
_RE_TAIL_LEADER_PAGE = re.compile(r"[.·…‥]{2,}\s*(\d{1,5})\s*$")
# 行尾「空格 + 数字」（无点线的空列页码）
_RE_TAIL_SPACE_PAGE = re.compile(r"[ ]{1,}(\d{1,5})\s*$")
# 标题两侧的点线装饰
_RE_STRIP_DOTS = re.compile(r"^[.·…‥]+|[.·…‥]+$")

_PAGE_LEN_MAX = 5


@dataclass
class TOCEntry:
    """目录条目（语义化结果，可序列化）。"""

    number: str = ""
    title: str = ""
    page: str = ""
    raw: str = ""
    line: int = 0

    def to_dict(self) -> dict:
        return {
            "number": self.number,
            "title": self.title,
            "page": self.page,
            "raw": self.raw,
            "line": self.line,
        }


def parse_entry_text(text: str) -> Optional[TOCEntry]:
    """解析一行文本 → 目录条目；非目录行返回 None。

    规则：
    - 行首必须有编号（``2`` 或 ``2.3``）；
    - 行尾的独立数字（点线或空列分隔）才是页码，从标题剥离；
    - 无编号／纯数字行／过长标题不判定（避免正文误拆）。
    """
    line = (text or "").strip("\r\n \t")
    if not line:
        return None
    m = _RE_ENTRY_HEAD.match(line)
    if not m:
        mf = _RE_ENTRY_HEAD_FLAT.match(line)
        # 纯数字章节（2 / 12）也允许，但要满足「标题 + 独立页码」形态
        if not mf:
            return None
        number = mf.group(1)
        title_raw = line[mf.end() :].strip()
    else:
        number = m.group(1)
        title_raw = line[m.end() :].strip()
    if not title_raw:
        return None
    page = ""
    pm = _RE_TAIL_LEADER_PAGE.search(title_raw)
    if pm:
        page = pm.group(1)
        title_raw = title_raw[: pm.start()].strip()
    else:
        pm2 = _RE_TAIL_SPACE_PAGE.search(title_raw)
        if pm2 and len(pm2.group(1)) <= _PAGE_LEN_MAX:
            page = pm2.group(1)
            title_raw = title_raw[: pm2.start()].strip()
    title = _RE_STRIP_DOTS.sub("", title_raw).strip(" \t:.-–—")
    if not title:
        return None
    if "." not in number and not page:
        return None  # 纯数字章节必须带独立页码（否则是正文标题，非目录行）
    return TOCEntry(number=number, title=title, page=page, raw=text)


def split_merged_block(block, page_width: float = 0.0) -> List[dict]:
    """把一个（可能是合并的）BlockModel 切为逐条目录条目。

    规则：
      - 行数 < 2 或命中比例 < 0.5 → 非目录块，返回空（交给 Paragraph）；
      - 每行独立解析；页码优先用几何列（x > 0.8*w），否则文本行尾数字；
      - 返回 ``[{number, title, page, raw, line}]``。

    ``block`` 只需提供 ``text``（及可选的 ``lines``，``lines`` 带 ``spans``）。
    """
    lines = [
        (getattr(l, "text", "") or "") for l in (getattr(block, "lines", []) or [])
    ]
    if not lines:
        lines = [
            ln for ln in (getattr(block, "text", "") or "").split("\n") if ln.strip()
        ]
    if not lines:
        return []

    parsed = [parse_entry_text(ln) for ln in lines]
    hit = sum(1 for e in parsed if e is not None)
    if len(lines) < 2 or hit < max(2, int(0.5 * len(lines))):
        return []

    others: List[dict] = []
    for i, (ln, entry) in enumerate(zip(lines, parsed)):
        if entry is None:
            continue
        page = entry.page
        if not page and page_width > 0.0 and hasattr(block, "lines"):
            for l_obj in block.lines:
                if (getattr(l_obj, "text", "") or "") == ln:
                    page = _span_geometric_check(l_obj, page_width)
                    break
        others.append(
            {
                "number": entry.number,
                "title": entry.title,
                "page": page,
                "raw": entry.raw,
                "line": i,
            }
        )
    # 页码占比护栏：多数命中条目都带页码才判定为目录块
    # （避免「编号标题列表」被误当目录）
    with_page = sum(1 for e in others if e["page"])
    if with_page * 2 < len(others):
        return []
    return others


def _span_geometric_check(line_obj, page_width: float) -> str:
    """几何页码优先；有几何值时覆盖文本页码（不能吞标题数字）。"""
    if page_width > 0.0:
        s = _span_geometric(line_obj, page_width)
        if s:
            return s
    text = (getattr(line_obj, "text", "") or "").strip()
    m = _RE_TAIL_SPACE_PAGE.search(text)
    return m.group(1) if m else ""


def _span_geometric(line_obj, page_width: float) -> str:
    """（内部）x 列几何页码。"""
    if page_width <= 0.0:
        return ""
    for span in getattr(line_obj, "spans", []) or []:
        txt = (getattr(span, "text", "") or "").strip()
        x0 = float(getattr(span, "x0", 0.0) or 0.0)
        if x0 > 0.8 * page_width and txt.isdigit() and 1 <= len(txt) <= _PAGE_LEN_MAX:
            return txt
    return ""


def analyze_toc_blocks(blocks, page_width: float = 0.0) -> List[dict]:
    """Block[] → ``[{index, block, entries}]``（纯语义判定，不改块）。"""
    out: List[dict] = []
    for i, block in enumerate(blocks):
        out.append(
            {
                "index": i,
                "block": block,
                "entries": split_merged_block(block, page_width),
            }
        )
    return out


def rebuild_toc_page(page) -> dict:
    """页级目录树重建：块 → 条目 → 层级树（build_toc_tree 视图）。"""
    from pdf2zh.v3.toc_tree import build_toc_tree

    records: List[dict] = []
    line = 0
    for be in analyze_toc_blocks(page.blocks, getattr(page, "width", 0.0)):
        for ent in be["entries"]:
            num = ent.get("number", "")
            records.append(
                {
                    "line": line,
                    "number": num,
                    "title": ent.get("title", ""),
                    "page": ent.get("page", ""),
                    "raw": ent.get("raw", ""),
                    "kind": "section" if "." in num else "chapter",
                    "level": num.count(".") + 1,
                }
            )
            line += 1
    return build_toc_tree(records)


def analyze_toc_result(page) -> dict:
    """页面级目录语义结果：条目 + 块计数 + 树。"""
    from pdf2zh.v3.toc_tree import build_toc_tree

    entries: List[dict] = []
    records: List[dict] = []
    line = 0
    for be in analyze_toc_blocks(page.blocks, getattr(page, "width", 0.0)):
        for ent in be["entries"]:
            num = ent.get("number", "")
            records.append(
                {
                    "line": line,
                    "number": num,
                    "title": ent.get("title", ""),
                    "page": ent.get("page", ""),
                    "raw": ent.get("raw", ""),
                    "kind": "section" if "." in num else "chapter",
                    "level": num.count(".") + 1,
                }
            )
            line += 1
        entries.extend(be["entries"])
    return {
        "entries": entries,
        "count": len(entries),
        "tree": build_toc_tree(records),
    }


def render_toc_entry(
    number: str,
    title: str,
    page: str = "",
    level: int = 0,
    leader: str = "...",
    lang_out: str = "zh-CN",
) -> str:
    """目录条目专用渲染：``title ---- page``（按行，不走段落流）。

    ``leader``/``page`` 原样保留（永不翻译）；``level`` 控制缩进。
    """
    indent = "    " * max(0, level)
    head = f"{number} {title}" if number else (title or "")
    gap = f" {leader} " if page else ""
    return f"{indent}{head}{gap}{page}".rstrip()


def split_toc_blocks(page) -> int:
    """Semantic Pass 重切：把合并目录块替换为逐条目录块（就地改 ``page.blocks``）。

    规则（改结构、写 metadata，不改 geometry）：
      - 对每个块调用 ``split_merged_block``；命中 ≥2 条才拆；
      - 每个条目 → 新建 ``BlockModel``（kind="toc"），其 ``lines`` 划分自原块
        对应行（几何（bbox）沿用原行），``metadata`` 写入
        toc_number/toc_title/toc_page/toc_scan，与 ``annotate_toc_scan`` 格式一致；
      - 非目录块原样保留，顺序不变。
    返回替换（拆分）的块数。
    """
    from pdf2zh.v3.canonical_page import BlockModel, LineModel

    page_width = float(getattr(page, "width", 0.0) or 0.0)
    new_blocks = []
    splits = 0
    for block in page.blocks:
        entries = split_merged_block(block, page_width)
        if len(entries) < 2:
            new_blocks.append(block)
            continue
        splits += 1
        lines_by_text: dict = {}
        for l_obj in getattr(block, "lines", []) or []:
            lines_by_text.setdefault(getattr(l_obj, "text", "") or "", l_obj)
        for ent in entries:
            nb = BlockModel()
            nb.kind = "toc"
            nb.metadata["kind"] = "toc"
            nb.metadata["toc_number"] = ent.get("number", "")
            nb.metadata["toc_title"] = ent.get("title", "")
            nb.metadata["toc_page"] = ent.get("page", "")
            nb.metadata["toc_confidence"] = 0.55
            nb.metadata["toc_scan"] = True
            raw = ent.get("raw", "")
            nb.text = raw
            src = lines_by_text.get(raw)
            if src is not None:
                nb.lines.append(src)
                nb.x0, nb.y0, nb.x1, nb.y1 = src.x0, src.y0, src.x1, src.y1
            if not nb.lines and (block.x0 or block.x1):
                nb.x0, nb.y0, nb.x1, nb.y1 = block.x0, block.y0, block.x1, block.y1
            new_blocks.append(nb)
    page.blocks = new_blocks
    return splits


def split_merged_toc_paragraphs(
    conv, ltpage, sstk, pstk, toc_track, page_width: float = 0.0, pfkstk=None
) -> dict:
    """V1.17-3：legacy 渲染路径 —— 把合并目录段按物理行重切（side-channel）。

    ``receive_layout`` 的字符循环把「无点线页码列」的多条目录行并成一个
    sstk 段（``brk=True``），``detect_toc_line`` 对 brk 段落直接放弃，
    整段走普通 Paragraph→Translate→Layout 渲染（后半段挤成一段的根因）。
    本钩子在 sstk/pstk/toc_track 构建完成后、翻译前重切：

      - 用原始 LTChar 流按基线聚出物理行（不做栏切分/竖向剔除，
        页码列保留在行内 —— geometry.build_page 会剔除右缘数字列）；
      - 逐行 ``parse_entry_text``，全部命中且带页码比例 ≥ 护栏才重切；
      - 每行 → 独立 sstk/pstk/toc_track（``brk=False`` + 行级 bbox +
        行级点线/数字记录），之后既有的 ``detect_toc_line`` 可逐行识别
        （点线页码或空列页码），走 toc_mode 渲染（标题单独翻译、
        点线/页码原位渲染、页码右对齐）。

    参数：
        conv:       TranslateConverter 实例（仅用于取 fontmap 等，可缺省）
        ltpage:     正在处理的 pdfminer LTPage
        sstk/pstk/toc_track: legacy 段落三件套，**就地改写**
        page_width: 页面宽度（页码列几何检测；缺省用 ltpage.width）
        pfkstk: 每段字体指纹（可缺省）。**与 sstk 同步重切** —— 漏改会导致
            翻译期 ``font_sigs[i]`` IndexError（sstk 比 pfkstk 长）。
    返回：
        dict 报告（page / split / reason）；异常或无条件返回 reason（不拆）。
    """
    from pdf2zh.toc import TOC_LEADER_CHARS
    from pdf2zh.v3.geometry import GeometryEngine, chars_from_ltpage
    from pdf2zh.v3.geometry_merge import AdoptedParagraph

    pageid = getattr(ltpage, "pageid", 0)
    if page_width <= 0.0:
        page_width = float(getattr(ltpage, "width", 0.0) or 0.0)
    try:
        chars = chars_from_ltpage(ltpage, page_num=pageid)
        rows = _physical_rows(chars)
    except Exception as e:  # noqa: BLE001 — 侧通道失败回退 legacy
        return {"page": pageid, "split": 0, "reason": str(e)[:120]}
    if not rows:
        return {"page": pageid, "split": 0, "reason": "no_rows"}

    cfg = GeometryEngine().config
    split_count = 0
    for i in range(len(sstk) - 1, -1, -1):
        if not getattr(pstk[i], "brk", False):
            continue
        text = sstk[i] or ""
        if "{v" in text:  # 含公式占位符：行文本无法逐字对齐，跳过
            continue
        p = pstk[i]
        # 段落 bbox 内的物理行（PDF 坐标 y 向上）
        pad = max(1.0, p.size * 0.5)
        inner = [r for r in rows if r["y1"] >= p.y0 - pad and r["y0"] <= p.y1 + pad]
        inner.sort(key=lambda r: -r["y0"])
        if len(inner) < 2:
            continue
        entries = [parse_entry_text(r["text"]) for r in inner]
        hits = [e for e in entries if e is not None]
        # 目录块的硬性前提：足够多的行命中 TOC 语法（编号 + 页码形态）。
        # 行内混入非目录行（如 Preface/Contents 等无编号前缀）不再整块放弃——
        # 否则整页目录被并成一段，翻译会毁掉点线/页码结构（TOC 混乱）。
        # 未命中的行仍拆成独立物理行段落，交由后续 detect_toc_line/普通段落处理。
        if len(hits) < 2:
            continue
        with_page = sum(1 for e in hits if e.page)
        if with_page * 2 < len(hits):
            continue
        # 一致性校验：重切文本与 legacy 段文本逐字一致（去空白后）
        joined = "".join(r["text"].replace(" ", "") for r in inner)
        if joined != text.replace(" ", ""):
            continue
        # 重切：每行 → 独立段落
        texts, paras, tracks = [], [], []
        for r, e in zip(inner, entries):
            texts.append(r["text"])
            paras.append(
                AdoptedParagraph(
                    y=r["y0"],
                    x=r["x0"],
                    x0=r["x0"],
                    x1=r["x1"],
                    y0=r["y0"],
                    y1=r["y1"],
                    size=r["size"],
                    brk=False,
                )
            )
            tracks.append(
                [
                    (c, a, b)
                    for c, a, b in r["chars"]
                    if c in TOC_LEADER_CHARS or c.isdigit()
                ]
            )
        sstk[i : i + 1] = texts
        pstk[i : i + 1] = paras
        toc_track[i : i + 1] = tracks
        if pfkstk is not None and 0 <= i < len(pfkstk):
            # pfkstk 与 sstk 同步重切：每行继承合并段的字体指纹并集
            # （仅作缓存 variant 键，宽松无碍；不同步则翻译期 IndexError）
            parent_fonts = set(pfkstk[i] or set())
            pfkstk[i : i + 1] = [set(parent_fonts) for _ in texts]
        split_count += 1
    return {
        "page": pageid,
        "split": split_count,
        "reason": "ok" if split_count else "none",
    }


def _physical_rows(chars) -> List[dict]:
    """原始字符流 → 物理行（不做栏切分/竖向剔除，保留页码列）。

    返回 ``[{text, x0, y0, x1, y1, size, chars: [(ch, x0, x1), ...]}]``，
    按 y 降序（PDF 坐标：上在前）。
    """
    from pdf2zh.v3.geometry import GeometryConfig
    from pdf2zh.v3.geometry import GeometryEngine

    cfg = GeometryConfig()
    words = GeometryEngine().build_words(chars)
    lines: List[dict] = []
    for w in words:
        for ln in lines:
            ref = ln["words"][0]
            tol = cfg.baseline_tol_ratio * max(w.size, ref.size)
            if abs(w.baseline_y - ref.baseline_y) <= tol:
                ln["words"].append(w)
                break
        else:
            lines.append({"words": [w]})
    rows: List[dict] = []
    for ln in lines:
        ws = ln["words"]
        ws.sort(key=lambda w: w.x0)
        text = " ".join(w.text for w in ws)
        if not text.strip():
            continue
        chs = [c for w in ws for c in w.chars]
        rows.append(
            {
                "text": text,
                "x0": min(w.x0 for w in ws),
                "y0": min(w.y0 for w in ws),
                "x1": max(w.x1 for w in ws),
                "y1": max(w.y1 for w in ws),
                "size": max(w.size for w in ws),
                "chars": [(c.text, c.x0, c.x1) for c in chs],
            }
        )
    rows.sort(key=lambda r: -r["y0"])
    return rows


__all__ = [
    "TOCEntry",
    "parse_entry_text",
    "split_merged_block",
    "analyze_toc_blocks",
    "split_toc_blocks",
    "rebuild_toc_page",
    "analyze_toc_result",
    "render_toc_entry",
    "split_merged_toc_paragraphs",
    "_physical_rows",
]
