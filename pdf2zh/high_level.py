"""Functions that can be used for the most common use-cases for pdf2zh.six"""

import asyncio
import io
import os
import re
import sys
import tempfile
import logging
from asyncio import CancelledError
from pathlib import Path
from string import Template
from typing import Any, BinaryIO, List, Optional, Dict

import numpy as np
import requests
import tqdm

from pdf2zh.converter_docx import convert_to_pdf, is_convertible
from pdfminer.pdfdocument import PDFDocument
from pdfminer.pdfexceptions import PDFValueError
from pdfminer.pdfinterp import PDFResourceManager
from pdfminer.pdfpage import PDFPage
from pdfminer.pdfparser import PDFParser
from pymupdf import Document, Font

from pdf2zh.converter import TranslateConverter
from pdf2zh.doclayout import OnnxModel
from pdf2zh.pdfinterp import PDFPageInterpreterEx
from pdf2zh.font_resolver import FontResolver
from pdf2zh.font_cache import DocumentFontCache

from pdf2zh.config import ConfigManager
from babeldoc.assets.assets import get_font_and_metadata
from pdf2zh.text_metrics import TextMetrics
from pdf2zh.translation_cache import TranslationCache
from pdf2zh.collision_resolver import CollisionResolver
from pdf2zh.layout_graph import LayoutGraph


NOTO_NAME = "noto"

logger = logging.getLogger(__name__)

noto_list = [
    "am",  # Amharic
    "ar",  # Arabic
    "bn",  # Bengali
    "bg",  # Bulgarian
    "chr",  # Cherokee
    "el",  # Greek
    "gu",  # Gujarati
    "iw",  # Hebrew
    "hi",  # Hindi
    "kn",  # Kannada
    "ml",  # Malayalam
    "mr",  # Marathi
    "ru",  # Russian
    "sr",  # Serbian
    "ta",  # Tamil
    "te",  # Telugu
    "th",  # Thai
    "ur",  # Urdu
    "uk",  # Ukrainian
]


def check_files(files: List[str]) -> List[str]:
    files = [
        f for f in files if not f.startswith("http://")
    ]  # exclude online files, http
    files = [
        f for f in files if not f.startswith("https://")
    ]  # exclude online files, https
    missing_files = [file for file in files if not os.path.exists(file)]
    return missing_files


