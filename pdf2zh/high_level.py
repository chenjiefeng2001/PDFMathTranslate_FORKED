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
from pdf2zh.translator import build_translator
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
    # 并行模式：由调用方预创建每页的新内容流 xref（保证主进程与 worker 进程编号一致）
    page_xref_map: dict = None,
    apply_page_xrefs: bool = True,
# V8.3/V8.4: 主链路 IR 产出 + 写回前重排版门控
    emit_ir: bool = False,
    relayout_gate: object = None,
    v3_output: dict = None,
# V8.5: 采集逐篇段落源/目标几何（link_remap 桥接数据）
    link_remap: bool = False,
    # V9.0: Processor 层语义通道（RAW/SEMANTIC + TOC 结构化记录，side-channel）
    processor_channels: bool = True,
    # V8.3 后半程/阶段六八: 渲染接管计划 + 翻译 QA + 双轨聚类接管（side-channel）
    render_takeover: bool = False,
    translation_qa: bool = False,
    geometry_cluster: bool = False,

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
        emit_ir=emit_ir,
        relayout_gate=relayout_gate,
    )

    # V8.5: 超链接重定位桥 —— 让 converter side-channel 采集逐段落源/目标几何
    # （translate_stream 经 **dict(locals()) 传入 relink_links，worker 串行回退也一致）
    device.link_remap = bool(kwarg.get("relink_links", link_remap))
    device.gate_records_by_page = {}
    # V9.0: Processor 语义通道开关（converter 无 __init__ 参数，动态接线）
    device.processor_channels = bool(processor_channels)
    # V8.3 后半程/阶段六八: side-channel 开关 + 采集容器（动态接线）
    device.render_takeover = bool(render_takeover)
    device.translation_qa = bool(translation_qa)
    device.geometry_cluster = bool(geometry_cluster)
    device.geometry_adoptions = {}
    device.render_plans = {}
    device.translation_qa_records = {}

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
            if page_xref_map and pageno in page_xref_map:
                # 并行模式：page_xref 由调用方（主进程）预创建，worker 与主进程编号一致；
                # worker 进程中该对象不存在，故跳过 update_object/update_stream/set_contents
                page.page_xref = page_xref_map[pageno]
                if apply_page_xrefs:
                    doc_zh[page.pageno].set_contents(page.page_xref)
            else:
                page.page_xref = doc_zh.get_new_xref()  # hack 插入页面的新 xref
                doc_zh.update_object(page.page_xref, "<<>>")
                doc_zh.update_stream(page.page_xref, b"")
                doc_zh[page.pageno].set_contents(page.page_xref)
            interpreter.process_page(page)

    device.close()
    # V8.3–V9.0: 主链路 side-channel 数据回传（IR 快照 + 门控裁决 +
    # 超链接重定位桥 + Processor 语义通道）
    if v3_output is not None:
        v3_output["ir_snapshots"] = dict(getattr(device, "ir_snapshots", {}))
        v3_output["gate_verdicts"] = dict(getattr(device, "gate_verdicts", {}))
        v3_output["link_records"] = dict(getattr(device, "gate_records_by_page", {}))
        v3_output["processor_reports"] = dict(getattr(device, "processor_reports", {}))
        v3_output["toc_ir_records"] = dict(getattr(device, "toc_ir_records", {}))
        v3_output["render_plans"] = dict(getattr(device, "render_plans", {}))
        v3_output["translation_qa_records"] = dict(getattr(device, "translation_qa_records", {}))
        v3_output["geometry_adoptions"] = dict(getattr(device, "geometry_adoptions", {}))
    return obj_patch


