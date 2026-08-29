"""Functions that can be used for the most common use-cases for pdf2zh.six"""

import asyncio
import io
import json
import os
import re
import sys
import tempfile
import time
import logging
from asyncio import CancelledError
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from string import Template
from typing import Any, BinaryIO, Dict, List, Optional, Tuple

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


def _int_env(name: str, default: int) -> int:
    """读取整数环境变量，非法值回落默认（防御用户手写非数字）。"""
    try:
        raw = os.environ.get(name, "").strip()
        return int(raw) if raw else default
    except ValueError:
        return default


def _prefetch_predict(model, image, imgsz):
    """8.3.2 预取线程目标：后台执行单页版面推理。

    返回 ``(layout, elapsed)``；异常封装为异常对象返回（主线程检测后同步
    兜底 predict，绝不把预取失败传播为整页失败）。
    """
    try:
        _t = time.perf_counter()
        result = model.predict(image, imgsz=imgsz)[0]
        return result, time.perf_counter() - _t
    except Exception as exc:  # noqa: BLE001 -- 失败由主线程同步兜底
        return exc


class _LayoutBatchPredictor:
    """批量版面推理封装（动态 Batch 并行，V3 iteration）。

    基于 DocLayout-YOLO 的动态轴导出（``['batch', 3, 'height', 'width']``），
    由调用方（translate_patch 页循环）攒够 batch 页后调用 ``predict_images``，
    内部把 N 页图片 stack 成单张 ``[N, 3, H, W]`` 一次 ``session.run``（经
    ``OnnxModel.predict_batch``），相比逐页推理减少 Python/ORT 调度开销；模型
    不支持动态 batch 时自动退化为逐页 ``predict``（行为与现状完全等价）。

    注意（本机 CPU 实测）：ORT 对动态 batch 不做批量融合，compute 为相加，
    CPU 上 batch 反而略慢（~0.9x）；建议仅在 GPU/DML 后端或批量融合导出的
    模型上开启。默认关闭，``PDF2ZH_LAYOUT_BATCH``（≥2）开启并设定批大小。
    """

    def __init__(self, model, batch_size: int = 8):
        self.model = model
        self.batch_size = max(2, int(batch_size))
        self._flush_count = 0
        self._predicted_pages = 0
        self._infer_secs = 0.0

    def predict_images(self, images) -> list:
        """批量推理 ``images``，返回 ``List[YoloResult]``（顺序一一对应）。"""
        if not images:
            return []
        t0 = time.perf_counter()
        if hasattr(self.model, "predict_batch"):
            results = self.model.predict_batch(images)
        else:
            # 防御：无 batch 能力的模型逐张预测
            results = [self.model.predict(im)[0] for im in images]
        self._infer_secs += time.perf_counter() - t0
        self._flush_count += 1
        self._predicted_pages += len(images)
        return results

    def stats(self) -> tuple:
        """(ONNX 调度次数, 预测页数, 推理总耗时秒数)"""
        return self._flush_count, self._predicted_pages, self._infer_secs


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
    # 文本层质量预检 gate（扫描损坏检测长期实现，scan_damaged 报告 §6.2）：
    # 开启时翻译启动前对源 PDF 跑多信号融合预检，命中损坏信号 → 写入
    # v3_output["text_quality"] 并输出强警告（legacy 无 OCR 兜底）。
    text_quality_gate: bool = False,
    # V8.5: 采集逐篇段落源/目标几何（link_remap 桥接数据）
    link_remap: bool = False,
    # V9.0: Processor 层语义通道（RAW/SEMANTIC + TOC 结构化记录，side-channel）
    processor_channels: bool = True,
    # V8.3 后半程/阶段六八: 渲染接管计划 + 翻译 QA + 双轨聚类接管（side-channel）
    render_takeover: bool = False,
    translation_qa: bool = False,
    geometry_cluster: bool = False,
    # V1.17-3: 合并目录段按物理行重切（渲染路径，空列页码目录行逐行渲染）
    toc_split: bool = True,
    # 可观测层: 逐阶段 dump（Glyph/Line/Block/TOC/Translation/Layout）
    pipeline_dump: bool = False,
    # V11: 文档统一模型（多页树 + Relations，回传 v3_output["document_model"]）
    document_model: bool = False,
    # Phase D: 可观测层（Trace/Snapshot/Decision → v3_output["observability"]）
    observability: bool = False,
    # P5–P10: 语义文本重建 + 公式几何重构（→ v3_output["reconstruction"]）
    # 默认 True：真实运行必须产出重建 QA（此前默认 False 且服务层未透传，
    # 实际运行中通道全程关闭 → 「测试全过、实际无产物」的假象）。side-channel
    # 纪律保证失败只进 debug 日志，绝不干扰主链路渲染。
    reconstruction_channel: bool = True,
    # 阶段 3 主链路接线：渲染前以 P5–P10 SolvedUnit 几何接管 legacy 段落
    # （文本集一致才接管；公式锚点经旧 {vN} 机制逐字形还原 → 零漂移）。
    reconstruction_adopt: bool = True,
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
    device.toc_split = bool(toc_split)
    device.toc_split_reports = {}
    device.render_plans = {}
    device.translation_qa_records = {}
    device.pipeline_dump = bool(pipeline_dump)
    device.pipeline_dumps = {}
    device.document_model_enabled = bool(document_model)
    device.document_model = None
    # Phase D: 可观测会话（converter side-channel 写，本函数收尾回传）
    device.observability = bool(observability)
    device.obs_session = None
    # P5–P10: 语义重建 + 公式几何 side-channel
    device.reconstruction_channel = bool(reconstruction_channel)
    device.reconstruction_records = {}
    device.reconstruction_qa = {}
    # 阶段 3 主链路接线容器（渲染前接管 + 完整对象存档）
    device.reconstruction_adopt = bool(reconstruction_adopt)
    device.reconstruction_results = {}
    device.reconstruction_adoptions = {}

    # 文本层质量预检 gate（side-channel，异常只进日志）：复用
    # pdf2zh.scanned_detection 的多信号融合判定（scan_damaged 报告 §6.2/§6.3），
    # 把判定结果写入 v3_output["text_quality"]；legacy 无 OCR 能力，命中时
    # 输出强警告并建议切换 --parse-engine magicpdf --magicpdf-ocr。
    if text_quality_gate and v3_output is not None:
        _run_text_quality_gate(inf, v3_output)

    assert device is not None
    obj_patch = {}
    interpreter = PDFPageInterpreterEx(rsrcmgr, device, obj_patch)
    if pages:
        total_pages = len(pages)
    else:
        total_pages = doc_zh.page_count

    parser = PDFParser(inf)
    doc = PDFDocument(parser)
    # 动态 Batch 版面推理（可选，V3 iteration）：PDF2ZH_LAYOUT_BATCH ≥ 2 时开启。
    # CPU 上 ORT 对动态 batch 不做批量融合（实测略慢），默认关闭；GPU/DML 后端
    # 或批量融合导出的模型受益。模型未加载（model=None）时自动回落逐页 predict。
    _layout_batch = _int_env("PDF2ZH_LAYOUT_BATCH", 0)
    _layout_predictor = None
    if model is not None and _layout_batch >= 2:
        try:
            _layout_predictor = _LayoutBatchPredictor(model, batch_size=_layout_batch)
            logger.info(
                "Layout batch inference enabled (batch_size=%d, supports_batch=%s)",
                _layout_batch,
                bool(getattr(model, "supports_batch", False)),
            )
        except Exception as batch_err:  # noqa: BLE001 -- 初始化失败逐页兜底
            logger.warning(
                "Layout batch predictor init failed (%s); per-page predict",
                batch_err,
            )
            _layout_predictor = None
    with tqdm.tqdm(total=total_pages) as progress:
        _pd_n, _pd_secs = 0, 0.0  # L2: 每页推理耗时聚合（布局阶段观测）
        vcls = ["abandon", "figure", "table", "isolate_formula", "formula_caption"]

        def _process_page_layout(page, pix, page_layout) -> None:
            """版面掩码 + 新内容流 xref + 逐页渲染（逐页/批量两路径共用）。"""
            # kdtree 是不可能 kdtree 的，不如直接渲染成图片，用空间换时间
            box = np.ones((pix.height, pix.width))
            h, w = box.shape
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
            if page_xref_map and page.pageno in page_xref_map:
                # 并行模式：page_xref 由调用方（主进程）预创建，worker 与主进程编号一致；
                # worker 进程中该对象不存在，故跳过 update_object/update_stream/set_contents
                page.page_xref = page_xref_map[page.pageno]
                if apply_page_xrefs:
                    doc_zh[page.pageno].set_contents(page.page_xref)
            else:
                page.page_xref = doc_zh.get_new_xref()  # hack 插入页面的新 xref
                doc_zh.update_object(page.page_xref, "<<>>")
                doc_zh.update_stream(page.page_xref, b"")
                doc_zh[page.pageno].set_contents(page.page_xref)
            interpreter.process_page(page)

        if _layout_predictor is not None:
            # 批量路径（可选，PDF2ZH_LAYOUT_BATCH ≥ 2）：攒够 batch 页后一次
            # ONNX 调度批量推理，再逐页执行版面处理（进度/取消语义与逐页一致）。
            _pending = []  # (page, pix, image)
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
                _pending.append((page, pix, image))
                if len(_pending) >= _layout_batch:
                    _t_predict = time.perf_counter()
                    results = _layout_predictor.predict_images(
                        [im for _, _, im in _pending]
                    )
                    _d_predict = time.perf_counter() - _t_predict
                    _pd_n += len(_pending)
                    _pd_secs += _d_predict
                    if _pd_n % 25 == 0 or _pd_n == total_pages:
                        logger.info(
                            "layout predict so far: %d pages, avg %.3fs/page (last %.3fs)",
                            _pd_n,
                            _pd_secs / max(_pd_n, 1),
                            _d_predict,
                        )
                    for (pg, px, _im), res in zip(_pending, results):
                        logger.debug(
                            "page %d layout predict boxes=%d",
                            pg.pageno,
                            len(res.boxes),
                        )
                        _process_page_layout(pg, px, res)
                    _pending = []
        else:
            # 8.3.2 预测预取流水线（PDF2ZH_LAYOUT_PREFETCH=1 启用，默认关闭）：
            # 下一页版面推理在线程中与当前页翻译（网络等待）/渲染重叠执行，
            # 把串行「推理 0.36s → 翻译 1-2s → 渲染 0.10s」压掉推理墙钟。
            # 严格顺序边界保留：process_page 永远串行（TOC/书签/公式组跨页
            # 依赖不受影响）；预取失败自动同步兜底。
            _prefetch = model is not None and _int_env("PDF2ZH_LAYOUT_PREFETCH", 0) >= 1
            _pf_executor = (
                concurrent.futures.ThreadPoolExecutor(max_workers=1)
                if _prefetch
                else None
            )
            _pf_future = None
            _page_records = []
            for _pn, _pg in enumerate(PDFPage.create_pages(doc)):
                _pg.pageno = _pn
                if pages and (_pn not in pages):
                    continue
                _page_records.append((_pn, _pg))
            try:
                for _idx, (_pageno, page) in enumerate(_page_records):
                    if cancellation_event and cancellation_event.is_set():
                        raise CancelledError("task cancelled")
                    progress.update()
                    if callback:
                        callback(progress)
                    pix = doc_zh[page.pageno].get_pixmap()
                    image = np.frombuffer(pix.samples, np.uint8).reshape(
                        pix.height, pix.width, 3
                    )[:, :, ::-1]
                    if _pf_future is not None:
                        _pf_res = _pf_future.result()
                        if isinstance(_pf_res, Exception):
                            # 预取失败 → 同步兜底（绝不带病使用旧布局）
                            _t_predict = time.perf_counter()
                            page_layout = model.predict(
                                image, imgsz=int(pix.height / 32) * 32
                            )[0]
                            _d_predict = time.perf_counter() - _t_predict
                        else:
                            page_layout, _d_predict = _pf_res
                    else:
                        _t_predict = time.perf_counter()
                        page_layout = model.predict(
                            image, imgsz=int(pix.height / 32) * 32
                        )[0]
                        _d_predict = time.perf_counter() - _t_predict
                    _pd_n, _pd_secs = _pd_n + 1, _pd_secs + _d_predict
                    logger.debug(
                        "page %d layout predict %.3fs boxes=%d",
                        page.pageno,
                        _d_predict,
                        len(page_layout.boxes),
                    )
                    if _pd_n % 25 == 0 or _pd_n == total_pages:
                        logger.info(
                            "layout predict so far: %d pages, avg %.3fs/page (last %.3fs)",
                            _pd_n,
                            _pd_secs / max(_pd_n, 1),
                            _d_predict,
                        )
                    # 预取下一页（主线程渲染 pixmap 保证顺序，后台只做推理）
                    _pf_future = None
                    if _pf_executor is not None and _idx + 1 < len(_page_records):
                        _nxt_pageno = _page_records[_idx + 1][0]
                        _nxt_pix = None
                        try:
                            _nxt_pix = doc_zh[_nxt_pageno].get_pixmap()
                        except Exception:  # noqa: BLE001 -- 预取失败同步兜底
                            pass
                        if _nxt_pix is not None:
                            _nxt_image = np.frombuffer(
                                _nxt_pix.samples, np.uint8
                            ).reshape(_nxt_pix.height, _nxt_pix.width, 3)[:, :, ::-1]
                            _pf_future = _pf_executor.submit(
                                _prefetch_predict,
                                model,
                                _nxt_image,
                                int(_nxt_pix.height / 32) * 32,
                            )
                    _process_page_layout(page, pix, page_layout)
            finally:
                if _pf_executor is not None:
                    _pf_executor.shutdown(wait=False, cancel_futures=True)

    # 批量路径：处理未满批的剩余页
    if _layout_predictor is not None and _pending:
        _t_predict = time.perf_counter()
        results = _layout_predictor.predict_images([im for _, _, im in _pending])
        _d_predict = time.perf_counter() - _t_predict
        for (pg, px, _im), res in zip(_pending, results):
            _process_page_layout(pg, px, res)
        logger.info(
            "Layout batch remainder: %d page(s) in %.3fs",
            len(_pending),
            _d_predict,
        )
    if _layout_predictor is not None:
        _f, _p, _secs = _layout_predictor.stats()
        if _p:
            logger.info(
                "Layout batch inference: %d ONNX call(s) for %d page(s) in %.3fs",
                _f,
                _p,
                _secs,
            )
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
        v3_output["translation_qa_records"] = dict(
            getattr(device, "translation_qa_records", {})
        )
        v3_output["geometry_adoptions"] = dict(
            getattr(device, "geometry_adoptions", {})
        )
        v3_output["pipeline_dumps"] = dict(getattr(device, "pipeline_dumps", {}))
        v3_output["layout_violations"] = dict(
            getattr(device, "layout_violations_by_page", {})
        )
        v3_output["reconstruction"] = dict(
            getattr(device, "reconstruction_records", {})
        )
        v3_output["reconstruction_qa"] = dict(getattr(device, "reconstruction_qa", {}))
        dm = getattr(device, "document_model", None)
        if dm is not None and hasattr(dm, "to_dict"):
            v3_output["document_model"] = dm.to_dict()
    if observability:
        obs_extra = _collect_observability(device, v3_output)
        if obs_extra and v3_output is None:
            return {"__obs__": obs_extra, **obj_patch}
    return obj_patch