def translate_patch(
    inf: BinaryIO,
    pages: Optional[list[int]] = None,
    vfont: str = "",
    vchar: str = "",
    thread: int = 0,
    doc_zh: Document = None,
    lang_in: str = "",
    lang_out: str = "",
    service: str = "",
    noto_name: str = "",
    noto: Font = None,
    callback: object = None,
    cancellation_event: asyncio.Event = None,
    model: OnnxModel = None,
    envs: Dict = None,
    prompt: Template = None,
    ignore_cache: bool = False,
    skip_subset_fonts: bool = False,
    # 2.0 additions
    text_metrics: dict = None,
    font_resolver: object = None,
    layout_graph: object = None,
    collision_resolver: object = None,
    translation_cache: object = None,
    **kwarg: Any,
) -> None:
    rsrcmgr = PDFResourceManager()
    layout = {}
    device = TranslateConverter(
        rsrcmgr,
        vfont,
        vchar,
        thread,
        layout,
        lang_in,
        lang_out,
        service,
        noto_name,
        noto,
        envs,
        prompt,
        ignore_cache,
        skip_subset_fonts=skip_subset_fonts,
        text_metrics=text_metrics,
        font_resolver=font_resolver,
        layout_graph=layout_graph,
        collision_resolver=collision_resolver,
        translation_cache=translation_cache,
    )

    assert device is not None
    obj_patch = {}
    interpreter = PDFPageInterpreterEx(rsrcmgr, device, obj_patch)
    if pages:
        total_pages = len(pages)
    else:
        total_pages = doc_zh.page_count

    parser = PDFParser(inf)
    doc = PDFDocument(parser)
    with tqdm.tqdm(total=total_pages) as progress:
        for pageno, page in enumerate(PDFPage.create_pages(doc)):
            if cancellation_event and cancellation_event.is_set():
                raise CancelledError("task cancelled")
            if pages and (pageno not in pages):
                continue
            progress.update()
            if callback:
                callback(progress)
            page.pageno = pageno
            pix = doc_zh[page.pageno].get_pixmap()
            image = np.frombuffer(pix.samples, np.uint8).reshape(
                pix.height, pix.width, 3
            )[:, :, ::-1]
            page_layout = model.predict(image, imgsz=int(pix.height / 32) * 32)[0]
            # kdtree 是不可能 kdtree 的，不如直接渲染成图片，用空间换时间
            box = np.ones((pix.height, pix.width))
            h, w = box.shape
            vcls = ["abandon", "figure", "table", "isolate_formula", "formula_caption"]
            for i, d in enumerate(page_layout.boxes):
                if page_layout.names[int(d.cls)] not in vcls:
                    x0, y0, x1, y1 = d.xyxy.squeeze()
                    x0, y0, x1, y1 = (
                        np.clip(int(x0 - 1), 0, w - 1),
                        np.clip(int(h - y1 - 1), 0, h - 1),
                        np.clip(int(x1 + 1), 0, w - 1),
                        np.clip(int(h - y0 + 1), 0, h - 1),
                    )
                    box[y0:y1, x0:x1] = i + 2
            for i, d in enumerate(page_layout.boxes):
                if page_layout.names[int(d.cls)] in vcls:
                    x0, y0, x1, y1 = d.xyxy.squeeze()
                    x0, y0, x1, y1 = (
                        np.clip(int(x0 - 1), 0, w - 1),
                        np.clip(int(h - y1 - 1), 0, h - 1),
                        np.clip(int(x1 + 1), 0, w - 1),
                        np.clip(int(h - y0 + 1), 0, h - 1),
                    )
                    box[y0:y1, x0:x1] = 0
            layout[page.pageno] = box
            # 新建一个 xref 存放新指令流
            page.page_xref = doc_zh.get_new_xref()  # hack 插入页面的新 xref
            doc_zh.update_object(page.page_xref, "<<>>")
            doc_zh.update_stream(page.page_xref, b"")
            doc_zh[page.pageno].set_contents(page.page_xref)
            interpreter.process_page(page)

    device.close()
    return obj_patch