# ---------------------------------------------------------------------------
# Marker: translate_stream start
def _relink_translated_doc(doc_zh, v3_output: dict = None) -> dict:
    """V8.5: 用 converter side-channel 的段落源→目标几何重定位译文页超链接。

    文档级守护：v3_output 缺失 / 无采集数据 / PyMuPDF 不可用时返回零统计，
    绝不抛异常拨乱主链路。跳过错位页（含旋转页）与无锚点匹配的链接。
    """
    empty = {"pages": 0, "relinked": 0, "skipped": 0}
    if not v3_output:
        return empty
    records = (v3_output or {}).get("link_records") or {}
    if not records:
        return empty
    try:
        from pdf2zh.v3.link_remap import remap_document_links
    except Exception as e:
        logger.warning("link_remap import failed: %s", str(e)[:120])
        return empty
    try:
        # v1.6 P1：真实翻译产物回归 —— gate 记录为 pdfminer 坐标系（y 向上），
        # fitz link /Rect 为左上原点（y 向下），需按页高翻转才能命中锚点。
        heights = {}
        for pno in range(doc_zh.page_count):
            try:
                heights[pno] = float(doc_zh[pno].rect.height)
            except Exception:
                continue
        return remap_document_links(doc_zh, records, page_offset=0,
                                    y_flip=True, page_heights=heights)
    except Exception as e:
        logger.warning("link_remap failed at doc level: %s", str(e)[:160])
        return empty


def _collect_preservation_side_channel(
    doc_zh,
    v3_output: dict = None,
    image_engine: bool = False,
    content_preservation: bool = False,
    image_render: bool = False,
) -> dict:
    """V8.6: 图片翻译决策 + 内容保护决策的 side-channel 采集。

    只把决策回填到 ``v3_output["preservation_records"]``，**不修改任何页面
    内容/像素/链接**，也不影响 legacy 主链路渲染。所有异常仅在 debug 日志
    可见（side-channel 纪律，与 V8.3/V8.4 一致）。
    ``image_render=True`` 时额外对每页栅格跑一遍完整图片渲染管线
    （OCR→决策→翻译→渲染），把摘要写入 ``v3_output["image_render_records"]``。
    """
    empty = {"pages": 0, "objects": 0, "translated": 0, "preserved": 0,
             "overlay": 0}
    if not getattr(doc_zh, "page_count", None):
        return empty
    if not (image_engine or content_preservation):
        return empty
    try:
        from pdf2zh.v3.image_engine import (
            TranslationDecisionEngine, analyze_pdf_images,
        )
        from pdf2zh.v3.content_preservation import ContentPreservationEngine
    except Exception as e:
        logger.debug("preservation engine import failed: %s", str(e)[:120])
        return empty
    try:
        engine = TranslationDecisionEngine()
        image_objs = analyze_pdf_images(
            doc_zh, engine=engine,
            page_range=list(range(doc_zh.page_count)) if doc_zh.page_count < 2000 else None,
        )
        pres_engine = ContentPreservationEngine(engine=engine)
        rec = {}
        for page_no, objs in image_objs.items():
            page_rec = []
            for obj in objs:
                dec = pres_engine.decide_image(obj)
                page_rec.append(dec.to_dict())
            rec[str(page_no)] = page_rec

        stats = {"pages": len(image_objs), "objects": sum(len(v) for v in image_objs.values()),
                 "translated": 0, "preserved": 0, "overlay": 0}
        for page_no, objs in image_objs.items():
            for obj in objs:
                if obj.decision and obj.decision.render_mode.value == "overlay":
                    stats["overlay"] += 1
                elif obj.decision and obj.decision.translate:
                    stats["translated"] += 1
                else:
                    stats["preserved"] += 1
        if v3_output is not None:
            v3_output["preservation_records"] = rec
            v3_output["preservation_stats"] = stats
        if image_render:
            render_records = _render_page_previews(doc_zh)
            if v3_output is not None:
                v3_output["image_render_records"] = render_records
        return stats
    except Exception as e:
        logger.debug("preservation collection failed: %s", str(e)[:160])
        return empty