# ---------------------------------------------------------------------------
# Marker: translate_stream start
def _run_text_quality_gate(inf: BinaryIO, v3_output: dict) -> None:
    """翻译前文本层质量预检 gate（scan_damaged 报告 §6.2 长期实现）。

    在翻译启动前对源 PDF 跑 :func:`pdf2zh.scanned_detection.preflight_scan_check`
    多信号融合预检，把判定写入 ``v3_output["text_quality"]``。legacy 内核无
    OCR 兜底：命中损坏信号时输出强警告并建议切换 magicpdf / BabelDOC OCR。
    任何异常仅记 debug 日志（side-channel 纪律），绝不阻断翻译。
    """
    path = getattr(inf, "name", "") or ""
    if not path or not os.path.exists(path) or not path.lower().endswith(".pdf"):
        v3_output["text_quality"] = {"preflight": None, "scanned": False, "reasons": []}
        return
    try:
        from pdf2zh.scanned_detection import preflight_scan_check

        decision = preflight_scan_check(path)
        v3_output["text_quality"] = {
            "preflight": decision.to_dict(),
            "scanned": decision.is_scanned,
            "reasons": decision.reasons,
        }
        if decision.is_scanned:
            logger.warning(
                "文本层质量预检命中扫描/损坏信号（%s）。legacy 内核无 OCR 兜底，"
                "译文可能基于乱码输出。建议改用 --parse-engine magicpdf "
                "--magicpdf-ocr，或 --babeldoc-ocr on。",
                "; ".join(decision.reasons) or "unknown",
            )
    except Exception as exc:  # noqa: BLE001 -- 预检失败不阻断翻译
        v3_output["text_quality"] = {
            "preflight": None,
            "scanned": False,
            "reasons": [],
            "error": str(exc),
        }
        logger.debug("text quality gate skipped: %s", exc)


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
        return remap_document_links(
            doc_zh, records, page_offset=0, y_flip=True, page_heights=heights
        )
    except Exception as e:
        logger.warning("link_remap failed at doc level: %s", str(e)[:160])
        return empty