def translate_stream(
    stream: bytes,
    pages: Optional[list[int]] = None,
    lang_in: str = "",
    lang_out: str = "",
    service: str = "",
    thread: int = 0,
    vfont: str = "",
    vchar: str = "",
    callback: object = None,
    cancellation_event: asyncio.Event = None,
    model: OnnxModel = None,
    envs: Dict = None,
    prompt: Template = None,
    skip_subset_fonts: bool = False,
    ignore_cache: bool = False,
    use_text_metrics: bool = True,
    use_translation_cache: bool = True,
    parallel_pages: bool = True,
    parallel_workers: int = 4,

    **kwarg: Any,
):
    font_list = [("tiro", None)]

    font_path = download_remote_fonts(lang_out.lower())
    # Phase 1: Style-aware font resolver
    font_resolver = FontResolver(lang_out)
    noto_name = NOTO_NAME
    noto = Font(noto_name, font_path)
    font_list.append((noto_name, font_path))

    doc_en = Document(stream=stream)
    stream = io.BytesIO()
    doc_en.save(stream)
    doc_zh = Document(stream=stream)
    page_count = doc_zh.page_count
    # Phase 1: Document-level font cache
    font_cache = DocumentFontCache(doc_zh)
    registered_font_name = font_cache.register(font_path)
    # font_list = [("GoNotoKurrent-Regular.ttf", font_path), ("tiro", None)]
    font_id = {}
    for page in doc_zh:
        for font in font_list:
            font_id[font[0]] = page.insert_font(font[0], font[1])
    xreflen = doc_zh.xref_length()
    for xref in range(1, xreflen):
        for label in ["Resources/", ""]:  # 可能是基于 xobj 的 res
            try:  # xref 读写可能出错
                font_res = doc_zh.xref_get_key(xref, f"{label}Font")
                target_key_prefix = f"{label}Font/"
                if font_res[0] == "xref":
                    resource_xref_id = re.search("(\\d+) 0 R", font_res[1]).group(1)
                    xref = int(resource_xref_id)
                    font_res = ("dict", doc_zh.xref_object(xref))
                    target_key_prefix = ""

                if font_res[0] == "dict":
                    for font in font_list:
                        target_key = f"{target_key_prefix}{font[0]}"
                        font_exist = doc_zh.xref_get_key(xref, target_key)
                        if font_exist[0] == "null":
                            doc_zh.xref_set_key(
                                xref,
                                target_key,
                                f"{font_id[font[0]]} 0 R",
                            )
            except Exception:
                pass

    fp = io.BytesIO()

    doc_zh.save(fp)
    fp.seek(0)  # Rewind before passing to translate_patch / PDFParser

    # === 2.0: Create TextMetrics instances (M1) ===
    text_metrics = {}
    if use_text_metrics and font_path and os.path.exists(font_path):
        try:
            from pdf2zh.text_metrics import TextMetrics as _TM
            tm = _TM(font_path)
            text_metrics[noto_name] = tm
            text_metrics[registered_font_name] = tm
        except Exception as e:
            logger.warning("TextMetrics init failed (falling back to legacy width): %s", e)

    # === 2.0: Translation cache (L3) ===
    translation_cache_obj = None
    if use_translation_cache and not ignore_cache:
        try:
            translation_cache_obj = TranslationCache()
        except Exception as e:
            logger.warning("TranslationCache init failed: %s", e)

    # === 2.0: Collision Resolver & Layout Graph ===
    collision_resolver = CollisionResolver()
    layout_graph = LayoutGraph()

    # Ensure 2.0 module references exist in locals
    if 'text_metrics' not in dir():
        text_metrics = {}
    if 'translation_cache_obj' not in dir():
        translation_cache_obj = None

    # === 2.0: Parallel page processing (L2) ===
    if parallel_pages and page_count > 5:
        try:
            obj_patch = _translate_parallel(
                fp, dict(locals()),
                workers=parallel_workers,
            )
        except Exception as parallel_err:
            logger.warning(
                "Parallel page processing failed ({}), falling back to serial: {}".format(
                    type(parallel_err).__name__, str(parallel_err)[:120],
                ),
            )
            obj_patch = translate_patch(fp, **dict(locals()))
    else:
        obj_patch = translate_patch(fp, **dict(locals()))

    for obj_id, ops_new in obj_patch.items():
        # ops_old=doc_en.xref_stream(obj_id)
        # print(obj_id)
        # print(ops_old)
        # print(ops_new.encode())
        doc_zh.update_stream(obj_id, ops_new.encode())

    doc_en.insert_file(doc_zh)
    for id in range(page_count):
        doc_en.move_page(page_count + id, id * 2 + 1)
    def _protect_math_fonts(doc):
        """保护已知数学字体不被 MuPDF subset_fonts 子集化破坏宽度"""
        try:
            xreflen = doc.xref_length()
            for xref in range(1, xreflen):
                try:
                    subtype_res = doc.xref_get_key(xref, "/Subtype")
                    if subtype_res[0] == "name" and "Type3" in str(subtype_res[1]):
                        # Type3 字体跳过子集化
                        doc.xref_set_key(xref, "/Length", doc.xref_get_key(xref, "/Length")[1])
                except Exception:
                    pass
                try:
                    basefont_res = doc.xref_get_key(xref, "/BaseFont")
                    if basefont_res[0] == "name":
                        bf = str(basefont_res[1])
                        math_patterns = [
                            "CM", "CMSY", "CMEX", "CMMI", "EUFM", "MSBM", "MSAM",
                            "STIX", "XITS", "MnSymbol", "rsfs", "txsy", "wasy", "stmary",
                            "Symbol", "MT", "BL", "RM", "EU", "LA", "RS"
                        ]
                        for mp in math_patterns:
                            if mp in bf:
                                doc.xref_set_key(xref, "/Length", doc.xref_get_key(xref, "/Length")[1])
                                break
                except Exception:
                    pass
        except Exception:
            pass

    if not skip_subset_fonts:
        # 在子集化前保护数学字体
        _protect_math_fonts(doc_zh)
        _protect_math_fonts(doc_en)
        try:
            doc_zh.subset_fonts(fallback=False)
        except Exception as subset_err:
            logger.warning("subset_fonts failed for doc_zh: %s", str(subset_err)[:120])
        try:
            doc_en.subset_fonts(fallback=False)
        except Exception as subset_err:
            logger.warning("subset_fonts failed for doc_en: %s", str(subset_err)[:120])
    return (
        doc_zh.write(deflate=True, garbage=3, use_objstms=1),
        doc_en.write(deflate=True, garbage=3, use_objstms=1),
    )


