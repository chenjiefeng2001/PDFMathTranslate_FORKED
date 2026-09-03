"""Step 2.3 — ``--parse-engine magicpdf`` CLI 执行器。

在既有 CLI 的 Parse 层旁路打开 MinerU/magic-pdf 解析链路：::

    MagicPdfAdapter.parse -> MagicPdfBridge -> DocumentModel
    -> translate_document（复用 build_translator）-> render_plan

- 未安装引擎 / 解析异常 -> 自动降级回 legacy 内核（熔断，Step 3.3）；
- 产物：``{output}/magicpdf/{stem}_magicpdf.json``（解析结果）、
  ``{stem}_document.json``（DocumentModel 转储），以及（默认开启，可
  ``--no-magicpdf-render`` 关闭）经 RenderTakeover 修正渲染计划后渲染的
  译后 mono PDF ``{stem}_mono.pdf``（§12.3 渲染接管）。
"""

from __future__ import annotations

import inspect
import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


class MagicPdfDegradeError(Exception):
    """magicpdf 引擎需要按模式降级（BabelDOC）而非 legacy 的信号。

    服务层在矛盾组合（``mode_choice=babeldoc`` + ``parse_engine=magicpdf``）
    下传入 ``run_magicpdf_main(..., degrade_to="babeldoc")``：MinerU 不可用
    或解析失败时不再静默降级 legacy —— 那会让用户「看着 BabelDOC 模式却跑
    legacy 且无逐页进度 → 直接卡死」。改为抛本异常，由服务层改走
    ``_execute_babeldoc`` 让用户选择的模式真正兜底。CLI 默认不传该参数，
    保持原有 legacy 降级语义。
    """


def _output_dir(parsed_args) -> str:
    out = parsed_args.output or "."
    magic_dir = os.path.join(out, "magicpdf")
    os.makedirs(magic_dir, exist_ok=True)
    return magic_dir


def _fallback_legacy(parsed_args, reason: str, progress_cb=None) -> int:
    """熔断降级：记录原因后按 legacy 内核重跑（Step 3.3）。

    打 ``_magicpdf_fallback`` 防重入标记：legacy 内核的文本层预检看到该
    标记后不再自动切回 magicpdf 引擎（本进程中 magic-pdf 已被证实不可用），
    避免 magicpdf → legacy → (auto-switch) → magicpdf 的乒乓循环。

    ``progress_cb``（可选）：降级事件显式上报。旧版降级后 legacy 在服务
    进程内默默翻译，UI 永远停在解析期最后一个百分比（任务「假死」在
    ~38%）——这里把降级事实作为进度事件透传到前端日志面板：进度不回退
    （服务层 ``_emit_smooth`` 钳制），但用户能看到引擎已切换、当前在做什么。
    """
    parsed_args._magicpdf_fallback = True
    logger.warning("[magicpdf] %s —— 自动降级回 legacy 内核重试。", reason)
    if progress_cb is not None:
        try:
            progress_cb(
                "analyzing",
                _PCT_PARSE_START,
                "[降级] {}：MinerU/magic-pdf 不可用，自动切换 legacy 内核重试"
                "（legacy 翻译期间进度不再逐页刷新）".format(reason),
            )
        except Exception:  # noqa: BLE001 -- 进度上报永不致命
            pass
    from pdf2zh.pdf2zh import _run_legacy_kernel

    return _run_legacy_kernel(parsed_args)


def _degrade_engine(parsed_args, reason: str, progress_cb=None, degrade_to=None) -> int:
    """按降级目标路由：``degrade_to="babeldoc"`` 抛 :class:`MagicPdfDegradeError`
    由服务层改走 BabelDOC 执行器；否则（默认）走 :func:`_fallback_legacy`。

    矛盾配置（magicpdf 解析引擎 + BabelDOC 模式）下，MinerU 不可用/解析失败
    时若静默降级 legacy，用户会看到「选 BabelDOC 却毫无 BabelDOC 痕迹且进度
    不再刷新」——即「直接卡死」的体感。降级目标显式化 + 事件上报可消除该盲区。
    """
    if degrade_to == "babeldoc":
        if progress_cb is not None:
            try:
                progress_cb(
                    "analyzing",
                    _PCT_PARSE_START,
                    "[降级] {}：MinerU/magic-pdf 不可用，按模式（BabelDOC）"
                    "切换 BabelDOC 引擎重试".format(reason),
                )
            except Exception:  # noqa: BLE001 -- 进度上报永不致命
                pass
        raise MagicPdfDegradeError(reason)
    return _fallback_legacy(parsed_args, reason, progress_cb=progress_cb)


