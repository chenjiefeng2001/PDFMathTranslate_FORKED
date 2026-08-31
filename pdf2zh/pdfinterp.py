import logging
from typing import Any, Dict, Optional, Sequence, Tuple, cast
import numpy as np

from pdfminer import settings
from pdfminer.pdfcolor import PREDEFINED_COLORSPACE, PDFColorSpace
from pdfminer.pdfdevice import PDFDevice
from pdfminer.pdfinterp import (
    PDFPageInterpreter,
    PDFResourceManager,
    PDFContentParser,
    PDFInterpreterError,
    Color,
    PDFStackT,
    LITERAL_FORM,
    LITERAL_IMAGE,
)
from pdfminer.pdffont import PDFFont
from pdfminer.pdfpage import PDFPage
from pdfminer.pdftypes import (
    PDFObjRef,
    dict_value,
    list_value,
    resolve1,
    stream_value,
)
from pdfminer.psexceptions import PSEOF
from pdfminer.psparser import (
    PSKeyword,
    keyword_name,
    literal_name,
)
from pdfminer.utils import (
    MATRIX_IDENTITY,
    Matrix,
    Rect,
    mult_matrix,
    apply_matrix_pt,
)
from pdf2zh.pdfnum import pdf_num

log = logging.getLogger(__name__)


def safe_float(o: Any) -> Optional[float]:
    try:
        return float(o)
    except (TypeError, ValueError):
        return None


