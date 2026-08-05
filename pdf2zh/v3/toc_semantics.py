"""Module: TOC Semantic Rendering — 目录条目的结构化语义层（V8.7）。

目录条目**不应作为普通文本整行翻译**。本模块把目录行语义化为
结构化条目，使渲染与翻译分离：

    TOC Entry
        │
        ▼
    Semantic Parser
        ▼
    {kind, level, number, title, page, leader}
        │
        ▼
    TOC Translation Policy
        ├── kind+number → 模板（Chapter→第X章，Section→第X节…）
        ├── title      → 调用翻译器
        ├── leader     → 保留，不翻译
        └── page       → 保留，不翻译
        │
        ▼
    TOC Renderer
        ▼
    "第3.2节 实验设置 ........... 42"

设计原则（对应"目录语义渲染"建议）：
1. **Leader 永远不是文本**：`.·…‥` 是 Tab Leader（排版引导线），
   parse 时独立字段保存，翻译/OCR 永不触碰。
2. **结构词不走通用翻译器**：Chapter/Section/Appendix/Part/Contents/
   Index 由 TOC Grammar 识别 + 模板渲染，不依赖 Google 猜测
   （避免 "Section → 部分/截面" 这种领域错误）。
3. **编号、页码、引导线由渲染器生成**，不是由翻译器输出。
4. 纯逻辑、无 I/O、无 fitz —— 与 image_engine/link_remap 同风格。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple

# ── 结构化字段 ──────────────────────────────────────────────────────────────


class TOCKind(Enum):
    """目录条目的结构类别。"""

    CHAPTER = "chapter"
    SECTION = "section"
    SUBSECTION = "subsection"
    PART = "part"
    APPENDIX = "appendix"
    CONTENTS = "contents"
    INDEX = "index"
    PLAIN = "plain"       # 无结构前缀的普通目录标题
    UNKNOWN = "unknown"


@dataclass
class TOCEntry:
    """解析后的目录条目。

    Attributes:
        raw: 原始标题文本（不含点线/页码）。
        kind: 结构类别（chapter/section/...）。
        level: 层级（chapter=1, section=2, subsection=3, ...）。
        number: 编号字符串（"3"、"3.2"、"A"、"V"）。
        title: 去掉结构前缀后的描述性标题（可能为空）。
        page: 页码字符串（保留原样，不翻译）。
        leader: 引导线字符（保留原样，不翻译，仅记录）。
    """

    raw: str = ""
    kind: TOCKind = TOCKind.UNKNOWN
    level: int = 0
    number: str = ""
    title: str = ""
    page: str = ""
    leader: str = ""
    matched: bool = False

    def to_dict(self) -> dict:
        return {
            "raw": self.raw,
            "kind": self.kind.value,
            "level": self.level,
            "number": self.number,
            "title": self.title,
            "page": self.page,
            "leader": self.leader,
            "matched": self.matched,
        }


# ── TOC Grammar（规则式，无 LLM） ──────────────────────────────────────────


_RE_CHAPTER = re.compile(r"^\s*(?:chapter|ch\.)\s*([0-9IVXLC]+|[0-9]+(?:\.[0-9]+)*)", re.IGNORECASE)
_RE_SECTION = re.compile(r"^\s*(?:section|sec\.)\s*([0-9]+(?:\.[0-9]+)*)", re.IGNORECASE)
_RE_SUBSECTION = re.compile(r"^\s*(?:subsection|subsec\.)\s*([0-9]+(?:\.[0-9]+)*)", re.IGNORECASE)
_RE_PART = re.compile(r"^\s*part\s*([0-9IVXLC]+)", re.IGNORECASE)
_RE_APPENDIX = re.compile(r"^\s*(?:appendix|appx\.)\s*([A-Z0-9]+)", re.IGNORECASE)
_RE_CONTENTS = re.compile(r"^\s*(?:table\s+of\s+contents|contents)\s*$", re.IGNORECASE)
_RE_INDEX = re.compile(r"^\s*index\s*$", re.IGNORECASE)
# V8.7 P2：真实语料前缀变体 —— §N 小节、裸编号 "1." / "1.2.3"、中文"第X章/节"
_RE_SECTION_SIGN = re.compile(r"^\s*[§§]\s*([0-9]+(?:\.[0-9]+)*)")
_RE_BARE_NUMBERED = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)+|[0-9]+)\s*[.:、]")
_RE_ZH_PREFIX = re.compile(r"^\s*第\s*([0-9零一二三四五六七八九十百千万]+)\s*([章节篇部卷])")
# v1.6 变体补充：中文枚举 "一、" / "1、"（中文目录常见）；右括号编号 "1)" / "（1）"
_RE_ZH_ENUM = re.compile(r"^\s*[零一二三四五六七八九十百千万]+\s*[、.．]")
_RE_CLOSE_PAREN = re.compile(r"^\s*\(?\s*([0-9]+(?:\.[0-9]+)*)\s*\)?[)）]\s+")

_STRUCTURED_RE: List[Tuple[TOCKind, re.Pattern, int]] = [
    # (kind, 正则, 层级)
    (TOCKind.CHAPTER, _RE_CHAPTER, 1),
    (TOCKind.SECTION, _RE_SECTION, 2),
    (TOCKind.SUBSECTION, _RE_SUBSECTION, 3),
    (TOCKind.PART, _RE_PART, 1),
    (TOCKind.APPENDIX, _RE_APPENDIX, 1),
    (TOCKind.SECTION, _RE_SECTION_SIGN, 2),   # §2 / §3.2 → 节
]

# 中文单位 → 结构类别
_ZH_UNIT_KIND = {
    "章": TOCKind.CHAPTER, "节": TOCKind.SECTION,
    "篇": TOCKind.PART, "部": TOCKind.PART, "卷": TOCKind.PART,
    "部分": TOCKind.PART,
}

_NUMBER_SEPARATORS = " \t:.-–—()[]{}"


def parse_toc_entry(raw_title: str, page: str = "", leader: str = "") -> TOCEntry:
    """用 TOC Grammar 解析目录标题 → 结构化条目。

    只负责解析，不翻译。返回的 ``entry.title`` 是需要翻译的剩余部分
    （结构前缀已剥离）。未命中任何结构规则时返回 PLAIN（整条标题
    都视为可翻译 title）。
    """
    text = (raw_title or "").strip()
    entry = TOCEntry(raw=text, page=page, leader=leader, kind=TOCKind.UNKNOWN)

    if not text:
        entry.kind = TOCKind.UNKNOWN
        return entry

    if _RE_CONTENTS.match(text):
        entry.kind = TOCKind.CONTENTS
        entry.matched = True
        return entry
    if _RE_INDEX.match(text):
        entry.kind = TOCKind.INDEX
        entry.matched = True
        return entry

    for kind, regex, level in _STRUCTURED_RE:
        m = regex.match(text)
        if m:
            entry.kind = kind
            entry.level = level
            entry.number = m.group(1).strip()
            rest = text[m.end():]
            entry.title = rest.strip(" \t:.-–—()[]{}")
            entry.matched = True
            return entry

    # 中文"第X章 引言"（已本地化的目录行：识别即保留结构，不再整体送翻译器）
    m = _RE_ZH_PREFIX.match(text)
    if m:
        unit = m.group(2)
        entry.kind = _ZH_UNIT_KIND.get(unit, TOCKind.SECTION)
        entry.level = 1 if entry.kind in (TOCKind.CHAPTER, TOCKind.PART) else 2
        entry.number = m.group(1).strip()
        entry.title = text[m.end():].strip(_NUMBER_SEPARATORS + " ")
        entry.matched = True
        return entry

    # 裸编号目录行："1. 引言" / "1.2.3 方法"（层级随编号段数）
    m = _RE_BARE_NUMBERED.match(text)
    if m:
        entry.kind = TOCKind.SECTION
        entry.number = m.group(1).strip()
        entry.level = entry.number.count(".") + 1
        entry.title = text[m.end():].strip(_NUMBER_SEPARATORS + " ")
        entry.matched = True
        return entry

    # 右括号编号："1) 引言" / "1) Introduction"（编号后收进 number）
    m = _RE_CLOSE_PAREN.match(text)
    if m:
        entry.kind = TOCKind.SECTION
        entry.number = m.group(1).strip()
        entry.level = entry.number.count(".") + 1
        entry.title = text[m.end():].strip(_NUMBER_SEPARATORS + " ")
        entry.matched = True
        return entry

    # 中文枚举："一、引言"（中文目录常见无章结构条目）
    m = _RE_ZH_ENUM.match(text)
    if m:
        entry.kind = TOCKind.SECTION
        entry.number = text[:m.end() - 1].strip(" 、.．")
        entry.title = text[m.end():].strip(_NUMBER_SEPARATORS + " ")
        entry.matched = True
        return entry

    entry.kind = TOCKind.PLAIN
    entry.title = text
    entry.matched = False
    return entry


# ── TOC Translation Policy（模板 + 翻译边界） ───────────────────────────────


# 结构词模板：{number} 由渲染器填充（各目标语言一张表）
TOC_TEMPLATES: Dict[str, Dict[TOCKind, str]] = {
    "zh": {
        TOCKind.CHAPTER: "第{number}章",
        TOCKind.SECTION: "第{number}节",
        TOCKind.SUBSECTION: "第{number}节",
        TOCKind.PART: "第{number}篇",
        TOCKind.APPENDIX: "附录{number}",
        TOCKind.CONTENTS: "目录",
        TOCKind.INDEX: "索引",
    },
}

# 默认模板（未配置语言时：保留原文结构词 + 编号）
TOC_TEMPLATES["en"] = {
    TOCKind.CHAPTER: "Chapter {number}",
    TOCKind.SECTION: "Section {number}",
    TOCKind.SUBSECTION: "Section {number}",
    TOCKind.PART: "Part {number}",
    TOCKind.APPENDIX: "Appendix {number}",
    TOCKind.CONTENTS: "Contents",
    TOCKind.INDEX: "Index",
}


def _lang_family(lang_out: str) -> str:
    lang = (lang_out or "").lower().replace("_", "-")
    if lang.startswith("zh") or lang in ("cmn", "yue", "wuu", "chinese", "cht"):
        return "zh"
    return "en"


def toc_structure_prefix(entry: TOCEntry, lang_out: str = "zh-CN") -> str:
    """按模板渲染结构前缀（如 Chapter 3 → 第3章）。

    PLAIN/UNKNOWN 条目返回空串（整条标题交由翻译器）。
    """
    if not entry.matched:
        return ""
    family = _lang_family(lang_out)
    template = TOC_TEMPLATES.get(family, TOC_TEMPLATES["en"]).get(entry.kind)
    if template is None:
        return ""
    return template.format(number=entry.number)


class TOCTranslationPolicy:
    """目录翻译边界：什么进翻译器、什么不进。

    决策输出：
        translate_title  —— 剩余描述性标题（可能需要翻译）
        structure_prefix —— 模板渲染的结构前缀（不翻译，渲染前拼接）
        keep_leader      —— 引导线（永不翻译）
        keep_page        —— 页码（永不翻译）
        local_only       —— True 表示整条由模板渲染，无需调用翻译器
    """

    def __init__(self, lang_out: str = "zh-CN") -> None:
        self.lang_out = lang_out

    def decide(self, entry: TOCEntry) -> dict:
        prefix = toc_structure_prefix(entry, self.lang_out)
        local_only = entry.matched and not entry.title.strip()
        return {
            "translate_title": bool(entry.title.strip()) and not local_only,
            "structure_prefix": prefix,
            "keep_leader": True,
            "keep_page": True,
            "local_only": local_only,
            "kind": entry.kind.value,
            "level": entry.level,
            "number": entry.number,
        }

    def compose(self, entry: TOCEntry, translated_title: str) -> str:
        """渲染最终标题文本 = 结构前缀 + (翻译后的) 剩余标题。

        leader / page 由渲染器另行原位绘制（本函数不包含）。
        """
        prefix = toc_structure_prefix(entry, self.lang_out)
        title = (translated_title or "").strip()
        if prefix and title:
            return f"{prefix} {title}"
        return prefix + title


def compose_toc_title(entry: Optional["TOCEntry"], translated_title: str, lang_out: str = "zh-CN") -> str:
    """目录行标题合成（converter 主链路钩子）。

    - entry 为 None（非目录行）或 PLAIN（未命中结构规则）时恒等返回
      ``translated_title``，保证普通段落与既有目录路径零改动。
    - 命中的结构化条目：``结构前缀 + 剩余标题``（结构词不经过翻译器）。
    """
    if entry is None or not entry.matched:
        return translated_title
    return TOCTranslationPolicy(lang_out).compose(entry, translated_title)


def render_toc_line(entry: TOCEntry, translated_title: str,
                    lang_out: str = "zh-CN") -> str:
    """完整目录行渲染（语义渲染入口，供测试与独立渲染器使用）。

    返回形如 ``第3.2节 实验设置 ............ 42`` 的文本。
    """
    policy = TOCTranslationPolicy(lang_out)
    head = policy.compose(entry, translated_title)
    leader = entry.leader or ""
    page = entry.page or ""
    gap = " " if (head and (leader or page)) else ""
    return f"{head}{gap}{leader}{page}"


def toc_to_ir_records(entries: List[Tuple[TOCEntry, str, str]],
                      page_num: int = 0) -> List[dict]:
    """V8.7 P1：把结构化目录条目转成 Document IR 侧通道记录。

    ``entries`` 为 ``[(entry, title_remainder, translated_title), ...]``
    （与 converter 主链路一一对应）；输出为可 JSON 序列化的记录，
    供 ``mainline_wiring.run_toc_channel`` 回传 / ``DocumentIR`` 的
    TOC_ENTRY 节点结构化存储（kind/level/number/title/page/leader
    三字段分离契约不变）。
    """
    records: List[dict] = []
    for entry, title_remainder, translated_title in entries:
        if entry is None or not entry.matched:
            continue
        d = entry.to_dict()
        d["title_remainder"] = title_remainder
        d["translated_title"] = translated_title
        d["page_num"] = page_num
        records.append(d)
    return records


__all__ = [
    "TOCKind", "TOCEntry", "parse_toc_entry",
    "TOC_TEMPLATES", "toc_structure_prefix",
    "TOCTranslationPolicy", "compose_toc_title", "render_toc_line",
    "toc_to_ir_records",
]