def _preload_torch() -> bool:
    """torch 预载（Windows DLL 加载顺序防御），返回是否导入成功。

    onnxruntime 的 CUDA/TensorRT 执行级探测会先加载 ORT 自带的 cuDNN
    DLL；之后同一进程再 ``import torch`` 时，Windows 加载器解析到已驻留
    的冲突 DLL，``cudnn_cnn64_9.dll`` 报 WinError 127。magic-pdf 1.x 全部
    子模型为 PyTorch 实现，这里在解析前先把 torch 导入 ``sys.modules``
    即可规避顺序冲突；失败不阻断（后续按既有降级路径处理）。
    """
    try:
        import torch  # noqa: F401 -- 提前驻留 sys.modules 防 DLL 冲突

        return True
    except Exception as exc:  # noqa: BLE001 -- torch 缺失/损坏交由上层降级
        logger.debug("[magicpdf] torch preload failed: %s", exc)
        return False


def _prompt_text(parsed_args) -> str | None:
    if not parsed_args.prompt:
        return None
    return (
        parsed_args.prompt.template
        if hasattr(parsed_args.prompt, "template")
        else parsed_args.prompt
    )


# ── 解析期细粒度进度（P1，doc/granular_progress_feasibility_report.md）────────
#
# 适配器的计数回调只携带结构化 detail（页计数/组件加载）；这里升格为完整的
# 进度事件 (stage, pct, msg, detail)：parsing 相位按页计数线性内插到 ~[10,55]，
# 翻译/渲染两个相位由 run_magicpdf_main 显式发粗事件。百分比单调不回退。


#: magicpdf 单文件管线在总体进度上的相位区间（粗粒度锚点）。
_PCT_PARSE_START = 10.0
_PCT_PARSE_END = 55.0
_PCT_TRANSLATE = 62.0
_PCT_RENDER = 85.0


def _make_parse_progress(progress_cb, path: str):
    """把适配器级 ``progress_cb(detail)`` 升格为完整事件回调（解析期）。"""
    if progress_cb is None:
        return None
    name = os.path.basename(path)
    state = {"pct": _PCT_PARSE_START}

    def _report(detail: dict) -> None:
        try:
            d = dict(detail)
            cur = int(d.get("current") or 0)
            tot = int(d.get("total") or 0)
            if d.get("unit") == "component" or tot <= 0:
                pct = state["pct"]
                msg = "{}: {}".format(name, d.get("component") or "preparing models...")
            else:
                frac = max(0.0, min(1.0, cur / tot))
                pct = _PCT_PARSE_START + frac * (_PCT_PARSE_END - _PCT_PARSE_START)
                msg = f"{name}: analyzing page {cur}/{tot}"
            state["pct"] = max(state["pct"], pct)
            progress_cb("analyzing", pct, msg, d)
        except Exception:  # noqa: BLE001 -- 进度上报永不致命
            pass

    return _report


def _write_dumps(
    pdf_path: str,
    results: list[Any],
    document: Any,
    magic_dir: str,
    channel: Any = None,
    fixed_plan: list[dict] | None = None,
) -> None:
    stem = os.path.splitext(os.path.basename(pdf_path))[0]
    parse_dump = os.path.join(magic_dir, f"{stem}_magicpdf.json")
    doc_dump = os.path.join(magic_dir, f"{stem}_document.json")
    with open(parse_dump, "w", encoding="utf-8") as fh:
        json.dump([r.to_dict() for r in results], fh, ensure_ascii=False, indent=2)
    with open(doc_dump, "w", encoding="utf-8") as fh:
        json.dump(document.to_dict(), fh, ensure_ascii=False, indent=2)
    logger.info("[magicpdf] parse dump: %s", parse_dump)
    logger.info("[magicpdf] document dump: %s", doc_dump)
    if channel is not None:
        channel_dump = os.path.join(magic_dir, f"{stem}_formula_channel.json")
        with open(channel_dump, "w", encoding="utf-8") as fh:
            fh.write(channel.to_json())
        logger.info("[magicpdf] formula channel dump: %s", channel_dump)
    if fixed_plan:
        plan_dump = os.path.join(magic_dir, f"{stem}_render_plan.json")
        with open(plan_dump, "w", encoding="utf-8") as fh:
            json.dump(fixed_plan, fh, ensure_ascii=False, indent=2)
        logger.info("[magicpdf] render plan dump: %s", plan_dump)