def _collect_observability(device, v3_output: dict = None) -> dict:
    """Phase D: 收尾 ObsSession → bundle + 每页 Overlay SVG + Inspector HTML。

    只在 ``device.obs_session`` 存在且 ``observability`` 开启时产出：
    - v3_output 存在（串行路径）→ 写入 ``v3_output["observability"]``；
    - v3_output 缺失（并行 worker）→ 返回 payload，由 translate_patch
      挂 ``__obs__`` 私有键回传。
    任何异常只进 debug 日志（side-channel 纪律）。
    """
    session = getattr(device, "obs_session", None)
    if session is None:
        return {}
    try:
        from pdf2zh.v3.inspector_view import build_inspector_html
        from pdf2zh.v3.overlay_view import overlay_from_snapshot, render_svg

        bundle = session.bundle()
        snaps = (bundle.get("snapshots") or {}).get("snapshots") or {}
        overlays = []
        for pageid in sorted(session.page_dims or {}):
            w, h = session.page_dims[pageid]
            recs = overlay_from_snapshot(snaps.get(f"render_p{pageid}") or {})
            if not recs:
                continue
            overlays.append(
                {
                    "page": f"Page {pageid}",
                    "svg": render_svg(recs, float(w or 600.0), float(h or 800.0)),
                }
            )
        inspector_html = build_inspector_html(
            session.snapshot_store,
            decisions=bundle.get("decisions"),
            diagnostics=bundle.get("diagnostics"),
            overlays=overlays,
            title=f"Inspector {bundle.get('doc_id', 'doc')}",
        )
        payload = {
            "bundle": bundle,
            "overlays": overlays,
            "inspector_html": inspector_html,
        }
        if v3_output is not None:
            v3_output["observability"] = payload
            return {}
        return payload
    except Exception as e:  # noqa: BLE001 — 可观测层永不阻断主链路
        logger.debug("observability collect failed: %s", str(e)[:160])
        return {}


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
    empty = {"pages": 0, "objects": 0, "translated": 0, "preserved": 0, "overlay": 0}
    if not getattr(doc_zh, "page_count", None):
        return empty
    if not (image_engine or content_preservation):
        return empty
    try:
        from pdf2zh.v3.image_engine import (
            TranslationDecisionEngine,
            analyze_pdf_images,
        )
        from pdf2zh.v3.content_preservation import ContentPreservationEngine
    except Exception as e:
        logger.debug("preservation engine import failed: %s", str(e)[:120])
        return empty
    try:
        engine = TranslationDecisionEngine()
        image_objs = analyze_pdf_images(
            doc_zh,
            engine=engine,
            page_range=(
                list(range(doc_zh.page_count)) if doc_zh.page_count < 2000 else None
            ),
        )
        pres_engine = ContentPreservationEngine(engine=engine)
        rec = {}
        for page_no, objs in image_objs.items():
            page_rec = []
            for obj in objs:
                dec = pres_engine.decide_image(obj)
                page_rec.append(dec.to_dict())
            rec[str(page_no)] = page_rec

        stats = {
            "pages": len(image_objs),
            "objects": sum(len(v) for v in image_objs.values()),
            "translated": 0,
            "preserved": 0,
            "overlay": 0,
        }
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
                    pix.height, pix.width, pix.n
                )
                rgb = px[..., :3] if pix.n >= 3 else px
                out_bytes, summ = translate_image_pixels(
                    rgb, object_id=f"p{pno}_render", page_num=pno
                )
                out[str(pno)] = {
                    "mode": summ.render_mode,
                    "translated": summ.regions_translated,
                    "total": summ.regions_total,
                    "bytes": len(out_bytes),
                }
            except Exception as e:  # noqa: BLE001
                logger.debug("page preview render failed p%s: %s", pno, str(e)[:120])
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
    semantic_toc_entries: List[dict] = None,
) -> dict:
    """翻译并重建 PDF 书签（/Outlines）到译文文档（Commit 6D）。

    Outline 来源优先级（Source selection）：

    A. 同时有 visual TOC + 结构化条目：优先用语义 TOC 生成翻译后的
       Outline（标题已是译后），避免与 native outline 重复。
    B. 只有 visual TOC（无结构化条目时的回退）：语义条目非空时从条目创建。
    C. 只有 native Outline：读取源 PDF outline 树，翻译其标题并重建。
    D. 两者皆无：不创建任何 Outline。

    索引契约（单一边界）：语义条目的 ``destination_page`` 是 1-base 文档页
    （与 PyMuPDF ``set_toc`` 同构）；printed ``page_number`` 永远不用于
    书签目的地（``page_number != destination_page``）。mono（doc_zh）直接
    使用目标页；dual（doc_en）映射为 2n-1（指向英文页）。

    任一步失败仅记 warning，不回退整个翻译任务。返回
    ``{"source": semantic|native|none, "count": n}``。
    """
    import io

    result = {"source": "none", "count": 0}

    # ── 首选：语义 TOC 结构化条目（A/B）──
    semantic_entries = [e for e in (semantic_toc_entries or []) if e and e.get("title")]
    new_toc: List[list] = []
    used_semantic = False
    if semantic_entries:
        try:
            from pdf2zh.v3.outline_renderer import build_outline_toc

            new_toc = build_outline_toc(semantic_entries)
            used_semantic = True
        except Exception as e:
            logger.warning("bookmarks: semantic outline build failed: %s", str(e)[:120])
            new_toc = []
    if not new_toc and semantic_entries:
        # 兜底：build_outline_toc 保守失败时逐条退化（标题非空即建）
        used_semantic = True
        for e in semantic_entries:
            title = (e.get("title") or "").strip()
            if not title:
                continue
            dest = e.get("destination_page") or 1
            try:
                dest = int(dest) if int(dest) > 0 else 1
            except (TypeError, ValueError):
                dest = 1
            new_toc.append([1, title, dest])

    # ── 回退 C：native outline only（保留原 outline 并翻译标题）──
    native_toc = []
    translator = None
    if not new_toc:
        try:
            import pymupdf

            reader = pymupdf.open(stream=stream_bytes, filetype="pdf")
            native_toc = reader.get_toc()
            reader.close()
        except Exception as e:
            logger.warning("bookmarks: failed to read outline: %s", str(e)[:120])
            return result
        if not native_toc:
            # D：两者皆无 → 不创建
            return result
        try:
            translator = build_translator(
                service, lang_in, lang_out, envs, prompt, ignore_cache
            )
        except Exception as e:
            logger.warning("bookmarks: translator init failed: %s", str(e)[:120])
            return result
        if translator is None:
            return result
        for item in native_toc:
            lvl = item[0] if len(item) > 0 else 1
            title = (item[1] if len(item) > 1 else "").strip()
            page = item[2] if len(item) > 2 else 1
            if not title:
                continue
            try:
                translated = translator.translate(title)
            except Exception as e:
                logger.warning(
                    "bookmarks: title translate failed (%r): %s", title[:30], str(e)[:120]
                )
                translated = title
            t = (translated or "").strip() or title
            new_toc.append([lvl, t, page])

    if not new_toc:
        return result
    result["source"] = "semantic" if used_semantic else "native"
    result["count"] = len(new_toc)

    def _page_map(p: int) -> int:
        return p

    try:
        doc_zh.set_toc([[lvl, t, _page_map(int(page))] for lvl, t, page in new_toc])
    except Exception as e:
        logger.warning("bookmarks: set_toc mono failed: %s", str(e)[:120])
    dual_toc = []
    for lvl, t, page in new_toc:
        dual_toc.append([lvl, t, max(1, 2 * int(page) - 1)])
    try:
        doc_en.set_toc(dual_toc)
    except Exception as e:
        logger.warning("bookmarks: set_toc dual failed: %s", str(e)[:120])
    return result


def _resolve_parallel_settings(
    parallel_pages: Optional[bool], parallel_workers: Optional[int], default_pages: bool
):
    """统一并行设置解析：显式参数 > PDF2ZH_PARALLEL / PDF2ZH_NO_PARALLEL /
    PDF2ZH_PARALLEL_WORKERS 环境变量 > 默认值。

    内存不足的机器可用 PDF2ZH_PARALLEL_WORKERS=2 或 CLI --parallel-workers 降压，
    或 PDF2ZH_NO_PARALLEL=1 / --no-parallel 完全关闭并行。
    """
    pages = default_pages if parallel_pages is None else parallel_pages
    if not pages and os.environ.get("PDF2ZH_PARALLEL", "") in ("1", "true", "True"):
        pages = True
    if pages and os.environ.get("PDF2ZH_NO_PARALLEL", "") in ("1", "true", "True"):
        pages = False
    if parallel_workers is None:
        try:
            parallel_workers = int(os.environ.get("PDF2ZH_PARALLEL_WORKERS") or 0)
        except (TypeError, ValueError):
            parallel_workers = 0
    if not parallel_workers or parallel_workers < 1:
        parallel_workers = 4
    return pages, parallel_workers


