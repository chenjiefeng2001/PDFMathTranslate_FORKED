import concurrent.futures
import logging
import re
import unicodedata
from enum import Enum
from string import Template
from typing import Dict

import numpy as np
from pdfminer.converter import PDFConverter
from pdfminer.layout import LTChar, LTFigure, LTLine, LTPage
from pdfminer.pdffont import PDFCIDFont, PDFUnicodeNotDefined
from pdfminer.pdfinterp import PDFGraphicState, PDFResourceManager
from pdfminer.utils import apply_matrix_pt, mult_matrix
from pymupdf import Font
from tenacity import retry, wait_fixed

from pdf2zh.translator import (
    AnythingLLMTranslator,
    ArgosTranslator,
    AzureOpenAITranslator,
    AzureTranslator,
    BaseTranslator,
    BingTranslator,
    DeepLTranslator,
    DeepLXTranslator,
    DeepseekTranslator,
    DifyTranslator,
    GeminiTranslator,
    GoogleTranslator,
    GrokTranslator,
    GroqTranslator,
    MiniMaxTranslator,
    ModelScopeTranslator,
    OllamaTranslator,
    OpenAIlikedTranslator,
    OpenAITranslator,
    QwenMtTranslator,
    SiliconTranslator,
    TencentTranslator,
    XinferenceTranslator,
    ZhipuTranslator,
    X302AITranslator,
)

log = logging.getLogger(__name__)


class PDFConverterEx(PDFConverter):
    def __init__(
        self,
        rsrcmgr: PDFResourceManager,
    ) -> None:
        PDFConverter.__init__(self, rsrcmgr, None, "utf-8", 1, None)

    def begin_page(self, page, ctm) -> None:
        # 重载替换 cropbox
        x0, y0, x1, y1 = page.cropbox
        x0, y0 = apply_matrix_pt(ctm, (x0, y0))
        x1, y1 = apply_matrix_pt(ctm, (x1, y1))
        mediabox = (0, 0, abs(x0 - x1), abs(y0 - y1))
        self.cur_item = LTPage(page.pageno, mediabox)
        # === 2.0: 每页开始时重置跨页排版状态，避免跨页坐标误判 ===
        # 不同页面的坐标是相对坐标，若沿用上一页的段落 BBox 会导致误判重叠
        self._rendered_paragraphs = []
        self._rendered_obstacles = []
        self._overflow_flags = []
        self._page_rect = None

    def end_page(self, page):
        # 重载返回指令流
        return self.receive_layout(self.cur_item)

    def begin_figure(self, name, bbox, matrix) -> None:
        # 重载设置 pageid
        self._stack.append(self.cur_item)
        self.cur_item = LTFigure(name, bbox, mult_matrix(matrix, self.ctm))
        self.cur_item.pageid = self._stack[-1].pageid

    def end_figure(self, _: str) -> None:
        # 重载返回指令流
        fig = self.cur_item
        assert isinstance(self.cur_item, LTFigure), str(type(self.cur_item))
        self.cur_item = self._stack.pop()
        self.cur_item.add(fig)
        return self.receive_layout(fig)

    def render_char(
        self,
        matrix,
        font,
        fontsize: float,
        scaling: float,
        rise: float,
        cid: int,
        ncs,
        graphicstate: PDFGraphicState,
    ) -> float:
        # 重载设置 cid 和 font
        try:
            text = font.to_unichr(cid)
            assert isinstance(text, str), str(type(text))
        except PDFUnicodeNotDefined:
            text = self.handle_undefined_char(font, cid)
        textwidth = font.char_width(cid)
        textdisp = font.char_disp(cid)
        item = LTChar(
            matrix,
            font,
            fontsize,
            scaling,
            rise,
            text,
            textwidth,
            textdisp,
            ncs,
            graphicstate,
        )
        self.cur_item.add(item)
        item.cid = cid  # hack 插入原字符编码
        item.font = font  # hack 插入原字符字体
        return item.adv


class Paragraph:
    def __init__(self, y, x, x0, x1, y0, y1, size, brk):
        self.y: float = y  # 初始纵坐标
        self.x: float = x  # 初始横坐标
        self.x0: float = x0  # 左边界
        self.x1: float = x1  # 右边界
        self.y0: float = y0  # 上边界
        self.y1: float = y1  # 下边界
        self.size: float = size  # 字体大小
        self.brk: bool = brk  # 换行标记