def convert_to_pdfa(input_path, output_path):
    """
    Convert PDF to PDF/A format

    Args:
        input_path: Path to source PDF file
        output_path: Path to save PDF/A file
    """
    from pikepdf import Dictionary, Name, Pdf

    # Open the PDF file
    pdf = Pdf.open(input_path)

    # Add PDF/A conformance metadata
    metadata = {
        "pdfa_part": "2",
        "pdfa_conformance": "B",
        "title": pdf.docinfo.get("/Title", ""),
        "author": pdf.docinfo.get("/Author", ""),
        "creator": "PDF Math Translate",
    }

    with pdf.open_metadata() as meta:
        meta.load_from_docinfo(pdf.docinfo)
        meta["pdfaid:part"] = metadata["pdfa_part"]
        meta["pdfaid:conformance"] = metadata["pdfa_conformance"]

    # Create OutputIntent dictionary
    output_intent = Dictionary(
        {
            "/Type": Name("/OutputIntent"),
            "/S": Name("/GTS_PDFA1"),
            "/OutputConditionIdentifier": "sRGB IEC61966-2.1",
            "/RegistryName": "http://www.color.org",
            "/Info": "sRGB IEC61966-2.1",
        }
    )

    # Add output intent to PDF root
    if "/OutputIntents" not in pdf.Root:
        pdf.Root.OutputIntents = [output_intent]
    else:
        pdf.Root.OutputIntents.append(output_intent)

    # Save as PDF/A
    pdf.save(output_path, linearize=True)
    pdf.close()


def _translate_parallel(
    fp: io.BytesIO,
    locals_dict: dict,
    workers: int = 4,
) -> dict:
    """Parallel page processing for translate_patch (2.0 L2).

    Splits pages across multiple processes. Each process handles a chunk
    of pages independently, and results are merged at the end.

    Args:
        fp: PDF file pointer (pre-saved document)
        locals_dict: Locals from translate_stream
        workers: Number of parallel workers

    Returns:
        Merged obj_patch dictionary
    """
    import concurrent.futures

    from pdf2zh.collision_resolver import CollisionResolver as _CR
    from pdf2zh.layout_graph import LayoutGraph as _LG
    from pdf2zh.font_resolver import FontResolver as _FR

    doc_zh = locals_dict.get("doc_zh")
    if doc_zh is None:
        # Fall back to serial
        return translate_patch(fp, **locals_dict)

    all_pages = list(range(doc_zh.page_count))
    chunk_size = max(1, len(all_pages) // workers)
    chunks = [all_pages[i:i+chunk_size] for i in range(0, len(all_pages), chunk_size)]

    def _process_chunk(chunk_pages):
        """Process a chunk of pages in a separate process."""
        import io as _io
        with open(fp.name, "rb") if hasattr(fp, "name") else _io.BytesIO(fp.getvalue()) as chunk_fp:
            chunk_locals = dict(locals_dict)
            chunk_locals.pop("obj_patch", None)
            chunk_locals["pages"] = chunk_pages
            # Each worker gets its own instances
            chunk_locals["collision_resolver"] = _CR()
            chunk_locals["layout_graph"] = _LG()
            return translate_patch(
                chunk_fp if not hasattr(chunk_fp, "name") else chunk_fp,
                **chunk_locals,
            )

    obj_patch = {}
    with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(_process_chunk, chk) for chk in chunks]
        for f in concurrent.futures.as_completed(futures):
            chunk_result = f.result()
            if chunk_result:
                obj_patch.update(chunk_result)

    return obj_patch