# ── 页切片-回贴（slice-splice，性能基准报告 P0 #2）───────────────────────────
#
# 基准结论（doc/perf/itbook-benchmark/report.md）：legacy 链路单页运行 ~93s，
# 其中 ~90s 是与所选页数无关的全文档固定开销 —— 字体 xref 广播 O(xrefs)、
# doc_en.insert_file(doc_zh) 全量对象复制、逐页 move_page 交错（730 页书
# 实测占绝对主导）。pages 过滤只跳过版面推理，不缩减这些 O(全文档) 阶段。
#
# 切片-回贴：pages 为严格子集时，把原文档切成仅含选中页的切片 → 在切片上
# 跑完整翻译主流程（所有 O(N) 阶段 N=切片页数）→ 译页回贴原文档：
#   - mono：原位替换选中页（保留原文档对象图/TOC/跨页链接）；
#   - dual：逐页交错重建（选中页 = 原页 + 译页；未选中页 = 原页 ×2，与
#     全文档 merge 的既有语义一致）。
# 任何异常都整体回退全文档路径（绝不因优化引入失败）。emit_ir /
# document_model / observability 打开时停用（这些产物按全文档页号索引，
# 消费方假设全文档语义）。PDF2ZH_NO_SLICE_SPLICE=1 全局关闭。


def _normalize_slice_pages(pages: Any, page_count: int) -> List[int]:
    """归一化页选择为去重升序的 0 基页号列表（剔除越界项）。"""
    sel: set = set()
    for p in pages or []:
        try:
            pi = int(p)
        except (TypeError, ValueError):
            continue
        if 0 <= pi < page_count:
            sel.add(pi)
    return sorted(sel)


def _slice_pdf_pages(stream: bytes, sel: List[int]) -> Tuple[bytes, Dict[int, int]]:
    """把 PDF 切成仅含 ``sel`` 页的切片；返回 (切片字节, {切片页号: 原页号})。"""
    import pymupdf

    src = pymupdf.open(stream=stream, filetype="pdf")
    out = pymupdf.open()
    try:
        page_map: Dict[int, int] = {}
        for new_idx, orig_idx in enumerate(sel):
            out.insert_pdf(src, from_page=orig_idx, to_page=orig_idx)
            page_map[new_idx] = orig_idx
        return out.tobytes(deflate=True, garbage=4), page_map
    finally:
        out.close()
        src.close()


def _splice_mono_pages(
    original: bytes, translated_slice: bytes, sel: List[int]
) -> bytes:
    """把切片 mono 的译页原位回贴进原文档（替换选中页，其余页原样保留）。

    从最后一个选中页向前处理：译页追加到末尾 → move 到目标位（原页后移
    一位）→ 删除被顶出的原页。倒序保证已回贴区域不受后续插入影响；
    TOC 在改动前快照、完成后原样恢复（页序不变 → 页号映射不变）。
    """
    import pymupdf

    src = pymupdf.open(stream=original, filetype="pdf")
    tsl = pymupdf.open(stream=translated_slice, filetype="pdf")
    try:
        toc = src.get_toc(simple=True)
        for j in range(len(sel) - 1, -1, -1):
            orig = sel[j]
            src.insert_pdf(tsl, from_page=j, to_page=j, start_at=src.page_count)
            src.move_page(src.page_count - 1, orig)
            src.delete_page(orig + 1)
        if toc:
            src.set_toc(toc)
        return src.tobytes(deflate=True, garbage=4)
    finally:
        tsl.close()
        src.close()


def _interleave_dual_pages(original: bytes, slice_dual: bytes, sel: List[int]) -> bytes:
    """重建全文档 dual：逐页 [原页, 译页]（未选中页 [原页, 原页副本]）。

    与全文档 merge（insert_file + move_page 交错）产出结构一致；TOC 页号
    重映射到译页（原 1 基页 p → dual 1 基 2p）。

    性能关键：按「连续游程」分组插入（每次 insert_pdf 拷贝一整段页区间），
    而非逐页拷贝 —— 730 页文档单页回贴从 ~1460 次 insert 降到 ~2×游程数次
    （实测逐页重建 324s → 游程重建秒级）。译页先用 ``select`` 压缩成按选中
    顺序排列的紧凑文档，游程内即可整段范围插入。
    """
    import pymupdf

    src = pymupdf.open(stream=original, filetype="pdf")
    tsl = pymupdf.open(stream=slice_dual, filetype="pdf")
    out = pymupdf.open()
    try:
        # 译页压缩：slice dual 的奇数位（0 基 2j+1）是第 j 个选中页的译页；
        # select 后 zh[j] 即第 j 个选中页的译页（按选中顺序）。
        zh = pymupdf.open(stream=slice_dual, filetype="pdf")
        try:
            zh.select([2 * j + 1 for j in range(len(sel))])
        except Exception:  # noqa: BLE001 -- 压缩失败逐页兜底
            zh.close()
            zh = None

        # 连续选中页游程分组：[(起始原页, 结束原页, 切片起始序号), ...]
        runs: List[List[int]] = []
        for j, orig in enumerate(sel):
            if runs and orig == runs[-1][1] + 1:
                runs[-1][1] = orig
            else:
                runs.append([orig, orig, j])

        cursor = 0

        def _emit_unselected(a: int, b: int) -> None:
            # 未选中段：原页 + 原页副本（与全文档 merge 语义一致）
            out.insert_pdf(src, from_page=a, to_page=b)
            out.insert_pdf(src, from_page=a, to_page=b)

        for a, b, ja in runs:
            if cursor < a:
                _emit_unselected(cursor, a - 1)
            out.insert_pdf(src, from_page=a, to_page=b)
            if zh is not None:
                out.insert_pdf(zh, from_page=ja, to_page=ja + (b - a))
            else:
                for j in range(ja, ja + (b - a) + 1):
                    out.insert_pdf(tsl, from_page=2 * j + 1, to_page=2 * j + 1)
            cursor = b + 1
        if cursor < src.page_count:
            _emit_unselected(cursor, src.page_count - 1)

        toc = src.get_toc(simple=True)
        if toc:
            out.set_toc(
                [
                    [lvl, title, 2 * p if 0 < p <= src.page_count else p]
                    for lvl, title, p in toc
                ]
            )
        return out.tobytes(deflate=True, garbage=4)
    finally:
        out.close()
        if zh is not None:
            zh.close()
        tsl.close()
        src.close()