def _render_page_previews(doc_zh) -> dict:
    """V8.6 P1: 对每页栅格跑一遍图片渲染管线，返回逐页渲染摘要。

    只产摘要（渲染模式/翻译区域数/字节数），不写回 PDF —— 用于验证
    OCR→决策→翻译→渲染后端在真实页面上可跑通。失败页跳过（side-channel）。
    """
    import numpy as _np
    out: dict = {}
    try:
        from pdf2zh.v3.image_pipeline import translate_image_pixels
        for pno in range(doc_zh.page_count):
            try:
                page = doc_zh[pno]
                pix = page.get_pixmap(matrix=None)
                px = _np.frombuffer(pix.samples, dtype=_np.uint8).reshape(
                    pix.height, pix.width, pix.n)
                rgb = px[..., :3] if pix.n >= 3 else px
                out_bytes, summ = translate_image_pixels(
                    rgb, object_id=f"p{pno}_render", page_num=pno)
                out[str(pno)] = {
                    "mode": summ.render_mode,
                    "translated": summ.regions_translated,
                    "total": summ.regions_total,
                    "bytes": len(out_bytes),
                }
            except Exception as e:  # noqa: BLE001
                logger.debug("page preview render failed p%s: %s", pno,
                             str(e)[:120])
    except Exception as e:  # noqa: BLE001
        logger.debug("image render channel failed: %s", str(e)[:120])
    return out