# fmt: off
class TranslateConverter(PDFConverterEx):
    def __init__(
        self,
        rsrcmgr,
        vfont: str = None,
        vchar: str = None,
        thread: int = 0,
        layout={},
        lang_in: str = "",
        lang_out: str = "",
        service: str = "",
        noto_name: str = "",
        noto: Font = None,
        envs: Dict = None,
        prompt: Template = None,
        ignore_cache: bool = False,
        # === 2.0 additions ===
        text_metrics: dict = None,
        font_resolver: object = None,
        layout_graph: object = None,
        collision_resolver: object = None,
        translation_cache: object = None,
        skip_subset_fonts: bool = False,
    ) -> None:
        super().__init__(rsrcmgr)
        self.vfont = vfont
        self.vchar = vchar
        self.thread = thread
        self.layout = layout
        self.noto_name = noto_name
        self.noto = noto
        self.skip_subset_fonts = skip_subset_fonts
        # 2.0 modules
        self.text_metrics = text_metrics or {}
        self.font_resolver = font_resolver
        self.layout_graph = layout_graph
        self.collision_resolver = collision_resolver
        self.cache = translation_cache
        self._para_orig_fonts: dict = {}
        self._rendered_obstacles: list = []
        self._rendered_paragraphs: list = []
        # === 2.0: 溢出/避让失败 QA 标记（S5）与页面边界 ===
        self._overflow_flags: list = []
        self._page_rect = None  # 当前页边界（BoundingBox），供碰撞求解夹紧

        self.translator: BaseTranslator = None
        # e.g. "ollama:gemma2:9b" -> ["ollama", "gemma2:9b"]
        param = service.split(":", 1)
        service_name = param[0]
        service_model = param[1] if len(param) > 1 else None
        if not envs:
            envs = {}
        for translator in [GoogleTranslator, BingTranslator, DeepLTranslator, DeepLXTranslator, OllamaTranslator, XinferenceTranslator, AzureOpenAITranslator,
                           OpenAITranslator, ZhipuTranslator, ModelScopeTranslator, SiliconTranslator, GeminiTranslator, AzureTranslator, TencentTranslator, DifyTranslator, AnythingLLMTranslator, ArgosTranslator, GrokTranslator, GroqTranslator, DeepseekTranslator, MiniMaxTranslator, OpenAIlikedTranslator, QwenMtTranslator, X302AITranslator]:
            if service_name == translator.name:
                self.translator = translator(lang_in, lang_out, service_model, envs=envs, prompt=prompt, ignore_cache=ignore_cache)
        if not self.translator:
            raise ValueError("Unsupported translation service")

    def receive_layout(self, ltpage: LTPage):
        # 段落
        sstk: list[str] = []            # 段落文字栈
        pstk: list[Paragraph] = []      # 段落属性栈
        vbkt: int = 0                   # 段落公式括号计数
        # 公式组
        vstk: list[LTChar] = []         # 公式符号组
        vlstk: list[LTLine] = []        # 公式线条组
        vfix: float = 0                 # 公式纵向偏移
        # 公式组栈
        var: list[list[LTChar]] = []    # 公式符号组栈
        varl: list[list[LTLine]] = []   # 公式线条组栈
        varf: list[float] = []          # 公式纵向偏移栈
        vlen: list[float] = []          # 公式宽度栈
        # 全局
        lstk: list[LTLine] = []         # 全局线条栈
        xt: LTChar = None               # 上一个字符
        xt_cls: int = -2                # 上一个字符所属段落。初始为 -2 哨兵值：布局缺失时 cls 回退为 -1，
        # 若此处也是 -1，首字符会误入同一段落分支并越界访问空 sstk/pstk
        vmax: float = ltpage.width / 4  # 行内公式最大宽度
        ops: str = ""                   # 渲染结果
        # === 2.0: 记录页面边界，供碰撞求解夹紧（S1）===
        from pdf2zh.collision_resolver import BoundingBox
        _page_w = float(getattr(ltpage, "width", 0.0) or 0.0)
        _page_h = float(getattr(ltpage, "height", 0.0) or 0.0)
        self._page_rect = BoundingBox(0.0, 0.0, _page_w, _page_h)

        def _extract_font_name(font: str) -> str:
            """从 PDF 字体引用中提取规范字体名（改进版）"""
            if isinstance(font, bytes):
                try:
                    font = font.decode('utf-8')
                except UnicodeDecodeError:
                    return ""
            # 处理 /ABCDEF+CMMI10 格式（取最后一个 + 之后的部分）
            if "+" in font:
                font = font.split("+")[-1]
            return font

        def vflag(font: str, char: str):    # 匹配公式（和角标）字体
            font = _extract_font_name(font)  # 字体名截断（改进版）
            if re.match(r"\(cid:", char):
                return True
            # 基于字体名规则的判定
            if self.vfont:
                if re.match(self.vfont, font):
                    return True
            else:
                # 扩展默认公式字体正则，覆盖 Springer/Elsevier/AMS/LaTeX 等常见数学字体
                if re.match(
                    r"(CM[^R]|MS[BM]|XY|MT|BL|RM|EU[FM]|LA|RS|LINE|LCIRCLE|"
                    r"TeX-|rsfs|txsy|wasy|stmary|"
                    r".*Mono|.*Code|.*Ital|.*Sym|.*Math|"
                    r"EUFM|MSBM|MSAM|CMSY|CMEX|CMMI|S[0-9]|"
                    r"STIX.*|XITS.*Math|Cambria\s*Math|Asana\s*Math|LMMath|MnSymbol|"
                    r"bb[0-9]?|bbold|cal[0-9]?|frak[0-9]?|mathscr)",
                    font,
                ):
                    return True
            # 基于字符集规则的判定
            if self.vchar:
                if re.match(self.vchar, char):
                    return True
            else:
                if (
                    char
                    and char != " "                                     # 非空格
                    and (
                        unicodedata.category(char[0])
                        in ["Lm", "Mn", "Sk", "Sm", "Zl", "Zp", "Zs"]   # 文字修饰符、数学符号、分隔符号
                        or ord(char[0]) in range(0x370, 0x400)          # 希腊字母
                    )
                ):
                    return True
            return False

        ############################################################
        # A. 原文档解析
        cur_line_size = 0.0  # 当前行文字字号基准（同行角标判定），随行切换重置，避免标题→正文字号切换污染整个段落

        for child in ltpage:
            if isinstance(child, LTChar):
                cur_v = False
                try:
                    layout = self.layout[ltpage.pageid]
                    # ltpage.height 可能是 fig 里面的高度，这里统一用 layout.shape
                    h, w = layout.shape
                    # 读取当前字符在 layout 中的类别
                    cx, cy = np.clip(int(child.x0), 0, w - 1), np.clip(int(child.y0), 0, h - 1)
                    cls = layout[cy, cx]
                except (KeyError, IndexError) as e:
                    log.debug("Layout missing for page %s: %s, falling back to default class", ltpage.pageid, e)
                    cls = -1
                    h, w = ltpage.height, ltpage.width
                # 锚定文档中 bullet 的位置
                if child.get_text() == "•":
                    cls = 0
                # 行内字号基准：行切换时重置（同行角标判定用），避免段落级最大字号（如标题）污染角标判定
                if xt is None or abs(child.y0 - xt.y0) > 0.5 * max(child.size, xt.size):
                    cur_line_size = child.size

                # 判定当前字符是否属于公式
                if (                                                                                        # 判定当前字符是否属于公式
                    cls == 0                                                                                # 1. 类别为保留区域
                    or (cls == xt_cls and sstk and len(sstk[-1].strip()) > 1 and child.size < cur_line_size * 0.79)  # 2. 角标字体，有 0.76 的角标和 0.799 的大写，这里用 0.79 取中，同时考虑首字母放大的情况
                    or vflag(child.fontname, child.get_text())                                              # 3. 公式字体
                    or (child.matrix[0] == 0 and child.matrix[3] == 0)                                      # 4. 垂直字体
                ):
                    cur_v = True
                # 判定括号组是否属于公式
                if not cur_v:
                    if vstk and child.get_text() == "(":
                        cur_v = True
                        vbkt += 1
                    if vbkt and child.get_text() == ")":
                        cur_v = True
                        vbkt -= 1
                if (                                                        # 判定当前公式是否结束
                    not cur_v                                               # 1. 当前字符不属于公式
                    or cls != xt_cls                                        # 2. 当前字符与前一个字符不属于同一段落
                    # or (abs(child.x0 - xt.x0) > vmax and cls != 0)        # 3. 段落内换行，可能是一长串斜体的段落，也可能是段内分式换行，这里设个阈值进行区分
                    # 禁止纯公式（代码）段落换行，直到文字开始再重开文字段落，保证只存在两种情况
                    # A. 纯公式（代码）段落（锚定绝对位置）sstk[-1]=="" -> sstk[-1]=="{v*}"
                    # B. 文字开头段落（排版相对位置）sstk[-1]!=""
                    or (sstk and sstk[-1] != "" and abs(child.x0 - xt.x0) > vmax)    # 因为 cls==xt_cls==0 一定有 sstk[-1]==""，所以这里不需要再判定 cls!=0
                ):
                    if vstk:
                        if (                                                # 根据公式右侧的文字修正公式的纵向偏移
                            not cur_v                                       # 1. 当前字符不属于公式
                            and cls == xt_cls                               # 2. 当前字符与前一个字符属于同一段落
                            and child.x0 > max([vch.x0 for vch in vstk])    # 3. 当前字符在公式右侧
                        ):
                            vfix = vstk[0].y0 - child.y0
                        if sstk and sstk[-1] == "":
                            xt_cls = -1 # 禁止纯公式段落（sstk[-1]=="{v*}"）的后续连接，但是要考虑新字符和后续字符的连接，所以这里修改的是上个字符的类别
                        sstk[-1] += f"{{v{len(var)}}}"
                        var.append(vstk)
                        varl.append(vlstk)
                        varf.append(vfix)
                        vstk = []
                        vlstk = []
                        vfix = 0
                # 当前字符不属于公式或当前字符是公式的第一个字符
                if not vstk:
                    if xt is not None and cls == xt_cls:               # 当前字符与前一个字符属于同一段落
                        if child.x0 > xt.x1 + 1:    # 添加行内空格
                            sstk[-1] += " "
                        elif child.x1 < xt.x0:      # 添加换行空格并标记原文段落存在换行
                            sstk[-1] += " "
                            pstk[-1].brk = True
                    else:                           # 根据当前字符构建一个新的段落
                        sstk.append("")
                        pstk.append(Paragraph(child.y0, child.x0, child.x0, child.x0, child.y0, child.y1, child.size, False))
                if not cur_v:                                               # 文字入栈
                    if (                                                    # 根据当前字符修正段落属性
                        child.size > pstk[-1].size                          # 1. 当前字符比段落字体大
                        or len(sstk[-1].strip()) == 1                       # 2. 当前字符为段落第二个文字（考虑首字母放大的情况）
                    ) and child.get_text() != " ":                          # 3. 当前字符不是空格
                        pstk[-1].y -= child.size - pstk[-1].size            # 修正段落初始纵坐标，假设两个不同大小字符的上边界对齐
                        pstk[-1].size = child.size
                    if child.size > cur_line_size and child.get_text() != " ":
                        cur_line_size = child.size                          # 更新当前行文字字号基准（仅文字字符，公式字符不污染）

                    sstk[-1] += child.get_text()
                else:                                                       # 公式入栈
                    if (                                                    # 根据公式左侧的文字修正公式的纵向偏移
                        not vstk                                            # 1. 当前字符是公式的第一个字符
                        and cls == xt_cls                                   # 2. 当前字符与前一个字符属于同一段落
                        and child.x0 > xt.x0                                # 3. 前一个字符在公式左侧
                    ):
                        vfix = child.y0 - xt.y0
                    vstk.append(child)
                # 更新段落边界，因为段落内换行之后可能是公式开头，所以要在外边处理
                pstk[-1].x0 = min(pstk[-1].x0, child.x0)
                pstk[-1].x1 = max(pstk[-1].x1, child.x1)
                pstk[-1].y0 = min(pstk[-1].y0, child.y0)
                pstk[-1].y1 = max(pstk[-1].y1, child.y1)
                # 更新上一个字符
                xt = child
                xt_cls = cls
            elif isinstance(child, LTFigure):   # 图表
                # === 2.0: 记录原文非文字元素 BBox 供碰撞检测使用 ===
                # 图片、表格、公式块等非文字元素不参与翻译排版，
                # 但译文段落必须避开其占据的空间，避免图文重叠
                try:
                    from pdf2zh.collision_resolver import BoundingBox
                    self._rendered_obstacles.append(
                        BoundingBox(
                            float(child.x0), float(child.y0),
                            float(child.x1), float(child.y1),
                        )
                    )
                except Exception as e:
                    log.debug("Failed to record figure obstacle: %s", e)
            elif isinstance(child, LTLine):     # 线条
                try:
                    layout = self.layout[ltpage.pageid]
                    # ltpage.height 可能是 fig 里面的高度，这里统一用 layout.shape
                    h, w = layout.shape
                    # 读取当前线条在 layout 中的类别
                    cx, cy = np.clip(int(child.x0), 0, w - 1), np.clip(int(child.y0), 0, h - 1)
                    cls = layout[cy, cx]
                except (KeyError, IndexError):
                    cls = -1
                    lstk.append(child)  # 布局缺失时按全局线条处理
                    continue
                if vstk and cls == xt_cls:      # 公式线条
                    vlstk.append(child)
                else:                           # 全局线条
                    lstk.append(child)
                    # === 2.0: 表格边框/公式块边界登记为障碍物 (S6) ===
                    # 表格与块级公式区域在 layout 中被标记为 0（保留区域），
                    # 其边框线不会进入 vstk（非公式上下文），登记后译文段落可避让。
                    # 过滤细线/装饰线（linewidth < 1.0 或长度 < 30pt），降低误报。
                    if cls == 0 and child.linewidth >= 1.0:
                        dx = child.x1 - child.x0
                        dy = child.y1 - child.y0
                        if dx * dx + dy * dy >= 900.0:
                            try:
                                self._rendered_obstacles.append(
                                    BoundingBox(
                                        float(child.x0), float(child.y0),
                                        float(child.x1), float(child.y1),
                                    )
                                )
                            except Exception as e:
                                log.debug("Failed to record table/formula obstacle: %s", e)

            else:
                pass
        # 处理结尾
        if vstk:    # 公式出栈
            sstk[-1] += f"{{v{len(var)}}}"
            var.append(vstk)
            varl.append(vlstk)
            varf.append(vfix)
        log.debug("\n==========[VSTACK]==========\n")
        for id, v in enumerate(var):  # 计算公式宽度
            l = max([vch.x1 for vch in v]) - v[0].x0
            log.debug(f'< {l:.1f} {v[0].x0:.1f} {v[0].y0:.1f} {v[0].cid} {v[0].fontname} {len(varl[id])} > v{id} = {"".join([ch.get_text() for ch in v])}')
            vlen.append(l)

        ############################################################
        # B. 段落翻译
        log.debug("\n==========[SSTACK]==========\n")

        @retry(wait=wait_fixed(1))
        def worker(s: str):  # 多线程翻译
            if not s.strip() or re.match(r"^\{v\d+\}$", s):  # 空白和公式不翻译
                return s
            try:
                new = self.translator.translate(s)
                return new
            except BaseException as e:
                if log.isEnabledFor(logging.DEBUG):
                    log.exception(e)
                else:
                    log.exception(e, exc_info=False)
                raise e

        def _safe_worker(s: str):
            """带 fallback + cache 的 worker (2.0 L3)"""
            if self.cache:
                cached = self.cache.get(s, self.translator.lang_in, self.translator.lang_out)
                if cached is not None:
                    return cached
            try:
                result = worker(s)
                if self.cache:
                    self.cache.set(s, self.translator.lang_in, self.translator.lang_out, result)
                return result
            except BaseException as e:
                log.error("Translation worker exhausted retries, falling back to original: %s", str(e)[:120])
                return s

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, self.thread or 4)  # thread<=0 时兜底为 4，避免 max_workers=0 崩溃
        ) as executor:
            news = list(executor.map(_safe_worker, sstk))

        ############################################################
        # C. 新文档排版
        def raw_string(fcur: str, cstk: str):  # 编码字符串
            if fcur == self.noto_name:
                return "".join(["%04x" % self.noto.has_glyph(ord(c)) for c in cstk])
            elif isinstance(self.fontmap.get(fcur), PDFCIDFont):  # 判断编码长度
                return "".join(["%04x" % ord(c) for c in cstk])
            else:
                return "".join(["%02x" % ord(c) for c in cstk])

        # 根据目标语言获取默认行距
        LANG_LINEHEIGHT_MAP = {
            "zh-cn": 1.4, "zh-tw": 1.4, "zh-hans": 1.4, "zh-hant": 1.4, "zh": 1.4,
            "ja": 1.1, "ko": 1.2, "en": 1.2, "ar": 1.0, "ru": 0.8, "uk": 0.8, "ta": 0.8
        }
        default_line_height = LANG_LINEHEIGHT_MAP.get(self.translator.lang_out.lower(), 1.1) # 小语种默认1.1
        _x, _y = 0, 0
        ops_list = []

        def _safe_float(val):
            """防止 NaN/Inf 进入 PDF 指令流，避免 MuPDF bad 'value' 错误"""
            try:
                if isinstance(val, (bool, str, bytes, bytearray)):
                    val_v = float(val) if not isinstance(val, bool) else float(int(val))
                else:
                    val_v = float(val)
                if np.isfinite(val_v):
                    return f"{val_v:.4f}"
                return "0.0000"
            except (ValueError, TypeError, OverflowError, AttributeError):
                return "0.0000"

        def gen_op_txt(font, size, x, y, rtxt):
            return f"/{font} {_safe_float(size)} Tf 1 0 0 1 {_safe_float(x)} {_safe_float(y)} Tm [<{rtxt}>] TJ "

        def gen_op_line(x, y, xlen, ylen, linewidth):
            return f"ET q 1 0 0 1 {_safe_float(x)} {_safe_float(y)} cm [] 0 d 0 J {_safe_float(linewidth)} w 0 0 m {_safe_float(xlen)} {_safe_float(ylen)} l S Q BT "

        for id, new in enumerate(news):
            x: float = pstk[id].x                       # 段落初始横坐标
            y: float = pstk[id].y                       # 段落初始纵坐标
            x0: float = pstk[id].x0                     # 段落左边界
            x1: float = pstk[id].x1                     # 段落右边界
            height: float = pstk[id].y1 - pstk[id].y0   # 段落高度
            size: float = pstk[id].size                 # 段落字体大小
            brk: bool = pstk[id].brk                    # 段落换行标记
            cstk: str = ""                              # 当前文字栈
            fcur: str = None                            # 当前字体 ID
            lidx = 0                                    # 记录换行次数
            tx = x
            fcur_ = fcur
            ptr = 0
            log.debug(f"< {y} {x} {x0} {x1} {size} {brk} > {sstk[id]} | {new}")

            ops_vals: list[dict] = []

            while ptr < len(new):
                vy_regex = re.match(
                    r"\{\s*v([\d\s]+)\}", new[ptr:], re.IGNORECASE
                )  # 匹配 {vn} 公式标记
                mod = 0  # 文字修饰符
                if vy_regex:  # 加载公式
                    ptr += len(vy_regex.group(0))
                    raw_vid_str = vy_regex.group(1).replace(" ", "")
                    if not raw_vid_str.isdigit():
                        log.warning("Translator generated non-numeric formula tag: {%s}", vy_regex.group(1))
                        continue
                    vid = int(raw_vid_str)
                    if vid >= len(var):
                        log.warning("Translator hallucinated formula tag {v%d} (max %d), page %d", vid, len(var) - 1, ltpage.pageid)
                        continue
                    adv = vlen[vid]
                    if var[vid][-1].get_text() and unicodedata.category(var[vid][-1].get_text()[0]) in ["Lm", "Mn", "Sk"]:  # 文字修饰符
                        mod = var[vid][-1].width
                else:  # 加载文字
                    ch = new[ptr]
                    fcur_ = None
                    try:
                        if fcur_ is None and self.fontmap["tiro"].to_unichr(ord(ch)) == ch:
                            fcur_ = "tiro"
                    except Exception:
                        pass
                    if fcur_ is None:
                        fcur_ = self.noto_name
                    tm = self.text_metrics.get(fcur_) if self.text_metrics else None
                    if tm:
                        adv = tm.char_width(ch, size)
                    elif fcur_ == self.noto_name:
                        adv = self.noto.char_lengths(ch, size)[0]
                    else:
                        font_obj = self.fontmap.get(fcur_)
                        if font_obj:
                            # pdfminer PDFType1Font.char_width 返回 0~1 的 em 比例（Times 等 Type1 字体），
                            # 必须乘字号才是 pt；此前未缩放导致英文宽度被低估约 16 倍，
                            # 中英混排时后续中文字符起点严重偏左、压在英文之上（重叠根因）。
                            adv = font_obj.char_width(ord(ch)) * size
                            if adv <= 0 and not self.skip_subset_fonts:
                                adv = size * 0.5
                        else:
                            adv = size * 0.5
                    ptr += 1
                if (                                # 输出文字缓冲区
                    fcur_ != fcur                   # 1. 字体更新
                    or vy_regex                     # 2. 插入公式
                    or x + adv > x1 + 0.1 * size    # 3. 到达右边界（可能一整行都被符号化，这里需要考虑浮点误差）
                ):
                    if cstk:
                        ops_vals.append({
                            "type": OpType.TEXT,
                            "font": fcur,
                            "size": size,
                            "x": tx,
                            "dy": 0,
                            "rtxt": raw_string(fcur, cstk),
                            "lidx": lidx
                        })
                        cstk = ""
                if x + adv > x1 + 0.1 * size:  # 到达右边界一律换行（S4：不再要求原文 brk，译文单行段落超宽也能折行）
                    x = x0
                    lidx += 1
                if vy_regex:  # 插入公式
                    fix = 0
                    if fcur is not None:  # 段落内公式修正纵向偏移
                        fix = varf[vid]
                    for vch in var[vid]:  # 排版公式字符
                        vc = chr(vch.cid)
                        ops_vals.append({
                            "type": OpType.TEXT,
                            "font": self.fontid.get(vch.font, "0"),
                            "size": vch.size,
                            "x": x + vch.x0 - var[vid][0].x0,
                            "dy": fix + vch.y0 - var[vid][0].y0,
                            "rtxt": raw_string(self.fontid.get(vch.font, "0"), vc),
                            "lidx": lidx
                        })
                        if log.isEnabledFor(logging.DEBUG):
                            lstk.append(LTLine(0.1, (_x, _y), (x + vch.x0 - var[vid][0].x0, fix + y + vch.y0 - var[vid][0].y0)))
                            _x, _y = x + vch.x0 - var[vid][0].x0, fix + y + vch.y0 - var[vid][0].y0
                    for l in varl[vid]:  # 排版公式线条
                        if l.linewidth < 5:  # hack 有的文档会用粗线条当图片背景
                            ops_vals.append({
                                "type": OpType.LINE,
                                "x": l.pts[0][0] + x - var[vid][0].x0,
                                "dy": l.pts[0][1] + fix - var[vid][0].y0,
                                "linewidth": l.linewidth,
                                "xlen": l.pts[1][0] - l.pts[0][0],
                                "ylen": l.pts[1][1] - l.pts[0][1],
                                "lidx": lidx
                            })
                else:  # 插入文字缓冲区
                    if not cstk:  # 单行开头
                        tx = x
                        if x == x0 and ch == " ":  # 消除段落换行空格
                            adv = 0
                        else:
                            cstk += ch
                    else:
                        cstk += ch
                adv -= mod # 文字修饰符
                fcur = fcur_
                x += adv
                if log.isEnabledFor(logging.DEBUG):
                    lstk.append(LTLine(0.1, (_x, _y), (x, y)))
                    _x, _y = x, y
            # 处理结尾
            if cstk:
                ops_vals.append({
                    "type": OpType.TEXT,
                    "font": fcur,
                    "size": size,
                    "x": tx,
                    "dy": 0,
                    "rtxt": raw_string(fcur, cstk),
                    "lidx": lidx
                })

            # === 2.0: TextMetrics line height (M1, S5) ===
            line_height = default_line_height
            # CJK/western mixed line height（扩展字符覆盖：全角标点 U+FF00-U+FFEF 也计入 CJK）
            has_cjk = any(
                ("一" <= c <= "鿿")
                or ("　" <= c <= "〿")
                or ("＀" <= c <= "￯")
                for c in new
            )
            if has_cjk:
                line_height = max(default_line_height, 1.3)
            # 行高下限优先取目标字形真实跨度 ascent-descent（>1.0），
            # 避免压缩到 1.0 时相邻行字面盒相接（行级重叠来源，S5）
            line_height_min = 1.0
            tm_line = self.text_metrics.get(fcur) if self.text_metrics else None
            if tm_line:
                ascent = getattr(tm_line, 'ascent', 0.8)
                descent = getattr(tm_line, 'descent', -0.2)
                if np.isfinite(ascent) and np.isfinite(descent):
                    glyph_span = max(ascent - descent, 1.0)
                    line_height_min = max(glyph_span, 1.0)
                    line_height = line_height_min
            if has_cjk:
                line_height = max(line_height, 1.3)
                line_height_min = max(line_height_min, 1.3)
            # 压缩循环：原文高度不足以容纳译文行数时压缩行距，
            # 但止步于行高下限；压缩到下限仍溢出则记录 QA 溢出标记
            if (
                height > 0
                and np.isfinite(height)
                and (lidx + 1) * size * line_height > height
            ):
                max_iter = max(int((default_line_height - 0.5) / 0.05), 1)
                iter_count = 0
                while (
                    (lidx + 1) * size * line_height > height
                    and line_height > line_height_min
                    and iter_count < max_iter
                ):
                    line_height = max(line_height - 0.05, line_height_min)
                    iter_count += 1
                if (lidx + 1) * size * line_height > height:
                    self._overflow_flags.append(
                        {
                            "page": ltpage.pageid,
                            "lidx": lidx,
                            "size": size,
                            "line_height": line_height,
                            "required_height": (lidx + 1) * size * line_height,
                            "available_height": height,
                            "text": new[:40],
                        }
                    )
            # === 2.0: Collision detection & resolution (M2, S1/S2/S3) ===
            para_bottom = y - (lidx + 1) * size * line_height
            if self.collision_resolver:
                from pdf2zh.collision_resolver import BoundingBox
                pb = BoundingBox(x0, para_bottom, x1, y)
                shift = 0.0
                # 融合已渲染段落与原文非文字元素（图片/表格/公式块等）。
                # lidx > 0 门控已移除：即使译文仍为单行（lidx == 0），
                # 中文宽度膨胀导致底部下探同样可能侵占下方段落空间。
                all_obstacles = list(self._rendered_paragraphs) + list(self._rendered_obstacles)
                colliding = [obs for obs in all_obstacles if pb.overlaps(obs)]
                if colliding:
                    # S2: 一次传入全部障碍物，求解器给出全局可行位置；
                    # S3: 解包 (x, y, size) 全部应用（宽度/字号缩减真正落地）。
                    nx, ny, nsize, strategy = self.collision_resolver.resolve(
                        pb,
                        all_obstacles,
                        size,
                        page_rect=self._page_rect,
                        return_strategy=True,
                    )
                    # S1: 无条件应用位移 —— 负 shift 即向下推挤（正文推进方向），
                    # 消除 `if shift > 0` 对下移解的丢弃；配合全量障碍物，
                    # "当前段被推到所有重叠段之下"自动形成整页链式流式重排。
                    shift = ny - pb.y0
                    if shift:
                        y += shift
                    if nsize != size:  # S3: 字号缩减生效
                        size = nsize
                    if nx != x0:  # S3: 水平缩进生效，平移已生成行
                        dx = nx - x0
                        for v in ops_vals:
                            v["x"] += dx
                # 记录已渲染段落（使用最终位置），供后续段落链式避让
                pb = BoundingBox(x0, para_bottom + shift, x1, y)
                self._rendered_paragraphs.append(pb)
                if colliding and strategy == "none":
                    # 全部策略失败：位置/字号均未变，必然仍重叠 → QA 标记
                    self._overflow_flags.append(
                        {
                            "page": ltpage.pageid,
                            "kind": "collision-unresolved",
                            "lidx": lidx,
                            "bbox": f"[{pb.x0:.1f},{pb.y0:.1f},{pb.x1:.1f},{pb.y1:.1f}]",
                            "text": new[:40],
                        }
                    )


            for vals in ops_vals:
                if vals["type"] == OpType.TEXT:
                    ops_list.append(gen_op_txt(vals["font"], vals["size"], vals["x"], vals["dy"] + y - vals["lidx"] * size * line_height, vals["rtxt"]))
                elif vals["type"] == OpType.LINE:
                    ops_list.append(gen_op_line(vals["x"], vals["dy"] + y - vals["lidx"] * size * line_height, vals["xlen"], vals["ylen"], vals["linewidth"]))

        # === 2.0: QA 溢出标记（S5）→ 内容流注释 + debug 日志，供自动化回归解析 ===
        for flag in self._overflow_flags:
            if "required_height" in flag:
                log.debug(
                    "QA overflow page=%s lidx=%s required=%.1f available=%.1f",
                    flag["page"], flag["lidx"], flag["required_height"], flag["available_height"],
                )
                ops_list.append(
                    f"% pdf2zh-qa-overflow page={flag['page']} lidx={flag['lidx']} "
                    f"required={flag['required_height']:.1f} available={flag['available_height']:.1f}\n"
                )
            else:
                log.debug(
                    "QA collision-unresolved page=%s lidx=%s bbox=%s",
                    flag["page"], flag["lidx"], flag["bbox"],
                )
                ops_list.append(
                    f"% pdf2zh-qa-overflow page={flag['page']} lidx={flag['lidx']} "
                    f"kind={flag['kind']} bbox={flag['bbox']}\n"
                )
        for l in lstk:  # 排版全局线条
            if l.linewidth < 5:  # hack 有的文档会用粗线条当图片背景
                ops_list.append(gen_op_line(l.pts[0][0], l.pts[0][1], l.pts[1][0] - l.pts[0][0], l.pts[1][1] - l.pts[0][1], l.linewidth))

        ops = f"BT {''.join(ops_list)}ET "
        return ops


class OpType(Enum):
    TEXT = "text"
    LINE = "line"