def _remap_slice_local_pages(
    v3_output: Optional[dict], page_map: Dict[int, int]
) -> None:
    """把 v3_output 里以页号为键的 side-channel 字典重映射回原文档页号。

    ir_snapshots / gate_verdicts / processor_reports 等均按 pageno（int）
    键控；切片翻译期间写的是切片局部页号，回贴后统一还原。仅处理
    「全 int 键」的顶层字典，其余结构原样保留（绝不抛错）。
    """
    if not v3_output or not page_map:
        return
    for key, val in list(v3_output.items()):
        if (
            isinstance(val, dict)
            and val
            and all(isinstance(k, int) for k in val.keys())
        ):
            try:
                v3_output[key] = {page_map.get(k, k): v for k, v in val.items()}
            except Exception:  # noqa: BLE001 -- 诊断通道重映射永不致命
                pass


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
    # V1.17-3: 合并目录段按物理行重切（渲染路径，空列页码目录行逐行渲染）
    toc_split: bool = True,
    # 可观测层: 逐阶段 dump（Glyph/Line/Block/TOC/Translation/Layout）
    pipeline_dump: bool = False,
    # V11: 文档统一模型（多页树 + Relations）
    document_model: bool = False,
    # Phase D: 可观测框架（Trace/Snapshot/Decision/Overlay/Inspector，默认关）
    observability: bool = False,
    # 并行进度回调：progress_cb(percent: float, message: str)，percent ∈ [0, 100]
    # 只由主进程调用（并行路径按 chunk 完成回报；串行路径不回报）。
    progress_cb: object = None,
    # P5–P10 主链路接管（阶段 3）：与 reconstruction_channel 配套，经
    # **dict(locals()) 透传到 translate_patch / 并行 worker。默认 True。
    reconstruction_adopt: bool = True,
    # 内部开关：切片-回贴递归调用时置 False，防止无限递归。
    _allow_slice_splice: bool = True,
    **kwarg: Any,
):
    # ── 页切片-回贴（P0 #2）：pages 为严格子集时只在切片上跑翻译主流程 ──
    # 必须放在一切重工作之前（字体嵌入/xref 广播/merge 都是 O(全文档)）。
    if (
        _allow_slice_splice
        and pages
        and not emit_ir
        and not document_model
        and not observability
        and os.environ.get("PDF2ZH_NO_SLICE_SPLICE", "") not in ("1", "true", "True")
    ):
        try:
            _probe = Document(stream=stream)
            try:
                _total = _probe.page_count
            finally:
                _probe.close()
            _sel = _normalize_slice_pages(pages, _total)
        except Exception:  # noqa: BLE001 -- 探测失败走全文档路径
            _sel, _total = [], 0
        if _sel and 0 < len(_sel) < _total:
            try:
                _slice_bytes, _page_map = _slice_pdf_pages(stream, _sel)
                _dual_s, _mono_s = translate_stream(
                    _slice_bytes,
                    pages=None,
                    lang_in=lang_in,
                    lang_out=lang_out,
                    service=service,
                    thread=thread,
                    vfont=vfont,
                    vchar=vchar,
                    callback=callback,
                    cancellation_event=cancellation_event,
                    model=model,
                    envs=envs,
                    prompt=prompt,
                    skip_subset_fonts=skip_subset_fonts,
                    ignore_cache=ignore_cache,
                    use_text_metrics=use_text_metrics,
                    use_translation_cache=use_translation_cache,
                    parallel_pages=parallel_pages,
                    parallel_workers=parallel_workers,
                    emit_ir=emit_ir,
                    relayout_gate=relayout_gate,
                    v3_output=v3_output,
                    relink_links=relink_links,
                    image_engine=image_engine,
                    content_preservation=content_preservation,
                    emit_preservation=emit_preservation,
                    processor_channels=processor_channels,
                    render_takeover=render_takeover,
                    translation_qa=translation_qa,
                    geometry_cluster=geometry_cluster,
                    image_render=image_render,
                    toc_split=toc_split,
                    pipeline_dump=pipeline_dump,
                    document_model=document_model,
                    observability=observability,
                    progress_cb=progress_cb,
                    reconstruction_adopt=reconstruction_adopt,
                    _allow_slice_splice=False,
                    **kwarg,
                )
                # 译页回贴失败也走全文档兜底 —— 输出正确性优先于速度。
                _mono_full = _splice_mono_pages(stream, _mono_s, _sel)
                _dual_full = _interleave_dual_pages(stream, _dual_s, _sel)
                _remap_slice_local_pages(v3_output, _page_map)
                logger.info(
                    "translate_stream: slice-splice complete "
                    "(%d/%d pages, mono=%d B, dual=%d B)",
                    len(_sel),
                    _total,
                    len(_mono_full),
                    len(_dual_full),
                )
                return _dual_full, _mono_full
            except CancelledError:
                raise
            except Exception as slice_exc:  # noqa: BLE001 -- 优化绝不破坏翻译
                logger.warning(
                    "translate_stream: slice-splice failed (%s); "
                    "falling back to full-document path",
                    str(slice_exc)[:200],
                )

    # 归一化翻译并发线程数：CLI 默认 4，但 API/编程方式调用时 thread 可能为 0/None，
    # TranslateConverter 内部 ThreadPoolExecutor(max_workers=0) 会抛 ValueError，
    # 导致整页翻译失败（并行路径中表现为 worker 崩溃、串行路径整份 PDF 空白）。
    thread = thread if thread and thread > 0 else 4
    parallel_pages, parallel_workers = _resolve_parallel_settings(
        parallel_pages, parallel_workers, default_pages=True
    )

    font_list = [("tiro", None)]

    font_path = download_remote_fonts(lang_out.lower())
    # Phase 1: Style-aware font resolver
    font_resolver = FontResolver(lang_out)
    noto_name = NOTO_NAME
    noto = Font(noto_name, font_path)
    font_list.append((noto_name, font_path))

    doc_en = Document(stream=stream)
    doc_zh = None
    # GC/leak guard: every exception path must release the two MuPDF
    # documents (native memory). Passthrough's inner finally closes
    # them first; outer close below is idempotent via ``is_closed``.
    try:
        stream = io.BytesIO()
        doc_en.save(stream)
        doc_zh = Document(stream=stream)
        page_count = doc_zh.page_count
        logger.info(
            "translate_stream: loaded %d pages, starting patch phase...", page_count
        )

        # V3 passthrough: 全文档无可提取文本（扫描件 / 纯矢量 / 纯图片）时跳过
        # 字体嵌入、翻译与补丁，直接压缩写出。原路径会把全量 SourceHanSerif 字体
        # （~9-14MB）嵌入到没有任何文本使用的输出中，导致体积膨胀 10-20 倍
        # （实测 603KB -> 9.6MB，xref 中单个字体流解压后 14MB）。
        if page_count > 0 and not any(_page.get_text().strip() for _page in doc_en):
            logger.warning(
                "translate_stream: no extractable text across %d page(s); "
                "running passthrough mode (no translation, output mirrors input)",
                page_count,
            )
            _pt_start = time.perf_counter()
            try:
                # clean=True 会触发 MuPDF 内容流消毒器，实测会把 converter 生成的
                # 文本指令重排破坏（Tf/Tm 丢失、TJ 脱离 BT/ET 块），导致输出整页
                # 空白（文本层与视觉层同时丢失）。deflate/garbage 已足够压缩。
                doc_dual = doc_zh.write(deflate=True, garbage=4, use_objstms=1)
                doc_mono = doc_en.write(deflate=True, garbage=4, use_objstms=1)
            finally:
                doc_en.close()
                doc_zh.close()
            if callable(progress_cb):
                try:
                    progress_cb(
                        100.0,
                        f"No extractable text ({page_count} page(s)); passthrough",
                    )
                except Exception:
                    pass
            logger.info(
                "translate_stream: passthrough complete (mono=%d bytes, dual=%d bytes, %.1fs)",
                len(doc_mono),
                len(doc_dual),
                time.perf_counter() - _pt_start,
            )
            return (doc_dual, doc_mono)
        import sys as _sys_init

        _sys_init.stdout.flush()
        # Phase 1: Document-level font cache
        font_cache = DocumentFontCache(doc_zh)
        registered_font_name = font_cache.register(font_path)
        # font_list = [("GoNotoKurrent-Regular.ttf", font_path), ("tiro", None)]
        # === 8.1.1 字体嵌入重构：O(N×F) → O(F) ===
        # 旧实现逐页 `insert_font`（`for page in doc_zh: for font in font_list`），
        # 每页从磁盘重载同一字体文件（`fz_new_font_from_file`）并嵌入一份副本，
        # 1918 次调用累计 18.5s（其中 7.5s 磁盘重载 14MB 字体 + 9.66s 逐副本嵌入）。
        # 重构要点：
        #   1) `fontbuffer` 一次读入内存，避开按路径重载（`fz_new_font_from_file`）；
        #   2) `insert_font` 只在文档第一页调用一次，拿到唯一 font_id；
        #   3) 保留下方 xref 共享广播循环（把字体引用写入各页 `Resources/Font`）。
        # 空文档（page_count==0）跳过嵌入；xref 广播循环此时因 font_id 为空由
        # `except Exception` 兜底（与旧行为一致）。
        font_id = {}
        if doc_zh.page_count > 0:
            _font_buffer = None
            try:
                with open(font_path, "rb") as _fb:
                    _font_buffer = _fb.read()
            except OSError:
                _font_buffer = None
            _first_page = doc_zh[0]
            for _fname, _fpath in font_list:
                if _fpath:
                    # 有真实字体文件 → 优先 fontbuffer 单次嵌入
                    if _font_buffer is not None:
                        try:
                            font_id[_fname] = _first_page.insert_font(
                                _fname, fontbuffer=_font_buffer
                            )
                            continue
                        except Exception:  # noqa: BLE001 -- buffer 路径失败回退路径加载
                            logger.debug(
                                "insert_font(fontbuffer=) failed for %s; retry via path",
                                _fname,
                            )
                    font_id[_fname] = _first_page.insert_font(_fname, _fpath)
                else:
                    # 内置字体（tiro）按名称嵌入
                    font_id[_fname] = _first_page.insert_font(_fname, None)
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
                logger.warning(
                    "TextMetrics init failed (falling back to legacy width): %s", e
                )

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
        if "text_metrics" not in dir():
            text_metrics = {}
        if "translation_cache_obj" not in dir():
            translation_cache_obj = None

        # === 2.0: Parallel page processing (L2) ===
        page_xref_map = None
        if parallel_pages and page_count > 5:
            # P2（V3）：主进程单写者预热 —— 确保 doclayout 模型与 optimized 缓存就绪，
            # worker 的 OnnxModel 加载直接命中 cached，绝无并发写竞争；预热失败时记录
            # 并跳过并行（等价于整体串行兜底），不让预热异常重复进 worker 初始化。
            from pdf2zh.doclayout import DocLayoutModel
            from pdf2zh.parallel.errors import ParallelError

            if not DocLayoutModel.ensure_model_prewarmed():
                logger.warning(
                    "Layout model prewarm failed; skipping page parallelism "
                    "(serial fallback)."
                )
                obj_patch = translate_patch(fp, **dict(locals()))
            else:
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
                    logger.warning(
                        "Failed to pre-create page xrefs (%s), falling back to serial",
                        str(px_err)[:120],
                    )
                    page_xref_map = None
                    obj_patch = translate_patch(fp, **dict(locals()))
                else:
                    try:
                        obj_patch = _translate_parallel(
                            fp,
                            dict(locals()),
                            workers=parallel_workers,
                            page_xref_map=page_xref_map,
                        )
                    except KeyboardInterrupt:
                        # V3（§5.4/§5.5）：Ctrl+C 绝不进入串行兜底 —— 直接传播给上层
                        # 关闭流程（GUI 优雅关闭 / CLI 退出），由上层负责 worker 回收。
                        raise
                    except ParallelError as parallel_err:
                        # V3（§5.5）语义化兜底：池整体不可用（bootstrap / 协议违例）
                        # 才整文档串行重跑；chunk 级失败已在 coordinator 内增量补跑。
                        logger.warning(
                            "Parallel engine degraded cleanly (%s: %s); "
                            "full serial fallback",
                            type(parallel_err).__name__,
                            str(parallel_err)[:120],
                        )
                        obj_patch = translate_patch(fp, **dict(locals()))
                    except (Exception, SystemExit) as parallel_err:
                        logger.warning(
                            "Parallel page processing failed (%s), falling back to serial: %s "
                            "(tip: fix the GPU backend or disable page parallelism with "
                            "--backend cpu / parallel_pages=False)",
                            type(parallel_err).__name__,
                            str(parallel_err)[:120],
                        )
                        # 并发 GPU session 冲突（多 worker 同时建 DirectML/CUDA session）
                        # 是 worker 原生崩溃最常见的诱因；在真正降级 CPU 之前，先用更少
                        # 的 worker 重试一次整个翻译（仍然并行架构，只是并发度减半）。
                        if isinstance(parallel_err, BrokenProcessPool):
                            try:
                                retry_workers = max(1, (parallel_workers or 4) // 2)
                                logger.info(
                                    "Parallel crash detected; retrying the whole task "
                                    "with %d worker(s) before degrading to CPU...",
                                    retry_workers,
                                )
                                obj_patch = _translate_parallel(
                                    fp,
                                    dict(locals()),
                                    workers=retry_workers,
                                    page_xref_map=page_xref_map,
                                )
                                parallel_err = None
                                logger.info(
                                    "Reduced-worker parallel retry succeeded; "
                                    "continuing without CPU degradation."
                                )
                            except (Exception, SystemExit) as retry_err:
                                parallel_err = retry_err
                                logger.warning(
                                    "Reduced-worker retry also failed (%s), degrading to CPU.",
                                    type(retry_err).__name__,
                                )
                        if parallel_err is not None:
                            # 自动降级：worker 进程被终止（BrokenProcessPool）时把后端切到 CPU，
                            # 并让本次串行回退也按 CPU provider 重新加载模型，保证
                            # "降级即生效"，且崩溃后的 GPU session 不再参与本任务。
                            degraded = _degrade_backend_on_crash(
                                parallel_err,
                                progress_cb=dict(locals()).get("progress_cb"),
                                context=f"pages={page_count} workers={parallel_workers}",
                            )
                            if degraded:
                                from pdf2zh.doclayout import (
                                    ModelInstance as _RemodelInst,
                                )
                                from pdf2zh.doclayout import OnnxModel as _RemodelOnnx

                                try:
                                    _RemodelInst.value = _RemodelOnnx.load_available()
                                except Exception as _remodel_err:
                                    logger.warning(
                                        "CPU model reload after degradation failed: %s",
                                        str(_remodel_err)[:120],
                                    )
                                # 覆盖本次的 model local，让串行回退使用 CPU 模型；
                                # 若重新加载失败，退回原 model（可能仍可用），
                                # 总比 model=None 在 translate_patch 里崩溃强。
                                model = _RemodelInst.value or locals().get("model")
                            # Serial fallback: use locals directly (all objects available in current process)
                            obj_patch = translate_patch(fp, **dict(locals()))
        else:
            obj_patch = translate_patch(fp, **dict(locals()))

        # Phase D: 并行路径的可观测 payload 经 __obs__ 私有键回传，这里并入 v3_output
        if (
            v3_output is not None
            and isinstance(obj_patch, dict)
            and "__obs__" in obj_patch
        ):
            v3_output["observability"] = obj_patch.pop("__obs__")

        total_objs = len(obj_patch)
        for idx, (obj_id, ops_new) in enumerate(obj_patch.items()):
            try:
                # Validate that the obj_id references a dict/stream before updating
                xref_type = doc_zh.xref_object(obj_id, compressed=True)
                if not xref_type.startswith("<<"):
                    logger.warning(
                        "Skipping obj_id %s: not a PDF dict (xref_object starts with %r)",
                        obj_id,
                        xref_type[:40],
                    )
                    continue
                doc_zh.update_stream(obj_id, ops_new.encode())
            except ValueError as ve:
                logger.warning(
                    "Skipping obj_id %s (ValueError: %s) — common for non-stream objects",
                    obj_id,
                    str(ve)[:80],
                )
            except Exception as stream_err:
                logger.warning(
                    "Skipping obj_id %s update_stream error: %s",
                    obj_id,
                    str(stream_err)[:120],
                )
            if idx % 5 == 0 or idx == total_objs - 1:
                logger.info(
                    "translate_stream: updated stream %d/%d (%.0f%%)",
                    idx + 1,
                    total_objs,
                    (idx + 1) / total_objs * 100,
                )

        # 并行模式下 worker 进程不会修改主进程 doc_zh 的页面 /Contents，
        # 这里统一将每个页面指向其新的（已写入译文指令流的）内容流对象。
        if page_xref_map:
            for _px_pageno, _px_xref in page_xref_map.items():
                try:
                    doc_zh[_px_pageno].set_contents(_px_xref)
                except Exception as se:
                    logger.warning(
                        "set_contents failed for page %s (xref %s): %s",
                        _px_pageno,
                        _px_xref,
                        str(se)[:80],
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
                        link_stats["relinked"],
                        link_stats["pages"],
                    )
            except Exception as relink_err:
                logger.warning(
                    "translate_stream: link relink skipped: %s", str(relink_err)[:160]
                )

        # === V8.6: 图片翻译 + 内容保护决策的 side-channel（仅采集回传，不改渲染） ===
        if emit_preservation:
            try:
                pres_stats = _collect_preservation_side_channel(
                    doc_zh,
                    v3_output,
                    image_engine=image_engine,
                    content_preservation=content_preservation,
                    image_render=image_render,
                )
                if pres_stats and pres_stats["objects"]:
                    logger.info(
                        "translate_stream: preservation decided %d image objects "
                        "(translate=%d preserve=%d overlay=%d)",
                        pres_stats["objects"],
                        pres_stats["translated"],
                        pres_stats["preserved"],
                        pres_stats["overlay"],
                    )
            except Exception as pres_err:
                logger.warning(
                    "translate_stream: preservation skipped: %s", str(pres_err)[:160]
                )

        logger.info("=" * 60)
        logger.info(
            "translate_stream: MERGING %d pages (this may take a while for large PDFs)...",
            page_count,
        )
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
            logger.info(
                "translate_stream: insert_file OK (%.1fs), reordering %d pages...",
                _insert_elapsed,
                page_count,
            )
            _sys.stdout.flush()
            for id in range(page_count):
                doc_en.move_page(page_count + id, id * 2 + 1)
                if id % 5 == 0 or id == page_count - 1:
                    logger.info(
                        "translate_stream: moved page %d/%d (%.1f%% done)",
                        id + 1,
                        page_count,
                        (id + 1) / page_count * 100,
                    )
                    _sys.stdout.flush()
                    _sys.stderr.flush()
            _merge_total = _merge_time.time() - _merge_start
            logger.info(
                "translate_stream: page merge complete (%d pages, %.1fs total)",
                page_count,
                _merge_total,
            )
        except Exception as merge_err:
            logger.error(
                "translate_stream: page merge failed after %.1fs: %s",
                _merge_time.time() - _merge_start,
                merge_err,
            )
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
                            doc.xref_set_key(
                                xref, "/Length", doc.xref_get_key(xref, "/Length")[1]
                            )
                    except Exception:
                        pass
                    try:
                        basefont_res = doc.xref_get_key(xref, "/BaseFont")
                        if basefont_res[0] == "name":
                            bf = str(basefont_res[1])
                            math_patterns = [
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
                            ]
                            for mp in math_patterns:
                                if mp in bf:
                                    doc.xref_set_key(
                                        xref,
                                        "/Length",
                                        doc.xref_get_key(xref, "/Length")[1],
                                    )
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
                logger.warning(
                    "subset_fonts failed for doc_zh: %s", str(subset_err)[:120]
                )
            logger.info("translate_stream: subsetting doc_en fonts...")
            try:
                doc_en.subset_fonts(fallback=False)
                logger.info("translate_stream: doc_en subset_fonts complete")
            except Exception as subset_err:
                logger.warning(
                    "subset_fonts failed for doc_en: %s", str(subset_err)[:120]
                )
        # === 书签（/Outlines）：翻译标题并重建到 mono/dual 文档（P0-3） ===
        # 在子集化之后、写出之前重建，避免子集化影响新写入的 outline 对象。
        # Commit 6D：优先从语义 TOC（document_model 的结构化 toc_entries）
        # 生成 Outline——标题已是译后，destination 用 destination_page
        # （1-base，见 outline_renderer）。没有 document_model 时回退既有
        # native outline 翻译路径。失败仅告警，绝不阻断写出。
        _bm_source, _bm_count = "none", 0
        _semantic_entries = []
        try:
            if v3_output.get("document_model"):
                from pdf2zh.v3.outline_renderer import extract_outline_entries

                _semantic_entries = extract_outline_entries(
                    v3_output.get("document_model")
                )
        except Exception:  # noqa: BLE001 -- 语义 outline 缺失回退 native
            _semantic_entries = []
        try:
            _bm = _apply_bookmarks(
                doc_zh,
                doc_en,
                stream.getvalue(),
                service=service,
                lang_in=lang_in,
                lang_out=lang_out,
                envs=envs,
                prompt=prompt,
                ignore_cache=ignore_cache,
                semantic_toc_entries=_semantic_entries,
            )
            _bm_source, _bm_count = _bm.get("source", "none"), _bm.get("count", 0)
        except Exception as e:  # noqa: BLE001
            logger.warning("bookmarks: apply failed: %s", str(e)[:160])
            _bm_source, _bm_count = "none", 0
        if v3_output is not None:
            v3_output["outline"] = {
                "source": _bm_source,
                "count": _bm_count,
            }
        logger.info("translate_stream: writing doc_zh (dual) PDF bytes...")
        try:
            _write_start = _merge_time.time()
            # clean=True 会触发 MuPDF 内容流消毒器，实测会把 converter 生成的
            # 文本指令重排破坏（Tf/Tm 丢失、TJ 脱离 BT/ET 块），导致输出整页
            # 空白（文本层与视觉层同时丢失）。deflate/garbage 已足够压缩。
            doc_dual = doc_zh.write(deflate=True, garbage=4, use_objstms=1)
            logger.info(
                "translate_stream: doc_zh write OK (size=%d bytes, %.1fs)",
                len(doc_dual),
                _merge_time.time() - _write_start,
            )
        except Exception as write_err:
            logger.error("translate_stream: doc_zh write failed: %s", write_err)
            raise
        logger.info("translate_stream: writing doc_en (mono) PDF bytes...")
        try:
            _write_start = _merge_time.time()
            doc_mono = doc_en.write(deflate=True, garbage=4, use_objstms=1)
            logger.info(
                "translate_stream: doc_en write OK (size=%d bytes, %.1fs)",
                len(doc_mono),
                _merge_time.time() - _write_start,
            )
        except Exception as write_err:
            logger.error("translate_stream: doc_en write failed: %s", write_err)
            raise
        logger.info(
            "translate_stream: write complete (mono=%d bytes, dual=%d bytes, total=%.1fs)",
            len(doc_mono),
            len(doc_dual),
            _merge_time.time() - _merge_start,
        )
        # V1.19: TOC 观察报告落盘（PDF2ZH_TOC_REPORT=1；无环境变量时零开销）
        if os.environ.get("PDF2ZH_TOC_REPORT", "") == "1":
            try:
                _toc_reports = getattr(device, "_toc_reports", None) or []
                if _toc_reports:
                    _reports_path = (
                        stream.name if hasattr(stream, "name") and stream.name else None
                    )
                    if _reports_path:
                        _dump_base = os.path.splitext(_reports_path)[0]
                    else:
                        _dump_base = "pdf2zh_toc_report"
                    _dump_path = f"{_dump_base}.toc_report.json"
                    with open(_dump_path, "w", encoding="utf-8") as _rf:
                        json.dump(_toc_reports, _rf, ensure_ascii=False, indent=1)
                    logger.info(
                        "translate_stream: TOC report written (%d entries) -> %s",
                        len(_toc_reports),
                        _dump_path,
                    )
            except Exception as _dump_err:
                logger.warning(
                    "translate_stream: TOC report dump failed: %s", str(_dump_err)[:120]
                )
        return (doc_dual, doc_mono)
    finally:
        if doc_en is not None and not getattr(doc_en, "is_closed", False):
            doc_en.close()
        if doc_zh is not None and not getattr(doc_zh, "is_closed", False):
            doc_zh.close()
        logger.info("translate_stream: documents closed")


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


def _init_worker_process(backend: str = None):
    """Initialize worker process: load layout model once into global singleton.

    Called once per worker in ProcessPoolExecutor(initializer=...).
    ``backend`` propagates the parent's execution-provider choice so spawned
    workers do not silently re-detect a GPU provider (e.g. DirectML) while the
    parent runs on CPU -- the classic cause of worker-process crashes
    (BrokenProcessPool) on GPU machines.

    V3 iteration: 实现已迁移至 ``pdf2zh.parallel.worker.init_worker_process``
    （增补 ORT 线程门控、DLL 预注册与 bootstrap 失败语义化）；此处保留兼容
    外壳，供现有 executor / 旧测试继续按原签名引用。
    """
    from pdf2zh.parallel.worker import init_worker_process

    return init_worker_process(backend)


def _degrade_backend_on_crash(
    err: Exception,
    progress_cb: object = None,
    context: str = "",
) -> bool:
    """BrokenProcessPool 自动降级到 CPU（幂等，可经 set_backend("auto") 恢复）。

    worker 进程被终止（BrokenProcessPool）通常源于 spawn 出的 worker 在
    DirectML/CUDA 推理时进程级崩溃（原生崩溃或显存耗尽），Python 无法捕获
    具体原因。本次任务已由调用方回退串行；这里把模块级后端降级为 CPU，
    使后续翻译任务（GUI 连续操作、CLI 批处理、服务复用进程）不再重复触发
    GPU 并行崩溃。

    注意：降级是"一次性"的——只在第一次 BrokenProcessPool 时生效；之后用户
    显式 ``set_backend("auto"/"dml"/"cuda")`` 会清除标记重新尝试 GPU。
    降级事件会经 ``progress_cb`` 上报给上层（GUI 进度/日志面板），
    而不是只写在 log 里。
    """
    from pdf2zh.doclayout import (
        get_backend,
        mark_cpu_degraded,
        release_model_instance,
    )

    prev_backend = get_backend()
    if isinstance(err, BrokenProcessPool) and mark_cpu_degraded():
        # 主进程可能已缓存 GPU session（ModelInstance 全局单例），重置为 None，
        # 使后续任何路径（worker spawn / 串行回退 / 新任务）都按 CPU provider
        # 重新加载。release_model_instance 附带 gc.collect + CUDA empty_cache，
        # 避免旧 GPU session 的显存滞留到新 session 加载之后。
        release_model_instance()
        msg = (
            "GPU-backed parallel workers crashed; execution provider degraded "
            "to CPU for this and subsequent translation tasks "
            "(fix the GPU backend, or re-run with --backend auto to retry GPU)."
        )
        if context:
            msg = f"{msg} context: {context}"
        logger.warning("%s previous backend: %s", msg, prev_backend)
        if callable(progress_cb):
            try:
                progress_cb(0.0, msg)
            except Exception:
                pass
        return True
    return False


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
    cancel_event: object = None,
    # --- V9.0/V11 side-channel scalar params（与串行路径一致） ---
    processor_channels: bool = True,
    render_takeover: bool = False,
    translation_qa: bool = False,
    geometry_cluster: bool = False,
    toc_split: bool = True,
    pipeline_dump: bool = False,
    document_model: bool = False,
    observability: bool = False,
    # P5–P10 主链路接管（阶段 3）：渲染前接管 + 完整对象存档（标量透传）
    reconstruction_channel: bool = True,
    reconstruction_adopt: bool = True,
    # 8.1.2: pages 子集透传（worker 侧与 chunk_pages 取交集）
    pages: tuple = None,
) -> dict:
    """Process a chunk of pages in a separate process (module-level for pickling).

    Only lightweight scalar parameters are passed across process boundary.
    Heavy C-extension objects (pymupdf.Document, OnnxModel, FontResolver, etc.)
    are reconstructed inside the worker process from fp_bytes and the
    global ModelInstance singleton. This avoids SwigPyObject pickle errors.

    V3 iteration: 实现已迁移至 ``pdf2zh.parallel.worker.execute_chunk``；
    此处保留原签名兼容外壳（供既有调用方 / 旧测试直接引用）。
    """
    from pdf2zh.parallel.chunk import ChunkTask
    from pdf2zh.parallel.errors import PageProcessingError
    from pdf2zh.parallel.worker import execute_chunk

    task = ChunkTask(
        chunk_pages=tuple(chunk_pages),
        fp_bytes=fp_bytes,
        page_xref_map=page_xref_map,
        cancel_event=cancel_event,
        lang_in=lang_in,
        lang_out=lang_out,
        service=service,
        thread=thread,
        vfont=vfont,
        vchar=vchar,
        noto_name=noto_name,
        font_path=font_path,
        skip_subset_fonts=skip_subset_fonts,
        ignore_cache=ignore_cache,
        use_text_metrics=use_text_metrics,
        use_translation_cache=use_translation_cache,
        envs_str=envs_str,
        prompt_template=prompt_template,
        processor_channels=processor_channels,
        render_takeover=render_takeover,
        translation_qa=translation_qa,
        geometry_cluster=geometry_cluster,
        toc_split=toc_split,
        pipeline_dump=pipeline_dump,
        document_model=document_model,
        observability=observability,
        reconstruction_channel=reconstruction_channel,
        reconstruction_adopt=reconstruction_adopt,
        pages=pages,
    )
    result = execute_chunk(task)
    if not result.ok:
        raise PageProcessingError(result.error_message)
    return _translate_parallel_chunk_result(result.obj_patch)


