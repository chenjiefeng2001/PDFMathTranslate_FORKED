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
    """熔断降级：记录原因后按 legacy 内核重跑（Step 3.3）。"""
    logger.warning("[magicpdf] %s —— 自动降级回 legacy 内核重试。", reason)
    from pdf2zh.pdf2zh import _run_legacy_kernel

    return _run_legacy_kernel(parsed_args)


def _prompt_text(parsed_args) -> str | None:
    if not parsed_args.prompt:
        return None
    return (
        parsed_args.prompt.template
        if hasattr(parsed_args.prompt, "template")
        else parsed_args.prompt
    )


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
        channel_dump = os.path.join(
            magic_dir, f"{stem}_formula_channel.json")
        with open(channel_dump, "w", encoding="utf-8") as fh:
            fh.write(channel.to_json())
        logger.info("[magicpdf] formula channel dump: %s", channel_dump)
    if fixed_plan:
        plan_dump = os.path.join(magic_dir, f"{stem}_render_plan.json")
        with open(plan_dump, "w", encoding="utf-8") as fh:
            json.dump(fixed_plan, fh, ensure_ascii=False, indent=2)
        logger.info("[magicpdf] render plan dump: %s", plan_dump)

def run_magicpdf_main(parsed_args) -> int:
    """magicpdf 解析引擎主流程（引擎不可用时自动降级 legacy）。"""
    from pdf2zh.magicpdf_adapter import MagicPdfAdapter
    from pdf2zh.v3.document_model import render_plan_from_model, translate_document
    from pdf2zh.v3.magicpdf_bridge import MagicPdfBridge

    adapter = MagicPdfAdapter(device=parsed_args.backend)
    if not adapter.is_available():
        return _fallback_legacy(parsed_args, "magic-pdf/MinerU 未安装")

    files = list(parsed_args.files or [])
    if parsed_args.dir and files:
        from pdf2zh.pdf2zh import find_all_files_in_directory

        files = find_all_files_in_directory(files[0])

    bridge = MagicPdfBridge(default_font="")
    magic_dir = _output_dir(parsed_args)
    ocr = bool(getattr(parsed_args, "magicpdf_ocr", False))
    prompt_text = _prompt_text(parsed_args)
    from pdf2zh.scanned_detection import preflight_scan_check

    for path in files:
        # 文本层质量预检（多信号融合）：auto 模式且未显式指定 --magicpdf-ocr
        # 时，若预检命中扫描/损坏信号，自动开启 OCR，避免乱码被直接翻译。
        if not ocr:
            try:
                decision = preflight_scan_check(path)
                if decision.is_scanned:
                    logger.warning(
                        "[magicpdf] %s 预检命中扫描/损坏信号 (%s)，自动开启 OCR",
                        path, "; ".join(decision.reasons) or "unknown",
                    )
                    ocr = True
            except Exception as exc:  # noqa: BLE001 -- 预检失败不阻断解析
                logger.debug("[magicpdf] preflight skipped: %s", exc)
        try:
            results = adapter.parse(path, pages=parsed_args.pages, ocr=ocr)
        except Exception as exc:  # noqa: BLE001 -- 熔断降级
            logger.warning("[magicpdf] %s 解析失败: %s", path, exc)
            return _fallback_legacy(parsed_args, f"{path} 解析失败")

        doc = bridge.to_document_model(bridge.convert_all(results))
        stats = {"translated": 0, "preserved": 0}
        try:
            from pdf2zh.translator import build_translator

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
            path, results, doc, magic_dir, channel=channel,
            fixed_plan=fixed_plan,
        )
        # §12.3 渲染接管：fixup 后的渲染计划 → 译后 mono PDF（默认开启，
        # --no-magicpdf-render 关闭；渲染失败仅告警，保留 JSON 转储）。
        if getattr(parsed_args, "magicpdf_render", True) and fixed_plan:
            from pdf2zh.v3.magicpdf_renderer import render_plan_to_pdf

            page_sizes = {
                p.page_num: [p.width, p.height]
                for p in doc.pages
                if getattr(p, "width", 0) and getattr(p, "height", 0)
            }
            stem = os.path.splitext(os.path.basename(path))[0]
            mono_pdf = os.path.join(magic_dir, f"{stem}_mono.pdf")
            try:
                _, render_stats = render_plan_to_pdf(
                    fixed_plan, page_sizes=page_sizes,
                    output_path=mono_pdf,
                )
                logger.info(
                    "[magicpdf] %s: mono PDF 已渲染（%d 页, %d 块, %d 字形）→ %s",
                    path, render_stats["pages"], render_stats["blocks"],
                    render_stats["glyphs"], mono_pdf,
                )
            except Exception as exc:  # noqa: BLE001 -- 渲染失败不阻断转储
                logger.warning(
                    "[magicpdf] %s mono PDF 渲染失败（保留 JSON 转储）: %s",
                    path, exc,
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
            path, len(doc.pages), len(plan), glyphs,
            stats.get("translated", 0), stats.get("preserved", 0),
            len(plan), formula_applied,
            fixup_stats.get("shifted", 0), fixup_stats.get("overflowed", 0),
        )
    return 0