def translate(
    files: list[str],
    output: str = "",
    pages: Optional[list[int]] = None,
    lang_in: str = "",
    lang_out: str = "",
    service: str = "",
    thread: int = 0,
    vfont: str = "",
    vchar: str = "",
    callback: object = None,
    compatible: bool = False,
    cancellation_event: asyncio.Event = None,
    model: OnnxModel = None,
    envs: Dict = None,
    prompt: Template = None,
    skip_subset_fonts: bool = False,
    ignore_cache: bool = False,
    # 2.0 additions
    parallel_pages: bool = False,
    parallel_workers: int = 4,
    use_text_metrics: bool = True,
    use_translation_cache: bool = True,
    **kwarg: Any,
):
    if not files:
        raise PDFValueError("No files to process.")

    missing_files = check_files(files)

    if missing_files:
        print("The following files do not exist:", file=sys.stderr)
        for file in missing_files:
            print(f"  {file}", file=sys.stderr)
        raise PDFValueError("Some files do not exist.")

    result_files = []

    for file in files:
        if type(file) is str and (
            file.startswith("http://") or file.startswith("https://")
        ):
            print("Online files detected, downloading...")
            try:
                r = requests.get(file, allow_redirects=True)
                if r.status_code == 200:
                    with tempfile.NamedTemporaryFile(
                        suffix=".pdf", delete=False
                    ) as tmp_file:
                        print(f"Writing the file: {file}...")
                        tmp_file.write(r.content)
                        file = tmp_file.name
                else:
                    r.raise_for_status()
            except Exception as e:
                raise PDFValueError(
                    f"Errors occur in downloading the PDF file. Please check the link(s).\nError:\n{e}"
                )

        # Convert doc/docx to PDF if needed
        _converted_pdf = None
        if is_convertible(file):
            _converted_pdf = convert_to_pdf(file)
            filename = os.path.splitext(os.path.basename(file))[0]
            file = _converted_pdf
        else:
            filename = os.path.splitext(os.path.basename(file))[0]

        # If the commandline has specified converting to PDF/A format
        # --compatible / -cp
        if compatible:
            with tempfile.NamedTemporaryFile(
                suffix="-pdfa.pdf", delete=False
            ) as tmp_pdfa:
                print(f"Converting {file} to PDF/A format...")
                convert_to_pdfa(file, tmp_pdfa.name)
                doc_raw = open(tmp_pdfa.name, "rb")
                os.unlink(tmp_pdfa.name)
        else:
            doc_raw = open(file, "rb")
        s_raw = doc_raw.read()
        doc_raw.close()

        temp_dir = Path(tempfile.gettempdir())
        file_path = Path(file)
        try:
            if file_path.exists() and file_path.resolve().is_relative_to(
                temp_dir.resolve()
            ):
                file_path.unlink(missing_ok=True)
                logger.debug(f"Cleaned temp file: {file_path}")
        except Exception:
            logger.warning(f"Failed to clean temp file {file_path}", exc_info=True)

        s_mono, s_dual = translate_stream(
            s_raw,
            **locals(),
        )
        file_mono = Path(output) / f"{filename}-mono.pdf"
        file_dual = Path(output) / f"{filename}-dual.pdf"
        doc_mono = open(file_mono, "wb")
        doc_dual = open(file_dual, "wb")
        doc_mono.write(s_mono)
        doc_dual.write(s_dual)
        doc_mono.close()
        doc_dual.close()
        result_files.append((str(file_mono), str(file_dual)))

    return result_files


def download_remote_fonts(lang: str):
    lang = lang.lower()
    LANG_NAME_MAP = {
        **{la: "GoNotoKurrent-Regular.ttf" for la in noto_list},
        **{
            la: f"SourceHanSerif{region}-Regular.ttf"
            for region, langs in {
                "CN": ["zh-cn", "zh-hans", "zh"],
                "TW": ["zh-tw", "zh-hant"],
                "JP": ["ja"],
                "KR": ["ko"],
            }.items()
            for la in langs
        },
    }
    font_name = LANG_NAME_MAP.get(lang, "GoNotoKurrent-Regular.ttf")

    # docker
    font_path = ConfigManager.get("NOTO_FONT_PATH", Path("/app", font_name).as_posix())
    if not Path(font_path).exists():
        font_path, _ = get_font_and_metadata(font_name)
        font_path = font_path.as_posix()

    logger.info(f"use font: {font_path}")

    return font_path