def _adapter_parse(adapter, path: str, pages, ocr: bool, progress_cb, lang=None):
    """防御性调用 ``adapter.parse``：旧版签名（无 progress_cb/lang 形参）兼容。

    第三方/测试代码可能 monkey-patch 或子类覆盖 ``parse`` 且不带新形参；
    按签名探测后再传 ``progress_cb``/``lang``，避免 TypeError 破坏解析主流程。
    """
    kwargs: dict = {"pages": pages, "ocr": ocr}
    if lang is not None:
        try:
            params = inspect.signature(adapter.parse).parameters
            if "lang" in params or any(
                p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
            ):
                kwargs["lang"] = lang
        except (TypeError, ValueError):
            pass
    if progress_cb is not None:
        try:
            params = inspect.signature(adapter.parse).parameters
            takes_cb = "progress_cb" in params or any(
                p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
            )
        except (TypeError, ValueError):  # pragma: no cover - 内置类兜底
            takes_cb = False
        if takes_cb:
            kwargs["progress_cb"] = progress_cb
    return adapter.parse(path, **kwargs)


def _write_ingest_dump(pdf_path: str, ingest_doc: Any, magic_dir: str) -> str:
    """Marker ingestion IR dump（{stem}_ingest.json）——双链路对照的原料。"""
    import json as _json

    stem = os.path.splitext(os.path.basename(pdf_path))[0]
    out = os.path.join(magic_dir, f"{stem}_ingest.json")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(ingest_doc.to_json(indent=2))
    logger.info(
        "[magicpdf] ingest dump: %s (%d pages, %d blocks, backend=%s)",
        out,
        ingest_doc.page_count,
        ingest_doc.block_count,
        ingest_doc.source_backend,
    )
    return out


def _marker_live_available() -> bool:
    """Whether the vendored Marker package can run live (presence probe).

    ``--marker-json`` 离线摄入不依赖它；live 摄入（MarkerBackend.ingest）
    需要 ``vendor/marker`` 已安装。探测只做 ``import marker`` 判断。
    """
    try:
        import marker  # noqa: F401 -- presence check only

        return True
    except Exception:  # noqa: BLE001 -- probe failures mean "not available"
        return False


def _run_marker_ingest(
    path: str, marker_json, marker_version, magic_dir: str, progress_cb=None
):
    """Marker ingestion → ``(ingest_doc, v3 DocumentModel)``.

    Shared by 强制 marker 模式与 auto 模式的回退路径；失败抛异常由调用方
    决定熔断（强制模式）或保留 MinerU 结果（auto 回退）。
    """
    from pdf2zh.v3.ingestion import MarkerBackend
    from pdf2zh.v3.ingestion.bridge import model_from_ingest_document

    if progress_cb is not None:
        try:
            progress_cb(
                "analyzing",
                _PCT_PARSE_START,
                f"{os.path.basename(path)}: Marker ingestion...",
            )
        except Exception:  # noqa: BLE001
            pass
    _backend = MarkerBackend(marker_version=marker_version)
    if marker_json:
        ingest_doc = _backend.ingest_json(marker_json, pdf_path=path)
    else:
        ingest_doc = _backend.ingest(path)
    doc = model_from_ingest_document(ingest_doc, default_font="")
    _write_ingest_dump(path, ingest_doc, magic_dir)
    return ingest_doc, doc