def _translate_parallel_chunk_result(result):
    """拆分 chunk 返回值：obj_patch + 可观测 bundle（并行侧通道回传）。"""
    obs = None
    if isinstance(result, dict) and "__obs__" in result:
        obs = result.pop("__obs__")
        result = dict(result)
    return result, obs


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
    (pymupdf.Document, OnnxModel, FontResolver, TextMetrics, etc.) are
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
    # === 8.1.2 并行路径 pages 过滤修复 ===
    # 旧实现按 `range(doc_zh.page_count)` 全量切分 chunk，`pages` 子集过滤只在
    # 串行 `translate_patch` 生效 → 并行下请求 `--pages 0-19` 会翻译整本 959 页
    # （实测 926s）。此处按调用方 `pages` 参数预过滤后再切分 chunk，并透传给
    # worker（scalar_args["pages"]）作为第二道防线（ChunkTask.pages 交集过滤）。
    target_pages = locals_dict.get("pages")
    valid_pages = (
        [p for p in target_pages if 0 <= p < doc_zh.page_count]
        if target_pages is not None
        else list(range(doc_zh.page_count))
    )
    chunk_size = max(1, len(valid_pages) // workers)
    chunks = [
        valid_pages[i : i + chunk_size] for i in range(0, len(valid_pages), chunk_size)
    ]

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
        "processor_channels": locals_dict.get("processor_channels", True),
        "render_takeover": locals_dict.get("render_takeover", False),
        "translation_qa": locals_dict.get("translation_qa", False),
        "geometry_cluster": locals_dict.get("geometry_cluster", False),
        "toc_split": locals_dict.get("toc_split", True),
        "pipeline_dump": locals_dict.get("pipeline_dump", False),
        "document_model": locals_dict.get("document_model", False),
        "observability": locals_dict.get("observability", False),
        "reconstruction_channel": locals_dict.get("reconstruction_channel", True),
        "reconstruction_adopt": locals_dict.get("reconstruction_adopt", True),
        # 8.1.2: pages 子集透传（worker 与 chunk_pages 取交集，防御其他路径丢失）
        "pages": tuple(valid_pages) if target_pages is not None else None,
    }

    obj_patch = {}
    obs_bundles: list = []
    progress_cb = locals_dict.get("progress_cb")
    from pdf2zh.doclayout import get_backend

    # S4: 跨进程取消桥 —— threading.Event / mp.Event 均不可 pickle
    # （Python 3.12+ spawn 下报 "Condition objects should only be shared..."），
    # 取消信号改用 pickle-safe 的 CancelToken（临时目录标记文件）：父进程起一个
    # 轻量 daemon 线程轮询调用方 cancellation_event，触发后 set() 写标记文件，
    # 各 worker 的页循环经 is_set()（每页一次 stat）感知，≤0.5s 内到达。
    from pdf2zh.parallel.chunk import CancelToken

    _cancel_event_arg = locals_dict.get("cancellation_event")
    if _cancel_event_arg is not None:
        import threading as _thr

        _shared_cancel = CancelToken()
        _bridge_stop = _thr.Event()

        def _cancel_bridge() -> None:
            try:
                while not _bridge_stop.is_set():
                    if _cancel_event_arg.is_set():
                        _shared_cancel.set()
                        break
                    _bridge_stop.wait(0.5)
            except Exception:  # noqa: BLE001 -- bridge failure never blocks spawn
                pass

        _bridge = _thr.Thread(target=_cancel_bridge, daemon=True)
        _bridge.start()
    else:
        _shared_cancel = None
        _bridge_stop = None
        _bridge = None

    # V3 iteration: Bounded in-flight 窗口调度 + 有限重试 + 增量降级（P3）。
    from pdf2zh.parallel.chunk import ChunkTask
    from pdf2zh.parallel.coordinator import TaskCoordinator
    from pdf2zh.parallel.errors import PageProcessingError

    chunk_tasks = [
        ChunkTask(
            chunk_pages=tuple(chk),
            fp_bytes=fp_bytes,
            page_xref_map=page_xref_map,
            cancel_event=_shared_cancel,
            **scalar_args,
        )
        for chk in chunks
    ]
    coordinator = TaskCoordinator(max_workers=workers)
    # 8.2.1 Warm Process Pool（PDF2ZH_WARM_POOL=1）：复用进程级常驻池，
    # 避免每次任务重新 spawn + 模型加载（实测 8.2s，约占总耗时 29%）。
    # reuse_executor=True 令协调器任务结束后不 shutdown 共享池；中断/异常
    # 时由 pool_owner 标记 broken，下次任务自动重建。未启用时行为与旧实现
    # 完全一致（每次任务新建池）。
    shared_pool = None
    try:
        from pdf2zh.parallel.pool import get_shared_pool  # noqa: PLC0415

        shared_pool = get_shared_pool(workers, get_backend())
    except Exception as pool_init_err:  # noqa: BLE001 -- 池初始化失败回落新建池
        logger.warning(
            "Warm pool unavailable (%s); falling back to per-task pool",
            str(pool_init_err)[:120],
        )
        shared_pool = None

    if shared_pool is not None:

        def _shared_pool_factory(_mw, _initializer, _initargs):  # noqa: ANN001
            return shared_pool.get()

        obj_patch, obs_bundles, serial_indices = coordinator.run(
            chunk_tasks,
            progress_cb=progress_cb,
            executor_factory=_shared_pool_factory,
            reuse_executor=True,
            pool_owner=shared_pool,
        )
    else:
        obj_patch, obs_bundles, serial_indices = coordinator.run(
            chunk_tasks,
            progress_cb=progress_cb,
            initializer=_init_worker_process,
            initargs=(get_backend(),),
        )

    # 增量降级：只有失败 chunk 走串行补跑，绝不整文档重跑（V3 §5.4）
    try:
        if serial_indices:
            logger.warning(
                "Incremental serial fallback for %d chunk(s): %s",
                len(serial_indices),
                serial_indices,
            )
            for idx in serial_indices:
                try:
                    chunk_result, obs_bundle = _translate_parallel_chunk(
                        chunks[idx],
                        fp_bytes,
                        page_xref_map=page_xref_map,
                        cancel_event=_shared_cancel,
                        **scalar_args,
                    )
                except KeyboardInterrupt:
                    raise
                except Exception as serial_err:
                    logger.error(
                        "Serial fallback for chunk %d failed (%s); deferring to "
                        "outer full-serial fallback",
                        idx,
                        str(serial_err)[:160],
                    )
                    raise PageProcessingError(
                        f"chunk {idx} serial fallback failed: {serial_err}"
                    ) from serial_err
                if chunk_result:
                    obj_patch.update(chunk_result)
                if obs_bundle:
                    obs_bundles.append(obs_bundle)

        if obs_bundles:
            merged = obs_bundles[0]
            for extra in obs_bundles[1:]:
                merged["bundle"]["snapshots"]["snapshots"].update(
                    (extra.get("bundle") or {})
                    .get("snapshots", {})
                    .get("snapshots", {})
                )
                merged["overlays"].extend(extra.get("overlays", []))
            obj_patch["__obs__"] = merged
    finally:
        if _bridge_stop is not None:
            _bridge_stop.set()  # stop the cancellation mirror (daemon, best effort)
        if isinstance(_shared_cancel, CancelToken):
            _shared_cancel.clear()  # 清理取消标记文件（尽力而为；异常传播时也执行）
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
    parallel_pages: Optional[bool] = None,
    parallel_workers: Optional[int] = None,
    use_text_metrics: bool = True,
    use_translation_cache: bool = True,
    **kwarg: Any,
):
    if not files:
        raise PDFValueError("No files to process.")

    parallel_pages, parallel_workers = _resolve_parallel_settings(
        parallel_pages, parallel_workers, default_pages=False
    )

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
                r = requests.get(file, allow_redirects=True, timeout=(15, 60))
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
        if output:
            os.makedirs(output, exist_ok=True)
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