def _apply_bookmarks(
    doc_zh,
    doc_en,
    stream_bytes: bytes,
    service: str,
    lang_in: str,
    lang_out: str,
    envs: Dict,
    prompt: Template,
    ignore_cache: bool = False,
) -> None:
    """翻译并重建 PDF 书签（/Outlines）到译文文档。

    - 读取源 PDF 的 outline 树（fitz get_toc）
    - 用与正文一致的翻译器翻译书签标题
    - mono 文档（doc_zh，纯译文）：书签页码保持不变
    - dual 文档（doc_en，双语交错 en0,zh0,en1,zh1,...）：
      原页码 n 映射为 2n-1（指向英文页），保持原书签页码语义
    - 任一步失败仅记 warning，不回退整个翻译任务
    """
    try:
        import fitz
        reader = fitz.open(stream=stream_bytes, filetype="pdf")
        toc = reader.get_toc()
        reader.close()
    except Exception as e:
        logger.warning("bookmarks: failed to read outline: %s", str(e)[:120])
        return
    if not toc:
        return
    translator = None
    try:
        translator = build_translator(service, lang_in, lang_out, envs, prompt, ignore_cache)
    except Exception as e:
        logger.warning("bookmarks: translator init failed: %s", str(e)[:120])
        return
    if translator is None:
        return
    new_toc = []
    for item in toc:
        lvl = item[0] if len(item) > 0 else 1
        title = (item[1] if len(item) > 1 else "").strip()
        page = item[2] if len(item) > 2 else 1
        if not title:
            continue
        try:
            translated = translator.translate(title)
        except Exception as e:
            logger.warning("bookmarks: title translate failed (%r): %s", title[:30], str(e)[:120])
            translated = title
        t = (translated or "").strip() or title
        new_toc.append([lvl, t, page])
    if not new_toc:
        return
    try:
        doc_zh.set_toc(new_toc)
    except Exception as e:
        logger.warning("bookmarks: set_toc mono failed: %s", str(e)[:120])
    dual_toc = []
    for lvl, t, page in new_toc:
        dual_toc.append([lvl, t, max(1, 2 * int(page) - 1)])
    try:
        doc_en.set_toc(dual_toc)
    except Exception as e:
        logger.warning("bookmarks: set_toc dual failed: %s", str(e)[:120])


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
    # V8.3/V8.4: 主链路 IR 产出 + 写回前重排版门控（经 **dict(locals()) 透传）
    emit_ir: bool = False,
    relayout_gate: object = None,
    v3_output: dict = None,
    # V8.5: 翻译页面上超链接 /Rect 重定位（默认开；无桥接数据时安全跳过）
    relink_links: bool = True,
    # V8.6: 图片翻译决策层（side-channel，仅回传决策不入主链路渲染；默认关）
    image_engine: bool = False,
    content_preservation: bool = False,
    emit_preservation: bool = True,
    # V9.0: Processor 层语义通道（RAW/SEMANTIC + TOC 结构化记录；默认开）
    processor_channels: bool = True,
    # V8.3 后半程/阶段六八: 渲染接管计划 + 翻译 QA + 双轨聚类接管（side-channel）
    render_takeover: bool = False,
    translation_qa: bool = False,
    geometry_cluster: bool = False,
    image_render: bool = False,

    **kwarg: Any,
):
    # 归一化翻译并发线程数：CLI 默认 4，但 API/编程方式调用时 thread 可能为 0/None，
    # TranslateConverter 内部 ThreadPoolExecutor(max_workers=0) 会抛 ValueError，
    # 导致整页翻译失败（并行路径中表现为 worker 崩溃、串行路径中整份 PDF 空白）。
    thread = thread if thread and thread > 0 else 4

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
    logger.info("translate_stream: loaded %d pages, starting patch phase...", page_count)
    import sys as _sys_init; _sys_init.stdout.flush()
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
    page_xref_map = None
    if parallel_pages and page_count > 5:
        # 主进程预创建所有页面的新内容流 xref：worker 进程从共享 fp_bytes 各自打开文档后，
        # get_new_xref() 会从相同的起始编号分配，导致跨 worker 编号冲突，且这些对象只存在于
        # worker 进程内；主进程 update_stream(page_xref) 会因此报 bad xref / object is no PDF dict。
        try:
            page_xref_map = {}
            for pageno in range(page_count):
                xref = doc_zh.get_new_xref()
                doc_zh.update_object(xref, "<<>>")
                doc_zh.update_stream(xref, b"")
                page_xref_map[pageno] = xref
        except Exception as px_err:
            logger.warning("Failed to pre-create page xrefs (%s), falling back to serial", str(px_err)[:120])
            page_xref_map = None
            obj_patch = translate_patch(fp, **dict(locals()))
        else:
            try:
                obj_patch = _translate_parallel(
                    fp, dict(locals()),
                    workers=parallel_workers,
                    page_xref_map=page_xref_map,
                )
            except (Exception, SystemExit, KeyboardInterrupt) as parallel_err:
                logger.warning(
                    "Parallel page processing failed (%s), falling back to serial: %s",
                    type(parallel_err).__name__, str(parallel_err)[:120],
                )
                # Serial fallback: use locals directly (all objects available in current process)
                obj_patch = translate_patch(fp, **dict(locals()))
    else:
        obj_patch = translate_patch(fp, **dict(locals()))

    total_objs = len(obj_patch)
    for idx, (obj_id, ops_new) in enumerate(obj_patch.items()):
        try:
            # Validate that the obj_id references a dict/stream before updating
            xref_type = doc_zh.xref_object(obj_id, compressed=True)
            if not xref_type.startswith('<<'):
                logger.warning(
                    'Skipping obj_id %s: not a PDF dict (xref_object starts with %r)',
                    obj_id, xref_type[:40],
                )
                continue
            doc_zh.update_stream(obj_id, ops_new.encode())
        except ValueError as ve:
            logger.warning(
                'Skipping obj_id %s (ValueError: %s) — common for non-stream objects',
                obj_id, str(ve)[:80],
            )
        except Exception as stream_err:
            logger.warning(
                'Skipping obj_id %s update_stream error: %s',
                obj_id, str(stream_err)[:120],
            )
        if idx % 5 == 0 or idx == total_objs - 1:
            logger.info("translate_stream: updated stream %d/%d (%.0f%%)", idx + 1, total_objs, (idx + 1) / total_objs * 100)

    # 并行模式下 worker 进程不会修改主进程 doc_zh 的页面 /Contents，
    # 这里统一将每个页面指向其新的（已写入译文指令流的）内容流对象。
    if page_xref_map:
        for _px_pageno, _px_xref in page_xref_map.items():
            try:
                doc_zh[_px_pageno].set_contents(_px_xref)
            except Exception as se:
                logger.warning(
                    "set_contents failed for page %s (xref %s): %s",
                    _px_pageno, _px_xref, str(se)[:80],
                )

    # === V8.5: 超链接重定位（必须发生在 insert_file 合并之前，译副本才能继承修正 rect） ===
    # 用 converter side-channel 采集的段落源→目标几何，把译文页面上继承自原文的
    # link /Rect 重新投影到译文实际渲染位置（mono 原文页的锚点保持原样不动）。
    if relink_links:
        try:
            link_stats = _relink_translated_doc(doc_zh, v3_output)
            if any(link_stats["relinked"] for _ in [0]):
                logger.info(
                    "translate_stream: relinked %d links across %d pages",
                    link_stats["relinked"], link_stats["pages"],
                )
        except Exception as relink_err:
            logger.warning("translate_stream: link relink skipped: %s", str(relink_err)[:160])

    # === V8.6: 图片翻译 + 内容保护决策的 side-channel（仅采集回传，不改渲染） ===
    if emit_preservation:
        try:
            pres_stats = _collect_preservation_side_channel(
                doc_zh, v3_output,
                image_engine=image_engine,
                content_preservation=content_preservation,
                image_render=image_render,
            )
            if pres_stats and pres_stats["objects"]:
                logger.info(
                    "translate_stream: preservation decided %d image objects "
                    "(translate=%d preserve=%d overlay=%d)",
                    pres_stats["objects"], pres_stats["translated"],
                    pres_stats["preserved"], pres_stats["overlay"],
                )
        except Exception as pres_err:
            logger.warning("translate_stream: preservation skipped: %s", str(pres_err)[:160])

    logger.info("=" * 60)
    logger.info("translate_stream: MERGING %d pages (this may take a while for large PDFs)...", page_count)
    logger.info("=" * 60)
    import time as _merge_time
    import sys as _sys
    try:
        _merge_start = _merge_time.time()
        logger.info("translate_stream: calling doc_en.insert_file(doc_zh)...")
        _sys.stdout.flush()
        _sys.stderr.flush()
        doc_en.insert_file(doc_zh)
        _insert_elapsed = _merge_time.time() - _merge_start
        logger.info("translate_stream: insert_file OK (%.1fs), reordering %d pages...", _insert_elapsed, page_count)
        _sys.stdout.flush()
        for id in range(page_count):
            doc_en.move_page(page_count + id, id * 2 + 1)
            if id % 5 == 0 or id == page_count - 1:
                logger.info("translate_stream: moved page %d/%d (%.1f%% done)", id + 1, page_count, (id + 1) / page_count * 100)
                _sys.stdout.flush()
                _sys.stderr.flush()
        _merge_total = _merge_time.time() - _merge_start
        logger.info("translate_stream: page merge complete (%d pages, %.1fs total)", page_count, _merge_total)
    except Exception as merge_err:
        logger.error("translate_stream: page merge failed after %.1fs: %s", _merge_time.time() - _merge_start, merge_err)
        raise
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

    logger.info("translate_stream: subsetting fonts...")
    if not skip_subset_fonts:
        _subset_start = _merge_time.time()
        # 在子集化前保护数学字体
        _protect_math_fonts(doc_zh)
        _protect_math_fonts(doc_en)
        logger.info("translate_stream: subsetting doc_zh fonts...")
        try:
            doc_zh.subset_fonts(fallback=False)
            logger.info("translate_stream: doc_zh subset_fonts complete")
        except Exception as subset_err:
            logger.warning("subset_fonts failed for doc_zh: %s", str(subset_err)[:120])
        logger.info("translate_stream: subsetting doc_en fonts...")
        try:
            doc_en.subset_fonts(fallback=False)
            logger.info("translate_stream: doc_en subset_fonts complete")
        except Exception as subset_err:
            logger.warning("subset_fonts failed for doc_en: %s", str(subset_err)[:120])
    # === 书签（/Outlines）：翻译标题并重建到 mono/dual 文档（P0-3） ===
    # 在子集化之后、写出之前重建，避免子集化影响新写入的 outline 对象。
    _apply_bookmarks(
        doc_zh, doc_en, stream.getvalue(),
        service=service, lang_in=lang_in, lang_out=lang_out,
        envs=envs, prompt=prompt, ignore_cache=ignore_cache,
    )
    logger.info("translate_stream: writing doc_zh (dual) PDF bytes...")
    try:
        _write_start = _merge_time.time()
        doc_dual = doc_zh.write(deflate=True, garbage=3, use_objstms=1)
        logger.info("translate_stream: doc_zh write OK (size=%d bytes, %.1fs)", len(doc_dual), _merge_time.time() - _write_start)
    except Exception as write_err:
        logger.error("translate_stream: doc_zh write failed: %s", write_err)
        raise
    logger.info("translate_stream: writing doc_en (mono) PDF bytes...")
    try:
        _write_start = _merge_time.time()
        doc_mono = doc_en.write(deflate=True, garbage=3, use_objstms=1)
        logger.info("translate_stream: doc_en write OK (size=%d bytes, %.1fs)", len(doc_mono), _merge_time.time() - _write_start)
    except Exception as write_err:
        logger.error("translate_stream: doc_en write failed: %s", write_err)
        raise
    logger.info("translate_stream: write complete (mono=%d bytes, dual=%d bytes, total=%.1fs)", len(doc_mono), len(doc_dual), _merge_time.time() - _merge_start)
    doc_en.close()
    doc_zh.close()
    logger.info("translate_stream: documents closed")
    return (doc_dual, doc_mono)


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