def run_magicpdf_main(
    parsed_args, progress_cb=None, degrade_to: str | None = None
) -> int:
    """magicpdf 解析引擎主流程（引擎不可用时自动降级）。

    ``progress_cb(stage, pct, msg, detail=None)``（可选）：解析期页级/
    组件级细粒度计数与翻译/渲染相位粗事件都经它上报；不传保持原行为。

    ``degrade_to``（可选）：降级目标。``"babeldoc"`` 时（服务层在
    ``mode_choice=babeldoc`` + ``parse_engine=magicpdf`` 矛盾组合下传入），
    MinerU 不可用/解析失败抛 :class:`MagicPdfDegradeError` 交由服务层改走
    BabelDOC；``None``（CLI 默认）保持原有 legacy 内核降级语义。
    """
    # torch 必须先于任何 onnxruntime CUDA 会话导入（DLL 加载顺序，见
    # _preload_torch docstring）；CLI 全局入口已不再无条件加载 doclayout
    # 模型，此处预载兜底覆盖 API/GUI 服务进程复用等其它进入形态。
    _preload_torch()
    from pdf2zh.magicpdf_adapter import MagicPdfAdapter
    from pdf2zh.v3.document_model import render_plan_from_model, translate_document
    from pdf2zh.v3.magicpdf_bridge import MagicPdfBridge

    # 摄入（ingestion）后端：auto（默认，mineru primary + Marker 质量门回退）、
    # mineru（既有 MinerU/magic-pdf 解析链路）或 marker（datalab-to/marker，
    # vendor/marker 子模块；JSON 离线或 live 运行）。与 v3/ingestion 计划一致：
    # Marker 只做 PDF understanding，不参与排版渲染；其 canonical IR 经
    # v3/ingestion/bridge 进同一 DocumentModel 主链路。
    ingest_backend = (getattr(parsed_args, "ingest_backend", "") or "auto").lower()
    if ingest_backend not in ("auto", "mineru", "marker"):
        ingest_backend = "auto"
    marker_json = getattr(parsed_args, "marker_json", None)
    marker_version = getattr(parsed_args, "marker_version", "") or None

    adapter = MagicPdfAdapter(
        device=parsed_args.backend,
        mineru_vram_size=getattr(parsed_args, "mineru_vram_size", "") or "",
        mineru_window_size=getattr(parsed_args, "mineru_window_size", "") or "",
        mineru_parse_method=getattr(parsed_args, "mineru_parse_method", "") or "",
        mineru_backend=getattr(parsed_args, "mineru_backend", "") or "",
    )
    # 解析前打印 magic-pdf 实际执行设备（torch CUDA 状态 + 配置 device-mode），
    # 避免"选 cuda 实际跑 cpu"的排障盲区；未走 GPU 时给出安装指引。
    try:
        from pdf2zh.magicpdf_adapter import get_magicpdf_device_status

        status = get_magicpdf_device_status(requested=parsed_args.backend)
        logger.info(
            "[magicpdf] device status: requested=%s torch=%s torch_cuda=%s "
            "device-mode=%s effective=%s mineru_venv=%s mineru_cuda=%s",
            status["requested"],
            status["torch"] or "-",
            status["torch_cuda"],
            status["device_mode"],
            status["effective"],
            status.get("mineru_venv") or "-",
            status.get("mineru_venv_torch_cuda"),
        )
        if status.get("hint"):
            logger.warning("[magicpdf] %s", status["hint"])
    except Exception as exc:  # noqa: BLE001 -- 诊断失败不阻断解析
        logger.debug("[magicpdf] device status probe skipped: %s", exc)
    if not adapter.is_available():
        adapter.close()
        return _degrade_engine(
            parsed_args,
            "magic-pdf/MinerU 未安装",
            progress_cb=progress_cb,
            degrade_to=degrade_to,
        )

    files = list(parsed_args.files or [])
    if parsed_args.dir and files:
        from pdf2zh.pdf2zh import find_all_files_in_directory

        files = find_all_files_in_directory(files[0])

    bridge = MagicPdfBridge(default_font="")
    magic_dir = _output_dir(parsed_args)
    # magicpdf OCR 三态（auto/on/off，见 pdf2zh.pdf2zh.resolve_magicpdf_ocr_mode）：
    #   auto：预检命中扫描/损坏信号才自动开启 OCR（历史行为，默认）；
    #   on  ：强制对所有 PDF 执行 OCR；
    #   off ：用户显式关闭 OCR，预检命中也绝不强制开启。
    from pdf2zh.pdf2zh import resolve_magicpdf_ocr_mode

    ocr_mode = resolve_magicpdf_ocr_mode(parsed_args)
    prompt_text = _prompt_text(parsed_args)
    from pdf2zh.scanned_detection import preflight_scan_check

    for path in files:
        #: 本次摄入故事（doc, end-status, fallback_from）＋决策，随后按序写入
        #: flight recorder：mineru (FAIL) → marker fallback (PASS, fallback_from)
        #: → ingest.select，audit 的 first_divergence 因此能指向 ingest。
        ingest_events: list = []
        ingest_decision = None
        # v1.1 trace 开关 + 目录：--trace 默认关闭；开启后 trace JSONL 写
        # 到 ``<trace_dir or output>/trace/``，audit 到 ``<trace_dir or output>/audit/``。
        # 关闭时 FlightRecorder(None) 为全 no-op，也不产生审计产物。
        # rec 提到循环顶：raw / parse-crash 故事也落在同一 trace —— 即便最终
        # engine 级 legacy/BabelDOC 降级，前面的 ingestion failure 痕迹也不被吞掉。
        from pdf2zh.v3.flight_recorder import FlightRecorder

        stem = os.path.splitext(os.path.basename(path))[0]
        trace_on = bool(getattr(parsed_args, "trace", False))
        trace_root = (
            (getattr(parsed_args, "trace_dir", "") or "").strip()
            or parsed_args.output
            or "."
        )
        rec = FlightRecorder(
            (
                os.path.join(trace_root, "trace", f"{stem}_events.jsonl")
                if trace_on
                else None
            ),
            book_id=stem,
        )
        # 文本层质量预检（多信号融合）：auto 模式下且用户未显式关闭 OCR 时，
        # 若预检命中扫描/损坏信号，自动开启 OCR，避免乱码被直接翻译。off
        # 模式下尊重用户选择，预检命中也不强制开启。
        if ocr_mode == "on":
            ocr = True
        elif ocr_mode == "off":
            ocr = False
        else:  # auto
            ocr = False
            try:
                decision = preflight_scan_check(path)
                if decision.is_scanned:
                    logger.warning(
                        "[magicpdf] %s 预检命中扫描/损坏信号 (%s)，自动开启 OCR",
                        path,
                        "; ".join(decision.reasons) or "unknown",
                    )
                    ocr = True
            except Exception as exc:  # noqa: BLE001 -- 预检失败不阻断解析
                logger.debug("[magicpdf] preflight skipped: %s", exc)
        #: 非 None ⇒ doc 已由解析崩溃兜底路径产出，跳过 canonical 选择。
        served_doc = None
        try:
            results = _adapter_parse(
                adapter,
                path,
                parsed_args.pages,
                ocr,
                _make_parse_progress(progress_cb, path),
                lang=getattr(parsed_args, "lang_in", None),
            )
        except Exception as exc:  # noqa: BLE001 -- 解析崩溃
            logger.warning("[magicpdf] %s 解析失败: %s", path, exc)
            from pdf2zh.v3.ingestion import BACKEND_MARKER, BACKEND_MINERU
            from pdf2zh.v3.ingestion.base import (
                emit_ingest_run_failure,
                ingest_block_events,
            )
            from pdf2zh.v3.ingestion.selector import (
                QUALITY_FAIL,
                REASON_PRIMARY_PARSE_FAIL,
                decide,
                gate_quality,
            )

            # P1: auto + parse crash → Marker 回退（同一 selector 决策模型，
            # reason=mineru_parse_failed，绝不伪装成 quality failure）。失败链
            # 完整进 trace：mineru run_failure → marker run_failure → engine 级
            # legacy/BabelDOC 降级（降级不吞掉前面的 ingestion failure）。
            if ingest_backend == "auto" and (
                bool(marker_json) or _marker_live_available()
            ):
                try:
                    emit_ingest_run_failure(
                        BACKEND_MINERU, f"parse failed: {exc}", rec, pdf_path=path
                    )
                except Exception:  # noqa: BLE001 -- 采集失败不阻断回退
                    pass
                try:
                    ingest_doc, doc = _run_marker_ingest(
                        path, marker_json, marker_version, magic_dir, progress_cb
                    )
                    results = []
                    # 解析崩溃走同一条回退路由：以 failed primary 表达（没有事件
                    # 可过门），reason 随后如实覆盖为 mineru_parse_failed —— 绝不
                    # 伪装成 quality failure；quality/failed_rules 也随后被 marker
                    # run 自身的 gate 结果覆盖。
                    ingest_decision = decide(
                        "auto",
                        primary=BACKEND_MINERU,
                        primary_quality=QUALITY_FAIL,
                        fallback_available=True,
                    )
                    ingest_decision.reason = REASON_PRIMARY_PARSE_FAIL
                    ingest_decision.failed_rules = []
                    marker_gate = gate_quality(ingest_block_events(ingest_doc))
                    ingest_decision.quality = marker_gate.quality
                    ingest_decision.fallback_succeeded = True
                    ingest_events.append(
                        (
                            ingest_doc,
                            ("FAIL" if marker_gate.quality == QUALITY_FAIL else "PASS"),
                            BACKEND_MINERU,
                        )
                    )
                    served_doc = doc
                    logger.warning(
                        "[magicpdf] %s MinerU 解析失败，auto 回退 Marker 成功: %s",
                        path,
                        exc,
                    )
                except Exception as exc2:  # noqa: BLE001 -- Marker 也失败 → engine 降级
                    logger.warning(
                        "[magicpdf] %s MinerU 解析失败且 Marker 回退也失败: %s / %s",
                        path,
                        exc,
                        exc2,
                    )
                    try:
                        emit_ingest_run_failure(
                            BACKEND_MARKER,
                            f"fallback failed: {exc2}",
                            rec,
                            pdf_path=path,
                            fallback_from=BACKEND_MINERU,
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    rec.close()
                    adapter.close()
                    return _degrade_engine(
                        parsed_args,
                        f"{path} 解析失败且 Marker 回退失败",
                        progress_cb=progress_cb,
                        degrade_to=degrade_to,
                    )
            else:
                try:
                    emit_ingest_run_failure(
                        BACKEND_MINERU, f"parse failed: {exc}", rec, pdf_path=path
                    )
                except Exception:  # noqa: BLE001
                    pass
                rec.close()
                adapter.close()
                return _degrade_engine(
                    parsed_args,
                    f"{path} 解析失败",
                    progress_cb=progress_cb,
                    degrade_to=degrade_to,
                )

        if served_doc is not None:
            # 解析崩溃 + Marker 兜底成功：doc 已就绪，ingest_events/ingest_decision
            # 也已 staged，直接进入翻译/渲染主链路。
            doc = served_doc
        else:
            # 解析成功：raw 证据先于任何 canonicalization 落盘 —— audit 因此能
            # 区分「MinerU 本身没给 geometry」与「adapter 转换时丢掉了 geometry」。
            try:
                from pdf2zh.v3.ingestion.base import emit_raw_ingest_events

                emit_raw_ingest_events(results, rec, pdf_path=path)
            except Exception as exc:  # noqa: BLE001 -- 采集失败不阻断主链路
                logger.debug("[magicpdf] raw ingest trace emission failed: %s", exc)
            pages = bridge.convert_all(results)
            if ingest_backend == "marker":
                # Marker ingestion backend：丢弃 MinerU 块（本页已解析），从 Marker
                # JSON（离线，--marker-json）或 live 转换产出 canonical IR，再由
                # ingestion/bridge 投影成与 MinerU 同构的 DocumentModel —— 之后
                # translate → plan → fixup → render → audit 链路逐字节不变。
                from pdf2zh.v3.ingestion import BACKEND_MARKER
                from pdf2zh.v3.ingestion.base import ingest_block_events
                from pdf2zh.v3.ingestion.selector import (
                    QUALITY_FAIL,
                    decide,
                    gate_quality,
                )

                try:
                    ingest_doc, doc = _run_marker_ingest(
                        path, marker_json, marker_version, magic_dir, progress_cb
                    )
                    results = []
                except Exception as exc:  # noqa: BLE001 -- 熔断降级保持既有语义
                    logger.warning("[magicpdf] %s Marker 摄入失败: %s", path, exc)
                    try:
                        from pdf2zh.v3.ingestion.base import emit_ingest_run_failure

                        emit_ingest_run_failure(
                            BACKEND_MARKER,
                            f"Marker ingestion failed: {exc}",
                            rec,
                            pdf_path=path,
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    rec.close()
                    try:
                        adapter.close()
                    except Exception:  # noqa: BLE001
                        pass
                    return _degrade_engine(
                        parsed_args,
                        f"Marker ingestion failed: {exc}",
                        progress_cb=progress_cb,
                        degrade_to=degrade_to,
                    )
                gate = gate_quality(ingest_block_events(ingest_doc))
                ingest_events.append(
                    (ingest_doc, "FAIL" if gate.quality == QUALITY_FAIL else None, None)
                )
                ingest_decision = decide(
                    "marker",
                    primary=BACKEND_MARKER,
                    primary_quality=gate.quality,
                    primary_failed_rules=gate.failed_rules,
                    fallback_available=False,
                )
            else:
                # mineru primary（强制 mineru 或 auto）：既有 MinerU 块先适配成
                # canonical IR 过质量门（ingestion/rules 的 canonical invariants），
                # auto 模式下门 FAIL 且 Marker 可用才回退 —— 决策本身落 ingest.select。
                from pdf2zh.v3.ingestion import BACKEND_MARKER, BACKEND_MINERU
                from pdf2zh.v3.ingestion.adapter import existing_pages_to_document
                from pdf2zh.v3.ingestion.base import ingest_block_events
                from pdf2zh.v3.ingestion.selector import (
                    QUALITY_FAIL,
                    REASON_FALLBACK_RUN_FAILED,
                    decide,
                    gate_quality,
                )

                mineru_doc = existing_pages_to_document(
                    pages, source_backend=BACKEND_MINERU
                )
                gate = gate_quality(ingest_block_events(mineru_doc))
                fallback_available = bool(marker_json) or (
                    _marker_live_available() if ingest_backend == "auto" else False
                )
                ingest_decision = decide(
                    ingest_backend,
                    primary=BACKEND_MINERU,
                    primary_quality=gate.quality,
                    primary_failed_rules=gate.failed_rules,
                    fallback_available=fallback_available,
                )
                ingest_events.append(
                    (mineru_doc, "FAIL" if gate.quality == QUALITY_FAIL else None, None)
                )
                if (
                    ingest_decision.selected_backend == BACKEND_MARKER
                    and fallback_available
                ):
                    # auto 质量门 FAIL → Marker 回退；回退失败保留 MinerU 结果
                    # （决策如实改为 fallback_ingest_failed，失败可见不静默）。
                    try:
                        ingest_doc, doc = _run_marker_ingest(
                            path, marker_json, marker_version, magic_dir, progress_cb
                        )
                        results = []
                        marker_gate = gate_quality(ingest_block_events(ingest_doc))
                        # quality = 被选中 run 自身的门结果；failed_rules 保持
                        # primary 的违规清单（"为什么回退"，见 IngestionDecision
                        # 契约）。
                        ingest_decision.quality = marker_gate.quality
                        ingest_decision.fallback_succeeded = True
                        ingest_events.append(
                            (
                                ingest_doc,
                                (
                                    "FAIL"
                                    if marker_gate.quality == QUALITY_FAIL
                                    else "PASS"
                                ),
                                BACKEND_MINERU,
                            )
                        )
                    except Exception as exc:  # noqa: BLE001 -- 回退失败保留主结果
                        logger.warning(
                            "[magicpdf] %s auto Marker 回退失败，保留 MinerU 结果: %s",
                            path,
                            exc,
                        )
                        doc = bridge.to_document_model(pages)
                        ingest_decision.selected_backend = BACKEND_MINERU
                        ingest_decision.fallback = False
                        ingest_decision.fallback_succeeded = False
                        ingest_decision.reason = REASON_FALLBACK_RUN_FAILED
                else:
                    doc = bridge.to_document_model(pages)
        stats = {"translated": 0, "preserved": 0}
        try:
            from pdf2zh.translator import build_translator

            if progress_cb is not None:
                progress_cb(
                    "translating",
                    _PCT_TRANSLATE,
                    f"{os.path.basename(path)}: translating blocks...",
                )
            translator = build_translator(
                parsed_args.service,
                parsed_args.lang_in,
                parsed_args.lang_out,
                envs={},
                prompt=prompt_text,
                ignore_cache=parsed_args.ignore_cache,
            )
            stats = translate_document(
                doc, translator.translate, lang_out=parsed_args.lang_out
            )
        except Exception as exc:  # noqa: BLE001 -- 翻译失败不阻断转储
            logger.warning("[magicpdf] 翻译阶段失败（转储原始模型）: %s", exc)

        # Step 1.3：收集 magic-pdf 的公式 LaTeX 侧通道并回填模型，供
        # 公式重建 / 评测消费；随后 RenderTakeover 修正渲染计划。
        from pdf2zh.v3.formula_side_channel import (
            apply_formula_latex,
            collect_formula_latex,
        )
        from pdf2zh.v3.render_takeover import fixup_render_plan

        # FlightRecorder 已在循环顶创建（见上）：raw / parse-crash / canonical
        # 摄入故事与 plan → render 事件按序写进同一 JSONL（rec.close() 在下方
        # 主链路的 finally 与各降级出口处幂等执行）。
        # 把 ingestion 阶段写进同一 FlightRecorder：ingest.* 事件先于 plan 事件
        # 落盘，audit 的 first_divergence 因此能指向 ingest（见 ingestion/rules）。
        # auto 回退故事完整落盘：mineru (FAIL) → marker fallback (PASS,
        # fallback_from=mineru) → ingest.select（决策与原因，无需猜测）。
        if ingest_events:
            try:
                from pdf2zh.v3.ingestion.base import (
                    emit_ingest_events,
                    emit_ingest_selection,
                )

                for idoc, status, fallback_from in ingest_events:
                    emit_ingest_events(
                        idoc,
                        rec,
                        pdf_path=path,
                        status=status,
                        fallback_from=fallback_from,
                    )
                if ingest_decision is not None:
                    emit_ingest_selection(ingest_decision, rec, pdf_path=path)
            except Exception as exc:  # noqa: BLE001 -- 采集失败不阻断主链路
                logger.debug("[magicpdf] ingest trace emission failed: %s", exc)
        try:
            channel = collect_formula_latex(doc)
            formula_applied = apply_formula_latex(doc, channel)
            plan = render_plan_from_model(doc, trace=rec)
            fixed_plan, fixup_stats = fixup_render_plan(plan, trace=rec)
            _write_dumps(
                path,
                results,
                doc,
                magic_dir,
                channel=channel,
                fixed_plan=fixed_plan,
            )
            # §12.3 渲染接管：fixup 后的渲染计划 → 译后 mono PDF（默认开启，
            # --no-magicpdf-render 关闭；渲染失败仅告警，保留 JSON 转储）。
            if getattr(parsed_args, "magicpdf_render", True) and fixed_plan:
                from pdf2zh.v3.magicpdf_renderer import render_plan_to_pdf

                if progress_cb is not None:
                    progress_cb(
                        "rendering",
                        _PCT_RENDER,
                        f"{os.path.basename(path)}: rendering mono PDF...",
                    )
                page_sizes = {
                    p.page_num: [p.width, p.height]
                    for p in doc.pages
                    if getattr(p, "width", 0) and getattr(p, "height", 0)
                }
                mono_pdf = os.path.join(magic_dir, f"{stem}_mono.pdf")
                try:
                    _, render_stats = render_plan_to_pdf(
                        fixed_plan,
                        page_sizes=page_sizes,
                        output_path=mono_pdf,
                        # 保留原 PDF 为背景层：图形/颜色块/图片以及公式、代码等
                        # 保留块的原文由背景显示，只重画真正翻译的块 —— 否则
                        # 有色方块被整页白底吞噬、公式 LaTeX 原文叠影。
                        source_pdf=path,
                        trace=rec,
                    )
                    logger.info(
                        "[magicpdf] %s: mono PDF 已渲染（%d 页, %d 块, %d 字形）→ %s",
                        path,
                        render_stats["pages"],
                        render_stats["blocks"],
                        render_stats["glyphs"],
                        mono_pdf,
                    )
                except Exception as exc:  # noqa: BLE001 -- 渲染失败不阻断转储
                    logger.warning(
                        "[magicpdf] %s mono PDF 渲染失败（保留 JSON 转储）: %s",
                        path,
                        exc,
                    )
        finally:
            rec.close()
        # Trace 自动审计：invariant rules → summary / defect-ledger / pages /
        # trace-index / qualification.md（Level-2 光栅证据只针对 FAIL 块）。
        if rec.path and os.path.exists(rec.path):
            try:
                from pdf2zh.v3.trace_audit import _run_audit

                mono_pdf = (
                    os.path.join(magic_dir, f"{stem}_mono.pdf")
                    if getattr(parsed_args, "magicpdf_render", True)
                    else None
                )
                _run_audit(
                    rec.path,
                    pdf=mono_pdf if mono_pdf and os.path.exists(mono_pdf) else None,
                    source=path,
                    out=os.path.join(trace_root, "audit"),
                )
            except Exception as exc:  # noqa: BLE001 -- trace 审计失败不阻断翻译产物
                logger.warning("[magicpdf] trace audit failed (kept dumps): %s", exc)
        glyphs = (
            sum(
                len(s.glyphs)
                for b in doc.pages[0].blocks
                for l in b.lines
                for s in l.spans
            )
            if doc.pages
            else 0
        )
        logger.info(
            "[magicpdf] %s: %d 页, %d 块, %d 字形, 翻译 %s, 保留 %s, "
            "渲染计划 %d 项, 公式LaTeX %d, fixup(shift=%d/overflow=%d)",
            path,
            len(doc.pages),
            len(plan),
            glyphs,
            stats.get("translated", 0),
            stats.get("preserved", 0),
            len(plan),
            formula_applied,
            fixup_stats.get("shifted", 0),
            fixup_stats.get("overflowed", 0),
        )
    adapter.close()
    return 0
