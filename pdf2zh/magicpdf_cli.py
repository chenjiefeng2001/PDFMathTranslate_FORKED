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


def _output_dir(parsed_args) -> str:
    out = parsed_args.output or "."
    magic_dir = os.path.join(out, "magicpdf")
    os.makedirs(magic_dir, exist_ok=True)
    return magic_dir


def _fallback_legacy(parsed_args, reason: str) -> int:
    """熔断降级：记录原因后按 legacy 内核重跑（Step 3.3）。

    打 ``_magicpdf_fallback`` 防重入标记：legacy 内核的文本层预检看到该
    标记后不再自动切回 magicpdf 引擎（本进程中 magic-pdf 已被证实不可用），
    避免 magicpdf → legacy → (auto-switch) → magicpdf 的乒乓循环。
    """
    parsed_args._magicpdf_fallback = True
    logger.warning("[magicpdf] %s —— 自动降级回 legacy 内核重试。", reason)
    from pdf2zh.pdf2zh import _run_legacy_kernel

    return _run_legacy_kernel(parsed_args)


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


def _adapter_parse(adapter, path: str, pages, ocr: bool, progress_cb):
    """防御性调用 ``adapter.parse``：旧版签名（无 progress_cb 形参）兼容。

    第三方/测试代码可能 monkey-patch 或子类覆盖 ``parse`` 且不带新形参；
    按签名探测后再传 ``progress_cb``，避免 TypeError 破坏解析主流程。
    """
    if progress_cb is not None:
        try:
            params = inspect.signature(adapter.parse).parameters
            takes_cb = "progress_cb" in params or any(
                p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
            )
        except (TypeError, ValueError):  # pragma: no cover - 内置类兜底
            takes_cb = False
        if takes_cb:
            return adapter.parse(path, pages=pages, ocr=ocr, progress_cb=progress_cb)
    return adapter.parse(path, pages=pages, ocr=ocr)


def run_magicpdf_main(parsed_args, progress_cb=None) -> int:
    """magicpdf 解析引擎主流程（引擎不可用时自动降级 legacy）。

    ``progress_cb(stage, pct, msg, detail=None)``（可选）：解析期页级/
    组件级细粒度计数与翻译/渲染相位粗事件都经它上报；不传保持原行为。
    """
    # torch 必须先于任何 onnxruntime CUDA 会话导入（DLL 加载顺序，见
    # _preload_torch docstring）；CLI 全局入口已不再无条件加载 doclayout
    # 模型，此处预载兜底覆盖 API/GUI 服务进程复用等其它进入形态。
    _preload_torch()
    from pdf2zh.magicpdf_adapter import MagicPdfAdapter
    from pdf2zh.v3.document_model import render_plan_from_model, translate_document
    from pdf2zh.v3.magicpdf_bridge import MagicPdfBridge

    adapter = MagicPdfAdapter(device=parsed_args.backend)
    # 解析前打印 magic-pdf 实际执行设备（torch CUDA 状态 + 配置 device-mode），
    # 避免"选 cuda 实际跑 cpu"的排障盲区；未走 GPU 时给出安装指引。
    try:
        from pdf2zh.magicpdf_adapter import get_magicpdf_device_status

        status = get_magicpdf_device_status(requested=parsed_args.backend)
        logger.info(
            "[magicpdf] device status: requested=%s torch=%s torch_cuda=%s "
            "device-mode=%s effective=%s",
            status["requested"],
            status["torch"] or "-",
            status["torch_cuda"],
            status["device_mode"],
            status["effective"],
        )
        if status.get("hint"):
            logger.warning("[magicpdf] %s", status["hint"])
    except Exception as exc:  # noqa: BLE001 -- 诊断失败不阻断解析
        logger.debug("[magicpdf] device status probe skipped: %s", exc)
    if not adapter.is_available():
        return _fallback_legacy(parsed_args, "magic-pdf/MinerU 未安装")

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
        try:
            results = _adapter_parse(
                adapter,
                path,
                parsed_args.pages,
                ocr,
                _make_parse_progress(progress_cb, path),
            )
        except Exception as exc:  # noqa: BLE001 -- 熔断降级
            logger.warning("[magicpdf] %s 解析失败: %s", path, exc)
            return _fallback_legacy(parsed_args, f"{path} 解析失败")

        doc = bridge.to_document_model(bridge.convert_all(results))
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

        channel = collect_formula_latex(doc)
        formula_applied = apply_formula_latex(doc, channel)
        plan = render_plan_from_model(doc)
        fixed_plan, fixup_stats = fixup_render_plan(plan)
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
            stem = os.path.splitext(os.path.basename(path))[0]
            mono_pdf = os.path.join(magic_dir, f"{stem}_mono.pdf")
            try:
                _, render_stats = render_plan_to_pdf(
                    fixed_plan,
                    page_sizes=page_sizes,
                    output_path=mono_pdf,
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
    return 0