def _init_worker_process():
    """Initialize worker process: load layout model once into global singleton.

    Called once per worker in ProcessPoolExecutor(initializer=...).
    """
    from pdf2zh.doclayout import ModelInstance, OnnxModel
    if ModelInstance.value is None:
        try:
            ModelInstance.value = OnnxModel.load_available()
        except Exception:
            ModelInstance.value = None


def _serialize_prompt(prompt) -> str:
    """Serialize a Template object to its base string (pickle-safe)."""
    if prompt is None:
        return ""
    if hasattr(prompt, "template"):
        return prompt.template
    if isinstance(prompt, str):
        return prompt
    return str(prompt)


def _translate_parallel_chunk(
    chunk_pages: list[int],
    fp_bytes: bytes,
    page_xref_map: dict = None,
    # --- lightweight scalar parameters only (pickle-safe) ---
    lang_in: str = "",
    lang_out: str = "",
    service: str = "",
    thread: int = 0,
    vfont: str = "",
    vchar: str = "",
    noto_name: str = "",
    font_path: str = "",
    skip_subset_fonts: bool = False,
    ignore_cache: bool = False,
    use_text_metrics: bool = True,
    use_translation_cache: bool = True,
    envs_str: str = "{}",
    prompt_template: str = "",
) -> dict:
    """Process a chunk of pages in a separate process (module-level for pickling).

    Only lightweight scalar parameters are passed across process boundary.
    Heavy C-extension objects (fitz.Document, OnnxModel, FontResolver, etc.)
    are reconstructed inside the worker process from fp_bytes and the
    global ModelInstance singleton. This avoids SwigPyObject pickle errors.
    """
    import io as _io
    import json
    import fitz as _fitz
    from pdf2zh.high_level import translate_patch
    from pdf2zh.doclayout import ModelInstance
    from pdf2zh.collision_resolver import CollisionResolver
    from pdf2zh.layout_graph import LayoutGraph
    from pdf2zh.font_resolver import FontResolver
    from string import Template

    # Reconstruct document from bytes (pickle-safe: open inside worker)
    doc_zh = _fitz.open(stream=fp_bytes, filetype="pdf")
    doc_en = _fitz.open(stream=fp_bytes, filetype="pdf")

    # Load model from singleton (set by _init_worker_process via initializer)
    model = ModelInstance.value

    # Reconstruct utility objects (each worker gets fresh instances)
    collision_resolver = CollisionResolver()
    layout_graph = LayoutGraph()
    font_resolver = FontResolver(lang_out)

    # Reconstruct font handle from path
    noto = _fitz.Font(noto_name, font_path) if font_path else None

    # Reconstruct TextMetrics (if available)
    text_metrics = {}
    if use_text_metrics and font_path:
        try:
            from pdf2zh.text_metrics import TextMetrics as _TM
            tm = _TM(font_path)
            text_metrics[noto_name] = tm
        except Exception:
            pass

    # Reconstruct translation cache
    translation_cache_obj = None
    if use_translation_cache and not ignore_cache:
        try:
            from pdf2zh.translation_cache import TranslationCache
            translation_cache_obj = TranslationCache()
        except Exception:
            pass

    # Reconstruct prompt from template string
    prompt = Template(prompt_template) if prompt_template else None

    # Reconstruct envs from JSON string
    envs = json.loads(envs_str) if isinstance(envs_str, str) else {}

    return translate_patch(
        _io.BytesIO(fp_bytes),
        pages=chunk_pages,
        doc_zh=doc_zh,
        doc_en=doc_en,
        model=model,
        lang_in=lang_in,
        lang_out=lang_out,
        service=service,
        thread=thread,
        vfont=vfont,
        vchar=vchar,
        noto_name=noto_name,
        noto=noto,
        envs=envs,
        prompt=prompt,
        ignore_cache=ignore_cache,
        skip_subset_fonts=skip_subset_fonts,
        text_metrics=text_metrics,
        font_resolver=font_resolver,
        layout_graph=layout_graph,
        collision_resolver=collision_resolver,
        translation_cache=translation_cache_obj,
        page_xref_map=page_xref_map,
        apply_page_xrefs=False,
    )