class PDFPageInterpreterEx(PDFPageInterpreter):
    """Processor for the content of a PDF page

    Reference: PDF Reference, Appendix A, Operator Summary
    """

    def __init__(
        self, rsrcmgr: PDFResourceManager, device: PDFDevice, obj_patch
    ) -> None:
        self.rsrcmgr = rsrcmgr
        self.device = device
        self.obj_patch = obj_patch

    def dup(self) -> "PDFPageInterpreterEx":
        return self.__class__(self.rsrcmgr, self.device, self.obj_patch)

    def init_resources(self, resources: Dict[object, object]) -> None:
        # 重载设置 fontid 和 descent
        """Prepare the fonts and XObjects listed in the Resource attribute."""
        self.resources = resources
        self.fontmap: Dict[object, PDFFont] = {}
        self.fontid: Dict[PDFFont, object] = {}
        self.xobjmap = {}
        self.csmap: Dict[str, PDFColorSpace] = PREDEFINED_COLORSPACE.copy()
        if not resources:
            return

        def get_colorspace(spec: object) -> Optional[PDFColorSpace]:
            if isinstance(spec, list):
                name = literal_name(spec[0])
            else:
                name = literal_name(spec)
            if name == "ICCBased" and isinstance(spec, list) and len(spec) >= 2:
                return PDFColorSpace(name, stream_value(spec[1])["N"])
            elif name == "DeviceN" and isinstance(spec, list) and len(spec) >= 2:
                return PDFColorSpace(name, len(list_value(spec[1])))
            else:
                return PREDEFINED_COLORSPACE.get(name)

        for k, v in dict_value(resources).items():
            # log.debug("Resource: %r: %r", k, v)
            if k == "Font":
                for fontid, spec in dict_value(v).items():
                    objid = None
                    if isinstance(spec, PDFObjRef):
                        objid = spec.objid
                    spec = dict_value(spec)
                    self.fontmap[fontid] = self.rsrcmgr.get_font(objid, spec)
                    self.fontmap[fontid].descent = 0  # hack fix descent
                    self.fontid[self.fontmap[fontid]] = fontid
            elif k == "ColorSpace":
                for csid, spec in dict_value(v).items():
                    colorspace = get_colorspace(resolve1(spec))
                    if colorspace is not None:
                        self.csmap[csid] = colorspace
            elif k == "ProcSet":
                self.rsrcmgr.get_procset(list_value(v))
            elif k == "XObject":
                for xobjid, xobjstrm in dict_value(v).items():
                    self.xobjmap[xobjid] = xobjstrm

    def do_S(self) -> None:
        # 重载过滤非公式线条
        """Stroke path"""

        def is_black(color: Color) -> bool:
            if isinstance(color, Tuple):
                return sum(color) == 0
            else:
                return color == 0

        if (
            len(self.curpath) == 2
            and self.curpath[0][0] == "m"
            and self.curpath[1][0] == "l"
            and apply_matrix_pt(self.ctm, self.curpath[0][-2:])[1]
            == apply_matrix_pt(self.ctm, self.curpath[1][-2:])[1]
            and is_black(self.graphicstate.scolor)
        ):  # 独立直线，水平，黑色
            # print(apply_matrix_pt(self.ctm,self.curpath[0][-2:]),apply_matrix_pt(self.ctm,self.curpath[1][-2:]),self.graphicstate.scolor)
            self.device.paint_path(self.graphicstate, True, False, False, self.curpath)
            self.curpath = []
            return "n"
        else:
            self.curpath = []

    ############################################################
    # 重载过滤非公式线条（F/B）
    def do_f(self) -> None:
        """Fill path using nonzero winding number rule"""
        # self.device.paint_path(self.graphicstate, False, True, False, self.curpath)
        self.curpath = []

    def do_F(self) -> None:
        """Fill path using nonzero winding number rule (obsolete)"""

    def do_f_a(self) -> None:
        """Fill path using even-odd rule"""
        # self.device.paint_path(self.graphicstate, False, True, True, self.curpath)
        self.curpath = []

    def do_B(self) -> None:
        """Fill and stroke path using nonzero winding number rule"""
        # self.device.paint_path(self.graphicstate, True, True, False, self.curpath)
        self.curpath = []

    def do_B_a(self) -> None:
        """Fill and stroke path using even-odd rule"""
        # self.device.paint_path(self.graphicstate, True, True, True, self.curpath)
        self.curpath = []

    ############################################################
    # 重载返回调用参数（SCN）

    def _stroking_cs(self):
        """当前描边色空间（兼容 pdfminer 新旧 API）。

        pdfminer 20250506 起 ``scs``/``ncs`` 移到 ``graphicstate`` 上，
        旧版本则是解释器级属性。
        """
        cs = getattr(self.graphicstate, "scs", None)
        if cs is None:
            cs = getattr(self, "scs", None)
        return cs

    def _nonstroking_cs(self):
        """当前非描边色空间（兼容 pdfminer 新旧 API）。"""
        cs = getattr(self.graphicstate, "ncs", None)
        if cs is None:
            cs = getattr(self, "ncs", None)
        return cs

    def do_SCN(self) -> None:
        """Set color for stroking operations."""
        scs = self._stroking_cs()
        if scs:
            n = scs.ncomponents
        else:
            if settings.STRICT:
                raise PDFInterpreterError("No colorspace specified!")
            n = 1
        args = self.pop(n)
        self.graphicstate.scolor = cast(Color, args)
        return args

    def do_scn(self) -> None:
        """Set color for nonstroking operations"""
        ncs = self._nonstroking_cs()
        if ncs:
            n = ncs.ncomponents
        else:
            if settings.STRICT:
                raise PDFInterpreterError("No colorspace specified!")
            n = 1
        args = self.pop(n)
        self.graphicstate.ncolor = cast(Color, args)
        return args

    def do_SC(self) -> None:
        """Set color for stroking operations"""
        return self.do_SCN()

    def do_sc(self) -> None:
        """Set color for nonstroking operations"""
        return self.do_scn()

    def do_Do(self, xobjid_arg: PDFStackT) -> None:
        # 重载设置 xobj 的 obj_patch
        """Invoke named XObject"""
        xobjid = literal_name(xobjid_arg)
        try:
            xobj = stream_value(self.xobjmap[xobjid])
        except KeyError:
            if settings.STRICT:
                raise PDFInterpreterError("Undefined xobject id: %r" % xobjid)
            return
        # log.debug("Processing xobj: %r", xobj)
        subtype = xobj.get("Subtype")
        if subtype is LITERAL_FORM and "BBox" in xobj:
            interpreter = self.dup()
            bbox = cast(Rect, list_value(xobj["BBox"]))
            matrix = cast(Matrix, list_value(xobj.get("Matrix", MATRIX_IDENTITY)))
            # According to PDF reference 1.7 section 4.9.1, XObjects in
            # earlier PDFs (prior to v1.2) use the page's Resources entry
            # instead of having their own Resources entry.
            xobjres = xobj.get("Resources")
            if xobjres:
                resources = dict_value(xobjres)
            else:
                resources = self.resources.copy()
            self.device.begin_figure(xobjid, bbox, matrix)
            ctm = mult_matrix(matrix, self.ctm)
            ops_base = (
                interpreter.render_contents(
                    resources,
                    [xobj],
                    ctm=ctm,
                )
                or ""
            )  # 空内容流返回 None，避免生成非法 PDF 指令串
            # 同步子解释器的色空间（兼容 pdfminer 新旧 API：
            # 20250506 起 ncs/scs 位于 graphicstate，旧版为解释器属性）
            sub_ncs = getattr(interpreter.graphicstate, "ncs", None) or getattr(
                interpreter, "ncs", None
            )
            sub_scs = getattr(interpreter.graphicstate, "scs", None) or getattr(
                interpreter, "scs", None
            )
            if hasattr(self.graphicstate, "ncs"):
                if sub_ncs is not None:
                    self.graphicstate.ncs = sub_ncs
                if sub_scs is not None:
                    self.graphicstate.scs = sub_scs
            else:
                if sub_ncs is not None:
                    self.ncs = sub_ncs
                if sub_scs is not None:
                    self.scs = sub_scs
            try:  # 有的时候 form 字体加不上这里会烂掉
                self.device.fontid = interpreter.fontid
                self.device.fontmap = interpreter.fontmap
                ops_new = self.device.end_figure(xobjid)
                ctm_inv = np.linalg.inv(np.array(ctm[:4]).reshape(2, 2))
                np_version = np.__version__
                if np_version.split(".")[0] >= "2":
                    pos_inv = -np.asmatrix(ctm[4:]) * ctm_inv
                else:
                    pos_inv = -np.mat(ctm[4:]) * ctm_inv
                # 7H-2B: 用 pdf_num 序列化 cm 矩阵，杜绝科学计数法 token
                # （避免 MuPDF tokenizer 在 'e' 处分字，如 -9.000000001435637e-05）。
                a, b, c, d = ctm_inv.reshape(4).tolist()
                e, f = pos_inv.tolist()[0]
                _cm = " ".join(pdf_num(x) for x in (a, b, c, d, e, f))
                self.obj_patch[self.xobjmap[xobjid].objid] = (
                    f"q {ops_base}Q {_cm} cm {ops_new}"
                )
            except Exception as e:
                log.warning(
                    "XObject %s form processing failed (%s: %s). "
                    "Restoring state and clearing XObject stream to prevent overlay.",
                    xobjid,
                    type(e).__name__,
                    str(e)[:120],
                )
                try:
                    if self.device._stack:
                        self.device.cur_item = self.device._stack.pop()
                except Exception as restore_err:
                    log.debug("Failed to restore cur_item: %s", restore_err)
                xobj_objid = self.xobjmap[xobjid].objid
                if xobj_objid not in self.obj_patch:
                    self.obj_patch[xobj_objid] = ""
        elif subtype is LITERAL_IMAGE and "Width" in xobj and "Height" in xobj:
            self.device.begin_figure(xobjid, (0, 0, 1, 1), MATRIX_IDENTITY)
            self.device.render_image(xobjid, xobj)
            self.device.end_figure(xobjid)
        else:
            # unsupported xobject type.
            pass

    def process_page(self, page: PDFPage) -> None:
        # 重载设置 page 的 obj_patch
        # log.debug("Processing page: %r", page)
        # print(page.mediabox,page.cropbox)
        # (x0, y0, x1, y1) = page.mediabox
        x0, y0, x1, y1 = page.cropbox
        if page.rotate == 90:
            ctm = (0, -1, 1, 0, -y0, x1)
        elif page.rotate == 180:
            ctm = (-1, 0, 0, -1, x1, y1)
        elif page.rotate == 270:
            ctm = (0, 1, -1, 0, y1, -x0)
        else:
            ctm = (1, 0, 0, 1, -x0, -y0)
        self.device.begin_page(page, ctm)
        ops_base = (
            self.render_contents(page.resources, page.contents, ctm=ctm) or ""
        )  # 空内容流安全
        self.device.fontid = self.fontid
        self.device.fontmap = self.fontmap
        ops_new = self.device.end_page(page)
        # 上面渲染的时候会根据 cropbox 减掉页面偏移得到真实坐标，这里输出的时候需要用 cm 把页面偏移加回来
        xref_key = getattr(page, "page_xref", getattr(page, "pageid", None))
        if xref_key is None:
            raise AttributeError("PDFPage has neither page_xref nor pageid")
        # 7H-2B: cm 偏移用 pdf_num 序列化（无科学计数法）。
        self.obj_patch[xref_key] = (
            f"q {ops_base}Q 1 0 0 1 {pdf_num(x0)} {pdf_num(y0)} cm {ops_new}"  # ops_base 里可能有图，需要让 ops_new 里的文字覆盖在上面，使用 q/Q 重置位置矩阵
        )
        for obj in page.contents:
            self.obj_patch[obj.objid] = ""

    def render_contents(
        self,
        resources: Dict[object, object],
        streams: Sequence[object],
        ctm: Matrix = MATRIX_IDENTITY,
    ) -> None:
        # 重载返回指令流
        """Render the content streams.

        This method may be called recursively.
        """
        # log.debug(
        #     "render_contents: resources=%r, streams=%r, ctm=%r",
        #     resources,
        #     streams,
        #     ctm,
        # )
        self.init_resources(resources)
        self.init_state(ctm)
        return self.execute(list_value(streams))

    def execute(self, streams: Sequence[object]) -> None:
        # 重载返回指令流
        ops = ""
        try:
            parser = PDFContentParser(streams)
        except PSEOF:
            # empty page
            return
        while True:
            try:
                _, obj = parser.nextobject()
            except PSEOF:
                break
            if isinstance(obj, PSKeyword):
                name = keyword_name(obj)
                method = "do_%s" % name.replace("*", "_a").replace('"', "_w").replace(
                    "'",
                    "_q",
                )
                if hasattr(self, method):
                    func = getattr(self, method)
                    nargs = func.__code__.co_argcount - 1
                    if nargs:
                        args = self.pop(nargs)
                        # log.debug("exec: %s %r", name, args)
                        if len(args) == nargs:
                            func(*args)
                            if not (
                                name[0] == "T"
                                or name in ['"', "'", "EI", "MP", "DP", "BMC", "BDC"]
                            ):  # 过滤 T 系列文字指令，因为 EI 的参数是 obj 所以也需要过滤（只在少数文档中画横线时使用），过滤 marked 系列指令
                                p = " ".join(
                                    [
                                        (
                                            f"{x:f}"
                                            if isinstance(x, float)
                                            else str(x).replace("'", "")
                                        )
                                        for x in args
                                    ]
                                )
                                ops += f"{p} {name} "
                    else:
                        # log.debug("exec: %s", name)
                        targs = func()
                        if targs is None:
                            targs = []
                        if not (name[0] == "T" or name in ["BI", "ID", "EMC"]):
                            p = " ".join(
                                [
                                    (
                                        f"{x:f}"
                                        if isinstance(x, float)
                                        else str(x).replace("'", "")
                                    )
                                    for x in targs
                                ]
                            )
                            ops += f"{p} {name} "
                elif settings.STRICT:
                    error_msg = "Unknown operator: %r" % name
                    raise PDFInterpreterError(error_msg)
            else:
                self.push(obj)
        # print('REV DATA',ops)
        return ops
