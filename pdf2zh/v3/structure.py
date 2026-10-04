"""Module: Structure Engine — 阶段三「这些 Paragraph 是什么」特征向量分类器。

纯规则 + 统计（不依赖 LLM）：对 Geometry Engine 产出的每个 Paragraph 计算
特征向量，然后按确定性规则分级分类：

    font size / font weight / indent / alignment / spacing / line count /
    numbering / punctuation / digit ratio / leader ratio / capital ratio

输出 ``SemanticRole``（对齐 ``document_ir.SemanticRole``）+ confidence，
并支持把整页几何模型升级为 Document IR（阶段一消费端）：

    PageGeometry --StructureClassifier--> BlockRole[] --> DocumentIR

Usage::

    from pdf2zh.v3.geometry import GeometryEngine
    from pdf2zh.v3.structure import StructureClassifier, to_document_ir
    engine, classifier = GeometryEngine(), StructureClassifier()
    pages = engine.build_document(chars_by_page)
    for page in pages:
        for para, role, conf in classifier.classify_page(page):
            print(role.value, round(conf, 2), para.text[:40])
    ir = to_document_ir(pages)          # → DocumentIR（可 to_json 快照）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Sequence, Tuple

from pdf2zh.v3.geometry import PageGeometry, Paragraph
from pdf2zh.v3.document_ir import (
    DocumentIR,
    ReadingRole,
    RenderingRole,
    SemanticRole,
    TranslationRole,
)

# ── 角色枚举 ──────────────────────────────────────────────────────────────


class BlockRole(Enum):
    """结构引擎输出的块角色（含目录/页码等 IR 尚未覆盖的角色）。"""

    PAGE_NUMBER = "page_number"
    HEADER = "header"
    FOOTER = "footer"
    TOC_ENTRY = "toc_entry"
    HEADING = "heading"
    CAPTION = "caption"
    FOOTNOTE = "footnote"
    FORMULA = "formula"
    CITATION = "citation"
    BODY_TEXT = "body_text"
    UNKNOWN = "unknown"

    # 7H-2C：语义策略层角色（F2 修复 —— CODE 由可靠证据确认后不得被降级）
    CODE = "code"
    COMMAND = "command"
    FILENAME = "filename"
    IDENTIFIER = "identifier"

    @property
    def semantic_role(self) -> SemanticRole:
        return _SEMANTIC_BY_ROLE.get(self, SemanticRole.UNKNOWN)


_SEMANTIC_BY_ROLE = {
    BlockRole.PAGE_NUMBER: SemanticRole.UNKNOWN,
    BlockRole.HEADER: SemanticRole.HEADER,
    BlockRole.FOOTER: SemanticRole.FOOTER,
    BlockRole.TOC_ENTRY: SemanticRole.UNKNOWN,
    BlockRole.HEADING: SemanticRole.HEADING,
    BlockRole.CAPTION: SemanticRole.CAPTION,
    BlockRole.FOOTNOTE: SemanticRole.FOOTNOTE,
    BlockRole.FORMULA: SemanticRole.FORMULA,
    BlockRole.CITATION: SemanticRole.CITATION,
    BlockRole.BODY_TEXT: SemanticRole.BODY_TEXT,
    BlockRole.UNKNOWN: SemanticRole.UNKNOWN,
    # 7H-2C
    BlockRole.CODE: SemanticRole.CODE,
    BlockRole.COMMAND: SemanticRole.UNKNOWN,
    BlockRole.FILENAME: SemanticRole.UNKNOWN,
    BlockRole.IDENTIFIER: SemanticRole.UNKNOWN,
}

# ── 正则特征 ──────────────────────────────────────────────────────────────

_RE_CAPTION = re.compile(
    r"^\s*(?:Fig(?:ure)?|Fig\.?|Table|Tab\.?|Tab|图|表|图\s|表\s)" r"[\s.:]?\s*\d+",
    re.IGNORECASE,
)

# ── 7H-2C 角色仲裁：CODE / COMMAND / FILENAME / IDENTIFIER ───────────────
# 一旦这些类别由可靠证据确认，后续普通角色规则（formula/body）不得覆盖。
_RE_CODE_LINE_KW = re.compile(
    r"^\s*(?:if|else|elif|for|while|do|switch|case|return|break|continue|def|class|"
    r"struct|typedef|using|namespace|include|import|from|export|function|var|let|const|"
    r"static|public|private|protected|void|int|float|double|char|bool|unsigned|signed|"
    r"long|short|template|typename|lambda|yield|pass|print|printf|catch|try)\b",
    re.IGNORECASE,
)
_RE_CODE_SYMBOLS = re.compile(r"(::|->|=>|\+\+|--|==|!=|&&|\|\||<=|>=|;\s*$)")
_RE_BRACE = re.compile(r"[{}]")
_RE_PATH_TRAIL = re.compile(r"(?:^|\s)[\w.\-]+(?:/[\w.\-]+)*(?:\.[A-Za-z0-9]{1,8})$")
_RE_IDENT_SNAKE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RE_COMMAND_VERB = re.compile(
    r"^\s*(?:sudo|cd|ls|cat|echo|gcc|clang|pip|npm|apt|yum|cmake|make|git|docker|"
    r"curl|wget|ssh|scp|export|chmod|python|python3|java|javac|node|npm|go|"
    r"pip3|conda|install|unzip|tar)\b",
    re.IGNORECASE,
)
_RE_HEADING_NUM = re.compile(
    r"^(?:\d+(?:\.\d+)*|[A-Z](?:\.\d+)*|[IVXLC]+\.?|\d+[.)])\s+",
)
_RE_PAGE_NUM = re.compile(r"^\s*\d{1,4}\s*$")
_RE_ROMAN_PAGE = re.compile(r"^\s*[ivxlcdm]+\s*$", re.IGNORECASE)
_RE_FOOTNOTE_MARK = re.compile(
    r"^(?:[†‡§¶*•◦◊○●][\s\d]|\d{1,3}[ .)]\s?)",
)
_RE_MATH_SYMBOL = re.compile(r"[\+\-*/=^_{}<>()~]")
_RE_CITATION = re.compile(
    r"\[\d+[,\]–-]?|^\s*(?:参考文献|References?|Bibliography)\s*$", re.IGNORECASE
)
_RE_LEADER = re.compile(r"[.·…‥].*?[.·…‥]\s*\d{1,4}\s*$")
_RE_DIGIT = re.compile(r"\d")
_RE_PUNCT = re.compile(r"[^\w\s]")
_RE_UPPER = re.compile(r"[A-Z]")
_RE_FORMULA_SYMBOLS = re.compile(r"^[\s\d+\-*/=(){}^_\\<>,.~]+$")

# ── 显示公式判定的「运算符」证据（块级 role 用）────────────────────────────
# 与 span 级的 _RE_MATH_SYMBOL 分开：后者把 () <> ~ 也算作数学符号，用于
# annotate_formulas 的 math 标记 / formula_density 统计；块级 role 决定
# 「整块是否保留原文」，误判代价是**整段永远不被翻译**，必须更保守。
#
# 不收 ()：括号在自然语言里同样高频 —— 引用 '(Smith et al., 2019)'、括注
# '(test)'、图号 'Fig. 7(a)'、区间 '[0, 1]'。<> 保留：'a < b' 是真数学，而
# 散文里几乎不出现。\ { } ~ 是 LaTeX 记号的可靠证据；'~' 在中文里是数值区间
# 连接号（'2001~2010'），但中文散文会被下面的 CJK 守卫先排除。
_RE_FORMULA_OPERATOR = re.compile(r"[\+\-*/=^_~\\{}×÷±∑∏∫√∞≠≈≤≥<>→←⇒∈∀∃]")

#: 判定「像自然语言」的词：整词为字母（可含内部连字符/撇号）、≥3 字符。
#: 按空白切 token 后要求**整词**匹配，这样 LaTeX 记号 '\int_0^1' 不会被
#: 误当成单词 'int'。
_RE_PROSE_WORD = re.compile(r"[A-Za-z][A-Za-z'\-]*")

#: 散文里至少要有这么多个自然语言词，才认定「这是句子，不是显示公式」。
_PROSE_MIN_WORDS = 2
#: CJK 字符达到这个数即视为散文（显示公式不含成句的汉字）。
_PROSE_MIN_CJK = 4


def _prose_word_count(text: str) -> int:
    """按空白切词后，统计「整词为字母且 ≥3 字符」的 token 数。"""
    count = 0
    for token in text.split():
        if len(token) >= 3 and _RE_PROSE_WORD.fullmatch(token):
            count += 1
    return count


def _looks_like_display_formula(text: str) -> bool:
    """文本启发式：这段文字**整体**是不是一条显示公式。

    这是布局类别缺失时的兜底（MinerU/magic-pdf 已判为 ``interline_equation``
    的块根本不会走到这里 —— ``annotate_roles`` 只处理 ``kind == "paragraph"``）。
    原文把「≥2 个数学符号 + 字母数字占比 < 0.85」当公式，但：

    1. 数学符号集含 ``()``，而英文散文的引用/括注/图号都带括号；
    2. ``str.isalnum()`` 不计空格，英文散文天然落在 0.72~0.84，这个守卫
       对散文**从不生效**。

    实测 20 句普通英文散文有 15 句被判成 formula（置信度 0.9），于是整块落入
    ``KEEP_KINDS`` 被保留 —— 扫描件里这些正文从此不再翻译。所以这里改成：
    要求真正的运算符证据，并显式排除「读起来是句子」的文本。
    """
    stripped = (text or "").strip()
    if not stripped:
        return False
    # 纯符号/数字串（'x = a + b'、'2001-2010'、'45.2 +/- 1.3'）直接认定。
    if _RE_FORMULA_SYMBOLS.match(stripped):
        return True
    operators = sum(1 for _ in _RE_FORMULA_OPERATOR.finditer(stripped))
    if operators < 2:
        return False
    # 「读起来是句子」→ 不是显示公式（哪怕句中含公式）。
    if _prose_word_count(stripped) >= _PROSE_MIN_WORDS:
        return False
    if sum(1 for c in stripped if "一" <= c <= "鿿") >= _PROSE_MIN_CJK:
        return False
    alnum = sum(1 for c in stripped if c.isalnum()) / max(len(stripped), 1)
    return alnum < 0.85


@dataclass
class BlockFeatures:
    """块的数值特征向量（Structure Classifier 的输入）。"""

    font_size: float = 0.0
    weight_est: float = 0.0
    """字体粗细估计（0~1）：从字体名关键词（Bold/Heavy/Black）启发。"""

    indent: float = 0.0
    alignment: str = "left"
    line_count: int = 1
    spacing_ratio: float = 0.0
    numbering: bool = False
    digit_ratio: float = 0.0
    punctuation_ratio: float = 0.0
    leader_ratio: float = 0.0
    """点线引导字符（.·…‥）占字符比例 —— 目录行核心特征。"""

    capital_ratio: float = 0.0
    position_top: float = 0.0
    position_bottom: float = 0.0
    """块在页面中的位置（0~1，自页顶/页底起）。"""

    text: str = ""

    def to_dict(self) -> dict:
        return {
            k: round(v, 4) if isinstance(v, float) else v
            for k, v in self.__dict__.items()
        }


@dataclass
class ClassifiedBlock:
    """分类结果：Paragraph + 角色 + 置信度。"""

    paragraph: Paragraph
    role: BlockRole
    confidence: float = 0.0
    features: BlockFeatures = field(default_factory=BlockFeatures)


def _weight_from_font(font: str) -> float:
    low = (font or "").lower()
    for token, w in (
        ("black", 1.0),
        ("heavy", 1.0),
        ("bold", 0.9),
        ("demibold", 0.75),
        ("semibold", 0.7),
        ("medium", 0.5),
    ):
        if token in low:
            return w
    return 0.0


def compute_features(
    para: Paragraph,
    page: Optional[PageGeometry] = None,
    body_font_size: Optional[float] = None,
) -> BlockFeatures:
    """计算 Paragraph 的特征向量（阶段三特征清单的数值化）。"""
    text = para.text
    total = max(len(text), 1)
    digits = len(_RE_DIGIT.findall(text))
    puncts = len(_RE_PUNCT.findall(text))
    leaders = sum(1 for c in text if c in ".·…‥")
    capitals = sum(1 for c in text if c.isupper())
    sizes = [l.size for l in para.lines]
    f = BlockFeatures(
        font_size=max(sizes) if sizes else 0.0,
        weight_est=(
            max(_weight_from_font(l.words[0].font) for l in para.lines if l.words)
            if para.lines
            else 0.0
        ),
        indent=para.first_line_indent,
        alignment=para.alignment,
        line_count=para.line_count,
        digit_ratio=digits / total,
        punctuation_ratio=puncts / total,
        leader_ratio=leaders / total,
        capital_ratio=capitals / total,
        numbering=bool(_RE_HEADING_NUM.match(text)),
        text=text,
    )
    if page is not None:
        top = max(p.y1 for p in page.paragraphs) if page.paragraphs else para.y1
        bot = min(p.y0 for p in page.paragraphs) if page.paragraphs else para.y0
        page_h = max(1.0, top - bot)
        f.position_top = (top - para.y1) / page_h if page_h else 1.0
        f.position_bottom = (para.y0 - bot) / page_h if page_h else 1.0
    else:
        f.position_top = 1.0
        f.position_bottom = 1.0
    if body_font_size:
        f.spacing_ratio = f.font_size / body_font_size
    return f


class StructureClassifier:
    """块角色分类器：规则 + 统计特征，确定性、可调试。

    判定顺序（优先级从高到低）：页码 → 目录行 → 页眉页脚 → 题注 →
    脚注 → 公式 → 引用 → 标题 → 正文。每个判定返回置信度，
    全部不命中则归为 BODY_TEXT（正文为默认角色，与路线图一致）。
    """

    def __init__(
        self,
        header_footer_margin: float = 0.06,
        heading_font_ratio: float = 1.15,
        body_font_size: Optional[float] = None,
    ) -> None:
        self.header_footer_margin = header_footer_margin
        self.heading_font_ratio = heading_font_ratio
        self._body_font_size = body_font_size

    # ── 字体基准 ────────────────────────────────────────────────────

    def estimate_body_font_size(self, pages: Sequence[PageGeometry]) -> float:
        """正文基准字号：全部段落字号的中位数（对标题/脚注免疫）。"""
        sizes = [p.size for page in pages for p in page.paragraphs if p.size > 0]
        if not sizes:
            return 12.0
        sizes.sort()
        mid = len(sizes) // 2
        if len(sizes) % 2 == 1:
            return sizes[mid]
        return (sizes[mid - 1] + sizes[mid]) / 2.0

    # ── 分类 ────────────────────────────────────────────────────────

    def classify_page(
        self,
        page: PageGeometry,
        body_font_size: Optional[float] = None,
        order: Optional[Sequence[int]] = None,
    ) -> List[ClassifiedBlock]:
        body = (
            body_font_size
            or self._body_font_size
            or self.estimate_body_font_size([page])
        )
        paras = (
            page.reading_order()
            if order is None
            else [page.paragraphs[i] for i in order]
        )
        return [
            self.classify_paragraph(p, page=page, body_font_size=body) for p in paras
        ]

    def classify_paragraph(
        self,
        para: Paragraph,
        page: Optional[PageGeometry] = None,
        body_font_size: Optional[float] = None,
    ) -> ClassifiedBlock:
        f = compute_features(
            para, page=page, body_font_size=body_font_size or self._body_font_size
        )
        has_page_context = page is not None and len(page.paragraphs) >= 3
        role, conf = self._decide(para, f, has_page_context)
        return ClassifiedBlock(paragraph=para, role=role, confidence=conf, features=f)

    def _arbitrate_preserve_role(
        self,
        para: Paragraph,
        f: BlockFeatures,
        text: str,
    ) -> Optional[Tuple[BlockRole, float]]:
        """7H-2C：高置信度 PRESERVE 类角色的可靠证据仲裁。

        PEP/风格纪律：**只放能被翻译器/策略层正确消费的类别**，且每个类别都
        要求“可靠证据”（不靠单一弱信号），杜绝把正文简单句子误判为代码。
        命中即返回高优先级角色；未命中返回 ``None``，交给既有规则流继续。

        判定顺序（优先级从高到低）：CODE → COMMAND → FILENAME → IDENTIFIER。
        为保守起见，只有**整块**满足多行证据时才给 CODE（含大括号/分号/
        作用域运算符/代码关键字的行占比过半），避免单行 `int x = 5;` 式弱信号
        或带代码示例的正文误伤。
        """
        if not text or len(text) > 4000:
            return None
        # 行跨度：整列代码块（多行、过半行命中结构信号）→ CODE。
        # 适配器（analyzer._RuleParagraphAdapter / document_model._NodeProxy）
        # 可能给出无 ``.text`` 的合成行 —— 此时回退按块文本换行拆解，
        # 保证 code 仲裁在 forensic/build_document_model 主路径同样生效。
        raw_lines = [
            (getattr(l, "text", "") or "").strip()
            for l in getattr(para, "lines", []) or []
        ]
        lines = [ln for ln in raw_lines if ln]
        if not lines:
            lines = [ln for ln in text.splitlines() if ln] or [text]
        if lines:
            sig_lines = sum(
                1
                for ln in lines
                if _RE_CODE_LINE_KW.search(ln)
                or _RE_BRACE.search(ln)
                or ln.rstrip().endswith(";")
                or _RE_CODE_SYMBOLS.search(ln)
            )
            # ≥2 行且命中行 ≥½：整列代码块。
            if len(lines) >= 2 and sig_lines >= 2 and sig_lines >= len(lines) / 2:
                return BlockRole.CODE, 0.92
            # 单/短代码行：需较强的单一主体责任信号（代码关键字 + 结构符号同现）
            if (
                len(lines) == 1
                and len(ln := lines[0]) <= 160
                and _RE_CODE_LINE_KW.search(ln)
                and (_RE_BRACE.search(ln) or ln.rstrip().endswith(";"))
            ):
                return BlockRole.CODE, 0.85
            # 作用域运算符/箭头：C++ 风格代码极强信号（`namespace std::` 等）
            if len(lines) <= 2 and _RE_CODE_SYMBOLS.search(text):
                if _RE_CODE_LINE_KW.search(text) or _RE_BRACE.search(text):
                    return BlockRole.CODE, 0.9
        # 单行 COMMAND（shell/CLI 动词起始）：只保护命令本身，不误伤正文。
        if (
            f.line_count <= 1
            and len(text) <= 200
            and _RE_COMMAND_VERB.match(text)
            and len(text.split()) >= 2
        ):
            return BlockRole.COMMAND, 0.8
        # 单行 FILENAME/路径。两种可靠证据（都需单行、短、非题注、不以句点收尾）：
        #   a) 以可识别扩展名结尾（config/settings.yaml / data.csv）
        #   b) 无空格的多段路径（/usr/local/bin/python3）—— 无扩展名也按路径保护。
        if (
            f.line_count <= 1
            and len(text) <= 160
            and not text.endswith(".")
            and not _RE_CAPTION.match(text)
        ):
            _last = text.split()[-1]
            if ("." in text and "." in _last and _RE_PATH_TRAIL.search(text)) or (
                len(text.split()) == 1
                and "/" in text
                and "." not in _last
                and not text.isspace()
            ):
                return BlockRole.FILENAME, 0.72
        # 单 token IDENTIFIER（snake_case / camelCase / 含下划线，无空格）
        tok = text.strip()
        if (
            len(tok.split()) == 1
            and len(tok) <= 120
            and _RE_IDENT_SNAKE.match(tok)
            and ("_" in tok or any(c.isdigit() for c in tok))
        ):
            return BlockRole.IDENTIFIER, 0.7
        return None

    def _decide(
        self, para: Paragraph, f: BlockFeatures, has_page_context: bool
    ) -> Tuple[BlockRole, float]:
        text = f.text.strip()
        if not text:
            return BlockRole.UNKNOWN, 0.0

        # 1. 页码：纯数字或罗马数字（位于页边缘时置信度更高）
        if (_RE_PAGE_NUM.match(text) and len(text) <= 6) or (
            _RE_ROMAN_PAGE.match(text) and len(text) <= 8
        ):
            if has_page_context and (
                f.position_bottom <= self.header_footer_margin
                or f.position_top <= self.header_footer_margin
            ):
                return BlockRole.PAGE_NUMBER, 0.95
            return BlockRole.PAGE_NUMBER, 0.7

        # 2. 目录行：点线引导 + 行尾页码（复用 toc 模式）
        if f.leader_ratio >= 0.08 and _RE_LEADER.search(text):
            return BlockRole.TOC_ENTRY, min(0.98, 0.6 + f.leader_ratio)

        # 3. 题注：Figure/Table/图/表 + 编号
        if _RE_CAPTION.match(text) and len(text) <= 200:
            return BlockRole.CAPTION, 0.92

        # 3.5 7H-2C 角色仲裁：CODE / COMMAND / FILENAME / IDENTIFIER ——
        # 必须在 formula（第 4 步）之前判定，否则代码里的 `->`/`=`/大括号会
        # 被数学符号密度规则吞成 formula，绕过 KEEP_KINDS 的保护。
        arb = self._arbitrate_preserve_role(para, f, text)
        if arb is not None:
            return arb

        # 4. 公式：要求真正的运算符证据，并排除读起来是句子的文本
        #    （旧规则把 '(Smith et al., 2019)' / '2001-2010' / '45.2 +/- 1.3'
        #     判成 formula，扫描件里这些正文整块落入 KEEP_KINDS 从不翻译）。
        if _looks_like_display_formula(text):
            return BlockRole.FORMULA, 0.9

        # 5. 脚注：脚注标记开头 + 字号小于正文（标记可为 † 等符号或数字标记）
        if (
            f.line_count <= 1
            and len(text) <= 400
            and _RE_FOOTNOTE_MARK.match(text)
            and f.spacing_ratio
            and f.spacing_ratio < 0.92
        ):
            return BlockRole.FOOTNOTE, 0.8

        # 6. 标题：字号 > 正文基准 × ratio，或带节编号且行数少
        if f.font_size > 0 and f.spacing_ratio >= self.heading_font_ratio:
            conf = min(0.98, 0.65 + 0.5 * (f.spacing_ratio - self.heading_font_ratio))
            return BlockRole.HEADING, conf
        if f.numbering and f.line_count <= 2:
            return BlockRole.HEADING, 0.72

        # 7. 页眉/页脚：单行、短文本、位于页边缘（需有页面上下文）
        if has_page_context and f.line_count == 1 and len(text) <= 60:
            if f.position_top <= self.header_footer_margin:
                return BlockRole.HEADER, 0.85
            if f.position_bottom <= self.header_footer_margin:
                return BlockRole.FOOTER, 0.85

        # 8. 引用/参考文献节标题
        if (
            _RE_CITATION.match(text)
            and f.line_count <= 1
            and text.startswith(("[", "References", "Bibliography", "参考文献"))
        ):
            return BlockRole.CITATION, 0.85

        # 9. 默认正文
        return BlockRole.BODY_TEXT, 0.6


# ── Document IR 升级（阶段一消费端） ─────────────────────────────────────


def to_document_ir(
    pages: Sequence[PageGeometry],
    classifier: Optional[StructureClassifier] = None,
    title: str = "",
    source_lang: str = "en",
    target_lang: str = "zh-cn",
) -> DocumentIR:
    """把多页几何模型 + 结构分类升级为 DocumentIR（阶段一消费端）。

    - 每页一个 Section 容器（ReadingRole.MAIN_FLOW）
    - 每个 Paragraph 一个节点：semantic=角色、translation/rendering 按角色映射
    - 子节点按阅读顺序（XY-Cut 结果）排列，保留几何 bbox
    """
    clf = classifier or StructureClassifier()
    body = clf.estimate_body_font_size(pages)
    ir = DocumentIR(title=title, source_lang=source_lang, target_lang=target_lang)
    for page in pages:
        page_id = f"page_{page.page_num}"
        ir.add_node(
            page_id,
            semantic=SemanticRole.SECTION,
            reading=ReadingRole.MAIN_FLOW,
            rendering=RenderingRole.BLOCK,
            text=f"Page {page.page_num + 1}",
            page_num=page.page_num,
        )
        for para in page.reading_order():
            block = clf.classify_paragraph(para, page=page, body_font_size=body)
            nid = f"p{page.page_num}_{len(ir.get_node(page_id).children)}"
            sem = block.role.semantic_role
            translation = _TRANSLATION_BY_SEM.get(sem, TranslationRole.TRANSLATE)
            rendering = _RENDERING_BY_SEM.get(sem, RenderingRole.BLOCK)
            ir.add_node(
                nid,
                semantic=sem,
                reading=ReadingRole.MAIN_FLOW,
                translation=translation,
                rendering=rendering,
                parent_id=page_id,
                bbox=(para.x0, para.y0, para.x1, para.y1),
                text=para.text,
                page_num=page.page_num,
                confidence=block.confidence,
                metadata={
                    "role": block.role.value,
                    "font_size": round(para.size, 2),
                    "lines": para.line_count,
                    "alignment": para.alignment,
                },
            )
    return ir


_TRANSLATION_BY_SEM = {
    SemanticRole.FORMULA: TranslationRole.KEEP_FORMULA,
    SemanticRole.CITATION: TranslationRole.KEEP_NUMBER,
    SemanticRole.CAPTION: TranslationRole.NEED_CONTEXT,
    SemanticRole.FOOTNOTE: TranslationRole.NEED_CONTEXT,
    SemanticRole.HEADER: TranslationRole.SKIP,
    SemanticRole.FOOTER: TranslationRole.SKIP,
}

_RENDERING_BY_SEM = {
    SemanticRole.HEADER: RenderingRole.OVERLAY,
    SemanticRole.FOOTER: RenderingRole.OVERLAY,
    SemanticRole.FOOTNOTE: RenderingRole.ANCHORED,
}


__all__ = [
    "BlockRole",
    "BlockFeatures",
    "ClassifiedBlock",
    "compute_features",
    "StructureClassifier",
    "to_document_ir",
]