def _translate_parallel(
    fp: io.BytesIO,
    locals_dict: dict,
    workers: int = 4,
    page_xref_map: dict = None,
) -> dict:
    """Parallel page processing for translate_patch (2.0 L2).

    Splits pages across multiple processes. Each process handles a chunk
    of pages independently, and results are merged at the end.

    ARCHITECTURE: Only lightweight scalar parameters are passed across the
    process boundary (str, int, bool, bytes). Heavy C-extension objects
    (fitz.Document, OnnxModel, FontResolver, TextMetrics, etc.) are
    reconstructed inside each worker process. The layout model is loaded
    once per worker via ProcessPoolExecutor(initializer=...), stored in
    the global ModelInstance singleton.

    Args:
        fp: PDF file pointer (pre-saved document)
        locals_dict: Locals from translate_stream (used only for scalar extraction)
        workers: Number of parallel workers

    Returns:
        Merged obj_patch dictionary
    """
    import concurrent.futures
    import json

    doc_zh = locals_dict.get("doc_zh")
    if doc_zh is None:
        # Fall back to serial
        return translate_patch(fp, **locals_dict)

    all_pages = list(range(doc_zh.page_count))
    chunk_size = max(1, len(all_pages) // workers)
    chunks = [all_pages[i:i + chunk_size] for i in range(0, len(all_pages), chunk_size)]

    # Snapshot fp bytes once (pickle-safe bytestring)
    fp_bytes = fp.getvalue()

    # Extract only pickle-safe scalar parameters from locals_dict
    # 与 translate_stream 顶部归一化保持一致，避免 worker 内 TranslateConverter
    # 因 thread<=0 触发 ThreadPoolExecutor(max_workers=0) 崩溃
    _worker_thread = locals_dict.get("thread", 0)
    if not _worker_thread or _worker_thread <= 0:
        _worker_thread = 4
    scalar_args = {
        "lang_in": locals_dict.get("lang_in", ""),
        "lang_out": locals_dict.get("lang_out", ""),
        "service": locals_dict.get("service", ""),
        "thread": _worker_thread,
        "vfont": locals_dict.get("vfont", ""),
        "vchar": locals_dict.get("vchar", ""),
        "noto_name": locals_dict.get("noto_name", NOTO_NAME),
        "font_path": locals_dict.get("font_path", ""),
        "skip_subset_fonts": locals_dict.get("skip_subset_fonts", False),
        "ignore_cache": locals_dict.get("ignore_cache", False),
        "use_text_metrics": locals_dict.get("use_text_metrics", True),
        "use_translation_cache": locals_dict.get("use_translation_cache", True),
        "envs_str": json.dumps(locals_dict.get("envs", {})),
        "prompt_template": _serialize_prompt(locals_dict.get("prompt")),
    }

    obj_patch = {}
    with concurrent.futures.ProcessPoolExecutor(
        max_workers=workers, initializer=_init_worker_process,
    ) as executor:
        futures = [
            executor.submit(
                _translate_parallel_chunk, chk, fp_bytes,
                page_xref_map=page_xref_map, **scalar_args,
            )
            for chk in chunks
        ]
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
