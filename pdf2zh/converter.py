import concurrent.futures
import logging
import os
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
from tenacity import retry, stop_after_attempt, wait_fixed


def _translate_retry_attempts() -> int:
    """Resolve the per-segment translate retry budget (env-overridable).

    Unparseable / non-positive values fall back to the default of 3.
    """
    try:
        val = int(os.environ.get("PDF2ZH_TRANSLATE_RETRY") or 0)
    except (TypeError, ValueError):
        val = 0
    return val if val >= 1 else 3


#: 单段翻译失败的最大重试次数。无下限重试会让失败网络/服务把任务
#: 永久卡在 translating 阶段（progress 冻结、占用 GUI 并发槽位），
#: 超限后由 _safe_worker 的 fallback 返回原文，整任务继续推进。
_TRANSLATE_RETRY_ATTEMPTS = _translate_retry_attempts()

from pdf2zh.toc import TOC_LEADER_CHARS, char_adv, detect_toc_line, looks_like_toc_text
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
    build_translator,
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
        self._layout_violations = []

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
        emit_ir: bool = False, relayout_gate: object = None,
    ) -> None:
        super().__init__(rsrcmgr)
        self.vfont = vfont
        self.vchar = vchar
        self.thread = thread
        self.layout = layout
        self.noto_name = noto_name
        self.noto = noto
        self.skip_subset_fonts = skip_subset_fonts
        # V8.3/V8.4 side-channels（逻辑在 v3.mainline_wiring）
        self.emit_ir, self.relayout_gate = emit_ir, relayout_gate
        self.ir_snapshots, self.gate_verdicts = {}, {}
        # P5–P10 side-channels（语义重建 + 公式几何 QA）
        self.reconstruction_channel = False
        self.reconstruction_records = {}
        self.reconstruction_qa = {}
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
        # P3: 版面不变量验证（LayoutViolation 记录，只采集不阻断）。
        # 记录 source_bbox → target_bbox 的完整几何轨迹，供 QA/报告定位
        # 首个翻译块被推出页面顶部等系统性错位。
        self._layout_violations: list = []

        # F2: 接管段 display 公式垂直流标记（{vN} → 是否块级展示公式）
        self._render_display_marks: dict = {}
        # F3: 接管段源区域（{legacy_idx: source_bbox}，白底覆盖擦除旧图层）
        self._render_source_bboxes: dict = {}

        # V1.19: TOC 识别观察报告（每页 spec/置信度/mode），供 PDF2ZH_TOC_REPORT=1 落盘实证
        self._toc_reports: list = []

        self.translator: BaseTranslator = None
        self.translator = build_translator(service, lang_in, lang_out, envs, prompt, ignore_cache)

    def receive_layout(self, ltpage: LTPage):
        # 段落
        sstk, pstk = [], []         # 段落文字栈 / 段落属性栈
        vbkt: int = 0                   # 段落公式括号计数
        self._gate_records: list = []  # V8.4: 写回前门控段落几何
        from pdf2zh.v3.mainline_wiring import _new_gate_record, run_mainline_channels
        from pdf2zh.v3.toc_semantics import compose_toc_title, parse_toc_entry
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
        toc_track: list = []            # 目录行字符记录：每段 [(点线字符或数字, x0, x1), ...]
        pfkstk: list = [set()]          # 2.0-V1.19: 每段字体指纹（缓存 variant，多字体段分段隔离）
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

        # V8.4-F3: 整页型 Form XObject 文字平铺（v3/figure_flatten.py，保持行数门禁）
        from pdf2zh.v3.figure_flatten import flatten_page_children
        for child in flatten_page_children(ltpage, _page_w, _page_h):
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
                        toc_track.append([])
                        pfkstk.append(set())
                if not cur_v:                                               # 文字入栈
                    if (                                                    # 根据当前字符修正段落属性
                        child.size > pstk[-1].size                          # 1. 当前字符比段落字体大
                        or len(sstk[-1].strip()) == 1                       # 2. 当前字符为段落第二个文字（考虑首字母放大的情况）
                    ) and child.get_text() != " ":                          # 3. 当前字符不是空格
                        pstk[-1].y -= child.size - pstk[-1].size            # 修正段落初始纵坐标，假设两个不同大小字符的上边界对齐
                        pstk[-1].size = child.size
                    if child.size > cur_line_size and child.get_text() != " ":
                        cur_line_size = child.size                          # 更新当前行文字字号基准（仅文字字符，公式字符不污染）

                    _tch = child.get_text()
                    sstk[-1] += _tch
                    if isinstance(child, LTChar):
                        try:
                            pfkstk[-1].add(_extract_font_name(child.fontname))
                        except Exception:
                            pass
                    if _tch in TOC_LEADER_CHARS or _tch.isdigit():
                        toc_track[-1].append((_tch, child.x0, child.x1))
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
                # P1: pdfminer 会把 Form XObject 背景/装饰层包装成整页 LTFigure，
                # 若登记为障碍物则整页变"幽灵障碍物"，导致首翻译块被 push 出页面
                # （用户 dual PDF 的 bbox.y0<0）。面积 >70% 视为装饰层并跳过。
                try:
                    from pdf2zh.collision_resolver import BoundingBox
                    page_area = float(ltpage.width) * float(ltpage.height)
                    fig_area = max(float(child.x1 - child.x0), 0.0) * max(float(child.y1 - child.y0), 0.0)
                    if page_area > 0.0 and fig_area > 0.7 * page_area:
                        self._layout_violations.append({
                            "page": ltpage.pageid,
                            "kind": "skip-background-figure",
                            "category": "BACKGROUND_LAYER",
                            "source_bbox": [round(float(child.x0), 2), round(float(child.y0), 2),
                                            round(float(child.x1), 2), round(float(child.y1), 2)],
                            "reason": f"figure covers {100.0 * fig_area / page_area:.1f}% of page; "
                                      "treated as background/decor layer",
                        })
                    else:
                        self._rendered_obstacles.append(
                            BoundingBox(float(child.x0), float(child.y0),
                                        float(child.x1), float(child.y1)))
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
        if getattr(self, "geometry_cluster", False):  # V8.3 P1：双轨一致才接管聚类
            from pdf2zh.v3.geometry_merge import adopt_geometry_cluster
            self.geometry_adoptions = {**getattr(self, "geometry_adoptions", {}), ltpage.pageid: adopt_geometry_cluster(self, ltpage, sstk, pstk, var, varl, varf, toc_track, vlen)}
        # 阶段 3：P5–P10 主链路接管（渲染前；失败仅 debug 日志，adopt=False 也记报告）
        if getattr(self, "reconstruction_channel", False):
            from pdf2zh.v3.mainline_wiring import run_reconstruction_channel
            run_reconstruction_channel(self, ltpage)
            from pdf2zh.v3.reconstruction_adapter import adopt_reconstruction_cluster
            self.reconstruction_adoptions = {**getattr(self, "reconstruction_adoptions", {}), ltpage.pageid: adopt_reconstruction_cluster(self, ltpage, sstk, pstk, var, varl, varf, vlen, toc_track, pfkstk=pfkstk)}
        if getattr(self, "toc_split", False):  # V1.17-3：合并目录段按物理行重切（side-channel，渲染路径）
            from pdf2zh.v3.toc_analyzer import split_merged_toc_paragraphs
            self.toc_split_reports = {**getattr(self, "toc_split_reports", {}), ltpage.pageid: split_merged_toc_paragraphs(self, ltpage, sstk, pstk, toc_track, page_width=float(getattr(ltpage, "width", 0.0) or 0.0))}

        # B. 段落翻译
        log.debug("\n==========[SSTACK]==========\n")

        # === 目录行结构感知（P0-1/P0-2）：识别"标题+点线+页码"，标题单独翻译，点线/页码原位渲染；V8.7 结构词走模板本地渲染
        # V1.19：置信度双模式 —— full（结构化渲染）/ protect（保护性：标题单独翻译、点线/页码尾部原位保留）
        toc_specs: list = [None] * len(sstk)
        _page_w = float(getattr(ltpage, "width", 0.0) or 0.0)
        for _ti, _ptxt in enumerate(sstk):
            _spec = detect_toc_line(_ptxt, pstk[_ti].brk, toc_track[_ti], pstk[_ti].x1, page_width=_page_w)
            if _spec is not None:
                toc_specs[_ti] = _spec
                _ent = _spec["entry"] = parse_toc_entry(_spec["title"], page=_spec["page_digits"])
                sstk[_ti] = _ent.title if _ent.matched else _spec["title"]  # 结构化标题只送剩余部分（V8.7）；两种模式都不送点线/页码
            elif looks_like_toc_text(_ptxt) and not toc_track[_ti]:
                # 文本形态疑似目录行但缺逐字符几何（track 缺失）→ 提示观察，避免"点线被翻译"复现无据
                log.warning(
                    "page %d: toc-like text without character track, leader/page not protected: %r",
                    ltpage.pageid, _ptxt[:60],
                )
        # V1.19: 收集本页 TOC 观察记录（供 PDF2ZH_TOC_REPORT=1 落盘）
        for _sp in toc_specs:
            if _sp is None:
                continue
            _ent = _sp.get("entry")
            self._toc_reports.append({
                "page": ltpage.pageid,
                "title": _sp["title"],
                "page_digits": _sp["page_digits"],
                "leader": _sp["leader_orig"][:40],
                "score": _sp["score"],
                "mode": _sp["mode"],
                "entry_kind": getattr(getattr(_ent, "kind", None), "name", None)
                if _ent and _ent.matched else None,
            })

        @retry(wait=wait_fixed(1), stop=stop_after_attempt(_TRANSLATE_RETRY_ATTEMPTS))
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

        def _safe_worker(s: str, font_sig: str = ""):
            """带 fallback + cache 的 worker (2.0 L3)；font_sig=多字体段指纹走缓存 variant（V1.19）"""
            if self.cache:
                cached = _cache_get_font(s, font_sig)
                if cached is not None:
                    return cached
            try:
                result = worker(s)
                if self.cache:
                    _cache_set_font(s, result, font_sig)
                return result
            except BaseException as e:
                log.error("Translation worker exhausted retries, falling back to original: %s", str(e)[:120])
                return s

        def _cache_get_font(s: str, font_sig: str):
            # 兼容旧缓存接口（无 variant 参数）
            try:
                return self.cache.get(s, self.translator.lang_in, self.translator.lang_out, variant=font_sig)
            except TypeError:
                return self.cache.get(s, self.translator.lang_in, self.translator.lang_out)

        def _cache_set_font(s: str, result: str, font_sig: str):
            try:
                self.cache.set(s, self.translator.lang_in, self.translator.lang_out, result, variant=font_sig)
            except TypeError:
                self.cache.set(s, self.translator.lang_in, self.translator.lang_out, result)

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, self.thread or 4)  # thread<=0 时兜底为 4，避免 max_workers=0 崩溃
        ) as executor:
            _font_sigs = [
                ("|fonts:" + "|".join(sorted(f)[:8])) if len(f) > 1 else ""
                for f in pfkstk
            ]
            news = list(executor.map(_safe_worker, sstk, _font_sigs))
        news = [compose_toc_title(s.get("entry") if s else None, n, self.translator.lang_out) for s, n in zip(toc_specs, news)]

        # === F2: 接管段真实译文求解（P4 render_bbox 真实化 + P2 display 垂直流标记）===
        if getattr(self, "reconstruction_channel", False):
            try:
                from pdf2zh.v3.reconstruction_render import run_render_resolve
                run_render_resolve(self, ltpage, sstk, pstk, news)
            except Exception as _f2e:  # noqa: BLE001 — F2 失败回退 adapter 几何
                log.debug("F2 render resolve failed: %s", _f2e)

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

        _render_display = (getattr(self, "_render_display_marks", {}) or {}).get(
            ltpage.pageid, {})                            # F2: display 公式标记 {vN}
        _render_src = (getattr(self, "_render_source_bboxes", {}) or {}).get(
            ltpage.pageid, {})                            # F3: 接管段源区域
        _page_h = float(getattr(ltpage, "height", 0.0) or 0.0)
        for id, new in enumerate(news):
            x: float = pstk[id].x                       # 段落初始横坐标
            y: float = pstk[id].y                       # 段落初始纵坐标
            x0: float = pstk[id].x0                     # 段落左边界
            display_marks = _render_display
            vflow_extra = 0.0                            # F2: display 公式非均匀垂直推进累积
            x1: float = pstk[id].x1                     # 段落右边界
            height: float = pstk[id].y1 - pstk[id].y0   # 段落高度
            size: float = pstk[id].size                 # 段落字体大小
            brk: bool = pstk[id].brk                    # 段落换行标记
            spec = toc_specs[id] if toc_specs else None  # 目录行规格（None = 普通段落）
            _spec_mode = spec.get("mode", "full") if spec else None
            toc_mode = _spec_mode == "full"              # 目录行模式：禁折行、点线/页码原位渲染
            _toc_protect = _spec_mode == "protect"       # 保护模式：标题照常折行，点线/页码尾部原位保留
            x1_bound = float("inf") if toc_mode else x1  # 目录行标题不做右边界折行
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
                    or x + adv > x1_bound + 0.1 * size    # 3. 到达右边界（可能一整行都被符号化，这里需要考虑浮点误差）
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
                if x + adv > x1_bound + 0.1 * size:  # 到达右边界一律换行（S4：不再要求原文 brk，译文单行段落超宽也能折行）；目录行禁折行（P0-2）
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
                    # F2: display 公式独占一行，按公式物理高度推进垂直流
                    # （非均匀推进累积到 vflow_extra，供行落位偏移；后续
                    # 文本行必然绘制在公式块之下，杜绝文字叠在公式上）
                    if display_marks.get(vid):
                        _f_top = max(vch.y0 for vch in var[vid])
                        _f_bot = min(vch.y1 for vch in var[vid])
                        _f_h = max(0.0, _f_top - _f_bot) + 0.6 * size
                        vflow_extra += _f_h
                        lidx += 1
                        x = x0
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

            if _toc_protect:
                # === 目录行保护排版（V1.19/P0）：点线+页码从翻译文本剥离后原样追加在尾部 ===
                # 置信度 0.30~0.55 的弱形态（区间页码/弱结构开头）：不要求右对齐，
                # 只保证"点线/页码永不进翻译器"，译文+原始引导结构按序原位追加。
                _cx = x
                _gap = char_adv(self, " ", size) or size * 0.5
                _tail_txt = (spec["leader_orig"] or ".") + (
                    " " + str(spec["page_digits"]) if spec.get("page_digits") else ""
                )
                for _tch in _tail_txt:
                    if _tch == " ":
                        _cx += _gap
                        continue
                    _a = char_adv(self, _tch, size)
                    if not _a or _a <= 0:
                        _a = size * 0.5
                    ops_vals.append({
                        "type": OpType.TEXT,
                        "font": self.noto_name,
                        "size": size,
                        "x": _cx,
                        "dy": 0,
                        "rtxt": raw_string(self.noto_name, _tch),
                        "lidx": lidx,
                    })
                    _cx += _a

            if toc_mode:
                # === 目录行排版（P0-2）：标题已单独翻译，点线+页码原位渲染 ===
                # 点线：从标题结束处向后填充 '.'，到页码起点为止（保留点线引导结构）
                _page = spec["page_digits"]
                _page_start = spec["page_start_x"]
                _page_right = spec["page_right_x"]
                _dot_adv = char_adv(self, ".", size)
                if not _dot_adv or _dot_adv <= 0:
                    _dot_adv = size * 0.5
                _cx = x
                _dot_count = 0
                while _cx + _dot_adv <= _page_start - 0.5 and _dot_count < 300:
                    ops_vals.append({
                        "type": OpType.TEXT,
                        "font": self.noto_name,
                        "size": size,
                        "x": _cx,
                        "dy": 0,
                        "rtxt": raw_string(self.noto_name, "."),
                        "lidx": 0,
                    })
                    _cx += _dot_adv
                    _dot_count += 1
                # 页码：右对齐到页码右边界（保持目录右对齐页码列）
                _pw = sum(char_adv(self, c, size) for c in _page)
                _px = _page_right - _pw
                ops_vals.append({
                    "type": OpType.TEXT,
                    "font": self.noto_name,
                    "size": size,
                    "x": _px,
                    "dy": 0,
                    "rtxt": raw_string(self.noto_name, _page),
                    "lidx": 0,
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
            # 但止步于行高下限；压缩到下限仍溢出则记录 QA 溢出标记。
            # 目录行禁压缩（P1）：单行目录条目不压缩行距、不产生溢出标记。
            if not toc_mode and (
                height > 0
                and np.isfinite(height)
                and (lidx + 1) * size * line_height + vflow_extra > height
            ):
                max_iter = max(int((default_line_height - 0.5) / 0.05), 1)
                iter_count = 0
                while (
                    (lidx + 1) * size * line_height + vflow_extra > height
                    and line_height > line_height_min
                    and iter_count < max_iter
                ):
                    line_height = max(line_height - 0.05, line_height_min)
                    iter_count += 1
                if (lidx + 1) * size * line_height + vflow_extra > height:
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
            para_bottom = y - (lidx + 1) * size * line_height - vflow_extra
            strategy = "noop"  # P4: 本段碰撞求解策略（无碰撞/未启用即 noop）
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
                        # P2（越界防护）：禁止把段落推出页面边界（用户观测的 bbox.y0<0）。
                        # 越界时放弃该位移（不做 clamp），记录 QA；根因由 P1 消除。
                        _cand = y + shift
                        _para_top = _cand
                        _para_bottom = _cand - (lidx + 1) * size * line_height
                        _page_top = self._page_rect.y1 if self._page_rect else None
                        _page_bottom = self._page_rect.y0 if self._page_rect else None
                        _out_of_page = (
                            _page_top is not None
                            and (_para_top > _page_top - size
                                 or (_page_bottom is not None
                                     and _para_bottom < _page_bottom + size))
                        )
                        if _out_of_page:
                            self._overflow_flags.append(
                                {
                                    "page": ltpage.pageid,
                                    "kind": "collision-push-out-of-page",
                                    "lidx": lidx,
                                    "bbox": f"[{x0:.1f},{_para_bottom:.1f},{x1:.1f},{_para_top:.1f}]",
                                    "text": new[:40],
                                    "issue": (
                                        f"vertical shift {shift:.1f} pushes text out "
                                        "of page; shift dropped"
                                    ),
                                }
                            )
                            shift = 0.0
                        else:
                            y += shift
                    if nsize != size:  # S3: 字号缩减生效
                        size = nsize
                    if nx != x0 and not toc_mode:  # S3: 水平缩进生效，平移已生成行；目录行右对齐页码保持原位不动
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

            # === P3/P4: 版面不变量验证 + Source→Target 几何日志（只采集不阻断） ===
            try:
                _tgt_top = y
                _tgt_bottom = y - (lidx + 1) * size * line_height - vflow_extra
                _page_top = self._page_rect.y1 if self._page_rect else None
                _page_bottom = self._page_rect.y0 if self._page_rect else None
                _violation = None
                if _page_top is not None and _tgt_top > _page_top + 0.5:
                    _violation = "TOP_OVERFLOW"
                elif _page_top is not None and _tgt_top > _page_top - size - 0.5:
                    _violation = "TOP_MARGIN"
                elif (_page_bottom is not None
                      and _tgt_bottom < _page_bottom - size - 0.5):
                    _violation = "BOTTOM_OVERFLOW"
                if _violation:
                    _src = pstk[id]
                    _is_formula = bool(sstk[id].strip().startswith("{v"))
                    self._layout_violations.append(
                        {
                            "page": ltpage.pageid,
                            "block_id": id,
                            "block_type": (
                                "TOC" if toc_mode else
                                "FORMULA" if _is_formula else "PARAGRAPH"
                            ),
                            "violation": _violation,
                            "source_bbox": [
                                round(_src.x0, 2), round(_src.y0, 2),
                                round(_src.x1, 2), round(_src.y1, 2),
                            ],
                            "target_bbox": [
                                round(x0, 2), round(_tgt_bottom, 2),
                                round(x1, 2), round(_tgt_top, 2),
                            ],
                            "source_font_size": round(_src.size, 2),
                            "target_font_size": round(size, 2),
                            "layout_solver": strategy,
                            "text": new[:40],
                        }
                    )
            except Exception as _ve:
                log.debug("layout violation check failed: %s", _ve)
            self._gate_records.append(_new_gate_record(x0, y, x1, size, sstk[id], new, toc_mode, lidx, line_height, pstk[id].y0, pstk[id].y1))
            # F3: 接管段先擦除源区域（白底矩形，等价 redact 的物理擦除）——
            # 源区域旧图层（原文文字/公式背景）先被白色覆盖，译文/公式字形
            # 绘制在其上，杜绝「原文 / 公式背景与译文重叠」。
            _src_bbox = _render_src.get(id)
            if _src_bbox is not None and _page_h > 0:
                _sw = float(_src_bbox[2]) - float(_src_bbox[0])
                _sh = float(_src_bbox[3]) - float(_src_bbox[1])
                if _sw >= 1.0 and _sh >= 1.0:
                    ops_list.append(
                        f"q 1 1 1 rg {_safe_float(float(_src_bbox[0]) - 1.0)} "
                        f"{_safe_float(_page_h - float(_src_bbox[3]) - 1.0)} "
                        f"{_safe_float(_sw + 2.0)} {_safe_float(_sh + 2.0)} "
                        f"re f Q ")
            for vals in ops_vals:
                if vals["type"] == OpType.TEXT:
                    ops_list.append(gen_op_txt(vals["font"], vals["size"], vals["x"], vals["dy"] + y - vals["lidx"] * size * line_height - vflow_extra, vals["rtxt"]))
                elif vals["type"] == OpType.LINE:
                    ops_list.append(gen_op_line(vals["x"], vals["dy"] + y - vals["lidx"] * size * line_height - vflow_extra, vals["xlen"], vals["ylen"], vals["linewidth"]))

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
                    flag["page"], flag.get("lidx", "-"), flag.get("bbox", "-"),
                )
                ops_list.append(
                    f"% pdf2zh-qa-overflow page={flag['page']} "
                    f"lidx={flag.get('lidx', '-')} "
                    f"kind={flag.get('kind', '-')} "
                    f"issue={flag.get('issue', '')} "
                    f"bbox={flag.get('bbox', '-')}\n"
                )
        for l in lstk:  # 排版全局线条
            if l.linewidth < 5:  # hack 有的文档会用粗线条当图片背景
                ops_list.append(gen_op_line(l.pts[0][0], l.pts[0][1], l.pts[1][0] - l.pts[0][0], l.pts[1][1] - l.pts[0][1], l.linewidth))
        run_mainline_channels(self, ltpage)  # V8.3/V8.4 side-channels

        ops = f"BT {''.join(ops_list)}ET "
        return ops


class OpType(Enum):
    TEXT = "text"
    LINE = "line"
