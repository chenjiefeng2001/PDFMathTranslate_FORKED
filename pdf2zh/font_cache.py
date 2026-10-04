"""
Document-level font cache for pdf2zh 2.0.

Prevents per-page font re-embedding by caching font resources
at the Document level. Each font style is registered once and
reused across all pages, reducing PDF size significantly.
"""

import logging
from typing import Dict, Optional

from pymupdf import Document, Font

logger = logging.getLogger(__name__)


class DocumentFontCache:
    """Document-level cache for font resources.

    Ensures each font style is embedded only once per document,
    rather than once per page. Fonts are identified by their
    filename/base name for cache key purposes.

    Usage:
        cache = DocumentFontCache(doc)
        font_name = cache.register(doc, font_path)
    """

    def __init__(self, doc: Document):
        self.doc = doc
        # font_path -> (font_name, pymupdf.Font, font_path)
        self._cache: Dict[str, tuple] = {}
        self._counter = 0

    def register(self, font_path: str) -> str:
        """Register a font and return its document-level font name.

        If the font was already registered, returns the existing name.
        Otherwise creates a new Font object and inserts it.

        Args:
            font_path: Absolute path to the font file

        Returns:
            Short font name (e.g. 'f0') for use in PDF operators
        """
        if font_path in self._cache:
            font_name, _, _ = self._cache[font_path]
            return font_name

        font_name = f"f{self._counter}"
        self._counter += 1
        noto = Font(font_name, font_path)
        self._cache[font_path] = (font_name, noto, font_path)
        logger.debug("Registered font '%s' -> '%s'", font_path, font_name)
        return font_name

    def get_font(self, font_path: str) -> Optional[Font]:
        """Get the pymupdf Font object for a registered font.

        Args:
            font_path: Font file path used in register()

        Returns:
            pymupdf Font object, or None if not registered
        """
        if font_path in self._cache:
            _, noto, _ = self._cache[font_path]
            return noto
        return None

    def get_name(self, font_path: str) -> Optional[str]:
        """Get the short name for a registered font.

        Args:
            font_path: Font file path used in register()

        Returns:
            Short font name, or None if not registered
        """
        if font_path in self._cache:
            font_name, _, _ = self._cache[font_path]
            return font_name
        return None

    def get_registered_fonts(self) -> list:
        """Get all (font_name, Font, font_path) tuples."""
        return list(self._cache.values())

    @property
    def count(self) -> int:
        return len(self._cache)


#: 需要留意的数学/符号字体名片段。刻意从宽：这里只用来**报告**，不做任何
#: 写操作，写宽了最坏结果是日志里多列几个字体名。
MATH_FONT_PATTERNS = (
    "CM",
    "CMSY",
    "CMEX",
    "CMMI",
    "EUFM",
    "MSBM",
    "MSAM",
    "STIX",
    "XITS",
    "MnSymbol",
    "rsfs",
    "txsy",
    "wasy",
    "stmary",
    "Symbol",
    "MT",
    "BL",
    "RM",
    "EU",
    "LA",
    "RS",
)


def find_math_fonts(doc: Document) -> list:
    """列出文档里疑似数学/符号字体（按 ``/BaseFont`` 名匹配）。

    这是**只读**的诊断：``pymupdf.Document.subset_fonts()`` 没有按字体排除的
    接口，所以本函数只负责把「这份文档会被子集化的数学字体」列出来，让调用方
    记日志、并把 ``--skip-subset-fonts`` 作为逃生舱提示给用户。

    历史包袱：这里原先是 ``_protect_math_fonts``，声称「通过
    ``xref_set_key(xref, "/Length", xref_get_key(xref, "/Length")[1])`` 保护
    数学字体不被子集化」。那行代码有两个问题，且都已修掉：

    1. **会写出损坏的 PDF。** 流字典本来就有 ``/Length 58``，再写一次就成了
       重复键；更糟的是 PyMuPDF 1.28.2 上带前导斜杠读键返回 ``('null',
       'null')``，于是实际写入的是 ``/Length 58  / << /Length null >> >>``
       —— 重复键 + 无键子字典，阅读器报「文件已损坏」。
    2. **根本没生效过。** 同一处用 ``"/BaseFont"``/``"/Length"``（带前导斜杠）
       读键，在 1.28.2 上一律返回 null，所以整个函数是空转，「保护」是假的。

    读取时**不带前导斜杠**才是 PyMuPDF 的正确用法（``xref_get_keys`` 返回的键
    就没有斜杠）。
    """
    found = []
    try:
        xreflen = doc.xref_length()
    except Exception:  # noqa: BLE001 -- 诊断路径，坏 xref 直接放弃
        return found
    for xref in range(1, xreflen):
        try:
            basefont = doc.xref_get_key(xref, "BaseFont")
        except Exception:  # noqa: BLE001
            continue
        if basefont[0] != "name":
            continue
        name = str(basefont[1])
        if any(pattern in name for pattern in MATH_FONT_PATTERNS):
            found.append((xref, name))
    return found


def _page_font_dict_xref(doc: Document, page_xref: int):
    """把某页 ``/Resources/Font`` 解析成一个**间接对象**并返回其 xref。

    PDF 允许 ``/Font`` 是资源字典里的内联字典，而 ``xref_set_key`` 只能对
    间接对象写单个键（PyMuPDF 1.28.2 明确拒绝 ``"Resources/Font/noto"``
    这种多级路径：``JM_set_object_value: path to 'noto' has indirects``）。
    因此这里在需要时把内联字典/缺失项提升为独立对象，再让 ``/Font`` 指向它。
    """
    import re as _re

    res = doc.xref_get_key(page_xref, "Resources")
    if res[0] == "xref":
        m = _re.search(r"(\d+) 0 R", res[1])
        if not m:
            return None
        res_xref = int(m.group(1))
    elif res[0] == "dict":
        res_xref = doc.get_new_xref()
        doc.update_object(res_xref, res[1])
        doc.xref_set_key(page_xref, "Resources", f"{res_xref} 0 R")
    else:
        # 没有 /Resources，或被写成 null：新建一个空资源字典再挂上去。
        res_xref = doc.get_new_xref()
        doc.update_object(res_xref, "<<>>")
        doc.xref_set_key(page_xref, "Resources", f"{res_xref} 0 R")

    font_key = doc.xref_get_key(res_xref, "Font")
    if font_key[0] == "xref":
        m = _re.search(r"(\d+) 0 R", font_key[1])
        return int(m.group(1)) if m else None
    body = font_key[1] if font_key[0] == "dict" else "<<>>"
    new_xref = doc.get_new_xref()
    doc.update_object(new_xref, body)
    doc.xref_set_key(res_xref, "Font", f"{new_xref} 0 R")
    return new_xref


def broadcast_page_font(doc: Document, page_xref: int, font_id: int, name: str) -> bool:
    """把字体 ``name`` → ``font_id`` 登记进该页的 ``/Resources/Font``。

    成功返回 True。任何一步失败返回 False，由调用方退回受支持的
    ``page.insert_font``（更慢但不会写出畸形对象）。
    """
    try:
        font_xref = _page_font_dict_xref(doc, page_xref)
        if font_xref is None:
            return False
        if doc.xref_get_key(font_xref, name)[0] == "null":
            doc.xref_set_key(font_xref, name, f"{int(font_id)} 0 R")
        return True
    except Exception:  # noqa: BLE001 -- 兜底路径，交给调用方用 insert_font
        return False
